from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs._text import normalize_multiline_text
from cogs.pagination import format_event_footer, notify_event_creator
from database import (
    begin_vote_poll_close,
    complete_vote_poll,
    create_vote_poll,
    delete_vote_poll,
    get_open_vote_polls,
    get_vote_poll,
    get_vote_results,
    get_vote_selection,
    replace_vote_selection,
    set_vote_poll_webhook,
    set_vote_poll_message,
)


TAIWAN_TIMEZONE = timezone(timedelta(hours=8))
DEADLINE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
RESULT_RETRY_DELAY = timedelta(seconds=60)
logger = logging.getLogger(__name__)


class OpenVoteButton(discord.ui.Button["VoteView"]):
    def __init__(self, poll_id: int, disabled: bool = False) -> None:
        super().__init__(
            label="投票／編輯選擇",
            style=discord.ButtonStyle.primary,
            custom_id=f"vote:{poll_id}:open",
            disabled=disabled,
        )
        self.poll_id = poll_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            await interaction.response.send_message(
                "投票按鈕目前無法使用，請稍後再試。",
                ephemeral=True,
            )
            return
        await self.view.cog.open_vote_selection(
            interaction,
            self.poll_id,
        )


class VoteView(discord.ui.View):
    def __init__(
        self,
        cog: Vote,
        poll_id: int,
        disabled: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(OpenVoteButton(poll_id, disabled))


class VoteSelectionView(discord.ui.View):
    def __init__(
        self,
        cog: Vote,
        poll_id: int,
        options: list[str],
        max_votes: int,
        selected_indices: list[int],
        user_id: int,
    ) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.poll_id = poll_id
        self.user_id = user_id
        self.option_select = discord.ui.Select(
            placeholder="選擇你要投的選項",
            min_values=1,
            max_values=max_votes,
            options=[
                discord.SelectOption(
                    label=option,
                    value=str(index),
                    default=index in selected_indices,
                )
                for index, option in enumerate(options)
            ],
        )
        self.option_select.callback = self.on_selection_changed
        self.add_item(self.option_select)
        submit_button = discord.ui.Button(
            label="送出／覆蓋我的選擇",
            style=discord.ButtonStyle.success,
        )
        submit_button.callback = self.submit_selection
        self.add_item(submit_button)

    async def on_selection_changed(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer()

    async def submit_selection(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "這份私人投票選單不屬於你。",
                ephemeral=True,
            )
            return

        try:
            selected_indices = [
                int(value) for value in self.option_select.values
            ]
            result = self.cog.save_selection(
                self.poll_id,
                interaction.user.id,
                selected_indices,
            )
        except (sqlite3.Error, RuntimeError, TypeError, ValueError):
            logger.exception("Unable to save vote selection for poll %s", self.poll_id)
            await interaction.response.edit_message(
                content="目前無法記錄你的投票，請稍後再試。",
                view=None,
            )
            return

        messages = {
            "saved": "已送出投票；再次開啟選單並送出，可覆蓋你的選擇。",
            "closed": "此投票已截止，無法再更改選擇。",
            "invalid": "請至少選擇一個有效選項，且不可超過此投票的可選數量。",
        }
        await interaction.response.edit_message(
            content=messages[result],
            view=None,
        )


class Vote(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.close_tasks: dict[int, asyncio.Task[None]] = {}

    @property
    def database(self) -> sqlite3.Connection:
        connection = getattr(self.bot, "database", None)
        if not isinstance(connection, sqlite3.Connection):
            raise RuntimeError("SQLite 尚未初始化")
        return connection

    async def cog_load(self) -> None:
        for row in get_open_vote_polls(self.database):
            poll_id = int(row["id"])
            message_id = row["message_id"]
            try:
                options = json.loads(row["options_json"])
                deadline = datetime.fromisoformat(row["deadline_at"])
                if not isinstance(options, list) or not all(
                    isinstance(option, str) for option in options
                ):
                    raise ValueError("投票選項資料格式錯誤")
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.exception("Unable to restore vote poll %s", poll_id)
                continue

            if message_id is None:
                logger.warning(
                    "Removing incomplete vote poll %s with no published message",
                    poll_id,
                )
                delete_vote_poll(self.database, poll_id)
                continue

            if row["status"] == "open":
                self.bot.add_view(
                    VoteView(self, poll_id),
                    message_id=int(message_id),
                )
            self.schedule_close(poll_id, deadline)

    def cog_unload(self) -> None:
        for task in self.close_tasks.values():
            task.cancel()
        self.close_tasks.clear()

    def schedule_close(self, poll_id: int, deadline: datetime) -> None:
        existing = self.close_tasks.get(poll_id)
        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(self.close_at_deadline(poll_id, deadline))
        self.close_tasks[poll_id] = task
        task.add_done_callback(
            lambda completed, current_poll_id=poll_id: self.remove_close_task(
                current_poll_id,
                completed,
            )
        )

    def remove_close_task(
        self,
        poll_id: int,
        task: asyncio.Task[None],
    ) -> None:
        if self.close_tasks.get(poll_id) is task:
            self.close_tasks.pop(poll_id, None)

    async def close_at_deadline(
        self,
        poll_id: int,
        deadline: datetime,
    ) -> None:
        await self.bot.wait_until_ready()
        while True:
            seconds_until_deadline = (
                deadline - datetime.now(timezone.utc)
            ).total_seconds()
            if seconds_until_deadline > 0:
                await asyncio.sleep(min(seconds_until_deadline, 86400))
                continue

            try:
                await self.publish_results(poll_id)
            except (discord.HTTPException, sqlite3.Error, RuntimeError, TypeError):
                logger.exception(
                    "Unable to publish results for vote poll %s; retrying",
                    poll_id,
                )
                await asyncio.sleep(RESULT_RETRY_DELAY.total_seconds())
                continue
            return

    async def publish_results(self, poll_id: int) -> None:
        poll = begin_vote_poll_close(self.database, poll_id)
        if poll is None:
            return

        options = json.loads(poll["options_json"])
        counts, voter_count, ballot_count = get_vote_results(
            self.database,
            poll_id,
        )
        highest_vote_count = max(
            (counts.get(index, 0) for index in range(len(options))),
            default=0,
        )
        winners = (
            [
                index
                for index in range(len(options))
                if counts.get(index, 0) == highest_vote_count
            ]
            if highest_vote_count > 0
            else []
        )

        result_lines = [
            f"{'🏆 ' if index in winners else ''}**{option}** — "
            f"{counts.get(index, 0)} 票"
            for index, option in enumerate(options)
        ]
        if winners:
            winner_summary = "、".join(options[index] for index in winners)
            outcome = f"最高票：**{winner_summary}**（{highest_vote_count} 票）"
        else:
            outcome = "無人投票，沒有得票結果。"

        embed = discord.Embed(
            title=f"投票結果｜{poll['title']}",
            description=poll["description"],
            color=discord.Color(0xFAC84B),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="票數", value="\n".join(result_lines), inline=False)
        embed.add_field(name="最終結果", value=outcome, inline=False)
        embed.set_footer(
            text=(
                f"{format_event_footer(datetime.fromisoformat(poll['deadline_at']), closed=True)}"
                f"｜參與人數 {voter_count}｜總選項票數 {ballot_count}"
            )
        )

        channel = await self.get_text_channel(int(poll["channel_id"]))
        webhook = self.get_poll_webhook(poll)
        thread = channel if isinstance(channel, discord.Thread) else None
        message_id = poll["message_id"]
        if message_id is None:
            raise RuntimeError(f"Vote poll {poll_id} has no original panel message")

        creator_id = int(poll["creator_id"])
        edit_kwargs: dict[str, object] = {
            "content": f"<@{creator_id}> 投票「{poll['title']}」已截止，結果如下：",
            "embed": embed,
            "view": None,
            "allowed_mentions": discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[discord.Object(id=creator_id)],
                replied_user=False,
            ),
        }
        announcement_published = False
        try:
            if webhook is None:
                original_message = await channel.fetch_message(int(message_id))
                await original_message.edit(**edit_kwargs)
            else:
                if thread is not None:
                    edit_kwargs["thread"] = thread
                await webhook.edit_message(int(message_id), **edit_kwargs)
        except discord.NotFound:
            logger.warning(
                "Original message for vote poll %s no longer exists; "
                "cannot publish the result on it",
                poll_id,
            )
        else:
            announcement_published = True

        complete_vote_poll(
            self.database,
            poll_id,
            int(message_id),
        )
        if announcement_published:
            await notify_event_creator(
                self.bot,
                creator_id=creator_id,
                event_id=poll_id,
                event_type="投票",
                title=str(poll["title"]),
                guild_id=int(poll["guild_id"]),
                channel_id=int(poll["channel_id"]),
                message_id=int(message_id),
            )
        if webhook is not None:
            try:
                await webhook.delete(reason=f"Vote poll {poll_id} has ended")
            except discord.HTTPException:
                logger.exception("Unable to delete webhook for vote poll %s", poll_id)

    def get_poll_webhook(
        self,
        poll: sqlite3.Row,
    ) -> discord.Webhook | None:
        webhook_id = poll["webhook_id"]
        webhook_token = poll["webhook_token"]
        if webhook_id is None or webhook_token is None:
            return None
        return discord.Webhook.partial(
            int(webhook_id),
            str(webhook_token),
            client=self.bot,
        )

    async def cleanup_incomplete_poll(
        self,
        poll_id: int,
        poll_message: discord.WebhookMessage | None,
        webhook: discord.Webhook | None,
    ) -> None:
        try:
            delete_vote_poll(self.database, poll_id)
        except sqlite3.Error:
            logger.exception("Unable to remove incomplete vote poll %s", poll_id)

        if webhook is None:
            return
        if poll_message is not None:
            try:
                await webhook.delete_message(poll_message.id)
            except discord.HTTPException:
                logger.exception(
                    "Unable to remove incomplete vote message for poll %s",
                    poll_id,
                )
        try:
            await webhook.delete(reason="投票卡片未能建立")
        except discord.HTTPException:
            logger.exception(
                "Unable to remove incomplete webhook for poll %s",
                poll_id,
            )

    async def get_text_channel(
        self,
        channel_id: int,
    ) -> discord.TextChannel | discord.Thread:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        raise TypeError(f"Channel {channel_id} is not a text channel or thread")

    def save_selection(
        self,
        poll_id: int,
        voter_id: int,
        selected_indices: list[int],
    ) -> str:
        poll = get_vote_poll(self.database, poll_id)
        if (
            poll is None
            or poll["status"] != "open"
            or datetime.fromisoformat(poll["deadline_at"])
            <= datetime.now(timezone.utc)
        ):
            return "closed"
        return replace_vote_selection(
            self.database,
            poll_id,
            voter_id,
            selected_indices,
        )

    async def open_vote_selection(
        self,
        interaction: discord.Interaction,
        poll_id: int,
    ) -> None:
        if interaction.message is None:
            await interaction.response.send_message(
                "無法確認此投票訊息，請從原投票訊息操作。",
                ephemeral=True,
            )
            return

        try:
            poll = get_vote_poll(self.database, poll_id)
            if (
                poll is None
                or poll["status"] != "open"
                or poll["message_id"] != interaction.message.id
                or poll["channel_id"] != interaction.channel_id
                or datetime.fromisoformat(poll["deadline_at"])
                <= datetime.now(timezone.utc)
            ):
                await interaction.response.send_message(
                    "此投票已截止或無法使用。",
                    ephemeral=True,
                )
                return

            options = json.loads(poll["options_json"])
            selected_indices = get_vote_selection(
                self.database,
                poll_id,
                interaction.user.id,
            )
        except (sqlite3.Error, RuntimeError, TypeError, ValueError):
            logger.exception("Unable to load vote selection for poll %s", poll_id)
            await interaction.response.send_message(
                "目前無法讀取投票資料，請稍後再試。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "請選擇 1 到 "
            f"{poll['max_votes']} 個選項，再按「送出／覆蓋我的選擇」。",
            view=VoteSelectionView(
                self,
                poll_id,
                options,
                int(poll["max_votes"]),
                selected_indices,
                interaction.user.id,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="create-vote",
        description="建立限時按鈕投票，截止後公布結果",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        title="投票標題",
        description="投票說明",
        option1="第一個選項",
        option2="第二個選項",
        max_votes="每人最多可選幾個選項",
        deadline="截止時間，使用台灣時間 YYYY-MM-DD HH:MM",
        option3="第三個選項（選填）",
        option4="第四個選項（選填）",
        option5="第五個選項（選填）",
    )
    async def create_vote(
        self,
        interaction: discord.Interaction,
        title: str,
        option1: str,
        option2: str,
        max_votes: app_commands.Range[int, 1, 5],
        deadline: str,
        description: str | None = None,
        option3: str | None = None,
        option4: str | None = None,
        option5: str | None = None,
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                "請在伺服器文字頻道中使用此指令。",
                ephemeral=True,
            )
            return

        options = [
            option.strip()
            for option in (option1, option2, option3, option4, option5)
            if option is not None
        ]
        title = title.strip()
        description = normalize_multiline_text(description or "")

        if not title or len(title) > 250:
            await interaction.response.send_message(
                "投票標題不可空白，且最多 250 個字元。",
                ephemeral=True,
            )
            return
        if len(description) > 3000:
            await interaction.response.send_message(
                "投票說明最多 3000 個字元。",
                ephemeral=True,
            )
            return
        if any(not option or len(option) > 80 for option in options):
            await interaction.response.send_message(
                "每個選項不可空白，且最多 80 個字元。",
                ephemeral=True,
            )
            return
        if len(set(options)) != len(options):
            await interaction.response.send_message(
                "投票選項不可重複。",
                ephemeral=True,
            )
            return
        if max_votes > len(options):
            await interaction.response.send_message(
                f"每人可投票數不可大於選項數（{len(options)}）。",
                ephemeral=True,
            )
            return
        if not DEADLINE_PATTERN.fullmatch(deadline):
            await interaction.response.send_message(
                "截止時間格式錯誤，請使用 YYYY-MM-DD HH:MM（台灣時間）。",
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

        utc_deadline = local_deadline.astimezone(timezone.utc)
        if utc_deadline <= datetime.now(timezone.utc):
            await interaction.response.send_message(
                "截止時間必須晚於現在。",
                ephemeral=True,
            )
            return
        if not isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "目前頻道無法發布投票，請改在文字頻道或討論串使用。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        creator_name = interaction.user.display_name
        creator_avatar_url = (
            interaction.user.avatar or interaction.user.default_avatar
        ).url
        try:
            poll_id = create_vote_poll(
                self.database,
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                creator_name,
                creator_avatar_url,
                title,
                description,
                json.dumps(options, ensure_ascii=False),
                max_votes,
                utc_deadline.isoformat(),
                datetime.now(timezone.utc).isoformat(),
            )
        except (sqlite3.Error, RuntimeError):
            logger.exception("Unable to create vote poll")
            await interaction.followup.send(
                "建立投票失敗，請稍後再試。",
                ephemeral=True,
            )
            return

        poll_message: discord.WebhookMessage | None = None
        webhook: discord.Webhook | None = None
        deadline_display = local_deadline.strftime("%Y-%m-%d %H:%M")
        embed = discord.Embed(
            title=f"投票｜{title}",
            description=description,
            color=discord.Color(0xFAC84B),
        )
        embed.add_field(
            name="投票選項",
            value="\n".join(
                f"{index + 1}. {option}"
                for index, option in enumerate(options)
            ),
            inline=False,
        )
        embed.add_field(
            name="投票方式",
            value=(
                f"每人可選至多 **{max_votes}** 個選項。"
                "按下按鈕可開啟私人選單；再次送出會覆蓋原本選擇。"
            ),
            inline=False,
        )
        embed.add_field(
            name="截止時間",
            value=f"{deadline_display}（台灣時間）",
            inline=False,
        )
        embed.set_footer(
            text=format_event_footer(
                local_deadline,
                closed=False,
            )
        )
        try:
            webhook_channel = (
                interaction.channel.parent
                if isinstance(interaction.channel, discord.Thread)
                else interaction.channel
            )
            if not isinstance(webhook_channel, discord.TextChannel):
                raise TypeError("投票頻道不支援 Webhook")
            webhook = await webhook_channel.create_webhook(
                name=f"Vote poll {poll_id}",
                reason=f"{creator_name} 建立投票",
            )
            if webhook.token is None:
                raise RuntimeError("Discord did not return a webhook token")
            set_vote_poll_webhook(
                self.database,
                poll_id,
                webhook.id,
                webhook.token,
            )
            send_kwargs: dict[str, object] = {
                "view": VoteView(self, poll_id),
                "username": creator_name,
                "avatar_url": creator_avatar_url,
                "wait": True,
            }
            if isinstance(interaction.channel, discord.Thread):
                send_kwargs["thread"] = interaction.channel
            poll_message = await webhook.send(
                embed=embed,
                **send_kwargs,
            )
            set_vote_poll_message(
                self.database,
                poll_id,
                poll_message.id,
            )
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to publish vote poll %s in channel %s",
                poll_id,
                interaction.channel_id,
            )
            await self.cleanup_incomplete_poll(poll_id, poll_message, webhook)
            await interaction.followup.send(
                "無法在此頻道以投票發起者身份發布卡片。請確認 Bot 有"
                "「管理 Webhook」權限；若是在討論串，亦需能在該討論串傳送訊息。",
                ephemeral=True,
            )
            return
        except (discord.HTTPException, sqlite3.Error, RuntimeError, TypeError):
            logger.exception("Unable to publish vote poll %s", poll_id)
            await self.cleanup_incomplete_poll(poll_id, poll_message, webhook)
            await interaction.followup.send(
                "投票未能發布。請確認 Bot 有管理 Webhook、檢視頻道及"
                "在此頻道傳送訊息的權限。",
                ephemeral=True,
            )
            return

        self.schedule_close(poll_id, utc_deadline)
        await interaction.followup.send(
            f"投票已建立：{poll_message.jump_url}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Vote(bot))
