from __future__ import annotations

import json
import logging
import re
import psycopg
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._text import normalize_multiline_text
from cogs._pagination import (
    PaginationPage,
    PersistentPageButton,
    ResultPaginationEntryView,
    format_event_footer,
    notify_event_creator,
)
from database import (
    Database,
    complete_order_event,
    create_order_event,
    delete_order_event,
    get_open_order_events,
    get_order_event,
    get_order_participants,
    save_order_items,
    set_order_announcement_message_ids,
    set_order_event_message,
    set_order_event_status,
    set_order_event_webhook,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


TAIWAN_TIMEZONE = timezone(timedelta(hours=8))
DEADLINE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
MAX_ITEMS_LENGTH = 1000
logger = logging.getLogger(__name__)


def validate_menu_source(
    attachment: discord.Attachment | None,
    description: str,
) -> str | None:
    if attachment is not None and (
        attachment.content_type is None
        or not attachment.content_type.startswith("image/")
    ):
        return "附件必須是圖片，請重新上傳圖片。"
    if attachment is None and not description.strip():
        return (
            "請上傳菜單圖片，或在說明欄輸入菜單內容或菜單連結，"
            "至少提供其中一種。"
        )
    return None


class OrderItemsModal(discord.ui.Modal, title="登記揪團品項"):
    def __init__(self, cog: Order, event_id: int, current_items: str = "") -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.event_id = event_id
        self.items = discord.ui.TextInput(
            label="品項與價格（每行一項）",
            placeholder="例如：珍珠奶茶 60 元\n留空送出可清除登記",
            required=False,
            max_length=MAX_ITEMS_LENGTH,
            style=discord.TextStyle.paragraph,
            default=current_items,
        )
        self.add_item(self.items)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        items = "\n".join(
            line.strip()
            for line in str(self.items.value).splitlines()
            if line.strip()
        )
        try:
            saved = await save_order_items(
                self.cog.database,
                self.event_id,
                interaction.user.id,
                interaction.user.display_name,
                items,
                datetime.now(timezone.utc).isoformat(),
            )
        except psycopg.Error:
            logger.exception("Unable to save order items for event %s", self.event_id)
            await interaction.response.send_message(
                "目前無法保存品項，請稍後再試。",
                ephemeral=True,
            )
            return

        if not saved:
            await interaction.response.send_message(
                "此揪團已截止，無法修改品項。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "品項已清除。" if not items else "品項已更新。",
            ephemeral=True,
        )


class OrderPanelView(discord.ui.View):
    def __init__(
        self,
        cog: Order,
        event_id: int,
        disabled: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.event_id = event_id
        button = discord.ui.Button(
            label="登記／修改品項",
            style=discord.ButtonStyle.primary,
            custom_id=f"order:{event_id}:items",
            disabled=disabled,
        )
        button.callback = self.open_items
        self.add_item(button)

    async def open_items(self, interaction: discord.Interaction) -> None:
        event = await get_order_event(self.cog.database, self.event_id)
        if (
            event is None
            or event["status"] != "open"
            or interaction.guild_id != int(event["guild_id"])
        ):
            await interaction.response.send_message(
                "此揪團已截止或不存在。",
                ephemeral=True,
            )
            return

        participant = next(
            (
                row
                for row in await self.cog.get_participants(self.event_id)
                if int(row["user_id"]) == interaction.user.id
            ),
            None,
        )
        await interaction.response.send_modal(
            OrderItemsModal(
                self.cog,
                self.event_id,
                str(participant["items"]) if participant is not None else "",
            )
        )


class Order(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def database(self) -> Database:
        connection = getattr(self.bot, "database", None)
        if not isinstance(connection, Database):
            raise RuntimeError("PostgreSQL 尚未初始化")
        return connection

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(PersistentPageButton)
        for event in await get_open_order_events(self.database):
            if event["panel_message_id"] is not None:
                self.bot.add_view(
                    OrderPanelView(
                        self,
                        int(event["id"]),
                        event["status"] != "open",
                    ),
                    message_id=int(event["panel_message_id"]),
                )
        self.process_deadlines.start()

    def cog_unload(self) -> None:
        self.process_deadlines.cancel()

    @tasks.loop(seconds=10)
    async def process_deadlines(self) -> None:
        now = datetime.now(timezone.utc)
        for event in await get_open_order_events(self.database):
            event_id = int(event["id"])
            try:
                if (
                    event["status"] == "open"
                    and datetime.fromisoformat(event["deadline_at"]) <= now
                ):
                    if not await set_order_event_status(
                        self.database,
                        event_id,
                        "announcing",
                        expected_status="open",
                    ):
                        continue
                    event = await get_order_event(self.database, event_id)
                    if event is None:
                        continue
                if event["status"] == "announcing":
                    await self.publish_announcement(event)
            except (
                discord.HTTPException,
                psycopg.Error,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                logger.exception("Unable to process order event %s", event_id)

    @process_deadlines.before_loop
    async def before_process_deadlines(self) -> None:
        await self.bot.wait_until_ready()

    async def get_participants(self, event_id: int) -> list[dict[str, Any]]:
        return await get_order_participants(self.database, event_id)

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

    async def get_event_webhook(self, event: dict[str, Any]) -> discord.Webhook:
        webhook_id = event["webhook_id"]
        webhook_token = event["webhook_token"]
        if webhook_id is None or webhook_token is None:
            raise RuntimeError(f"Order event {event['id']} has no webhook credentials")
        return discord.Webhook.partial(
            int(webhook_id),
            str(webhook_token),
            client=self.bot,
        )

    @app_commands.command(
        name="create-order",
        description="建立揪團並收集成員欲購買的品項",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        title="揪團標題",
        deadline="台灣時間截止時間，格式 YYYY-MM-DD HH:MM",
        attachment="菜單圖片（選填）",
        description="說明文字或菜單連結（圖片與說明至少提供一種）",
    )
    async def create_order(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 100],
        deadline: str,
        attachment: discord.Attachment | None = None,
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
            deadline_local = datetime.strptime(
                deadline,
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=TAIWAN_TIMEZONE)
        except ValueError:
            await interaction.response.send_message(
                "截止時間不是有效日期，請檢查年月日與時間。",
                ephemeral=True,
            )
            return
        deadline_utc = deadline_local.astimezone(timezone.utc)
        if deadline_utc <= datetime.now(timezone.utc):
            await interaction.response.send_message(
                "揪團截止時間必須晚於現在。",
                ephemeral=True,
            )
            return

        title_value = str(title).strip()
        description_value = normalize_multiline_text(str(description or ""))
        if not title_value:
            await interaction.response.send_message(
                "揪團標題不可空白。",
                ephemeral=True,
            )
            return
        menu_source_error = validate_menu_source(attachment, description_value)
        if menu_source_error is not None:
            await interaction.response.send_message(
                menu_source_error,
                ephemeral=True,
            )
            return
        attachment_url = attachment.url if attachment is not None else ""

        await interaction.response.defer(ephemeral=True)
        creator_name = interaction.user.display_name
        creator_avatar_url = interaction.user.display_avatar.url
        try:
            event_id = await create_order_event(
                self.database,
                interaction.guild.id,
                interaction.channel_id,
                interaction.user.id,
                creator_name,
                creator_avatar_url,
                title_value,
                description_value,
                attachment_url,
                deadline_utc.isoformat(),
                datetime.now(timezone.utc).isoformat(),
            )
        except (psycopg.Error, RuntimeError):
            logger.exception("Unable to create order event")
            await interaction.followup.send(
                "建立揪團失敗，請稍後再試。",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        webhook_channel = channel.parent if isinstance(channel, discord.Thread) else channel
        webhook: discord.Webhook | None = None
        panel_message: discord.WebhookMessage | None = None
        try:
            if not isinstance(webhook_channel, discord.TextChannel):
                raise TypeError("此頻道不支援 Webhook")
            webhook = await webhook_channel.create_webhook(
                name=f"Order event {event_id}",
                reason=f"{creator_name} 建立揪團",
            )
            if webhook.token is None:
                raise RuntimeError("Discord did not return a webhook token")
            await set_order_event_webhook(
                self.database,
                event_id,
                webhook.id,
                webhook.token,
            )
            embed = self.create_order_embed(
                title_value,
                description_value,
                attachment_url,
                deadline_utc,
                open_status=True,
            )
            kwargs: dict[str, object] = {
                "username": creator_name,
                "avatar_url": creator_avatar_url,
                "embed": embed,
                "view": OrderPanelView(self, event_id),
                "wait": True,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if isinstance(channel, discord.Thread):
                kwargs["thread"] = channel
            panel_message = await webhook.send(**kwargs)
            await set_order_event_message(self.database, event_id, panel_message.id)
        except (
            discord.Forbidden,
            discord.HTTPException,
            psycopg.Error,
            RuntimeError,
            TypeError,
        ):
            logger.exception("Unable to publish order event %s", event_id)
            await self.cleanup_incomplete_event(event_id, panel_message, webhook)
            await interaction.followup.send(
                "無法建立揪團卡片。請確認 Bot 有「管理 Webhook」、「檢視頻道」、"
                "「傳送訊息」及「嵌入連結」權限。",
                ephemeral=True,
            )
            return

        self.bot.add_view(
            OrderPanelView(self, event_id),
            message_id=panel_message.id,
        )
        await interaction.followup.send(
            f"已建立揪團「{title_value}」：{panel_message.jump_url}。",
            ephemeral=True,
        )

    @staticmethod
    def create_order_embed(
        title: str,
        description: str,
        image_url: str,
        deadline: datetime,
        open_status: bool,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"揪團｜{title}",
            description=description or "未提供揪團說明。",
            color=discord.Color(0xE86060),
        )
        if image_url:
            embed.set_image(url=image_url)
        embed.add_field(
            name="參加方式",
            value="按下「登記／修改品項」，在表單中每行輸入一個品項與價格；"
            "重新提交會覆蓋原登記，空白提交會清除登記。",
            inline=False,
        )
        embed.add_field(
            name="截止時間",
            value=f"{deadline.astimezone(TAIWAN_TIMEZONE):%Y-%m-%d %H:%M}（台灣時間）",
            inline=False,
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
            await delete_order_event(self.database, event_id)
        except psycopg.Error:
            logger.exception("Unable to delete incomplete order event %s", event_id)
        if webhook is None:
            return
        if panel_message is not None:
            try:
                await webhook.delete_message(panel_message.id)
            except discord.HTTPException:
                logger.exception("Unable to delete incomplete order panel %s", event_id)
        try:
            await webhook.delete(reason=f"Order event {event_id} setup failed")
        except discord.HTTPException:
            logger.exception("Unable to delete incomplete order webhook %s", event_id)

    async def publish_announcement(self, event: dict[str, Any]) -> None:
        event_id = int(event["id"])
        participants = [
            row
            for row in await self.get_participants(event_id)
            if str(row["items"]).strip()
        ]
        pages = self.create_announcement_pages(event, participants)
        webhook = await self.get_event_webhook(event)
        channel = await self.get_channel(int(event["channel_id"]))
        thread = channel if isinstance(channel, discord.Thread) else None

        message_id = event["panel_message_id"]
        if message_id is None:
            raise RuntimeError(f"Order event {event_id} has no original panel message")
        kwargs: dict[str, object] = {
            "content": pages[0].content,
            "embed": pages[0].embed,
            "allowed_mentions": discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=[
                    discord.Object(id=user_id)
                    for user_id in pages[0].allowed_user_ids
                ],
                replied_user=False,
            ),
            "view": (
                ResultPaginationEntryView("order", event_id)
                if len(pages) > 1
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
                "Order panel %s for event %s no longer exists; "
                "cannot publish the result on the original message",
                message_id,
                event_id,
            )
        else:
            announcement_published = True
            await set_order_announcement_message_ids(
                self.database,
                event_id,
                json.dumps([int(message_id)]),
            )
        await complete_order_event(self.database, event_id)
        if announcement_published:
            await notify_event_creator(
                self.bot,
                creator_id=int(event["creator_id"]),
                event_id=event_id,
                event_type="揪團",
                title=str(event["title"]),
                guild_id=int(event["guild_id"]),
                channel_id=int(event["channel_id"]),
                message_id=int(message_id),
            )
        try:
            await webhook.delete(reason=f"Order event {event_id} has ended")
        except discord.HTTPException:
            logger.exception("Unable to delete webhook for order event %s", event_id)

    @staticmethod
    def create_announcement_pages(
        event: dict[str, Any],
        participants: Sequence[dict[str, Any]],
    ) -> list[PaginationPage]:
        title = f"揪團結果｜{event['title']}"
        description = str(event["description"]).strip()
        if not description:
            description = "菜單圖片如附圖。" if event["attachment_url"] else "揪團登記已截止。"
        deadline = datetime.fromisoformat(event["deadline_at"]).astimezone(
            TAIWAN_TIMEZONE
        )
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color(0xE86060),
            timestamp=datetime.now(timezone.utc),
        )
        if event["attachment_url"]:
            embed.set_image(url=str(event["attachment_url"]))

        if not participants:
            embed.description = f"{description}\n\n截止時沒有人登記品項。"
            embed.set_footer(
                text=format_event_footer(deadline, closed=True)
            )
            return [
                PaginationPage(
                    embed=embed,
                    content=(
                        f"<@{int(event['creator_id'])}> "
                        f"揪團「{event['title']}」已截止，結果如下："
                    ),
                    allowed_user_ids=(int(event["creator_id"]),),
                )
            ]

        base_description = f"{description}\n\n"
        max_details_length = 4096 - len(base_description)
        detail_blocks: list[tuple[str, int]] = []
        for participant in participants:
            user_id = int(participant["user_id"])
            items = [
                line.strip()
                for line in str(participant["items"]).splitlines()
                if line.strip()
            ]
            escaped_items = [
                discord.utils.escape_markdown(item, as_needed=False)
                for item in items
            ]
            for start in range(0, len(escaped_items), 14):
                block_lines = [
                    f"<@{user_id}>",
                    *(f"└ {item}" for item in escaped_items[start : start + 14]),
                ]
                detail_blocks.append(("\n".join(block_lines), len(block_lines)))

        detail_pages: list[str] = []
        current_blocks: list[str] = []
        current_length = 0
        current_line_count = 0
        for block, block_line_count in detail_blocks:
            separator_length = 2 if current_blocks else 0
            if current_blocks and (
                current_length + separator_length + len(block) > max_details_length
                or current_line_count + block_line_count > 15
            ):
                detail_pages.append("\n\n".join(current_blocks))
                current_blocks = []
                current_length = 0
                current_line_count = 0
                separator_length = 0
            current_blocks.append(block)
            current_length += separator_length + len(block)
            current_line_count += block_line_count
        if current_blocks:
            detail_pages.append("\n\n".join(current_blocks))

        result_pages: list[PaginationPage] = []
        for index, details in enumerate(detail_pages):
            page_embed = discord.Embed.from_dict(embed.to_dict())
            page_embed.description = f"{base_description}{details}"
            page_embed.set_footer(
                text=format_event_footer(
                    deadline,
                    closed=True,
                    page=(index + 1, len(detail_pages))
                    if len(detail_pages) > 1
                    else None,
                )
            )
            result_pages.append(
                PaginationPage(
                    embed=page_embed,
                    content=(
                        f"<@{int(event['creator_id'])}> "
                        f"揪團「{event['title']}」已截止，結果如下："
                        if index == 0
                        else ""
                    ),
                    allowed_user_ids=(
                        (int(event["creator_id"]),) if index == 0 else ()
                    ),
                )
            )
        return result_pages

    async def create_announcement_embeds_for_event(
        self,
        event_id: int,
        guild_id: int | None,
    ) -> list[PaginationPage]:
        event = await get_order_event(self.database, event_id)
        if (
            event is None
            or event["status"] != "closed"
            or guild_id != int(event["guild_id"])
        ):
            return []
        participants = [
            row
            for row in await self.get_participants(event_id)
            if str(row["items"]).strip()
        ]
        return self.create_announcement_pages(event, participants)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Order(bot))
