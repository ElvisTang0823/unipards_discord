import asyncio
import logging
import math
import os
import re
from datetime import datetime
from pathlib import Path

import discord
import psycopg
from pgembed import PostgresServer
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import (
    Database,
    DatabaseBusyError,
    close_embedded_postgres,
    cleanup_command_rate_limits,
    consume_command_rate_limit,
    initialize_embedded_database,
)


BASE_DIR = Path(__file__).resolve().parent
COGS_DIR = BASE_DIR / "cogs"
LOG_DIR = BASE_DIR / "logs"
CREATE_COMMANDS = {
    "create-channel",
    "create-vote",
    "create-signup",
    "create-order",
}

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


class SharedCommandRateLimit(commands.CommandError):
    def __init__(self, retry_after: float, is_create: bool) -> None:
        super().__init__("Shared command rate limit exceeded")
        self.retry_after = retry_after
        self.is_create = is_create


class RateLimitUnavailable(commands.CommandError):
    pass


class RateLimitedCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command = interaction.command
        if command is None or interaction.user.bot:
            return True

        bot = self.client
        if not isinstance(bot, DiscordBot) or bot.database is None:
            logger.error("Cannot rate-limit interaction without database")
            await interaction.response.send_message(
                "目前無法處理指令，請稍後再試。",
                ephemeral=True,
            )
            return False

        is_create = command.name in CREATE_COMMANDS
        bucket = "create" if is_create else "commands"
        try:
            allowed, retry_after = await consume_command_rate_limit(
                bot.database,
                interaction.user.id,
                bucket,
            )
        except (DatabaseBusyError, psycopg.Error):
            logger.exception("Unable to check application-command rate limit")
            await interaction.response.send_message(
                "系統目前忙碌，暫時無法驗證指令頻率，請稍後再試。",
                ephemeral=True,
            )
            return False
        if allowed:
            return True

        if is_create:
            wait_minutes = max(1, math.ceil(retry_after / 60))
            message = (
                f"建立類指令共用每小時一次的冷卻，請約 {wait_minutes} 分鐘後再試。"
            )
        else:
            wait_seconds = max(1, math.ceil(retry_after))
            message = f"指令使用過於頻繁，請 {wait_seconds} 秒後再試。"
        await interaction.response.send_message(message, ephemeral=True)
        return False


class DiscordBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_reactions = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            tree_cls=RateLimitedCommandTree,
        )
        self.database: Database | None = None
        self.postgres_server: PostgresServer | None = None
        self._guild_command_cleanup_complete = False
        self._cog_test_mode = False
        self._registered_global_commands: set[str] = set()
        self.add_check(self._rate_limit_prefix_command)

    async def setup_hook(self) -> None:
        self.database, self.postgres_server = await initialize_embedded_database(
            BASE_DIR / "data" / "postgres"
        )
        await cleanup_command_rate_limits(self.database)
        self.cleanup_rate_limits.start()
        logger.info("Project-local PostgreSQL database initialized")

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

    @tasks.loop(hours=24)
    async def cleanup_rate_limits(self) -> None:
        if self.database is None:
            return
        try:
            await cleanup_command_rate_limits(self.database)
        except (DatabaseBusyError, psycopg.Error):
            logger.exception("Unable to clean expired command rate limits")

    @cleanup_rate_limits.before_loop
    async def before_cleanup_rate_limits(self) -> None:
        await self.wait_until_ready()

    async def _rate_limit_prefix_command(self, ctx: commands.Context) -> bool:
        if ctx.author.bot:
            return True
        if self.database is None:
            raise RuntimeError("PostgreSQL database is not initialized")

        try:
            allowed, retry_after = await consume_command_rate_limit(
                self.database,
                ctx.author.id,
                "commands",
            )
        except (DatabaseBusyError, psycopg.Error) as error:
            logger.exception("Unable to check prefix-command rate limit")
            raise RateLimitUnavailable(
                "Unable to verify the command rate limit"
            ) from error
        if not allowed:
            raise SharedCommandRateLimit(retry_after, is_create=False)
        return True

    async def on_command_error(
        self,
        ctx: commands.Context,
        error: commands.CommandError,
    ) -> None:
        if isinstance(error, SharedCommandRateLimit):
            wait_seconds = max(1, math.ceil(error.retry_after))
            try:
                await ctx.send(
                    f"指令使用過於頻繁，請 {wait_seconds} 秒後再試。"
                )
            except discord.HTTPException:
                logger.exception("Unable to send prefix-command rate-limit notice")
            return
        if isinstance(error, RateLimitUnavailable):
            try:
                await ctx.send(
                    "系統目前忙碌，暫時無法驗證指令頻率，請稍後再試。"
                )
            except discord.HTTPException:
                logger.exception("Unable to send rate-limit storage failure notice")
            return
        if isinstance(error, commands.CommandNotFound):
            return
        await super().on_command_error(ctx, error)

    async def close(self) -> None:
        self.cleanup_rate_limits.cancel()
        try:
            await super().close()
        finally:
            try:
                if self.database is not None:
                    database = self.database
                    self.database = None
                    await database.close()
                    logger.info("PostgreSQL database pool closed")
            finally:
                if self.postgres_server is not None:
                    server = self.postgres_server
                    self.postgres_server = None
                    await close_embedded_postgres(server)
                    logger.info("Project-local PostgreSQL server stopped")


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
