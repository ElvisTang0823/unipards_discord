from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from xml.etree import ElementTree
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands, tasks


RSS_URL = "https://community.firstinspires.org/topic/frc/rss.xml"
TARGET_CHANNEL_ID = 1555919971002089595
TARGET_ROLE_ID = 1555933526665334977
POLL_INTERVAL_SECONDS = 60
MAX_FEED_BYTES = 2 * 1024 * 1024
FOOTER_PREFIX = "FIRST RSS ID: "
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedEntry:
    guid: str
    title: str
    link: str
    image_url: str


class FirstImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image_url: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "img" or self.image_url is not None:
            return

        attributes = dict(attrs)
        source = attributes.get("src")
        if not source:
            return

        parsed_url = urlparse(source)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            return
        if attributes.get("width") == "1" or attributes.get("height") == "1":
            return
        if parsed_url.netloc.casefold() == "track.hubspot.com":
            return
        self.image_url = source


def extract_image_url(*html_content: str) -> str | None:
    for content in html_content:
        parser = FirstImageParser()
        parser.feed(html.unescape(content))
        if parser.image_url is not None:
            return parser.image_url
    return None


def parse_feed(payload: bytes) -> list[FeedEntry]:
    root = ElementTree.fromstring(payload)
    item_nodes = root.findall("./channel/item")
    if not item_nodes:
        raise ValueError("RSS feed contains no items")

    entries: list[FeedEntry] = []
    for item in item_nodes[:3]:
        guid = (item.findtext("guid") or "").strip()
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = item.findtext("description") or ""
        encoded_content = item.findtext(
            "{http://purl.org/rss/1.0/modules/content/}encoded"
        ) or ""
        image_url = extract_image_url(description, encoded_content)
        parsed_link = urlparse(link)

        if (
            not guid
            or len(guid) > 2048 - len(FOOTER_PREFIX)
            or not title
            or not link
            or parsed_link.scheme != "https"
            or not parsed_link.netloc
            or image_url is None
        ):
            logger.warning(
                "Skipping an RSS item missing a valid GUID, title, link, or image"
            )
            continue

        entries.append(
            FeedEntry(
                guid=guid,
                title=title[:256],
                link=link,
                image_url=image_url,
            )
        )
    return entries


class FirstRss(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.published_guids: set[str] = set()
        self._channel_history_loaded = False

    async def cog_load(self) -> None:
        self.check_feed.start()

    def cog_unload(self) -> None:
        self.check_feed.cancel()

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def check_feed(self) -> None:
        try:
            entries = await self.fetch_latest_entries()
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ElementTree.ParseError,
            ValueError,
        ):
            logger.exception("Unable to retrieve or parse the FIRST RSS feed")
            return

        channel = self.bot.get_channel(TARGET_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(TARGET_CHANNEL_ID)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to fetch FIRST RSS target channel %s",
                    TARGET_CHANNEL_ID,
                )
                return
        if not isinstance(channel, discord.TextChannel):
            logger.error(
                "FIRST RSS target %s is not a text channel",
                TARGET_CHANNEL_ID,
            )
            return

        history_limit = None if not self._channel_history_loaded else 100
        try:
            async for message in channel.history(limit=history_limit):
                for embed in message.embeds:
                    footer_text = embed.footer.text or ""
                    if footer_text.startswith(FOOTER_PREFIX):
                        self.published_guids.add(
                            footer_text[len(FOOTER_PREFIX):]
                        )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Unable to read FIRST RSS channel history; "
                "skipping this polling cycle"
            )
            return
        self._channel_history_loaded = True

        if channel.guild.get_role(TARGET_ROLE_ID) is None:
            logger.error(
                "FIRST RSS notification role %s was not found in guild %s",
                TARGET_ROLE_ID,
                channel.guild.id,
            )
            return

        pending_entries = [
            entry
            for entry in entries
            if entry.guid not in self.published_guids
        ]
        role = discord.Object(id=TARGET_ROLE_ID)
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[role],
            replied_user=False,
        )

        for entry in reversed(pending_entries):
            embed = discord.Embed(
                title=entry.title,
                url=entry.link,
                color=discord.Color(0xED1C24),
            )
            embed.set_image(url=entry.image_url)
            embed.set_footer(text=f"{FOOTER_PREFIX}{entry.guid}")
            try:
                await channel.send(
                    content=(
                        f":loudspeaker: FRC部落格更新囉！來看看最新的文章~ "
                        f"<@&{TARGET_ROLE_ID}>"
                    ),
                    embed=embed,
                    allowed_mentions=allowed_mentions,
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to send FIRST RSS item with GUID %s",
                    entry.guid,
                )
                break

            self.published_guids.add(entry.guid)
            logger.info("Published FIRST RSS item with GUID %s", entry.guid)

    @check_feed.before_loop
    async def before_check_feed(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    async def fetch_latest_entries() -> list[FeedEntry]:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                RSS_URL,
                headers={"User-Agent": "Unipards Discord RSS monitor"},
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total_bytes = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_FEED_BYTES:
                        raise ValueError("RSS feed exceeds the 2 MiB size limit")
                    chunks.append(chunk)
                payload = b"".join(chunks)
        return parse_feed(payload)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FirstRss(bot))
