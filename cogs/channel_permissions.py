import logging
import psycopg

import discord
from discord.ext import commands

from cogs._config import required_snowflake
from database import load_all_channel_owners


DESTINATION_CATEGORY_ID = required_snowflake("ARCHIVE_CATEGORY_ID")
PRIVATE_GROUP_CATEGORY_ID = required_snowflake("PRIVATE_GROUP_CATEGORY_ID")
ACTIVITY_PROJECT_CATEGORY_ID = required_snowflake(
    "ACTIVITY_PROJECT_CATEGORY_ID"
)
logger = logging.getLogger(__name__)


async def send_permission_guide(channel: discord.TextChannel | discord.VoiceChannel):
    embed = discord.Embed(
        title="頻道管理方式",
        description=(
            "請在 Discord 的「編輯頻道 → 權限」管理此頻道的成員與身分組權限。"
            "建立者只有頻道層級「管理權限」，不會獲得「管理頻道」覆寫。"
            "其他成員及身分組不能透過頻道覆寫取得「管理頻道」權限。"
            "機器人在線時會保護機器人、@everyone 及建立者的權限覆寫；"
            "若被修改，機器人會自動恢復。"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="`!rename <頻道名稱>`",
        value="只有頻道建立者可以變更此頻道名稱。",
        inline=False,
    )
    embed.add_field(
        name="`!close`",
        value="活動專案群會封存；語音頻道與私聊群會直接刪除。",
        inline=False,
    )
    embed.set_footer(text="- FRC6998隊務管理Bot")

    try:
        message = await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Unable to send permission guide to channel %s", channel.id)
        return

    try:
        await message.pin(reason="釘選自動生成頻道的管理方式說明")
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Unable to pin channel guide in channel %s", channel.id)


class ChannelPermissions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.protected_overwrites: dict[
            int, dict[int, discord.PermissionOverwrite]
        ] = {}

    def get_managed_owner(self, channel: discord.abc.GuildChannel) -> int | None:
        cog_name = (
            "TempVoice"
            if isinstance(channel, discord.VoiceChannel)
            else "TemporaryGroups"
        )
        owner_cog = self.bot.get_cog(cog_name)
        managed_channels = getattr(owner_cog, "managed_channels", None)
        if not isinstance(managed_channels, dict):
            return None
        owner_id = managed_channels.get(channel.id)
        return owner_id if isinstance(owner_id, int) else None

    def protected_targets(
        self,
        channel: discord.abc.GuildChannel,
        owner_id: int,
    ) -> tuple[discord.Role | discord.Member | discord.Object, ...]:
        bot_member = channel.guild.me
        owner = channel.guild.get_member(owner_id) or discord.Object(id=owner_id)
        if bot_member is None:
            return (channel.guild.default_role, owner)
        return (bot_member, channel.guild.default_role, owner)

    def prepare_protected_overwrites(
        self,
        channel: discord.TextChannel | discord.VoiceChannel,
        owner_id: int,
    ) -> None:
        expected = {
            target.id: channel.overwrites_for(target)
            for target in self.protected_targets(channel, owner_id)
        }
        owner = channel.guild.get_member(owner_id) or discord.Object(id=owner_id)
        owner_overwrite = channel.overwrites_for(owner)
        owner_permissions = (
            ("view_channel", "send_messages", "read_message_history")
            if isinstance(channel, discord.TextChannel)
            else ("view_channel", "connect")
        )
        for permission in owner_permissions:
            setattr(owner_overwrite, permission, True)
        owner_overwrite.manage_permissions = True
        owner_overwrite.manage_channels = False
        expected[owner_id] = owner_overwrite

        bot_member = channel.guild.me
        if bot_member is not None:
            bot_overwrite = channel.overwrites_for(bot_member)
            bot_permissions = ["view_channel", "manage_channels"]
            if isinstance(channel, discord.TextChannel):
                bot_permissions.extend(("send_messages", "read_message_history"))
            else:
                bot_permissions.extend(("connect", "move_members"))
            for permission in bot_permissions:
                setattr(bot_overwrite, permission, True)
            expected[bot_member.id] = bot_overwrite

        self.protected_overwrites[channel.id] = expected

    async def ensure_owner_permissions(
        self,
        channel: discord.TextChannel | discord.VoiceChannel,
        owner_id: int,
    ) -> bool:
        owner = channel.guild.get_member(owner_id) or discord.Object(id=owner_id)
        overwrite = channel.overwrites_for(owner)
        required_permissions = (
            ("view_channel", "send_messages", "read_message_history")
            if isinstance(channel, discord.TextChannel)
            else ("view_channel", "connect")
        )
        changed = False
        for permission in required_permissions:
            if getattr(overwrite, permission) is not True:
                setattr(overwrite, permission, True)
                changed = True
        if overwrite.manage_permissions is not True:
            overwrite.manage_permissions = True
            changed = True
        if overwrite.manage_channels is not False:
            overwrite.manage_channels = False
            changed = True
        if not changed:
            return True

        try:
            await channel.set_permissions(
                owner,
                overwrite=overwrite,
                reason="確保頻道建立者只有頻道層級管理權限",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Unable to ensure owner permissions for generated channel %s",
                channel.id,
            )
            return False
        return True

    async def ensure_bot_permissions(
        self,
        channel: discord.TextChannel | discord.VoiceChannel,
    ) -> bool:
        bot_member = channel.guild.me
        if bot_member is None:
            logger.error(
                "Cannot confirm bot permissions in generated channel %s",
                channel.id,
            )
            return False

        overwrite = channel.overwrites_for(bot_member)
        required_permissions = ["view_channel", "manage_channels"]
        if isinstance(channel, discord.TextChannel):
            required_permissions.extend(("send_messages", "read_message_history"))
        else:
            required_permissions.extend(("connect", "move_members"))
        changed = False
        for permission in required_permissions:
            if getattr(overwrite, permission) is not True:
                setattr(overwrite, permission, True)
                changed = True
        if not changed:
            return True

        try:
            await channel.set_permissions(
                bot_member,
                overwrite=overwrite,
                reason="確保機器人可管理自動建立頻道",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Unable to ensure bot permissions for generated channel %s",
                channel.id,
            )
            return False
        return True

    async def remove_other_manage_channel_grants(
        self,
        channel: discord.TextChannel | discord.VoiceChannel,
        owner_id: int,
    ) -> bool:
        protected_ids = {
            target.id for target in self.protected_targets(channel, owner_id)
        }
        succeeded = True
        for target, current_overwrite in list(channel.overwrites.items()):
            if (
                target.id in protected_ids
                or current_overwrite.manage_channels is not True
            ):
                continue

            overwrite = discord.PermissionOverwrite.from_pair(
                *current_overwrite.pair()
            )
            overwrite.manage_channels = False
            try:
                await channel.set_permissions(
                    target,
                    overwrite=overwrite,
                    reason="禁止在自動建立頻道授予其他對象管理頻道權限",
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to remove Manage Channels from generated channel %s "
                    "target %s",
                    channel.id,
                    target.id,
                )
                succeeded = False
        return succeeded

    async def enforce_channel_permission_policy(
        self,
        channel: discord.TextChannel | discord.VoiceChannel,
        owner_id: int,
    ) -> bool:
        self.prepare_protected_overwrites(channel, owner_id)
        owner_permissions_set = await self.ensure_owner_permissions(channel, owner_id)
        bot_permissions_set = await self.ensure_bot_permissions(channel)
        other_grants_removed = await self.remove_other_manage_channel_grants(
            channel,
            owner_id,
        )
        return (
            owner_permissions_set
            and bot_permissions_set
            and other_grants_removed
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for cog_name in ("TemporaryGroups",):
            owner_cog = self.bot.get_cog(cog_name)
            managed_channels = getattr(owner_cog, "managed_channels", None)
            if not isinstance(managed_channels, dict):
                continue
            for channel_id, owner_id in list(managed_channels.items()):
                channel = self.bot.get_channel(channel_id)
                if not isinstance(
                    channel,
                    (discord.TextChannel, discord.VoiceChannel),
                ):
                    continue
                await self.enforce_channel_permission_policy(channel, owner_id)

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        if not isinstance(after, (discord.TextChannel, discord.VoiceChannel)):
            return
        owner_id = self.get_managed_owner(after)
        if owner_id is None:
            return

        expected = self.protected_overwrites.setdefault(
            after.id,
            {
                target.id: before.overwrites_for(target)
                for target in self.protected_targets(before, owner_id)
            },
        )
        targets = {
            target.id: target
            for target in self.protected_targets(after, owner_id)
        }
        for target_id, overwrite in expected.items():
            target = targets.get(target_id)
            if target is None or after.overwrites_for(target) == overwrite:
                continue
            try:
                await after.set_permissions(
                    target,
                    overwrite=None if overwrite.is_empty() else overwrite,
                    reason="恢復自動建立頻道受保護對象的權限",
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to restore protected overwrite for channel %s target %s",
                    after.id,
                    target_id,
                )

        await self.remove_other_manage_channel_grants(after, owner_id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self,
        channel: discord.abc.GuildChannel,
    ) -> None:
        self.protected_overwrites.pop(channel.id, None)

    async def restore_owner_tracking(
        self,
        channel: discord.TextChannel | discord.VoiceChannel,
        owner_cog: commands.Cog,
        owner_id: int,
    ):
        managed_channels = getattr(owner_cog, "managed_channels")
        managed_channels[channel.id] = owner_id
        if await owner_cog.save_ownership() is False:
            logger.error("Unable to restore ownership tracking for channel %s", channel.id)

    async def stop_tracking(
        self, channel: discord.TextChannel | discord.VoiceChannel
    ) -> tuple[commands.Cog, int] | None:
        cog_name = (
            "TempVoice"
            if isinstance(channel, discord.VoiceChannel)
            else "TemporaryGroups"
        )
        owner_cog = self.bot.get_cog(cog_name)
        managed_channels = getattr(owner_cog, "managed_channels", None)
        save_ownership = getattr(owner_cog, "save_ownership", None)
        if not isinstance(managed_channels, dict) or not callable(save_ownership):
            return None

        owner_id = managed_channels.pop(channel.id, None)
        if owner_id is None:
            return None
        if await save_ownership() is False:
            managed_channels[channel.id] = owner_id
            await save_ownership()
            return None
        self.protected_overwrites.pop(channel.id, None)
        return owner_cog, owner_id

    async def load_owners(self) -> dict[int, int] | None:
        try:
            if self.bot.database is None:
                raise RuntimeError("PostgreSQL 尚未初始化")
            return await load_all_channel_owners(self.bot.database)
        except (psycopg.Error, RuntimeError):
            logger.exception("Unable to load channel ownership")
            return None

    async def get_owned_channel(
        self, ctx: commands.Context
    ) -> discord.TextChannel | discord.VoiceChannel | None:
        channel = ctx.channel
        if ctx.guild is None or not isinstance(
            channel, (discord.TextChannel, discord.VoiceChannel)
        ):
            await ctx.send("請在自己建立的文字或語音頻道內使用此指令。")
            return None

        owners = await self.load_owners()
        if owners is None:
            await ctx.send("無法讀取頻道建立者資料，請通知管理員。")
            return None

        owner_id = owners.get(channel.id)
        if owner_id is None:
            await ctx.send("此頻道不是由自動建頻道功能建立的頻道。")
            return None
        if owner_id != ctx.author.id:
            await ctx.send("只有此頻道的建立者可以使用此指令。")
            return None
        return channel

    @commands.command(name="rename")
    async def rename_channel(self, ctx: commands.Context, *, name: str):
        channel = await self.get_owned_channel(ctx)
        if channel is None:
            return

        name = name.strip()
        if not name or len(name) > 100:
            await ctx.send("頻道名稱需為 1 到 100 個字元。")
            return

        bot_member = ctx.guild.me if ctx.guild is not None else None
        if bot_member is None or not channel.permissions_for(
            bot_member
        ).manage_channels:
            await ctx.send("Bot 在此頻道需要「管理頻道」權限才能變更名稱。")
            return

        try:
            await channel.edit(name=name, reason=f"{ctx.author} 使用 !rename")
        except discord.Forbidden:
            logger.exception("Bot cannot rename generated channel %s", channel.id)
            await ctx.send("頻道名稱變更失敗，請檢查 Bot 的頻道權限。")
            return
        except discord.HTTPException:
            logger.exception("Unable to rename generated channel %s", channel.id)
            await ctx.send("Discord 暫時無法變更頻道名稱，請稍後再試。")
            return

        try:
            await ctx.send(
                f"已將頻道名稱變更為「{name}」。",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Channel %s was renamed, but the confirmation could not be sent",
                channel.id,
            )

    @commands.command(name="close")
    async def close_channel(self, ctx: commands.Context):
        channel = await self.get_owned_channel(ctx)
        if channel is None or ctx.guild is None:
            return

        bot_member = ctx.guild.me
        if bot_member is None:
            await ctx.send("無法確認 Bot 在伺服器中的權限，請稍後再試。")
            return

        is_voice = isinstance(channel, discord.VoiceChannel)
        if is_voice:
            archive = False
        elif channel.category_id in {
            ACTIVITY_PROJECT_CATEGORY_ID,
            DESTINATION_CATEGORY_ID,
        }:
            archive = True
        elif channel.category_id == PRIVATE_GROUP_CATEGORY_ID:
            archive = False
        else:
            await ctx.send("此頻道不屬於可封存或刪除的自動生成頻道。")
            return

        category = None
        if archive:
            category = ctx.guild.get_channel(DESTINATION_CATEGORY_ID)
            if not isinstance(category, discord.CategoryChannel):
                await ctx.send("找不到活動專案封存分類，請通知管理員。")
                return

            source_permissions = channel.permissions_for(bot_member)
            if not source_permissions.manage_channels:
                await ctx.send(
                    "Bot 在目前頻道需要「管理頻道」權限才能移動至封存分類。"
                )
                return

        elif not channel.permissions_for(bot_member).manage_channels:
            await ctx.send("Bot 在此頻道需要「管理頻道」權限才能刪除。")
            return

        tracking = await self.stop_tracking(channel)
        if tracking is None:
            await ctx.send("無法解除此頻道的自動追蹤，沒有執行封存或刪除。")
            return
        owner_cog, owner_id = tracking

        if archive:
            archived_overwrites = {
                ctx.guild.default_role: discord.PermissionOverwrite(
                    view_channel=False,
                    send_messages=False,
                    read_message_history=False,
                )
            }
            for target, overwrite in channel.overwrites.items():
                if (
                    target != ctx.guild.default_role
                    and overwrite.view_channel is True
                ):
                    archived_overwrites[target] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=False,
                        read_message_history=True,
                    )

            try:
                await channel.edit(
                    category=category,
                    sync_permissions=False,
                    overwrites=archived_overwrites,
                    reason=f"{ctx.author} 使用 !close 封存活動專案群",
                )
            except discord.Forbidden as error:
                print(f"無法封存活動專案群 {channel.id}：{error}")
                await self.restore_owner_tracking(channel, owner_cog, owner_id)
                await ctx.send("封存失敗，已恢復頻道追蹤。請檢查 Bot 的頻道權限。")
                return
            except discord.HTTPException as error:
                print(f"Discord 無法封存活動專案群 {channel.id}：{error}")
                await self.restore_owner_tracking(channel, owner_cog, owner_id)
                await ctx.send("Discord 暫時無法封存頻道，已恢復頻道追蹤。")
                return

            return

        try:
            await ctx.send("正在刪除此頻道；完成後將不再追蹤。")
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"無法在即將刪除的頻道 {channel.id} 發送提示：{error}")

        try:
            await channel.delete(reason=f"{ctx.author} 使用 !close 刪除自動生成頻道")
        except discord.Forbidden as error:
            print(f"無法刪除自動生成頻道 {channel.id}：{error}")
            await self.restore_owner_tracking(channel, owner_cog, owner_id)
            await ctx.send("刪除失敗，已恢復頻道追蹤。請確認 Bot 有「管理頻道」權限。")
        except discord.HTTPException as error:
            print(f"Discord 無法刪除頻道 {channel.id}：{error}")
            await self.restore_owner_tracking(channel, owner_cog, owner_id)
            await ctx.send("Discord 暫時無法刪除頻道，已恢復頻道追蹤。")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChannelPermissions(bot))