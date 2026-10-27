from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from cogs._config import required_snowflake, required_snowflake_list

REQUEST_CHANNEL_ID = required_snowflake("ROLE_REQUEST_CHANNEL_ID")
ADMIN_NOTIFICATION_CHANNEL_ID = required_snowflake(
    "ROLE_REQUEST_NOTIFICATION_CHANNEL_ID"
)
ADMIN_ROLE_IDS = required_snowflake_list("ROLE_REQUEST_ADMIN_ROLE_IDS")
ROLE_REQUEST_CARD_FOOTER = "- FRC6998入群資料註冊面板"
ROLE_NOTIFICATION_PENDING_FOOTER = "unipards:role-notification:v1:pending"
logger = logging.getLogger(__name__)


async def get_text_channel(
    bot: discord.Client,
    channel_id: int,
) -> discord.TextChannel:
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise TypeError(f"Channel {channel_id} is not a text channel")
    return channel


class RoleRequestModal(discord.ui.Modal, title="FRC6998 群組資料註冊"):
    member_name = discord.ui.TextInput(
        label="姓名",
        placeholder="請輸入你的姓名",
        min_length=1,
        max_length=80,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.member_name.value).strip()
        if not name:
            await interaction.response.send_message(
                "姓名不可空白，請重新點擊按鈕填寫。",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "無法確認你的伺服器成員資料，請稍後再試。",
                ephemeral=True,
            )
            return

        if interaction.user.nick:
            await interaction.response.send_message(
                "你已經設定伺服器暱稱，無需填寫此表單。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            channel = await get_text_channel(
                interaction.client,
                ADMIN_NOTIFICATION_CHANNEL_ID,
            )
            embed = discord.Embed(
                title="需要設定成員身分組",
                color=discord.Color.orange(),
            )
            embed.add_field(name="姓名", value=name, inline=False)
            embed.add_field(
                name="Discord 成員",
                value=f"{interaction.user.mention} (`{interaction.user.id}`)",
                inline=False,
            )
            embed.description = "請管理員協助為此成員設定適當的身分組。"
            embed.set_footer(text=ROLE_NOTIFICATION_PENDING_FOOTER)
            mentions = " ".join(f"<@&{role_id}>" for role_id in ADMIN_ROLE_IDS)
            notification = await channel.send(
                content=(
                    f"{mentions} 請協助設定此成員的身分組。"
                    "設定完成後，請對這則通知按下 :white_check_mark: ，"
                    "機器人會將通知更新為已設定完成。"
                ),
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    roles=[discord.Object(id=role_id) for role_id in ADMIN_ROLE_IDS]
                ),
            )
            await notification.add_reaction("✅")
        except (discord.HTTPException, TypeError):
            logger.exception(
                "Failed to send role setup notification to channel %s",
                ADMIN_NOTIFICATION_CHANNEL_ID,
            )
            await interaction.followup.send(
                "通知未能送出，請稍後再試或聯絡管理員。",
                ephemeral=True,
            )
            return

        try:
            await interaction.user.edit(
                nick=name,
                reason="Member submitted the role request form",
            )
        except discord.Forbidden:
            logger.exception(
                "Bot is not allowed to set nickname for member %s",
                interaction.user.id,
            )
            await interaction.followup.send(
                "已通知管理員，但 Bot 無法自動設定你的伺服器暱稱。"
                "請管理員確認 Bot 有「管理暱稱」權限，且 Bot 的角色順位高於你的身分組。",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception(
                "Failed to set nickname for member %s",
                interaction.user.id,
            )
            await interaction.followup.send(
                "已通知管理員，但自動設定伺服器暱稱失敗，請聯絡管理員。",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "已將你的伺服器暱稱設定為表單姓名，並通知管理員協助設定身分組。",
            ephemeral=True,
        )


class RoleRequestView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="填寫姓名",
        style=discord.ButtonStyle.primary,
        custom_id="role_request:open_modal",
    )
    async def request_role(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[RoleRequestView],
    ) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "無法確認你的伺服器暱稱，請稍後再試。",
                ephemeral=True,
            )
            return

        if interaction.user.nick:
            await interaction.response.send_message(
                "你已經設定伺服器暱稱，無需填寫此表單。",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(RoleRequestModal())


class RoleRequest(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        channel = await get_text_channel(self.bot, REQUEST_CHANNEL_ID)
        if self.bot.user is None:
            raise RuntimeError("Bot user is unavailable while loading role request cog")

        try:
            async for message in channel.history(limit=None):
                if message.author.id == self.bot.user.id and any(
                    embed.footer.text == ROLE_REQUEST_CARD_FOOTER
                    for embed in message.embeds
                ):
                    logger.info(
                        "Role request card already exists in channel %s",
                        REQUEST_CHANNEL_ID,
                    )
                    return

            embed = discord.Embed(
                title="FRC6998 群組資料註冊",
                description=(
                    "請點擊下方按鈕並輸入姓名，"
                    "管理員收到通知後會協助設定你的身分組。"
                ),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=ROLE_REQUEST_CARD_FOOTER)
            await channel.send(embed=embed, view=RoleRequestView())
        except discord.HTTPException:
            logger.exception(
                "Failed to check or publish role request card in channel %s",
                REQUEST_CHANNEL_ID,
            )
            raise

        logger.info(
            "Published role request card to channel %s",
            REQUEST_CHANNEL_ID,
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        logger.info(
            "Received reaction %s from user %s on message %s in channel %s",
            payload.emoji,
            payload.user_id,
            payload.message_id,
            payload.channel_id,
        )
        if (
            payload.channel_id != ADMIN_NOTIFICATION_CHANNEL_ID
            or str(payload.emoji) != "✅"
            or self.bot.user is None
            or payload.user_id == self.bot.user.id
        ):
            return

        try:
            channel = await get_text_channel(self.bot, payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            if message.author.id != self.bot.user.id:
                return

            guild = message.guild
            if guild is None:
                return
            member = payload.member or guild.get_member(payload.user_id)
            if member is None:
                member = await guild.fetch_member(payload.user_id)
            if not any(role.id in ADMIN_ROLE_IDS for role in member.roles):
                logger.info(
                    "Ignoring completion reaction from user %s without an admin role",
                    payload.user_id,
                )
                return

            notification_embed = next(
                (
                    embed
                    for embed in message.embeds
                    if embed.footer.text == ROLE_NOTIFICATION_PENDING_FOOTER
                ),
                None,
            )
            if notification_embed is None:
                return

            member_field = next(
                (
                    field
                    for field in notification_embed.fields
                    if field.name == "Discord 成員"
                ),
                None,
            )
            member_match = (
                re.search(r"<@!?(\d+)>", member_field.value)
                if member_field is not None
                else None
            )
            if member_match is None:
                logger.error(
                    "Role setup notification %s has no valid member mention",
                    message.id,
                )
                return

            await message.edit(
                content=f"<@{member_match.group(1)}> 的權限已被設定完成",
                embeds=[],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            logger.info(
                "Role setup notification %s marked complete by user %s",
                message.id,
                payload.user_id,
            )
        except (discord.HTTPException, TypeError):
            logger.exception(
                "Failed to mark role setup notification %s as complete",
                payload.message_id,
            )


async def setup(bot: commands.Bot) -> None:
    bot.add_view(RoleRequestView())
    await bot.add_cog(RoleRequest(bot))
