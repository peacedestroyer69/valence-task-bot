import os
import logging
import discord
from discord.ext import commands
from typing import Literal, Optional

logger = logging.getLogger("TaskBot.sync")


class SyncCog(commands.Cog):
    """Owner-only prefix command for safely syncing/clearing slash commands.

    Usage (prefix commands — no registration needed):
        !sync              → Copy global commands to the current guild and sync (instant).
        !sync global       → Sync all commands globally (up to 1 hr propagation).
        !sync clear_global → Wipe all global commands from Discord.
        !sync clear_guild  → Wipe all guild-level commands from Discord.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync_commands(
        self,
        ctx: commands.Context,
        spec: Optional[Literal["global", "guild", "clear_global", "clear_guild", "copy_global"]] = None,
    ):
        guild = ctx.guild
        if not guild:
            await ctx.send("❌ Run this inside a server, not DMs.")
            return

        async with ctx.typing():
            try:
                if spec is None:
                    # Default: copy global tree → guild, then sync to guild (instant)
                    self.bot.tree.copy_global_to(guild=guild)
                    synced = await self.bot.tree.sync(guild=guild)
                    await ctx.send(f"✅ Synced **{len(synced)}** command(s) to this guild.")
                    logger.info(f"Guild sync: {len(synced)} cmds → {guild.id} by {ctx.author}")

                elif spec == "global":
                    synced = await self.bot.tree.sync()
                    await ctx.send(f"🌍 Synced **{len(synced)}** command(s) globally (up to 1 hr).")
                    logger.info(f"Global sync: {len(synced)} cmds by {ctx.author}")

                elif spec == "clear_global":
                    self.bot.tree.clear_commands(guild=None)
                    await self.bot.tree.sync()
                    await ctx.send("🧹 Cleared all **global** commands.")
                    logger.info(f"Global commands cleared by {ctx.author}")

                elif spec == "clear_guild":
                    self.bot.tree.clear_commands(guild=guild)
                    await self.bot.tree.sync(guild=guild)
                    await ctx.send("🧹 Cleared all **guild** commands for this server.")
                    logger.info(f"Guild commands cleared for {guild.id} by {ctx.author}")

                elif spec == "copy_global":
                    self.bot.tree.copy_global_to(guild=guild)
                    synced = await self.bot.tree.sync(guild=guild)
                    await ctx.send(f"✅ Copied global → guild, synced **{len(synced)}** command(s).")
                    logger.info(f"copy_global sync: {len(synced)} cmds → {guild.id} by {ctx.author}")

            except discord.HTTPException as e:
                await ctx.send(f"❌ Discord API error: `{e}`")
                logger.error(f"Sync HTTP error: {e}", exc_info=True)
            except Exception as e:
                await ctx.send(f"❌ Unexpected error: `{e}`")
                logger.error(f"Sync error: {e}", exc_info=True)

    @sync_commands.error
    async def sync_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            await ctx.send("🚫 Only the bot owner can use `!sync`.")
        else:
            await ctx.send(f"⚠️ Error: `{error}`")
            logger.error(f"Sync command error: {error}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SyncCog(bot))
    logger.info("[SYNC] Loaded Sync Cog Extension.")
