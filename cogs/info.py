import json
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands


COMMANDS_PATH = Path(__file__).resolve().parent.parent / "commands.json"
INFO_CHANNEL_ID = 1556259986278195280
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

    @app_commands.command(name="info", description="查看機器人目前可用的指令")
    @app_commands.guild_only()
    async def info(self, interaction: discord.Interaction) -> None:
        if interaction.channel_id != INFO_CHANNEL_ID:
            await interaction.response.send_message(
                f"請在 <#{INFO_CHANNEL_ID}> 使用此指令。",
                ephemeral=True,
            )
            return

        try:
            available_commands = load_commands()
        except (OSError, ValueError):
            logger.exception("Failed to load command list from %s", COMMANDS_PATH)
            await interaction.response.send_message(
                "目前無法讀取指令清單，請稍後再試。",
                ephemeral=True,
            )
            return

        description = "\n\n".join(
            f"**{command['name']}** (`{command['usage']}`)\n"
            f"{command['description']}"
            for command in available_commands
        )
        embed = discord.Embed(
            title="可用指令",
            description=description,
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Info(bot))
