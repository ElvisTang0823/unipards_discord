from discord.ext import commands


class Sudo(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="sudo")
    async def sudo(self, ctx: commands.Context) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await ctx.send(f":leftwards_hand: :six: :seven:  :rightwards_hand:")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Sudo(bot))
