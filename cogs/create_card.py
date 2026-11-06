import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from cogs._text import normalize_multiline_text


logger = logging.getLogger(__name__)
HEX_COLOR_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}")


class CreateCard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="create-card",
        description="使用 Webhook 建立自訂卡片",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        title="卡片標題（必填）",
        body="卡片內文（選填）",
        footer="卡片頁尾（選填）",
        color="卡片邊框 HEX 顏色，例如 #5865F2",
        image="卡片圖片（選填）",
    )
    async def create_card(
        self,
        interaction: discord.Interaction,
        title: str,
        body: str | None = None,
        footer: str | None = None,
        color: str = "#5865F2",
        image: discord.Attachment | None = None,
    ) -> None:
        title = normalize_multiline_text(title)
        body = normalize_multiline_text(body or "")
        footer = normalize_multiline_text(footer or "")
        color = color.strip()

        if not title or len(title) > 256:
            await interaction.response.send_message(
                "卡片標題不可空白，且最多 256 個字元。",
                ephemeral=True,
            )
            return
        if len(body) > 4096 or len(footer) > 2048:
            await interaction.response.send_message(
                "卡片內文最多 4096 個字元，頁尾最多 2048 個字元。",
                ephemeral=True,
            )
            return
        if len(title) + len(body) + len(footer) > 6000:
            await interaction.response.send_message(
                "卡片標題、內文與頁尾合計不可超過 6000 個字元。",
                ephemeral=True,
            )
            return
        if not HEX_COLOR_PATTERN.fullmatch(color):
            await interaction.response.send_message(
                "邊框顏色格式錯誤，請使用 HEX 色碼，例如 #5865F2。",
                ephemeral=True,
            )
            return
        if image is not None and (
            image.content_type is None
            or not image.content_type.startswith("image/")
        ):
            await interaction.response.send_message(
                "上傳的附件必須是圖片。",
                ephemeral=True,
            )
            return

        channel = interaction.channel
        webhook_channel = (
            channel.parent if isinstance(channel, discord.Thread) else channel
        )
        if not isinstance(webhook_channel, discord.TextChannel):
            await interaction.response.send_message(
                "請在文字頻道或其討論串中使用此指令。",
                ephemeral=True,
            )
            return

        user = interaction.user
        avatar_url = (user.avatar or user.default_avatar).url
        embed = discord.Embed(
            title=title,
            description=body or None,
            color=discord.Color(int(color[1:], 16)),
        )
        if footer:
            embed.set_footer(text=footer)

        await interaction.response.defer(ephemeral=True)
        webhook: discord.Webhook | None = None
        try:
            uploaded_file = await image.to_file() if image is not None else None
            if uploaded_file is not None:
                embed.set_image(url=f"attachment://{uploaded_file.filename}")

            webhook = await webhook_channel.create_webhook(
                name="自訂卡片",
                reason=f"{user.display_name} 使用 /create-card",
            )
            if webhook.token is None:
                raise RuntimeError("Discord did not return a webhook token")

            send_kwargs: dict[str, object] = {
                "embed": embed,
                "username": user.display_name,
                "avatar_url": avatar_url,
                "wait": True,
            }
            if uploaded_file is not None:
                send_kwargs["file"] = uploaded_file
            if isinstance(channel, discord.Thread):
                send_kwargs["thread"] = channel
            await webhook.send(**send_kwargs)
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to publish a custom card in channel %s",
                interaction.channel_id,
            )
            cleanup_succeeded = await self._delete_webhook(webhook)
            message = (
                "卡片未能發布。請確認 Bot 有「管理 Webhook」及"
                "在此頻道傳送訊息的權限。"
            )
            if not cleanup_succeeded:
                message += " 臨時 Webhook 也未能清除，請通知管理員。"
            await interaction.followup.send(
                message,
                ephemeral=True,
            )
            return
        except (discord.HTTPException, RuntimeError):
            logger.exception(
                "Unable to publish a custom card in channel %s",
                interaction.channel_id,
            )
            cleanup_succeeded = await self._delete_webhook(webhook)
            message = "卡片未能發布，請稍後再試。"
            if not cleanup_succeeded:
                message += " 臨時 Webhook 也未能清除，請通知管理員。"
            await interaction.followup.send(message, ephemeral=True)
            return

        if not await self._delete_webhook(webhook):
            await interaction.followup.send(
                "卡片已發布，但臨時 Webhook 未能清除，請通知管理員。",
                ephemeral=True,
            )
            return
        await interaction.followup.send("已建立並發布卡片。", ephemeral=True)

    @staticmethod
    async def _delete_webhook(webhook: discord.Webhook | None) -> bool:
        if webhook is None:
            return True
        try:
            await webhook.delete(reason="自訂卡片已發布或發布失敗")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Unable to delete temporary custom-card webhook")
            return False
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CreateCard(bot))
