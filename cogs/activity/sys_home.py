import logging

import discord
from discord import app_commands
from discord.ext import commands


logger = logging.getLogger(__name__)


class SysHome(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="sys-home",
        description="在 Discord Activity 開啟雲豹的窩網站",
    )
    @app_commands.guild_only()
    async def sys_home(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.launch_activity()
        except discord.HTTPException:
            logger.exception("Unable to launch the system-home Activity")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "目前無法啟動系統首頁 Activity，請稍後再試或聯絡管理員。",
                    ephemeral=True,
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SysHome(bot))
