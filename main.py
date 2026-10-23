import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from database import initialize_database


BASE_DIR = Path(__file__).resolve().parent
COGS_DIR = BASE_DIR / "cogs"
DATABASE_PATH = BASE_DIR / "database" / "bot.sqlite3"
LOG_DIR = BASE_DIR / "logs"

load_dotenv(BASE_DIR / ".env")

logger = logging.getLogger(__name__)


def configure_logging() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_path = LOG_DIR / f"bot_{timestamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, mode="x", encoding="utf-8"),
        ],
    )
    return log_path


class DiscordBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_reactions = True
        super().__init__(command_prefix="!", intents=intents)
        self.database: sqlite3.Connection | None = None
        self._legacy_guild_info_cleanup_complete = False

    async def setup_hook(self) -> None:
        self.database = initialize_database(DATABASE_PATH)
        logger.info("SQLite database initialized")

        for cog_file in sorted(COGS_DIR.glob("*.py")):
            if cog_file.name != "__init__.py" and not cog_file.name.startswith("_"):
                await self.load_extension(f"cogs.{cog_file.stem}")
                logger.info("Loaded cog: %s", cog_file.stem)

        synced_commands = await self.tree.sync()
        logger.info(
            "Synced %s application command(s): %s",
            len(synced_commands),
            ", ".join(command.name for command in synced_commands),
        )

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        if self._legacy_guild_info_cleanup_complete:
            return

        cleanup_complete = True
        for guild in self.guilds:
            try:
                guild_commands = await self.tree.fetch_commands(guild=guild)
                for command in guild_commands:
                    if (
                        command.name == "info"
                        and command.type is discord.AppCommandType.chat_input
                    ):
                        await command.delete()
                        logger.info(
                            "Removed stale guild-scoped /info command from guild %s",
                            guild.id,
                        )
            except discord.HTTPException:
                cleanup_complete = False
                logger.exception(
                    "Unable to check legacy guild commands in guild %s",
                    guild.id,
                )

        self._legacy_guild_info_cleanup_complete = cleanup_complete

    async def close(self) -> None:
        try:
            if self.database is not None:
                self.database.close()
                self.database = None
                logger.info("SQLite database connection closed")
        finally:
            await super().close()


async def main() -> None:
    configure_logging()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("DISCORD_TOKEN is missing from the .env file")
        raise RuntimeError(
            "Missing DISCORD_TOKEN. Add your bot token to the .env file."
        )

    logger.info("Starting Discord bot")
    async with DiscordBot() as bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
