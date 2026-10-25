from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._text import normalize_multiline_text
from cogs.pagination import (
    PaginationPage,
    PersistentPageButton,
    ResultPaginationEntryView,
    format_event_footer,
    notify_event_creator,
)
from database import (
    add_signup_member,
    begin_signup_selection,
    complete_signup_event,
    create_signup_event,
    delete_signup_event,
    finalize_signup_event,
    get_open_signup_events,
    get_signup_event,
    get_signup_event_by_message,
    get_signup_members,
    reconcile_signup_members,
    remove_signup_member,
    set_signup_announcement_message_ids,
    set_signup_event_message,
    set_signup_event_status,
    set_signup_event_webhook,
    set_signup_eligibility,
    set_signup_selected_user_ids,
    set_signup_selection_message,
    update_signup_member_note,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


TAIWAN_TIMEZONE = timezone(timedelta(hours=8))
DEADLINE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
SIGNUP_EMOJI = "✅"
MAX_NOTE_LENGTH = 300
ORGANIZER_PICK_TIMEOUT = timedelta(minutes=30)
logger = logging.getLogger(__name__)


def signup_event_id(event: sqlite3.Row) -> int:
    return int(event["id"])


class SignupNoteModal(discord.ui.Modal, title="報名備註"):
    def __init__(self, cog: Signup, event_id: int, current_note: str) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.event_id = event_id
        self.note = discord.ui.TextInput(
            label="備註（選填，僅建立者可見）",
            placeholder="有需要告知建立者的事項可填寫於此",
            required=False,
            max_length=MAX_NOTE_LENGTH,
            style=discord.TextStyle.paragraph,
            default=current_note,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            saved = update_signup_member_note(
                self.cog.database,
                self.event_id,
                interaction.user.id,
                str(self.note.value).strip(),
            )
        except sqlite3.Error:
            logger.exception(
                "Unable to save signup note for event %s",
                self.event_id,
            )
            await interaction.response.send_message(
                "目前無法保存備註，請稍後再試。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "備註已更新。" if saved else "此活動已截止，無法修改備註。",
            ephemeral=True,
        )


class SignupPanelView(discord.ui.View):
    def __init__(
        self,
        cog: Signup,
        event_id: int,
        disabled: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.event_id = event_id
        note_button = discord.ui.Button(
            label="填寫／修改備註",
            style=discord.ButtonStyle.secondary,
            custom_id=f"signup:{event_id}:note",
            disabled=disabled,
        )
        note_button.callback = self.open_note
        self.add_item(note_button)

    async def open_note(self, interaction: discord.Interaction) -> None:
        event = get_signup_event(self.cog.database, self.event_id)
        if (
            event is None
            or event["status"] != "open"
            or interaction.guild_id != event["guild_id"]
        ):
            await interaction.response.send_message(
                "此活動已截止或不存在。",
                ephemeral=True,
            )
            return

        if (
            not isinstance(interaction.user, discord.Member)
            or interaction.guild is None
            or interaction.guild.id != int(event["guild_id"])
        ):
            await interaction.response.send_message(
                "請先在此活動的 Discord 訊息加入 ✅ 完成報名，再填寫備註。",
                ephemeral=True,
            )
            return

        signup_member = self.cog.get_signup_member(self.event_id, interaction.user.id)
        if signup_member is None:
            await interaction.response.send_message(
                "請先在此活動的 Discord 訊息加入 ✅ 完成報名，再填寫備註。",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            SignupNoteModal(
                self.cog,
                self.event_id,
                str(signup_member["note"]),
            )
        )


class OrganizerSelectionView(discord.ui.View):
    def __init__(
        self,
        cog: Signup,
        event_id: int,
        creator_id: int,
        candidates: Sequence[sqlite3.Row],
        selected_user_ids: set[int],
        page: int = 0,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.event_id = event_id
        self.creator_id = creator_id
        self.candidates = list(candidates)
        self.selected_user_ids = set(selected_user_ids)
        self.page = page
        self.refresh_items()

    def refresh_items(self) -> None:
        self.clear_items()
        page_count = max(1, (len(self.candidates) + 24) // 25)
        self.page = min(max(0, self.page), page_count - 1)
        page_candidates = self.candidates[self.page * 25 : (self.page + 1) * 25]
        page_ids = {int(row["user_id"]) for row in page_candidates}
        slots = self.cog.selection_slots(self.event_id)
        selected_elsewhere = len(self.selected_user_ids - page_ids)
        available_slots = max(1, slots - selected_elsewhere)
        select = discord.ui.Select(
            placeholder=f"選擇錄取者（第 {self.page + 1}/{page_count} 頁）",
            min_values=0,
            max_values=max(1, min(len(page_candidates), 25, available_slots)),
            options=[
                discord.SelectOption(
                    label=str(row["display_name"])[:100],
                    value=str(row["user_id"]),
                    default=int(row["user_id"]) in self.selected_user_ids,
                )
                for row in page_candidates
            ],
            custom_id=f"signup:{self.event_id}:select",
            row=0,
        )
        select.callback = self.select_candidates
        self.add_item(select)

        previous = discord.ui.Button(
            label="上一頁",
            style=discord.ButtonStyle.secondary,
            custom_id=f"signup:{self.event_id}:previous",
            disabled=self.page == 0,
            row=1,
        )
        previous.callback = self.previous_page
        self.add_item(previous)
        next_page = discord.ui.Button(
            label="下一頁",
            style=discord.ButtonStyle.secondary,
            custom_id=f"signup:{self.event_id}:next",
            disabled=self.page >= page_count - 1,
            row=1,
        )
        next_page.callback = self.next_page
        self.add_item(next_page)
        confirm = discord.ui.Button(
            label="確認錄取名單",
            style=discord.ButtonStyle.success,
            custom_id=f"signup:{self.event_id}:confirm",
            row=2,
        )
        confirm.callback = self.confirm
        self.add_item(confirm)

    async def check_owner(self, interaction: discord.Interaction) -> bool:
        event = get_signup_event(self.cog.database, self.event_id)
        if (
            event is None
            or event["status"] != "selecting"
            or interaction.user.id != self.creator_id
        ):
            await interaction.response.send_message(
                "只有此活動建立者可以選擇錄取名單。",
                ephemeral=True,
            )
            return False
        return True

    async def select_candidates(self, interaction: discord.Interaction) -> None:
        if not await self.check_owner(interaction):
            return
        page_candidates = self.candidates[self.page * 25 : (self.page + 1) * 25]
        page_ids = {int(row["user_id"]) for row in page_candidates}
        selected = self.selected_user_ids - page_ids
        selected.update(int(value) for value in self.children[0].values)
        if len(selected) > self.cog.selection_slots(self.event_id):
            await interaction.response.send_message(
                "錄取人數不可超過此活動剩餘名額。",
                ephemeral=True,
            )
            return
        self.selected_user_ids = selected
        try:
            set_signup_selected_user_ids(
                self.cog.database,
                self.event_id,
                json.dumps(sorted(selected)),
            )
        except sqlite3.Error:
            logger.exception(
                "Unable to save organizer selection for event %s",
                self.event_id,
            )
            await interaction.response.send_message(
                "無法保存目前選擇，請稍後再試。",
                ephemeral=True,
            )
            return
        self.refresh_items()
        await interaction.response.edit_message(view=self)

    async def previous_page(self, interaction: discord.Interaction) -> None:
        if not await self.check_owner(interaction):
            return
        self.page = max(0, self.page - 1)
        self.refresh_items()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction) -> None:
        if not await self.check_owner(interaction):
            return
        self.page += 1
        self.refresh_items()
        await interaction.response.edit_message(view=self)

    async def confirm(self, interaction: discord.Interaction) -> None:
        if not await self.check_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            finalized = await self.cog.finalize_event(
                self.event_id,
                {self.creator_id, *self.selected_user_ids},
            )
        except (discord.HTTPException, sqlite3.Error, RuntimeError, TypeError):
            logger.exception(
                "Unable to finalize selected signup list for event %s",
                self.event_id,
            )
            await interaction.followup.send(
                "公告錄取名單失敗，系統會稍後重試。",
                ephemeral=True,
            )
            return
        if not finalized:
            await interaction.followup.send(
                "錄取名單已截止或正在處理，請查看活動頻道公告。",
                ephemeral=True,
            )
            return
        await interaction.followup.send("錄取與候補名單已公告。", ephemeral=True)


class Signup(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.panel_event_ids: dict[int, int] = {}

    @property
    def database(self) -> sqlite3.Connection:
        connection = getattr(self.bot, "database", None)
        if not isinstance(connection, sqlite3.Connection):
            raise RuntimeError("SQLite 尚未初始化")
        return connection

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(PersistentPageButton)
        for event in get_open_signup_events(self.database):
            event_id = signup_event_id(event)
            message_id = event["panel_message_id"]
            if message_id is not None:
                self.panel_event_ids[int(message_id)] = event_id
                self.bot.add_view(
                    SignupPanelView(self, event_id, event["status"] != "open"),
                    message_id=int(message_id),
                )

            if event["status"] == "selecting" and event["selection_message_id"]:
                candidates = self.get_eligible_members(event_id, exclude_creator=True)
                selected_ids = set(json.loads(event["selected_user_ids_json"]))
                self.bot.add_view(
                    self.create_selection_view(event, candidates, selected_ids),
                    message_id=int(event["selection_message_id"]),
                )
        self.process_deadlines.start()

    def cog_unload(self) -> None:
        self.process_deadlines.cancel()

    @tasks.loop(seconds=10)
    async def process_deadlines(self) -> None:
        now = datetime.now(timezone.utc)
        for event in get_open_signup_events(self.database):
            event_id = signup_event_id(event)
            try:
                if event["status"] in ("open", "closing") and datetime.fromisoformat(
                    event["deadline_at"]
                ) <= now:
                    await self.handle_deadline(event)
                elif event["status"] == "selecting":
                    if datetime.fromisoformat(event["selection_deadline_at"]) <= now:
                        await self.finalize_by_registration_order(event)
                    elif not event["selection_message_id"]:
                        await self.send_organizer_selection(event)
                elif event["status"] == "announcing":
                    await self.publish_announcement(event)
            except (discord.HTTPException, sqlite3.Error, RuntimeError, TypeError, ValueError):
                logger.exception("Unable to process signup event %s", event_id)

    @process_deadlines.before_loop
    async def before_process_deadlines(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        await self.process_signup_reaction(payload, added=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        await self.process_signup_reaction(payload, added=False)

    async def process_signup_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        added: bool,
    ) -> None:
        if str(payload.emoji) != SIGNUP_EMOJI:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return

        event_id = self.panel_event_ids.get(payload.message_id)
        if event_id is None:
            event = get_signup_event_by_message(self.database, payload.message_id)
            if event is None:
                return
            event_id = signup_event_id(event)
            self.panel_event_ids[payload.message_id] = event_id
        event = get_signup_event(self.database, event_id)
        if (
            event is None
            or event["status"] != "open"
            or event["channel_id"] != payload.channel_id
            or event["guild_id"] != payload.guild_id
        ):
            return

        if not added:
            try:
                remove_signup_member(self.database, event_id, payload.user_id)
            except sqlite3.Error:
                logger.exception(
                    "Unable to remove signup for event %s, user %s",
                    event_id,
                    payload.user_id,
                )
            return

        member = payload.member or self.bot.get_user(payload.user_id)
        if member is not None and member.bot:
            return

        try:
            add_signup_member(
                self.database,
                event_id,
                payload.user_id,
                (
                    member.display_name
                    if member is not None
                    else f"使用者 {payload.user_id}"
                ),
                datetime.now(timezone.utc).isoformat(),
            )
        except sqlite3.Error:
            logger.exception(
                "Unable to record signup for event %s, user %s",
                event_id,
                payload.user_id,
            )

    def get_signup_member(
        self,
        event_id: int,
        user_id: int,
    ) -> sqlite3.Row | None:
        return next(
            (
                row
                for row in get_signup_members(self.database, event_id)
                if int(row["user_id"]) == user_id
            ),
            None,
        )

    def get_eligible_members(
        self,
        event_id: int,
        exclude_creator: bool = False,
    ) -> list[sqlite3.Row]:
        event = get_signup_event(self.database, event_id)
        if event is None:
            return []
        return [
            row
            for row in get_signup_members(self.database, event_id)
            if row["eligible_at_cutoff"] == 1
            and (not exclude_creator or int(row["user_id"]) != int(event["creator_id"]))
        ]

    def selection_slots(self, event_id: int) -> int:
        event = get_signup_event(self.database, event_id)
        if event is None or int(event["capacity"]) == 0:
            return len(self.get_eligible_members(event_id, exclude_creator=True))
        return max(0, int(event["capacity"]) - 1)

    def create_selection_view(
        self,
        event: sqlite3.Row,
        candidates: Sequence[sqlite3.Row],
        selected_ids: set[int],
    ) -> OrganizerSelectionView:
        return OrganizerSelectionView(
            self,
            int(event["id"]),
            int(event["creator_id"]),
            candidates,
            selected_ids - {int(event["creator_id"])},
        )

    async def get_channel(
        self,
        channel_id: int,
    ) -> discord.TextChannel | discord.Thread:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise TypeError(f"Channel {channel_id} is not a text channel or thread")
        return channel

    async def get_event_webhook(self, event: sqlite3.Row) -> discord.Webhook:
        webhook_id = event["webhook_id"]
        webhook_token = event["webhook_token"]
        if webhook_id is None or webhook_token is None:
            raise RuntimeError(f"Signup event {event['id']} has no webhook credentials")
        return discord.Webhook.partial(
            int(webhook_id),
            str(webhook_token),
            client=self.bot,
        )

    @app_commands.command(
        name="create-signup",
        description="建立表符活動報名，截止後公布錄取及候補名單",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        title="活動名稱",
        deadline="台灣時間截止時間，格式 YYYY-MM-DD HH:MM",
        capacity="名額上限；留空或輸入 0 表示不限",
        description="活動說明（選填）",
    )
    async def create_signup(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 100],
        deadline: str,
        capacity: app_commands.Range[int, 0, 1_000_000] | None = None,
        description: app_commands.Range[str, 0, 1000] | None = None,
    ) -> None:
        if (
            interaction.guild is None
            or not isinstance(
                interaction.channel,
                (discord.TextChannel, discord.Thread),
            )
            or interaction.channel_id is None
        ):
            await interaction.response.send_message(
                "請在伺服器文字頻道或討論串中使用此指令。",
                ephemeral=True,
            )
            return

        if not DEADLINE_PATTERN.fullmatch(deadline):
            await interaction.response.send_message(
                "截止時間格式錯誤，請輸入 YYYY-MM-DD HH:MM（台灣時間）。",
                ephemeral=True,
            )
            return
        try:
            local_deadline = datetime.strptime(
                deadline,
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=TAIWAN_TIMEZONE)
        except ValueError:
            await interaction.response.send_message(
                "截止時間不是有效日期，請檢查年月日與時間。",
                ephemeral=True,
            )
            return
        deadline_utc = local_deadline.astimezone(timezone.utc)
        if deadline_utc <= datetime.now(timezone.utc):
            await interaction.response.send_message(
                "報名截止時間必須晚於現在。",
                ephemeral=True,
            )
            return

        title_value = str(title).strip()
        description_value = normalize_multiline_text(str(description or ""))
        if not title_value:
            await interaction.response.send_message(
                "活動名稱不可空白。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        creator_name = interaction.user.display_name
        creator_avatar_url = interaction.user.display_avatar.url
        try:
            event_id = create_signup_event(
                self.database,
                interaction.guild.id,
                interaction.channel_id,
                interaction.user.id,
                creator_name,
                creator_avatar_url,
                title_value,
                description_value,
                deadline_utc.isoformat(),
                int(capacity or 0),
                "[]",
                datetime.now(timezone.utc).isoformat(),
            )
        except (sqlite3.Error, RuntimeError):
            logger.exception("Unable to create signup event")
            await interaction.followup.send(
                "建立報名活動失敗，請稍後再試。",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        webhook_channel = channel.parent if isinstance(channel, discord.Thread) else channel
        webhook: discord.Webhook | None = None
        panel_message: discord.WebhookMessage | None = None
        try:
            if not isinstance(webhook_channel, discord.TextChannel):
                raise TypeError("活動頻道不支援 Webhook")
            webhook = await webhook_channel.create_webhook(
                name=f"Signup event {event_id}",
                reason=f"{creator_name} 建立活動報名",
            )
            if webhook.token is None:
                raise RuntimeError("Discord did not return a webhook token")
            set_signup_event_webhook(
                self.database,
                event_id,
                webhook.id,
                webhook.token,
            )
            embed = self.create_signup_embed(
                title_value,
                description_value,
                deadline_utc,
                int(capacity or 0),
                open_status=True,
            )
            kwargs: dict[str, object] = {
                "username": creator_name,
                "avatar_url": creator_avatar_url,
                "view": SignupPanelView(self, event_id),
                "wait": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if isinstance(channel, discord.Thread):
                kwargs["thread"] = channel
            panel_message = await webhook.send(embed=embed, **kwargs)
            set_signup_event_message(self.database, event_id, panel_message.id)
            await panel_message.add_reaction(SIGNUP_EMOJI)
        except (discord.Forbidden, discord.HTTPException, sqlite3.Error, RuntimeError, TypeError):
            logger.exception("Unable to publish signup event %s", event_id)
            await self.cleanup_incomplete_event(event_id, panel_message, webhook)
            await interaction.followup.send(
                "無法建立報名卡片。請確認 Bot 有「管理 Webhook」、「檢視頻道」、"
                "「傳送訊息」、「嵌入連結」及「新增反應」權限。",
                ephemeral=True,
            )
            return

        self.panel_event_ids[panel_message.id] = event_id
        self.bot.add_view(
            SignupPanelView(self, event_id),
            message_id=panel_message.id,
        )
        await interaction.followup.send(
            f"已建立報名活動「{title_value}」：{panel_message.jump_url}。"
            "建立者已自動列入報名。",
            ephemeral=True,
        )

    @staticmethod
    def create_signup_embed(
        title: str,
        description: str,
        deadline: datetime,
        capacity: int,
        open_status: bool,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"報名｜{title}",
            description=description or "未提供活動說明。",
            color=discord.Color(0x5765F2),
        )
        embed.add_field(
            name="報名方式",
            value="按下 ✅ 報名，再次移除 ✅ 可取消；按「填寫／修改備註」提供僅建立者可見的備註。",
            inline=False,
        )
        embed.add_field(
            name="名額",
            value="不限" if capacity == 0 else str(capacity),
            inline=True,
        )
        embed.add_field(
            name="報名截止",
            value=f"{deadline.astimezone(TAIWAN_TIMEZONE):%Y-%m-%d %H:%M}（台灣時間）",
            inline=True,
        )
        embed.set_footer(
            text=format_event_footer(deadline, closed=not open_status)
        )
        return embed

    async def cleanup_incomplete_event(
        self,
        event_id: int,
        panel_message: discord.WebhookMessage | None,
        webhook: discord.Webhook | None,
    ) -> None:
        try:
            delete_signup_event(self.database, event_id)
        except sqlite3.Error:
            logger.exception("Unable to delete incomplete signup event %s", event_id)
        if webhook is None:
            return
        if panel_message is not None:
            try:
                await webhook.delete_message(panel_message.id)
            except discord.HTTPException:
                logger.exception(
                    "Unable to delete incomplete panel for event %s",
                    event_id,
                )
        try:
            await webhook.delete(reason=f"Signup event {event_id} setup failed")
        except discord.HTTPException:
            logger.exception(
                "Unable to delete incomplete webhook for event %s",
                event_id,
            )

    async def handle_deadline(self, event: sqlite3.Row) -> None:
        event_id = signup_event_id(event)
        set_signup_event_status(self.database, event_id, "closing")
        try:
            members = await self.get_reaction_members(event)
        except discord.NotFound:
            logger.warning(
                "Signup panel %s for event %s no longer exists; "
                "using the last persisted signup list",
                event["panel_message_id"],
                event_id,
            )
            members = None

        if members is not None:
            existing_timestamps = {
                int(row["user_id"]): str(row["registered_at"])
                for row in get_signup_members(self.database, event_id)
            }
            reconcile_signup_members(
                self.database,
                event_id,
                [
                    (
                        int(member.id),
                        str(member.display_name),
                        existing_timestamps.get(
                            int(member.id),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                    for member in members
                ],
                int(event["creator_id"]),
            )

        member_rows = get_signup_members(self.database, event_id)
        active_ids = (
            {int(member.id) for member in members}
            if members is not None
            else set()
        )
        active_ids.add(int(event["creator_id"]))
        set_signup_eligibility(
            self.database,
            event_id,
            {
                int(row["user_id"]): (
                    members is None
                    or int(row["user_id"]) in active_ids
                )
                for row in member_rows
            },
        )

        current_event = get_signup_event(self.database, event_id)
        if current_event is None:
            return
        active_members = [
            row
            for row in get_signup_members(self.database, event_id)
            if row["eligible_at_cutoff"] == 1
        ]
        capacity = int(current_event["capacity"])
        if capacity == 0 or len(active_members) <= capacity:
            await self.finalize_event(
                event_id,
                {int(row["user_id"]) for row in active_members},
            )
            return

        selection_deadline = datetime.now(timezone.utc) + ORGANIZER_PICK_TIMEOUT
        begin_signup_selection(
            self.database,
            event_id,
            selection_deadline.isoformat(),
            int(current_event["creator_id"]),
        )
        await self.disable_signup_panel(current_event)
        selecting_event = get_signup_event(self.database, event_id)
        if selecting_event is None or self.selection_slots(event_id) <= 0:
            await self.finalize_by_registration_order(event_id)
            return
        await self.send_organizer_selection(selecting_event)

    async def get_reaction_members(
        self,
        event: sqlite3.Row,
    ) -> list[discord.User | discord.Member]:
        channel = await self.get_channel(int(event["channel_id"]))
        panel_id = event["panel_message_id"]
        if panel_id is None:
            raise RuntimeError(f"Signup event {event['id']} has no panel message")
        message = await channel.fetch_message(int(panel_id))
        reaction = next(
            (reaction for reaction in message.reactions if str(reaction.emoji) == SIGNUP_EMOJI),
            None,
        )
        if reaction is None:
            return []

        users: list[discord.User | discord.Member] = []
        async for user in reaction.users(limit=None):
            if user.bot:
                continue
            users.append(user)
        return users

    async def disable_signup_panel(self, event: sqlite3.Row) -> None:
        if event["panel_message_id"] is None:
            return
        webhook = await self.get_event_webhook(event)
        channel = await self.get_channel(int(event["channel_id"]))
        thread = channel if isinstance(channel, discord.Thread) else None
        kwargs: dict[str, object] = {
            "embed": self.create_signup_embed(
                str(event["title"]),
                str(event["description"]),
                datetime.fromisoformat(event["deadline_at"]),
                int(event["capacity"]),
                open_status=False,
            ),
            "view": SignupPanelView(self, int(event["id"]), disabled=True),
        }
        if thread is not None:
            kwargs["thread"] = thread
        try:
            await webhook.edit_message(int(event["panel_message_id"]), **kwargs)
        except discord.NotFound:
            logger.warning(
                "Signup panel %s for event %s no longer exists; "
                "skipping panel update",
                event["panel_message_id"],
                event["id"],
            )
        except discord.HTTPException:
            logger.exception(
                "Unable to disable signup panel for event %s; "
                "continuing deadline processing",
                event["id"],
            )

    async def send_organizer_selection(self, event: sqlite3.Row) -> None:
        if event["selection_message_id"] is not None:
            return
        creator = self.bot.get_user(int(event["creator_id"]))
        if creator is None:
            try:
                creator = await self.bot.fetch_user(int(event["creator_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception("Unable to fetch organizer for event %s", event["id"])
                await self.finalize_by_registration_order(int(event["id"]))
                return
        candidates = self.get_eligible_members(int(event["id"]), exclude_creator=True)
        view = self.create_selection_view(
            event,
            candidates,
            set(json.loads(event["selected_user_ids_json"])),
        )
        try:
            message = await creator.send(
                f"活動「{event['title']}」報名人數超過名額。"
                "請在 30 分鐘內選擇錄取者；逾時將依報名順序取前面名額。",
                view=view,
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Unable to DM organizer for signup event %s", event["id"])
            await self.finalize_by_registration_order(int(event["id"]))
            return
        set_signup_selection_message(
            self.database,
            int(event["id"]),
            message.channel.id,
            message.id,
        )
        self.bot.add_view(view, message_id=message.id)

    async def finalize_by_registration_order(self, event_id: int) -> None:
        event = get_signup_event(self.database, event_id)
        if event is None:
            return
        capacity = int(event["capacity"])
        eligible = self.get_eligible_members(event_id)
        eligible.sort(key=lambda row: (row["registered_at"], int(row["user_id"])))
        accepted = eligible if capacity == 0 else eligible[:capacity]
        await self.finalize_event(
            event_id,
            {int(row["user_id"]) for row in accepted},
        )

    async def finalize_event(self, event_id: int, selected_ids: set[int]) -> bool:
        event = get_signup_event(self.database, event_id)
        if event is None:
            return False
        eligible_ids = {
            int(row["user_id"])
            for row in self.get_eligible_members(event_id)
        }
        creator_id = int(event["creator_id"])
        accepted_ids = (selected_ids & eligible_ids) | {creator_id}
        capacity = int(event["capacity"])
        if capacity > 0 and len(accepted_ids) > capacity:
            creator_first = [creator_id] + sorted(accepted_ids - {creator_id})
            accepted_ids = set(creator_first[:capacity])
        finalized = finalize_signup_event(
            self.database,
            event_id,
            sorted(accepted_ids),
        )
        if not finalized:
            return False
        await self.publish_announcement(event_id)
        return True

    async def publish_announcement(self, event_id: int) -> None:
        event = get_signup_event(self.database, event_id)
        if event is None or event["status"] != "announcing":
            return
        members = get_signup_members(self.database, event_id)
        accepted = [row for row in members if row["result"] == "accepted"]
        waitlist = [row for row in members if row["result"] == "waitlist"]
        accepted.sort(key=lambda row: (row["registered_at"], int(row["user_id"])))
        waitlist.sort(key=lambda row: (row["registered_at"], int(row["user_id"])))
        embeds = self.create_result_embeds(
            str(event["title"]),
            accepted,
            waitlist,
            datetime.fromisoformat(event["deadline_at"]),
        )
        webhook = await self.get_event_webhook(event)
        channel = await self.get_channel(int(event["channel_id"]))
        thread = channel if isinstance(channel, discord.Thread) else None
        message_id = event["panel_message_id"]
        if message_id is None:
            raise RuntimeError(f"Signup event {event_id} has no original panel message")
        kwargs: dict[str, object] = {
            "content": (
                f"<@{int(event['creator_id'])}> "
                f"報名「{event['title']}」已截止，結果如下："
            ),
            "embed": embeds[0],
            "allowed_mentions": discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[discord.Object(id=int(event["creator_id"]))],
                replied_user=False,
            ),
            "view": (
                ResultPaginationEntryView("signup", event_id)
                if len(embeds) > 1
                else None
            ),
        }
        if thread is not None:
            kwargs["thread"] = thread
        announcement_published = False
        try:
            await webhook.edit_message(int(message_id), **kwargs)
        except discord.NotFound:
            logger.warning(
                "Signup panel %s for event %s no longer exists; "
                "cannot publish the result on the original message",
                message_id,
                event_id,
            )
        else:
            announcement_published = True
            set_signup_announcement_message_ids(
                self.database,
                event_id,
                json.dumps([int(message_id)]),
            )

        await self.send_creator_notes(event, members)
        complete_signup_event(self.database, event_id)
        if announcement_published:
            await notify_event_creator(
                self.bot,
                creator_id=int(event["creator_id"]),
                event_id=event_id,
                event_type="報名",
                title=str(event["title"]),
                guild_id=int(event["guild_id"]),
                channel_id=int(event["channel_id"]),
                message_id=int(message_id),
            )
        try:
            await webhook.delete(reason=f"Signup event {event_id} has ended")
        except discord.HTTPException:
            logger.exception("Unable to delete webhook for signup event %s", event_id)

    @staticmethod
    def create_result_embeds(
        title: str,
        accepted: Sequence[sqlite3.Row],
        waitlist: Sequence[sqlite3.Row],
        deadline: datetime,
    ) -> list[discord.Embed]:
        lines: list[tuple[str, Sequence[sqlite3.Row]]] = []
        if accepted:
            lines.append(("錄取名單", accepted))
        if waitlist:
            lines.append(("候補名單", waitlist))
        embeds: list[discord.Embed] = []
        for heading, entries in lines:
            names = [
                f"{index}. <@{int(entry['user_id'])}>"
                for index, entry in enumerate(entries, start=1)
            ]
            for start in range(0, len(names), 15):
                embed = discord.Embed(
                    title=f"報名結果｜{title}",
                    description=(
                        f"**{heading}**\n"
                        + "\n".join(names[start : start + 15])
                    ),
                    color=discord.Color(0x5765F2),
                )
                embeds.append(embed)
        if not embeds:
            embeds.append(
                discord.Embed(
                    title=f"報名結果｜{title}",
                    description="截止時沒有人報名。",
                    color=discord.Color(0x5765F2),
                )
            )
        for index, embed in enumerate(embeds, start=1):
            embed.set_footer(
                text=format_event_footer(
                    deadline,
                    closed=True,
                    page=(index, len(embeds)) if len(embeds) > 1 else None,
                )
            )
        return embeds

    def create_result_embeds_for_event(
        self,
        event_id: int,
        guild_id: int | None,
    ) -> list[PaginationPage]:
        event = get_signup_event(self.database, event_id)
        if (
            event is None
            or event["status"] != "closed"
            or guild_id != int(event["guild_id"])
        ):
            return []
        members = get_signup_members(self.database, event_id)
        accepted = [row for row in members if row["result"] == "accepted"]
        waitlist = [row for row in members if row["result"] == "waitlist"]
        accepted.sort(key=lambda row: (row["registered_at"], int(row["user_id"])))
        waitlist.sort(key=lambda row: (row["registered_at"], int(row["user_id"])))
        return [
            PaginationPage(embed=embed)
            for embed in self.create_result_embeds(
                str(event["title"]),
                accepted,
                waitlist,
                datetime.fromisoformat(event["deadline_at"]),
            )
        ]

    async def send_creator_notes(
        self,
        event: sqlite3.Row,
        members: Sequence[sqlite3.Row],
    ) -> None:
        notes = [
            f"{row['display_name']}：{row['note']}"
            for row in members
            if row["note"]
        ]
        if not notes:
            return
        creator = self.bot.get_user(int(event["creator_id"]))
        if creator is None:
            try:
                creator = await self.bot.fetch_user(int(event["creator_id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception("Unable to fetch event creator %s", event["creator_id"])
                return
        if creator is None:
            logger.warning("Creator unavailable for private notes of event %s", event["id"])
            return
        header = f"活動「{event['title']}」的報名備註（僅你可見）：\n"
        messages: list[str] = []
        current_message = header
        for note in notes:
            if len(current_message) + len(note) + 1 > 1900:
                messages.append(current_message)
                current_message = header
            current_message += f"{note}\n"
        if current_message != header:
            messages.append(current_message)
        try:
            for message in messages:
                await creator.send(
                    message,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            logger.exception("Unable to DM signup notes for event %s", event["id"])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Signup(bot))
