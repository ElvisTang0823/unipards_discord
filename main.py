import asyncio
import logging
import os
import re
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
        self._guild_command_cleanup_complete = False
        self._cog_test_mode = False
        self._registered_global_commands: set[str] = set()

    async def setup_hook(self) -> None:
        self.database = initialize_database(DATABASE_PATH)
        logger.info("SQLite database initialized")

        available_cogs = {
            cog_file.stem
            for cog_file in COGS_DIR.glob("*.py")
            if cog_file.name != "__init__.py"
            and not cog_file.name.startswith("_")
        }
        configured_cogs = os.getenv("BOT_COGS")
        if configured_cogs is None:
            cogs_to_load = sorted(available_cogs)
        else:
            self._cog_test_mode = True
            requested_cogs = configured_cogs.strip()
            cogs_to_load = (
                ["vote"]
                if not requested_cogs
                else list(
                    dict.fromkeys(
                        cog_name
                        for cog_name in re.split(r"[\s,]+", requested_cogs)
                        if cog_name
                    )
                )
            )
            unknown_cogs = sorted(set(cogs_to_load) - available_cogs)
            if unknown_cogs:
                raise ValueError(
                    "BOT_COGS contains unknown cog(s): "
                    + ", ".join(unknown_cogs)
                )

        for cog_name in cogs_to_load:
            await self.load_extension(f"cogs.{cog_name}")
            logger.info("Loaded cog: %s", cog_name)

        self._registered_global_commands = {
            command.name
            for command in self.tree.get_commands()
        }
        synced_commands = await self.tree.sync()
        if self._cog_test_mode:
            logger.info(
                "Cog isolation enabled; loaded %s and synchronized %s global "
                "application command(s)",
                ", ".join(cogs_to_load),
                len(synced_commands),
            )
        else:
            logger.info(
                "Synchronized %s global application command(s): %s",
                len(synced_commands),
                ", ".join(command.name for command in synced_commands),
            )

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.playing,
                    name="FRC6998團隊的24小時助理機器人",
                )
            )
        except discord.HTTPException:
            logger.exception("Unable to update bot activity presence")

        if self._guild_command_cleanup_complete:
            return

        cleanup_complete = True
        for guild in self.guilds:
            try:
                guild_commands = await self.tree.fetch_commands(guild=guild)
                for command in guild_commands:
                    if (
                        command.name in self._registered_global_commands
                        and command.type is discord.AppCommandType.chat_input
                    ):
                        await command.delete()
                        logger.info(
                            "Removed guild-scoped /%s command that conflicts "
                            "with its global definition in guild %s",
                            command.name,
                            guild.id,
                        )
            except discord.HTTPException:
                cleanup_complete = False
                logger.exception(
                    "Unable to check legacy guild commands in guild %s",
                    guild.id,
                )

        self._guild_command_cleanup_complete = cleanup_complete

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            if self.database is not None:
                self.database.close()
                self.database = None
                logger.info("SQLite database connection closed")


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
