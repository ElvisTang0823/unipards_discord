import json
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from cogs.pagination import (
    PaginationPage,
    PersistentPageButton,
    PrivateEmbedPaginator,
)

COMMANDS_PATH = Path(__file__).resolve().parent.parent / "commands.json"
MAX_EMBED_DESCRIPTION_LENGTH = 700
logger = logging.getLogger(__name__)


def load_commands() -> list[dict[str, str]]:
    data = json.loads(COMMANDS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("commands"), list):
        raise ValueError("commands.json must contain a commands array")

    commands_data: list[dict[str, str]] = []
    for entry in data["commands"]:
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(key), str) or not entry[key].strip()
            for key in ("name", "usage", "description")
        ):
            raise ValueError(
                "Each command must have a name, usage, and description"
            )
        commands_data.append(
            {
                "name": entry["name"],
                "usage": entry["usage"],
                "description": entry["description"],
            }
        )

    if not commands_data:
        raise ValueError("commands.json must list at least one command")
    return commands_data


class Info(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(PersistentPageButton)

    def create_command_embeds(
        self,
        available_commands: list[dict[str, str]] | None = None,
    ) -> list[discord.Embed]:
        if available_commands is None:
            available_commands = load_commands()
        entries = [
            f"**{command['name']}** (`{command['usage']}`)\n"
            f"{command['description']}"
            for command in available_commands
        ]
        pages: list[list[str]] = []
        current_entries: list[str] = []
        current_length = 0
        for entry in entries:
            separator_length = 2 if current_entries else 0
            if (
                current_entries
                and current_length + separator_length + len(entry)
                > MAX_EMBED_DESCRIPTION_LENGTH
            ):
                pages.append(current_entries)
                current_entries = []
                current_length = 0
                separator_length = 0
            current_entries.append(entry)
            current_length += separator_length + len(entry)
        if current_entries:
            pages.append(current_entries)

        return [
            discord.Embed(
                title="可用指令",
                description="\n\n".join(page_entries),
                color=discord.Color.blurple(),
            ).set_footer(text=f"第 {index}/{len(pages)} 頁")
            for index, page_entries in enumerate(pages, start=1)
        ]

    @app_commands.command(name="info", description="查看可用指令或查詢單一指令用法")
    @app_commands.guild_only()
    @app_commands.describe(command="要查詢的指令，例如 /create-vote 或 !allow")
    async def info(
        self,
        interaction: discord.Interaction,
        command: str | None = None,
    ) -> None:
        try:
            available_commands = load_commands()
        except (OSError, ValueError):
            logger.exception("Failed to load command list from %s", COMMANDS_PATH)
            await interaction.response.send_message(
                "目前無法讀取指令清單，請稍後再試。",
                ephemeral=True,
            )
            return

        if command is not None:
            requested_name = command.strip().casefold()
            if requested_name.startswith(("/", "!")):
                requested_name = requested_name[1:]
            selected_command = next(
                (
                    entry
                    for entry in available_commands
                    if entry["name"].casefold().lstrip("/!") == requested_name
                    or entry["usage"].split(maxsplit=1)[0]
                    .casefold()
                    .lstrip("/!")
                    == requested_name
                ),
                None,
            )
            if selected_command is None:
                await interaction.response.send_message(
                    "找不到指定指令，請使用 `/info` 查看可用指令。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            embed = discord.Embed(
                title=f"{selected_command['name']} 使用方法",
                description=(
                    f"**用法**\n`{selected_command['usage']}`\n\n"
                    f"{selected_command['description']}"
                ),
                color=discord.Color.blurple(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        pages = self.create_command_embeds(available_commands)
        kwargs: dict[str, object] = {"embed": pages[0], "ephemeral": True}
        if len(pages) > 1:
            kwargs["view"] = PrivateEmbedPaginator(
                [PaginationPage(embed=page) for page in pages],
                source="info",
                item_id=0,
            )
        await interaction.response.send_message(**kwargs)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Info(bot))
