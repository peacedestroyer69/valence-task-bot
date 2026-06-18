import os
import logging
import time
import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger("TaskBot.Ping")


class Ping(commands.Cog):
    __cog_app_commands_guilds__ = [int(os.getenv("GUILD_ID", "1514186381348306964"))]

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.start_time = time.time()

    @app_commands.command(name="ping", description="Check bot latency and uptime")
    async def ping(self, interaction: discord.Interaction):
        latency_ms = round(self.bot.latency * 1000)
        uptime_secs = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(remainder, 60)

        embed = discord.Embed(
            title="🏓 Pong!",
            color=0x57F287 if latency_ms < 200 else 0xFEE75C if latency_ms < 500 else 0xED4245,
        )
        embed.add_field(name="📡 Latency", value=f"**{latency_ms}ms**", inline=True)
        embed.add_field(
            name="⏱️ Uptime",
            value=f"**{hours}h {minutes}m {seconds}s**",
            inline=True,
        )
        embed.set_footer(text="Valence Task Bot • Operational")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Ping(bot))
