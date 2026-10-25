import logging
import psycopg

import discord
from discord.ext import commands

from database import Database, load_all_channel_owners


DESTINATION_CATEGORY_ID = 1556268544277225533
PRIVATE_GROUP_CATEGORY_ID = 1555925845078704228
ACTIVITY_PROJECT_CATEGORY_ID = 1555925059258810441
PROTECTED_ROLE_IDS = {1563170216438136996}
logger = logging.getLogger(__name__)


async def send_permission_guide(channel: discord.TextChannel | discord.VoiceChannel):
    embed = discord.Embed(
        title="頻道權限指令",
        description=(
            "只有此頻道的建立者可以使用以下指令。對象可使用成員標註、"
            "身分組標註，或輸入 `everyone` 代表所有人。"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="`!allow <對象>`",
        value=(
            "文字頻道：允許瀏覽及發言。\n"
            "語音頻道：允許瀏覽；其他語音權限維持原設定。"
        ),
        inline=False,
    )
    embed.add_field(
        name="`!deny <對象>`",
        value=(
            "文字頻道：拒絕瀏覽及發言。\n"
            "語音頻道：拒絕瀏覽；其他語音權限維持原設定。"
        ),
        inline=False,
    )
    embed.add_field(
        name="`!reset <對象>`",
        value="清除本功能設定的權限，恢復繼承頻道或身分組設定。",
        inline=False,
    )
    embed.add_field(
        name="`!list`",
        value="列出此頻道明確允許瀏覽的成員、身分組及其發言／連線權限。",
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
        await message.pin(reason="釘選自動生成頻道的權限指令說明")
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Unable to pin permission guide in channel %s", channel.id)


class ChannelPermissions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def ensure_bot_message_access(
        self,
        channel: discord.TextChannel | discord.VoiceChannel,
    ) -> bool:
        bot_member = channel.guild.me
        if bot_member is None:
            logger.error(
                "Cannot confirm bot member permissions in channel %s",
                channel.id,
            )
            return False

        overwrite = channel.overwrites_for(bot_member)
        required_permissions = ("view_channel", "send_messages", "read_message_history")
        if all(getattr(overwrite, permission) is True for permission in required_permissions):
            return True

        for permission in required_permissions:
            setattr(overwrite, permission, True)

        try:
            await channel.set_permissions(
                bot_member,
                overwrite=overwrite,
                reason="確保 Bot 可回覆頻道權限指令",
            )
        except discord.Forbidden:
            logger.exception(
                "Bot cannot grant itself message access in channel %s",
                channel.id,
            )
            return False
        except discord.HTTPException:
            logger.exception(
                "Unable to ensure bot message access in channel %s",
                channel.id,
            )
            return False
        return True

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
        cog_name = "TempVoice" if isinstance(channel, discord.VoiceChannel) else "TemporaryGroups"
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
            await ctx.send("只有此頻道的建立者可以調整權限。")
            return None
        return channel

    async def resolve_target(
        self, ctx: commands.Context, target_text: str
    ) -> discord.Member | discord.Role:
        if target_text.casefold() in {"everyone", "@everyone"}:
            return ctx.guild.default_role

        try:
            return await commands.MemberConverter().convert(ctx, target_text)
        except commands.BadArgument:
            try:
                return await commands.RoleConverter().convert(ctx, target_text)
            except commands.BadArgument as error:
                raise commands.BadArgument(
                    "請標註一位成員或身分組，或使用 `everyone`。"
                ) from error

    async def update_access(
        self, ctx: commands.Context, target_text: str, action: str
    ):
        channel = await self.get_owned_channel(ctx)
        if channel is None:
            return

        try:
            target = await self.resolve_target(ctx, target_text)
        except commands.BadArgument as error:
            await ctx.send(str(error))
            return

        if isinstance(target, discord.Member) and target.id in {
            ctx.author.id,
            self.bot.user.id if self.bot.user is not None else None,
        }:
            await ctx.send("不能變更自己的頻道存取權，也不能移除 Bot 的存取權。")
            return

        if (
            isinstance(target, discord.Role)
            and target.id in PROTECTED_ROLE_IDS
            and action in {"deny", "reset"}
        ):
            await ctx.send("此身分組受到保護，不能透過頻道權限指令移除或重設其存取權。")
            return

        if not await self.ensure_bot_message_access(channel):
            try:
                await ctx.author.send(
                    f"無法在 <#{channel.id}> 確保 Bot 的發言權限，"
                    "因此沒有執行這次權限變更。請管理員檢查 Bot 的頻道權限。"
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to notify channel owner %s about bot access failure",
                    ctx.author.id,
                )
            return

        is_voice = isinstance(channel, discord.VoiceChannel)
        permission_names = ("view_channel",) if is_voice else (
            "view_channel",
            "send_messages",
        )
        overwrite = channel.overwrites_for(target)
        if action == "reset":
            for permission in permission_names:
                setattr(overwrite, permission, None)
            overwrite_value = None if overwrite.is_empty() else overwrite
        else:
            allowed = action == "allow"
            for permission in permission_names:
                setattr(overwrite, permission, allowed)
            overwrite_value = overwrite

        try:
            await channel.set_permissions(
                target,
                overwrite=overwrite_value,
                reason=f"{ctx.author} 使用頻道權限指令 {action}",
            )
        except discord.Forbidden:
            await ctx.send(
                "權限更新失敗。請確認 Bot 具有伺服器層級的「管理身分組」權限，"
                "且頻道未拒絕 Bot 存取。"
            )
            return
        except discord.HTTPException as error:
            logger.exception("Unable to update permissions for channel %s", channel.id)
            await ctx.send("Discord 暫時無法更新頻道權限，請稍後再試。")
            return
        except (TypeError, ValueError) as error:
            logger.exception("Invalid permission overwrite for channel %s", channel.id)
            await ctx.send("權限設定格式不正確，沒有變更頻道權限。")
            return

        if action == "allow":
            result = "設為允許瀏覽" if is_voice else "設為允許瀏覽及發言"
        elif action == "deny":
            result = "設為拒絕瀏覽" if is_voice else "設為拒絕瀏覽及發言"
        else:
            result = "恢復預設設定"

        if target == ctx.guild.default_role:
            target_name = "所有人"
        elif isinstance(target, discord.Member):
            target_name = target.display_name
        else:
            target_name = target.name
        try:
            await channel.send(
                f"> ### :loudspeaker: 已經將 {target_name} {result}。",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            print(f"無法在頻道 {channel.id} 發送權限更新摘要：{error}")

    @commands.command(name="allow")
    async def allow_access(self, ctx: commands.Context, *, target: str):
        await self.update_access(ctx, target, "allow")

    @commands.command(name="deny")
    async def deny_access(self, ctx: commands.Context, *, target: str):
        await self.update_access(ctx, target, "deny")

    @commands.command(name="reset")
    async def reset_access(self, ctx: commands.Context, *, target: str = ""):
        if not target.strip():
            await ctx.send(
                "請指定要重設的成員、身分組或 `everyone`。"
                "例如：`!reset @成員`。"
            )
            return
        await self.update_access(ctx, target, "reset")

    @commands.command(name="list")
    async def list_access(self, ctx: commands.Context) -> None:
        channel = await self.get_owned_channel(ctx)
        if channel is None or ctx.guild is None:
            return

        allowed_entries: list[str] = []
        denied_entries: list[str] = []
        for target, overwrite in channel.overwrites.items():
            if overwrite.view_channel not in {True, False}:
                continue

            if target == ctx.guild.default_role:
                target_name = "@everyone"
            elif isinstance(target, discord.Member):
                target_name = f"@{target.display_name}"
            elif isinstance(target, discord.Role):
                target_name = f"@{target.name}"
            else:
                target_name = f"ID {target.id}"
            target_name = target_name.replace("`", "\\`")

            if overwrite.view_channel is False:
                access = (
                    "拒絕瀏覽及連線"
                    if isinstance(channel, discord.VoiceChannel)
                    else "拒絕瀏覽及發言"
                )
                denied_entries.append(f"`{target_name}`：{access}")
                continue

            if isinstance(channel, discord.VoiceChannel):
                if overwrite.connect is True:
                    access = "可瀏覽並連線"
                elif overwrite.connect is False:
                    access = "可瀏覽，不可連線"
                else:
                    access = "可瀏覽；連線權限依繼承設定"
            elif overwrite.send_messages is True:
                access = "可瀏覽並發言"
            elif overwrite.send_messages is False:
                access = "可瀏覽，不可發言"
            else:
                access = "可瀏覽；發言權限依繼承設定"

            allowed_entries.append(f"`{target_name}`：{access}")

        if not allowed_entries:
            allowed_entries = ["目前沒有明確允許瀏覽的成員或身分組。"]
        if not denied_entries:
            denied_entries = ["目前沒有明確拒絕瀏覽的成員或身分組。"]

        entries = [
            "**明確允許**",
            *allowed_entries,
            "",
            "**明確拒絕（包含使用 `!deny` 拒絕的對象）**",
            *denied_entries,
        ]

        pages: list[list[str]] = [[]]
        page_length = 0
        for entry in entries:
            if pages[-1] and page_length + len(entry) + 1 > 3800:
                pages.append([])
                page_length = 0
            pages[-1].append(entry)
            page_length += len(entry) + 1

        for page_number, entries in enumerate(pages, start=1):
            title = f"{channel.name} 的頻道權限"
            if len(pages) > 1:
                title += f"（{page_number}/{len(pages)}）"
            embed = discord.Embed(
                title=title,
                description="\n".join(entries),
                color=discord.Color.blurple(),
            )
            embed.set_footer(
                text="依目前頻道權限覆寫列出允許與拒絕對象。"
            )
            await ctx.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
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