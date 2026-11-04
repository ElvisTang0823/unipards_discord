from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs._config import required_snowflake


UPDATE_OWNER_ID = required_snowflake("UPDATE_OWNER_ID")
logger = logging.getLogger(__name__)
MAX_MESSAGE_LENGTH = 2000
DISCORD_OPERATION_TIMEOUT = 10
WEBHOOK_CLEANUP_TIMEOUT = 5


class MigrationDestinationSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="選擇搬遷目的文字頻道",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, MessageMigrationView):
            await interaction.response.send_message(
                "此搬遷操作已失效，請重新選取訊息。",
                ephemeral=True,
            )
            return

        selected_channel = self.values[0]
        if (
            interaction.guild is None
            or selected_channel.guild_id != interaction.guild.id
        ):
            await interaction.response.send_message(
                "目的頻道必須位於目前伺服器。",
                ephemeral=True,
            )
            return
        destination = selected_channel.resolve()
        if destination is None:
            try:
                destination = await selected_channel.fetch()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception(
                    "Unable to resolve selected migration destination %s",
                    selected_channel.id,
                )
                await interaction.response.send_message(
                    "無法讀取所選目的頻道，請重新選擇；原訊息未變更。",
                    ephemeral=True,
                )
                return
        if not isinstance(destination, discord.TextChannel):
            await interaction.response.send_message(
                "目的地必須是伺服器文字頻道。",
                ephemeral=True,
            )
            return
        if destination.id == view.source_message.channel.id:
            await interaction.response.send_message(
                "目的頻道不能與原訊息所在頻道相同。",
                ephemeral=True,
            )
            return

        view.destination = destination
        view.confirm_button.disabled = False
        await interaction.response.edit_message(
            content=(
                f"目的頻道：{destination.mention}\n"
                "只會搬遷訊息文字；附件、表情反應、回覆關係及其他內容不會搬遷。"
                "確認後，原訊息會在搬遷成功時刪除。"
            ),
            view=view,
        )


