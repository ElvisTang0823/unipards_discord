import asyncio
import logging
import psycopg

import discord
from discord.ext import commands

from cogs.channel_permissions import send_permission_guide
from database import Database, load_channel_owners, save_channel_owners


TRIGGER_CHANNEL_ID = 1556272392458735668
logger = logging.getLogger(__name__)


class TempVoice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.managed_channels: dict[int, int] = {}
        self.ownership_ready = False
        self.channel_lock = asyncio.Lock()
        self.ownership_save_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        try:
            if self.bot.database is None:
                raise RuntimeError("PostgreSQL 尚未初始化")
            self.managed_channels = await load_channel_owners(
                self.bot.database,
                "voice",
            )
        except (psycopg.Error, RuntimeError, TypeError, ValueError):
            logger.exception("Unable to load temporary voice channel ownership")
            self.ownership_ready = False
            return
        self.ownership_ready = True

    async def save_ownership(self) -> bool:
        async with self.ownership_save_lock:
            try:
                if self.bot.database is None:
                    raise RuntimeError("PostgreSQL 尚未初始化")
                await save_channel_owners(
                    self.bot.database,
                    "voice",
                    dict(self.managed_channels),
                )
            except (psycopg.Error, RuntimeError, TypeError, ValueError):
                logger.exception("Unable to save temporary voice channel ownership")
                return False
            return True

    def find_owner_channel(
        self, guild: discord.Guild, owner_id: int
    ) -> discord.VoiceChannel | None:
        for channel_id, channel_owner_id in self.managed_channels.items():
            if channel_owner_id != owner_id:
                continue
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.VoiceChannel):
                return channel
        return None

    async def ensure_bot_access(self, channel: discord.VoiceChannel):
        bot_member = channel.guild.me
        if bot_member is None:
            return

        overwrite = channel.overwrites_for(bot_member)
        overwrite.view_channel = True
        overwrite.connect = True
        overwrite.move_members = True
        overwrite.manage_channels = True
        overwrite.manage_permissions = None
        if (
            channel.overwrites_for(bot_member).view_channel is True
            and channel.overwrites_for(bot_member).connect is True
            and channel.overwrites_for(bot_member).move_members is True
            and channel.overwrites_for(bot_member).manage_channels is True
            and channel.overwrites_for(bot_member).manage_permissions is None
        ):
            return

        try:
            await channel.set_permissions(
                bot_member,
                overwrite=overwrite,
                reason="保留機器人管理個人語音頻道的權限",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Unable to restore bot permissions for voice channel %s",
                channel.id,
            )

    async def ensure_owner_access(
        self, channel: discord.VoiceChannel, owner: discord.Member
    ):
        overwrite = channel.overwrites_for(owner)
        bot_member = channel.guild.me
        can_manage_overwrites = (
            bot_member is not None and bot_member.guild_permissions.administrator
        )
        manage_permissions = True if can_manage_overwrites else None
        required_permissions = (
            "view_channel",
            "connect",
            "manage_channels",
        )
        if (
            all(getattr(overwrite, permission) is True for permission in required_permissions)
            and overwrite.manage_permissions is manage_permissions
        ):
            return

        overwrite.manage_permissions = manage_permissions
        for permission in required_permissions:
            setattr(overwrite, permission, True)
        try:
            await channel.set_permissions(
                owner,
                overwrite=overwrite,
                reason="授予個人語音頻道建立者管理權限",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Unable to grant owner %s access to voice channel %s",
                owner.id,
                channel.id,
            )

    async def delete_if_empty(self, channel: discord.VoiceChannel):
        if channel.id not in self.managed_channels or channel.members:
            return

        try:
            await channel.delete(reason="個人語音頻道已無使用者")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Unable to delete empty voice channel %s", channel.id)
            return

        owner_id = self.managed_channels.pop(channel.id)
        if not await self.save_ownership():
            self.managed_channels[channel.id] = owner_id

    async def move_to_owner_channel(self, member: discord.Member):
        if not self.ownership_ready:
            logger.error(
                "Cannot create a temporary voice channel for member %s: "
                "ownership data is unavailable",
                member.id,
            )
            return

        existing_channel = self.find_owner_channel(member.guild, member.id)
        if existing_channel is not None:
            await self.ensure_owner_access(existing_channel, member)
            await self.ensure_bot_access(existing_channel)
            try:
                await member.move_to(existing_channel, reason="返回個人語音頻道")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to move member %s to owned channel %s",
                    member.id,
                    existing_channel.id,
                )
            return

        trigger = member.guild.get_channel(TRIGGER_CHANNEL_ID)
        bot_member = member.guild.me
        if not isinstance(trigger, discord.VoiceChannel) or bot_member is None:
            logger.error(
                "Unable to create voice channel: trigger %s or bot member is unavailable",
                TRIGGER_CHANNEL_ID,
            )
            return

        owner_overwrite = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            manage_channels=True,
        )
        if bot_member.guild_permissions.administrator:
            owner_overwrite.manage_permissions = True
        else:
            logger.warning(
                "Bot 不是伺服器管理員，因此無法授予建立者頻道的「管理權限」；"
                "個人語音頻道仍會建立，建立者可管理頻道名稱與設定。"
            )

        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
                connect=False,
            ),
            member: owner_overwrite,
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                send_messages=True,
                read_message_history=True,
                move_members=True,
                manage_channels=True,
            ),
        }

        try:
            channel = await member.guild.create_voice_channel(
                name=f"{member.display_name} 的語音",
                category=trigger.category,
                overwrites=overwrites,
                reason=f"{member} 加入個人語音頻道入口",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Unable to create temporary voice channel")
            return

        self.managed_channels[channel.id] = member.id
        if not await self.save_ownership():
            try:
                await channel.delete(reason="無法保存個人語音頻道建立者資料")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to delete untracked voice channel %s",
                    channel.id,
                )
            else:
                self.managed_channels.pop(channel.id, None)
            return

        try:
            await member.move_to(channel, reason="進入新建立的個人語音頻道")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Unable to move member %s to new voice channel %s",
                member.id,
                channel.id,
            )
            await self.delete_if_empty(channel)
            return

        await send_permission_guide(channel)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.ownership_ready:
            logger.error(
                "Skipping temporary voice channel recovery because ownership data "
                "is unavailable"
            )
            return

        for channel_id, owner_id in list(self.managed_channels.items()):
            channel = self.bot.get_channel(channel_id)
            if isinstance(channel, discord.VoiceChannel):
                await self.delete_if_empty(channel)
                if channel.id not in self.managed_channels:
                    continue

                owner = channel.guild.get_member(owner_id)
                if owner is None:
                    try:
                        owner = await channel.guild.fetch_member(owner_id)
                    except discord.NotFound:
                        owner = None
                    except (discord.Forbidden, discord.HTTPException):
                        logger.exception(
                            "Unable to fetch owner %s for voice channel %s",
                            owner_id,
                            channel.id,
                        )
                        continue
                if owner is not None:
                    await self.ensure_owner_access(channel, owner)
                await self.ensure_bot_access(channel)
            elif channel is not None:
                self.managed_channels.pop(channel_id, None)
                await self.save_ownership()

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ):
        if after.id not in self.managed_channels or not isinstance(
            after,
            discord.VoiceChannel,
        ):
            return

        bot_member = after.guild.me
        if (
            bot_member is not None
            and before.overwrites_for(bot_member)
            != after.overwrites_for(bot_member)
        ):
            await self.ensure_bot_access(after)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        owner_id = self.managed_channels.pop(channel.id, None)
        if owner_id is not None and not await self.save_ownership():
            self.managed_channels[channel.id] = owner_id

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        async with self.channel_lock:
            if after.channel is not None and after.channel.id == TRIGGER_CHANNEL_ID:
                await self.move_to_owner_channel(member)

            if isinstance(before.channel, discord.VoiceChannel):
                await self.delete_if_empty(before.channel)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TempVoice(bot))
