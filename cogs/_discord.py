from __future__ import annotations

import discord


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
