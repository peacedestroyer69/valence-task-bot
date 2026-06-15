import logging
import discord
from discord.ext import commands
from discord import app_commands

logger = logging.getLogger("TaskBot.Ping")

class Ping(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Ping command to test the Task Bot")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message("Pong! Task Bot is active and running.")

async def setup(bot: commands.Bot):
    await bot.add_cog(Ping(bot))
