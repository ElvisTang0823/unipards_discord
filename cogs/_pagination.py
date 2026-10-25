"""Shared pagination UI and event-notification helpers."""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, cast

import discord
from discord.ext import commands


logger = logging.getLogger(__name__)
PageSource = Literal["info", "signup", "order"]
PageAction = Literal["open", "previous", "next"]
TAIWAN_TIMEZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class PaginationPage:
    embed: discord.Embed
    content: str | None = None
    allowed_user_ids: tuple[int, ...] = ()


def format_event_footer(
    deadline: datetime,
    *,
    closed: bool,
    page: tuple[int, int] | None = None,
) -> str:
    footer = (
        f"截止時間：{deadline.astimezone(TAIWAN_TIMEZONE):%Y-%m-%d %H:%M}"
        f"（台灣時間）｜狀態：{'已截止' if closed else '進行中'}"
    )
    if page is not None:
        footer += f"｜第 {page[0]}/{page[1]} 頁"
    return footer


async def notify_event_creator(
    bot: commands.Bot,
    *,
    creator_id: int,
    event_id: int,
    event_type: str,
    title: str,
    guild_id: int,
    channel_id: int,
    message_id: int,
) -> bool:
    creator = bot.get_user(creator_id)
    if creator is None:
        try:
            creator = await bot.fetch_user(creator_id)
        except discord.HTTPException:
            logger.warning(
                "Unable to fetch creator for %s event %s DM notification",
                event_type,
                event_id,
                exc_info=True,
            )
            return False

    jump_url = (
        f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    )
    try:
        await creator.send(
            f"{event_type}「{title}」已截止，結果如下：\n{jump_url}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        logger.warning(
            "Unable to DM creator for %s event %s",
            event_type,
            event_id,
            exc_info=True,
        )
        return False
    return True


class PersistentPageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"private-pages:(?P<source>info|signup|order):(?P<item_id>\d+):(?P<page>\d+):(?P<action>open|previous|next)",
):
    def __init__(
        self,
        source: PageSource,
        item_id: int,
        page: int,
        action: PageAction,
        *,
        disabled: bool = False,
    ) -> None:
        label = {
            "open": "查看完整名單",
            "previous": "上一頁",
            "next": "下一頁",
        }[action]
        super().__init__(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                custom_id=f"private-pages:{source}:{item_id}:{page}:{action}",
                disabled=disabled,
            )
        )
        self.source = source
        self.item_id = item_id
        self.page = page
        self.action = action

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> PersistentPageButton:
        del interaction
        return cls(
            cast(PageSource, match["source"]),
            int(match["item_id"]),
            int(match["page"]),
            cast(PageAction, match["action"]),
            disabled=item.disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            pages = await self.get_pages(interaction)
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            logger.exception(
                "Unable to load %s pagination item %s",
                self.source,
                self.item_id,
            )
            await interaction.response.send_message(
                "目前無法載入分頁內容，請稍後再試。",
                ephemeral=True,
            )
            return

        if not pages:
            await interaction.response.send_message(
                "找不到這份分頁內容，請重新開啟指令或公告。",
                ephemeral=True,
            )
            return

        page = min(max(0, self.page), len(pages) - 1)
        if self.action == "previous":
            page = max(0, page - 1)
        elif self.action == "next":
            page = min(len(pages) - 1, page + 1)

        view = PrivateEmbedPaginator(
            pages,
            source=self.source,
            item_id=self.item_id,
            page=page,
        )
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=[
                discord.Object(id=user_id)
                for user_id in pages[page].allowed_user_ids
            ],
            replied_user=False,
        )
        try:
            if self.action == "open":
                await interaction.response.send_message(
                    content=pages[page].content,
                    embed=pages[page].embed,
                    view=view,
                    ephemeral=True,
                    allowed_mentions=allowed_mentions,
                )
            else:
                await interaction.response.edit_message(
                    content=pages[page].content,
                    embed=pages[page].embed,
                    view=view,
                    allowed_mentions=allowed_mentions,
                )
        except discord.HTTPException:
            logger.exception(
                "Unable to send %s pagination page %s for item %s",
                self.source,
                page,
                self.item_id,
            )

    async def get_pages(
        self,
        interaction: discord.Interaction,
    ) -> list[PaginationPage]:
        bot = interaction.client
        if not isinstance(bot, commands.Bot):
            return []

        if self.source == "info":
            from cogs.info import Info

            cog = bot.get_cog("Info")
            return (
                [
                    PaginationPage(embed=embed)
                    for embed in cog.create_command_embeds()
                ]
                if isinstance(cog, Info)
                else []
            )

        if self.source == "signup":
            from cogs.signup import Signup

            cog = bot.get_cog("Signup")
            return (
                [
                    PaginationPage(embed=embed)
                    for embed in cog.create_result_embeds_for_event(
                        self.item_id,
                        interaction.guild_id,
                    )
                ]
                if isinstance(cog, Signup)
                else []
            )

        from cogs.order import Order

        cog = bot.get_cog("Order")
        return (
            cog.create_announcement_embeds_for_event(
                self.item_id,
                interaction.guild_id,
            )
            if isinstance(cog, Order)
            else []
        )


class PrivateEmbedPaginator(discord.ui.View):
    def __init__(
        self,
        pages: list[PaginationPage],
        *,
        source: PageSource,
        item_id: int,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            PersistentPageButton(
                source,
                item_id,
                page,
                "previous",
                disabled=page <= 0,
            )
        )
        self.add_item(
            PersistentPageButton(
                source,
                item_id,
                page,
                "next",
                disabled=page >= len(pages) - 1,
            )
        )


class ResultPaginationEntryView(discord.ui.View):
    def __init__(self, source: Literal["signup", "order"], item_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(PersistentPageButton(source, item_id, 0, "open"))
