from __future__ import annotations

import logging

import discord
from discord.ext import commands

from cogs._config import required_snowflake, required_snowflake_list
from cogs._discord import get_text_channel

REQUEST_CHANNEL_ID = required_snowflake("ROLE_REQUEST_CHANNEL_ID")
NOTIFY_ROLE_CARD_FOOTER = "UNIPARDS_NOTIFY_ROLE_PANEL"
NOTIFY_ROLE_IDS = required_snowflake_list("NOTIFY_ROLE_ID")
NOTIFY_ROLE_EMOJI_IDS = required_snowflake_list("NOTIFY_ROLE_EMOJI_ID")
NOTIFY_ROLE_LABELS = (
    "TeamUpdate推播通知",
    "FRCblog推播推知",
    "TBA推播通知",
    "Bot更新推播通知",
)
NOTIFY_ROLE_EMOJI_NAMES = ("TU", "FB", "TBA", "BOT")
if len(NOTIFY_ROLE_IDS) < len(NOTIFY_ROLE_LABELS):
    raise RuntimeError("NOTIFY_ROLE_ID must contain at least four Discord role IDs")
if len(NOTIFY_ROLE_EMOJI_IDS) < len(NOTIFY_ROLE_LABELS):
    raise RuntimeError(
        "NOTIFY_ROLE_EMOJI_ID must contain at least four Discord emoji IDs"
    )
NOTIFY_ROLE_OPTIONS = tuple(
    (
        discord.PartialEmoji(name=emoji_name, id=emoji_id),
        role_id,
        label,
    )
    for emoji_name, emoji_id, role_id, label in zip(
        NOTIFY_ROLE_EMOJI_NAMES,
        NOTIFY_ROLE_EMOJI_IDS,
        NOTIFY_ROLE_IDS,
        NOTIFY_ROLE_LABELS,
    )
)
logger = logging.getLogger(__name__)


class NotifyRole(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        channel = await get_text_channel(self.bot, REQUEST_CHANNEL_ID)
        if self.bot.user is None:
            raise RuntimeError("Bot user is unavailable while loading notify role cog")

        try:
            notify_role_card: discord.Message | None = None
            async for message in channel.history(limit=None):
                if message.author.id == self.bot.user.id and any(
                    embed.footer.text == NOTIFY_ROLE_CARD_FOOTER
                    for embed in message.embeds
                ):
                    notify_role_card = message
                    break

            if notify_role_card is None:
                embed = discord.Embed(
                    title="通知推播身分組領取",
                    description="\n".join(
                        f"{emoji} — **{label}**"
                        for emoji, _, label in NOTIFY_ROLE_OPTIONS
                    ),
                    color=discord.Color.blurple(),
                )
                embed.set_footer(text=NOTIFY_ROLE_CARD_FOOTER)
                notify_role_card = await channel.send(embed=embed)
                logger.info(
                    "Published notification role card to channel %s",
                    REQUEST_CHANNEL_ID,
                )
            else:
                logger.info(
                    "Notification role card already exists in channel %s",
                    REQUEST_CHANNEL_ID,
                )

            existing_emoji_ids = {
                reaction.emoji.id
                for reaction in notify_role_card.reactions
                if isinstance(reaction.emoji, discord.PartialEmoji)
            }
            for emoji, _, _ in NOTIFY_ROLE_OPTIONS:
                if emoji.id not in existing_emoji_ids:
                    await notify_role_card.add_reaction(emoji)
        except discord.HTTPException:
            logger.exception(
                "Failed to check or publish notification role card "
                "in channel %s",
                REQUEST_CHANNEL_ID,
            )
            raise

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        await self.process_notify_role_reaction(payload, added=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        await self.process_notify_role_reaction(payload, added=False)

    async def process_notify_role_reaction(
        self,
        payload: discord.RawReactionActionEvent,
        added: bool,
    ) -> None:
        if payload.channel_id != REQUEST_CHANNEL_ID or payload.emoji.id is None:
            return
        if self.bot.user is not None and payload.user_id == self.bot.user.id:
            return

        selected_option = next(
            (
                option
                for option in NOTIFY_ROLE_OPTIONS
                if option[0].id == payload.emoji.id
            ),
            None,
        )
        if selected_option is None:
            return

        _, role_id, _ = selected_option
        try:
            channel = await get_text_channel(self.bot, payload.channel_id)
            message = await channel.fetch_message(payload.message_id)
            if (
                self.bot.user is None
                or message.author.id != self.bot.user.id
                or not any(
                    embed.footer.text == NOTIFY_ROLE_CARD_FOOTER
                    for embed in message.embeds
                )
            ):
                return

            guild = message.guild
            if guild is None:
                return
            role = guild.get_role(role_id)
            if role is None:
                logger.error(
                    "Notification role %s is not present in guild %s",
                    role_id,
                    guild.id,
                )
                return

            member = payload.member or guild.get_member(payload.user_id)
            if member is None:
                member = await guild.fetch_member(payload.user_id)
            if member.bot:
                return

            if added and role not in member.roles:
                await member.add_roles(role, reason="Member selected notification role")
            elif not added and role in member.roles:
                await member.remove_roles(
                    role,
                    reason="Member removed notification reaction",
                )
        except discord.HTTPException:
            logger.exception(
                "Failed to %s notification role %s for user %s",
                "add" if added else "remove",
                role_id,
                payload.user_id,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NotifyRole(bot))
