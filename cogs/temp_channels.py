import logging
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from cogs.channel_permissions import send_permission_guide
from database import load_channel_owners, save_channel_owners


ENTRY_CHANNEL_ID = 1556259986278195280
logger = logging.getLogger(__name__)
CHANNEL_TYPES = {
    "group": {
        "label": "私聊群",
        "suffix": "群組",
        "category_id": 1555925845078704228,
    },
    "discussion": {
        "label": "活動討論群",
        "suffix": "討論",
        "category_id": 1555925059258810441,
    },
}


class TemporaryGroups(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.managed_channels: dict[int, int] = {}
        self.ownership_ready = False
        self.load_ownership()

    def load_ownership(self) -> None:
        try:
            if self.bot.database is None:
                raise RuntimeError("SQLite 尚未初始化")
            self.managed_channels = load_channel_owners(
                self.bot.database,
                "generated",
            )
        except (sqlite3.Error, RuntimeError, TypeError, ValueError):
            logger.exception("Unable to load generated channel ownership")
            self.ownership_ready = False
            return
        self.ownership_ready = True

    def save_ownership(self) -> bool:
        try:
            if self.bot.database is None:
                raise RuntimeError("SQLite 尚未初始化")
            save_channel_owners(
                self.bot.database,
                "generated",
                self.managed_channels,
            )
        except (sqlite3.Error, RuntimeError, TypeError, ValueError):
            logger.exception("Unable to save generated channel ownership")
            return False
        return True

    @app_commands.command(
        name="create-channel",
        description="建立臨時私聊群或活動討論群",
    )
    @app_commands.choices(
        channel_type=[
            app_commands.Choice(name="私聊群", value="group"),
            app_commands.Choice(name="活動討論群", value="discussion"),
        ]
    )
    @app_commands.describe(
        channel_type="選擇要建立的頻道類型",
        channel_name="頻道名稱；留空時將使用你的帳號名稱",
    )
    async def create_channel(
        self,
        interaction: discord.Interaction,
        channel_type: app_commands.Choice[str],
        channel_name: str | None = None,
    ):
        if interaction.guild is None or interaction.channel_id != ENTRY_CHANNEL_ID:
            await interaction.response.send_message(
                f"請在 <#{ENTRY_CHANNEL_ID}> 使用此指令。",
                ephemeral=True,
            )
            return

        if not self.ownership_ready:
            await interaction.response.send_message(
                "頻道建立者資料無法讀取或遷移，請通知管理員。",
                ephemeral=True,
            )
            return

        channel_config = CHANNEL_TYPES[channel_type.value]
        category = interaction.guild.get_channel(channel_config["category_id"])
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                f"找不到設定的{channel_config['label']}分類，請通知管理員。",
                ephemeral=True,
            )
            return

        name = (
            channel_name
            or f"{interaction.user.display_name}-{channel_config['suffix']}"
        ).strip()
        if not name or len(name) > 100:
            await interaction.response.send_message(
                "頻道名稱需為 1 到 100 個字元。",
                ephemeral=True,
            )
            return

        overwrites = category.overwrites
        owner_overwrite = overwrites.get(
            interaction.user,
            discord.PermissionOverwrite(),
        )
        owner_overwrite.view_channel = True
        owner_overwrite.send_messages = True
        owner_overwrite.read_message_history = True
        owner_overwrite.manage_channels = True
        overwrites[interaction.user] = owner_overwrite

        bot_member = interaction.guild.me
        if bot_member is not None:
            bot_overwrite = overwrites.get(
                bot_member,
                discord.PermissionOverwrite(),
            )
            bot_overwrite.view_channel = True
            bot_overwrite.send_messages = True
            bot_overwrite.read_message_history = True
            bot_overwrite.manage_channels = True
            overwrites[bot_member] = bot_overwrite

        await interaction.response.defer(ephemeral=True)
        try:
            channel = await interaction.guild.create_text_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                reason=f"{interaction.user} 建立{channel_config['label']}",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Unable to create %s", channel_config["label"])
            await interaction.followup.send(
                f"建立{channel_config['label']}失敗，請確認機器人具有「管理頻道」權限。",
                ephemeral=True,
            )
            return

        self.managed_channels[channel.id] = interaction.user.id
        if not self.save_ownership():
            try:
                await channel.delete(reason="無法保存臨時頻道建立者資料")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to delete untracked generated channel %s",
                    channel.id,
                )
            else:
                self.managed_channels.pop(channel.id, None)
            await interaction.followup.send(
                "無法保存建立者資料，因此未能保留此頻道。",
                ephemeral=True,
            )
            return

        await send_permission_guide(channel)
        await interaction.followup.send(
            f"已建立{channel_config['label']}：{channel.mention}",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        owner_id = self.managed_channels.pop(channel.id, None)
        if owner_id is not None and not self.save_ownership():
            self.managed_channels[channel.id] = owner_id


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TemporaryGroups(bot))
