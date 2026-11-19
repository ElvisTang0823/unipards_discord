from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp
import discord
import psycopg
from discord.ext import commands, tasks

from cogs._config import required_snowflake, required_snowflake_list
from database import get_team_update_links, save_team_update_items

TEAM_UPDATE_URL = (
    "https://www.firstinspires.org/resources/library/frc/season-materials"
)
TARGET_CHANNEL_ID = required_snowflake("NOTIFY_CHANNEL_ID")
NOTIFY_ROLE_IDS = required_snowflake_list("NOTIFY_ROLE_ID")
if not NOTIFY_ROLE_IDS:
    raise RuntimeError("NOTIFY_ROLE_ID must contain at least one Discord role ID")
TARGET_ROLE_ID = NOTIFY_ROLE_IDS[0]
POLL_INTERVAL_SECONDS = 60
MAX_PAGE_BYTES = 2 * 1024 * 1024
TEAM_UPDATE_LABEL = re.compile(r"Team\s+Update\s+(\d+)", re.IGNORECASE)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TeamUpdate:
    number: int
    link: str


class TeamUpdateLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: dict[str, TeamUpdate] = {}
        self._anchor_href: str | None = None
        self._anchor_text: list[str] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "a" or self._anchor_text is not None:
            return

        href = dict(attrs).get("href")
        if href:
            self._anchor_href = href
            self._anchor_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor_text is not None:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._anchor_text is None:
            return

        label = " ".join(" ".join(self._anchor_text).split())
        match = TEAM_UPDATE_LABEL.fullmatch(label)
        if match is not None and self._anchor_href is not None:
            link = urljoin(TEAM_UPDATE_URL, self._anchor_href)
            parsed_link = urlsplit(link)
            if parsed_link.scheme == "https" and parsed_link.netloc:
                self.entries.setdefault(
                    link,
                    TeamUpdate(number=int(match.group(1)), link=link),
                )

        self._anchor_href = None
        self._anchor_text = None


def parse_team_updates(page: str) -> list[TeamUpdate]:
    parser = TeamUpdateLinkParser()
    parser.feed(page)
    parser.close()
    return sorted(
        parser.entries.values(),
        key=lambda entry: (entry.number, entry.link),
    )


def select_pending_team_updates(
    entries: list[TeamUpdate],
    known_links: set[str],
) -> list[TeamUpdate]:
    if not entries:
        return []
    if not known_links:
        return [max(entries, key=lambda entry: entry.number)]
    return [
        entry
        for entry in entries
        if entry.link not in known_links
    ]


class TeamUpdateNotifier(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.check_team_updates.start()

    def cog_unload(self) -> None:
        self.check_team_updates.cancel()

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def check_team_updates(self) -> None:
        try:
            entries = await self.fetch_team_updates()
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
        ):
            logger.exception("Unable to retrieve or parse FRC Team Updates")
            return

        if not entries:
            logger.warning("No FRC Team Update links found on the season page")
            return

        if self.bot.database is None:
            logger.error("Cannot check FRC Team Updates without PostgreSQL")
            return
        try:
            known_links = await get_team_update_links(self.bot.database)
        except (psycopg.Error, RuntimeError):
            logger.exception("Unable to load Team Update deduplication state")
            return

        channel = self.bot.get_channel(TARGET_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(TARGET_CHANNEL_ID)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to fetch Team Update target channel %s",
                    TARGET_CHANNEL_ID,
                )
                return
        if not isinstance(channel, discord.TextChannel):
            logger.error(
                "Team Update target %s is not a text channel",
                TARGET_CHANNEL_ID,
            )
            return

        if channel.guild.get_role(TARGET_ROLE_ID) is None:
            logger.error(
                "Team Update notification role %s was not found in guild %s",
                TARGET_ROLE_ID,
                channel.guild.id,
            )
            return

        pending_entries = select_pending_team_updates(entries, known_links)
        if not pending_entries:
            return

        role = discord.Object(id=TARGET_ROLE_ID)
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[role],
            replied_user=False,
        )
        first_run = not known_links

        for entry in pending_entries:
            embed = discord.Embed(
                title=f"Team Update {entry.number}",
                url=entry.link,
                color=discord.Color(0xDAB299),
            )
            embed.set_footer(text=entry.link)
            try:
                sent_message = await channel.send(
                    content=(
                        ":loudspeaker: FRC的Team Update更新囉！"
                        "來看看最新的規則更動！"
                        f"<@&{TARGET_ROLE_ID}>"
                    ),
                    embed=embed,
                    allowed_mentions=allowed_mentions,
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to send FRC Team Update %s",
                    entry.number,
                )
                return

            if first_run:
                baseline_items = [
                    (
                        item.link,
                        item.number,
                        sent_message.id if item.link == entry.link else None,
                    )
                    for item in entries
                ]
                first_run = False
            else:
                baseline_items = [(entry.link, entry.number, sent_message.id)]

            try:
                await save_team_update_items(
                    self.bot.database,
                    baseline_items,
                )
            except (psycopg.Error, RuntimeError):
                logger.exception(
                    "Unable to persist FRC Team Update %s",
                    entry.number,
                )
                return

            known_links.update(item[0] for item in baseline_items)
            logger.info(
                "Published FRC Team Update %s",
                entry.number,
            )

    @check_team_updates.before_loop
    async def before_check_team_updates(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    async def fetch_team_updates() -> list[TeamUpdate]:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                TEAM_UPDATE_URL,
                headers={"User-Agent": "Unipards Discord Team Update monitor"},
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total_bytes = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_PAGE_BYTES:
                        raise ValueError(
                            "FRC season materials page exceeds the 2 MiB limit"
                        )
                    chunks.append(chunk)

        page = b"".join(chunks).decode("utf-8", errors="replace")
        return parse_team_updates(page)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TeamUpdateNotifier(bot))