class MessageMigrationView(discord.ui.View):
    def __init__(
        self,
        cog: MessageMigration,
        source_message: discord.Message,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.source_message = source_message
        self.destination: discord.TextChannel | None = None
        self.confirm_button.disabled = True
        self.add_item(MigrationDestinationSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != UPDATE_OWNER_ID:
            await interaction.response.send_message(
                "只有指定管理員可以使用訊息搬遷功能。",
                ephemeral=True,
            )
            return False
        if (
            interaction.guild is None
            or self.source_message.guild is None
            or interaction.guild.id != self.source_message.guild.id
        ):
            await interaction.response.send_message(
                "此訊息搬遷操作不屬於目前伺服器。",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="確認搬遷並刪除原訊息",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[MessageMigrationView],
    ) -> None:
        if self.destination is None:
            await interaction.response.send_message(
                "請先選擇目的頻道。",
                ephemeral=True,
            )
            return

        button.disabled = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="正在搬遷訊息，請稍候；原訊息只會在搬遷成功後刪除。",
            view=None,
        )
        self.stop()
        try:
            _, response = await self.cog.migrate_message(
                self.source_message,
                self.destination,
                interaction.guild,
            )
        except Exception:
            logger.exception(
                "Unexpected error migrating source message %s",
                self.source_message.id,
            )
            response = (
                "搬遷流程發生未預期錯誤，請檢查機器人記錄及來源、目的頻道；"
                "在確認原訊息狀態前，請勿重複搬遷。"
            )

        if interaction.message is not None:
            try:
                await asyncio.wait_for(
                    interaction.message.edit(content=response, view=None),
                    timeout=WEBHOOK_CLEANUP_TIMEOUT,
                )
                return
            except (asyncio.TimeoutError, discord.HTTPException):
                logger.exception(
                    "Unable to update migration result for source message %s",
                    self.source_message.id,
                )
        try:
            await asyncio.wait_for(
                interaction.followup.send(response, ephemeral=True),
                timeout=WEBHOOK_CLEANUP_TIMEOUT,
            )
        except (asyncio.TimeoutError, discord.HTTPException):
            logger.exception(
                "Unable to report migration result for source message %s",
                self.source_message.id,
            )

    @discord.ui.button(
        label="取消",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[MessageMigrationView],
    ) -> None:
        del button
        self.stop()
        await interaction.response.edit_message(
            content="已取消搬遷，原訊息未變更。",
            view=None,
        )


class MessageMigration(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.message_context_menu = app_commands.ContextMenu(
            name="搬遷錯頻訊息",
            callback=self.migrate_wrong_channel_message,
        )

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.message_context_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(
            self.message_context_menu.name,
            type=discord.AppCommandType.message,
        )

    async def migrate_wrong_channel_message(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        if interaction.user.id != UPDATE_OWNER_ID:
            await interaction.response.send_message(
                "只有指定管理員可以使用訊息搬遷功能。",
                ephemeral=True,
            )
            return
        if (
            interaction.guild is None
            or message.guild is None
            or interaction.guild.id != message.guild.id
            or not isinstance(
                message.channel,
                (discord.TextChannel, discord.Thread),
            )
        ):
            await interaction.response.send_message(
                "只能搬遷目前伺服器文字頻道或討論串中的訊息。",
                ephemeral=True,
            )
            return
        if not message.content:
            await interaction.response.send_message(
                "此訊息沒有可搬遷的文字內容；原訊息未變更。",
                ephemeral=True,
            )
            return
        if len(message.content) > MAX_MESSAGE_LENGTH:
            await interaction.response.send_message(
                "訊息文字超過 Discord 單則訊息長度上限；原訊息未變更。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "選擇目的文字頻道後，確認即可搬遷。只有搬遷成功才會刪除原訊息。",
            view=MessageMigrationView(self, message),
            ephemeral=True,
        )

    async def migrate_message(
        self,
        source_message: discord.Message,
        destination: discord.TextChannel,
        guild: discord.Guild,
    ) -> tuple[bool, str]:
        source_channel = source_message.channel
        if (
            source_message.guild is None
            or source_message.guild.id != guild.id
            or destination.guild.id != guild.id
            or destination.id == source_channel.id
            or not isinstance(
                source_channel,
                (discord.TextChannel, discord.Thread),
            )
        ):
            return False, "來源或目的頻道不符合搬遷條件；原訊息未變更。"
        if not source_message.content:
            return False, "原訊息沒有可搬遷的文字內容。"
        if len(source_message.content) > MAX_MESSAGE_LENGTH:
            return False, "訊息文字超過 Discord 單則訊息長度上限；原訊息未變更。"

        bot_member = guild.me
        if bot_member is None:
            logger.error("Cannot confirm bot permissions for message migration")
            return False, "無法確認機器人權限；原訊息未變更。"

        source_permissions = source_channel.permissions_for(bot_member)
        destination_permissions = destination.permissions_for(bot_member)
        if not source_permissions.manage_messages:
            return False, "機器人在來源頻道缺少「管理訊息」權限；原訊息未變更。"
        if not (
            destination_permissions.view_channel
            and destination_permissions.send_messages
            and destination_permissions.manage_webhooks
        ):
            return (
                False,
                "機器人在目的頻道需要「檢視頻道」、「傳送訊息」及"
                "「管理 Webhook」權限；原訊息未變更。",
            )

        author = source_message.author
        if isinstance(author, discord.Member) and author.guild.id == guild.id:
            author_member = author
        else:
            author_member = guild.get_member(author.id)
            if author_member is None:
                try:
                    author_member = await asyncio.wait_for(
                        guild.fetch_member(author.id),
                        timeout=DISCORD_OPERATION_TIMEOUT,
                    )
                except (
                    asyncio.TimeoutError,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    logger.exception(
                        "Unable to fetch guild member %s for message migration",
                        author.id,
                    )
                    return (
                        False,
                        "無法取得原發言者在伺服器中的暱稱；原訊息未變更。",
                    )

        webhook: discord.Webhook | None = None
        sent_message: discord.WebhookMessage | None = None
        response = "搬遷失敗，原訊息已保留。"
        try:
            webhook = await asyncio.wait_for(
                destination.create_webhook(
                    name="訊息搬遷",
                    reason=f"指定管理員 {UPDATE_OWNER_ID} 搬遷錯頻訊息",
                ),
                timeout=DISCORD_OPERATION_TIMEOUT,
            )
            sent_message = await asyncio.wait_for(
                webhook.send(
                    content=source_message.content,
                    username=author_member.display_name[:80],
                    avatar_url=author_member.display_avatar.url,
                    wait=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=DISCORD_OPERATION_TIMEOUT,
            )
            if (
                sent_message is None
                or sent_message.content != source_message.content
                or sent_message.channel.id != destination.id
            ):
                logger.error(
                    "Message migration verification failed for source message %s",
                    source_message.id,
                )
                response = "目的訊息驗證失敗，原訊息已保留。"
                if sent_message is not None:
                    await self.rollback_destination_message(
                        sent_message,
                        source_message.id,
                    )
                return False, response

            try:
                await asyncio.wait_for(
                    source_message.delete(
                        reason=f"指定管理員 {UPDATE_OWNER_ID} 搬遷錯頻訊息"
                    ),
                    timeout=DISCORD_OPERATION_TIMEOUT,
                )
            except discord.NotFound:
                response = "訊息已搬遷；原訊息已不存在。"
                return True, response
            except discord.Forbidden:
                logger.exception(
                    "Unable to delete source message %s after migration",
                    source_message.id,
                )
                rollback_succeeded = await self.rollback_destination_message(
                    sent_message, source_message.id
                )
                if not rollback_succeeded:
                    return (
                        False,
                        "機器人無法刪除原訊息，且未能撤回目的訊息；"
                        "請確認來源頻道的「管理訊息」權限，並立即"
                        "人工檢查來源與目的頻道。",
                    )
                return (
                    False,
                    "機器人無法刪除來源訊息；原訊息已保留，目的訊息已撤回。"
                    "請確認來源頻道的「管理訊息」權限。",
                )
            except (asyncio.TimeoutError, discord.HTTPException):
                logger.exception(
                    "Unable to confirm deletion of source message %s",
                    source_message.id,
                )
                source_still_exists = await self.source_message_exists(
                    source_channel,
                    source_message.id,
                )
                if source_still_exists is False:
                    return True, "訊息已搬遷，原訊息已刪除。"
                if source_still_exists is True:
                    rollback_succeeded = await self.rollback_destination_message(
                        sent_message, source_message.id
                    )
                    if rollback_succeeded:
                        return (
                            False,
                            "機器人無法刪除來源訊息；原訊息已保留，目的訊息已撤回。"
                            "請確認來源頻道的「管理訊息」權限。",
                        )
                    return (
                        False,
                        "機器人無法刪除原訊息，且未能撤回目的訊息；"
                        "請確認來源頻道的「管理訊息」權限，並立即"
                        "人工檢查來源與目的頻道。",
                    )
                return (
                    False,
                    "無法確認原訊息是否已刪除；為避免遺失，保留目的訊息。"
                    "請檢查來源與目的頻道。",
                )

            response = "訊息已搬遷，原訊息已刪除。"
            return True, response
        except (asyncio.TimeoutError, discord.Forbidden):
            logger.exception(
                "Permission failure migrating message %s",
                source_message.id,
            )
            return False, "搬遷逾時或機器人權限不足；原訊息已保留。"
        except (discord.HTTPException, RuntimeError, TypeError):
            logger.exception(
                "Unable to migrate source message %s",
                source_message.id,
            )
            if sent_message is not None:
                rollback_succeeded = await self.rollback_destination_message(
                    sent_message, source_message.id
                )
                if not rollback_succeeded:
                    return (
                        False,
                        "搬遷失敗且未能撤回目的訊息；原訊息仍保留，"
                        "請立即人工檢查來源與目的頻道。",
                    )
            return False, "搬遷發生錯誤，原訊息已保留。"
        finally:
            if webhook is not None:
                try:
                    await asyncio.wait_for(
                        webhook.delete(reason="訊息搬遷完成"),
                        timeout=WEBHOOK_CLEANUP_TIMEOUT,
                    )
                except discord.NotFound:
                    pass
                except (
                    asyncio.TimeoutError,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    logger.exception(
                        "Unable to delete temporary migration webhook %s",
                        webhook.id,
                    )

    @staticmethod
    async def source_message_exists(
        channel: discord.TextChannel | discord.Thread,
        message_id: int,
    ) -> bool | None:
        try:
            await asyncio.wait_for(
                channel.fetch_message(message_id),
                timeout=WEBHOOK_CLEANUP_TIMEOUT,
            )
        except discord.NotFound:
            return False
        except (
            asyncio.TimeoutError,
            discord.Forbidden,
            discord.HTTPException,
        ):
            logger.exception(
                "Unable to verify whether source message %s still exists",
                message_id,
            )
            return None
        return True

    @staticmethod
    async def rollback_destination_message(
        message: discord.WebhookMessage,
        source_message_id: int,
    ) -> bool:
        try:
            await asyncio.wait_for(
                message.delete(reason="訊息搬遷未完成，撤回目的訊息"),
                timeout=WEBHOOK_CLEANUP_TIMEOUT,
            )
        except discord.NotFound:
            return True
        except (
            asyncio.TimeoutError,
            discord.Forbidden,
            discord.HTTPException,
        ):
            logger.exception(
                "Unable to roll back destination message %s for source %s",
                message.id,
                source_message_id,
            )
            return False
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MessageMigration(bot))
