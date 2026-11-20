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
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(
        guilds=True,
        dms=True,
        private_channels=True,
    )
    async def sys_home(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.launch_activity()
        except discord.HTTPException:
            logger.exception("Unable to launch the system-home Activity")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "無法啟動 Activity；請確認 Developer Portal 已啟用 "
                    "Activities 並設定 `/` URL Mapping，然後稍後再試。",
                    ephemeral=True,
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SysHome(bot))
