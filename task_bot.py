import os
import logging
import asyncio
import discord
from discord.ext import commands
import aiohttp
from aiohttp import web
from dotenv import load_dotenv

# Set up logging using standard logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TaskBot")

# Load environment variables from .env file
load_dotenv()

# Setup bot intents: default + message_content + members
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

def apply_global_gateway_lockout(bot: commands.Bot, locked_role_id: int = 1534636469443100692):
    """Patches bot.dispatch to drop messages/interactions from locked-out users at gateway level."""
    original_dispatch = bot.dispatch

    def check_locked(user):
        if not user:
            return False
        if hasattr(user, "roles"):
            for role in user.roles:
                if role.id == locked_role_id or str(role.id) == "1534636469443100692" or role.name in {"Locked Out", "Quarantined", "Unverified"}:
                    return True
        elif isinstance(user, (discord.User, discord.Member)):
            for guild in bot.guilds:
                member = guild.get_member(user.id)
                if member and any(role.id == locked_role_id or str(role.id) == "1534636469443100692" or role.name in {"Locked Out", "Quarantined", "Unverified"} for role in member.roles):
                    return True
        return False

    def patched_dispatch(event_name, /, *args, **kwargs):
        if event_name == "message" and args:
            message = args[0]
            if hasattr(message, "author") and check_locked(message.author):
                content = getattr(message, "content", "")
                if not content.startswith("!verify"):
                    return

        elif event_name == "interaction" and args:
            interaction = args[0]
            cmd_name = getattr(getattr(interaction, "command", None), "name", "").lower()
            qual_name = getattr(getattr(interaction, "command", None), "qualified_name", "").lower()
            custom_id = (interaction.data.get("custom_id") or "") if getattr(interaction, "data", None) else ""

            if cmd_name == "verify" or qual_name.startswith("verify") or custom_id.startswith("verify_") or "admin_override" in qual_name:
                return original_dispatch(event_name, *args, **kwargs)

            user = getattr(interaction, "user", None)
            if check_locked(user):
                async def send_lockout():
                    try:
                        if not interaction.response.is_done():
                            await interaction.response.send_message(
                                "🔒 You are currently **locked out**. Use `/verify` to solve puzzles and regain access.",
                                ephemeral=True,
                            )
                    except Exception:
                        pass
                if bot.loop and bot.loop.is_running():
                    bot.loop.create_task(send_lockout())
                return

        return original_dispatch(event_name, *args, **kwargs)

    bot.dispatch = patched_dispatch

apply_global_gateway_lockout(bot)

# Keep-alive web server route handlers
async def handle_root(request):
    html = """<!DOCTYPE html>
<html>
<head>
    <title>Task Bot Uptime Monitor</title>
</head>
<body style="font-family: sans-serif; text-align: center; padding: 60px; background: #1e1f22; color: #fff;">
    <h1>🤖 Valence Task Bot — Active</h1>
    <p>Keep-alive web server is running successfully.</p>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")

async def handle_health(request):
    return web.json_response({"status": "ok"})

_keepalive_started = False

async def start_keepalive_server():
    """Starts a separate aiohttp web server for uptime monitoring."""
    global _keepalive_started
    if _keepalive_started:
        return None
    _keepalive_started = True
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", "8081"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    try:
        await site.start()
        logger.info(f"Keep-alive web server started on port {port}")
    except Exception as e:
        logger.error(f"Failed to start keep-alive web server on port {port}: {e}", exc_info=True)
    return runner

async def load_extensions():
    """Dynamically loads cogs from the task_cogs directory."""
    cog_dir = "task_cogs"
    os.makedirs(cog_dir, exist_ok=True)
    
    # Load all Python files in task_cogs/ as extensions, excluding __init__.py
    for filename in os.listdir(cog_dir):
        if filename.endswith(".py") and filename != "__init__.py":
            cog_name = f"task_cogs.{filename[:-3]}"
            if cog_name in bot.extensions:
                continue
            try:
                await bot.load_extension(cog_name)
                logger.info(f"Loaded task bot extension: {cog_name}")
            except Exception as e:
                logger.error(f"Failed to load extension {cog_name}: {e}", exc_info=True)

@bot.event
async def setup_hook():
    """Performs async bot initialization: loading cogs and syncing slash commands."""
    await load_extensions()
    
    try:
        guild_id = int(os.getenv("GUILD_ID", "1514186381348306964"))
        target_guild = discord.Object(id=guild_id)
        
        # Copy global commands to the guild so it gets registered to the guild.
        bot.tree.copy_global_to(guild=target_guild)
        synced = await bot.tree.sync(guild=target_guild)
        logger.info(f"Successfully synced {len(synced)} command(s) to target guild (ID: {guild_id})")
    except Exception as e:
        logger.error(f"Error during slash command synchronization in setup_hook: {e}", exc_info=True)


VALENCE_ID = 1149959648938954752
UJJWAL_ID = 988358245595201536
LOCKOUT_ROLE_ID = 1534636469443100692
LOCKOUT_ROLE_NAMES = {"Locked Out", "Quarantined", "Unverified"}

from discord import app_commands


def is_user_locked_out(user: discord.User | discord.Member, udata: dict = None, guild: discord.Guild = None) -> bool:
    """Returns True if the user has Role ID 1534636469443100692, any lockout role name, or quarantined=True in DB."""
    if not user:
        return False

    member = user
    if guild and not isinstance(user, discord.Member):
        member = guild.get_member(user.id) or user

    roles = getattr(member, "roles", [])
    for r in roles:
        r_name_lower = r.name.lower()
        if (
            r.id == LOCKOUT_ROLE_ID
            or str(r.id) == "1534636469443100692"
            or "locked out" in r_name_lower
            or "quarantine" in r_name_lower
            or "unverified" in r_name_lower
            or r.name in LOCKOUT_ROLE_NAMES
        ):
            return True

    if udata and udata.get("quarantined", False):
        return True

    return False


@bot.tree.interaction_check
async def global_task_bot_lockout_check(interaction: discord.Interaction) -> bool:
    """Block all slash commands for quarantined/locked-out users except /verify."""
    if not interaction.command:
        return True

    cmd_name = getattr(interaction.command, "name", "").lower()
    qual_name = getattr(interaction.command, "qualified_name", "").lower()

    if cmd_name == "verify" or qual_name.startswith("verify") or "admin_override" in qual_name:
        return True

    user = interaction.user
    if not user:
        return True

    # 1. Fast Role check first
    if is_user_locked_out(user, None, guild=interaction.guild):
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message(
                    "🔒 You are currently **locked out**. Use `/verify` to solve puzzles and regain access.",
                    ephemeral=True,
                )
            except Exception:
                pass
        return False

    # 2. Check DB
    udata = {}
    try:
        from task_db import load_study_data
        data = await load_study_data()
        uid = str(user.id)
        udata = data.get("users", {}).get(uid, {})
    except Exception as e:
        logger.warning(f"Task bot lockout DB check error: {e}")

    if udata.get("quarantined", False):
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message(
                    "🔒 You are currently **locked out**. Use `/verify` to solve puzzles and regain access.",
                    ephemeral=True,
                )
            except Exception:
                pass
        return False

    return True


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Global interaction handler blocking non-verify button/component clicks on Task Bot for locked-out users."""
    if interaction.type == discord.InteractionType.component:
        user = interaction.user
        if user:
            custom_id = (interaction.data.get("custom_id") or "") if interaction.data else ""
            if not custom_id.startswith("verify_"):
                udata = {}
                try:
                    from task_db import load_study_data
                    data = await load_study_data()
                    uid = str(user.id)
                    udata = data.get("users", {}).get(uid, {})
                except Exception:
                    pass

                if is_user_locked_out(user, udata, guild=interaction.guild):
                    if not interaction.response.is_done():
                        try:
                            await interaction.response.send_message(
                                "🔒 You are currently **locked out**. Use `/verify` to solve puzzles and regain access.",
                                ephemeral=True,
                            )
                        except Exception:
                            pass
                    return


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return
    logger.error(f"Task Bot App command error: {error}", exc_info=True)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message("⚠️ An error occurred while executing this command.", ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_ready():
    """Triggers when the bot client is ready."""
    logger.info(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    
    # Reset connection retry delay back to 5 seconds upon a successful gateway connection
    if hasattr(bot, "retry_delay"):
        bot.retry_delay = 5
        logger.info("Gateway connection retry delay has been reset to 5s.")
        
    logger.info("Task Bot is fully initialized and operational.")

# --- Channel Moderation Logic ---
LEADERBOARD_CHANNEL_ID = int(os.getenv("LEADERBOARD_CHANNEL_ID", "1514208164071870514"))
CELEBRATION_CHANNEL_ID = int(os.getenv("CELEBRATION_CHANNEL_ID", "1514208252760424591"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1514208220946763807"))

MODERATED_CHANNELS = {
    LEADERBOARD_CHANNEL_ID: "leaderboard",
    CELEBRATION_CHANNEL_ID: "celebration",
    LOG_CHANNEL_ID: "study-logs"
}
YPT_BOT_ID = int(os.getenv("YPT_BOT_ID", "1517449638091689985"))

report_users_str = os.getenv("REPORT_USERS", "856485470171299891,1403716456025165864")
REPORT_USERS = [int(uid.strip()) for uid in report_users_str.split(",") if uid.strip()]

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return

    # Process commands first
    await bot.process_commands(message)

    # Moderate designated channels
    if message.channel.id in MODERATED_CHANNELS:
        # Only the YPT bot is allowed to send messages in these channels
        if message.author.id != YPT_BOT_ID:
            channel_name = MODERATED_CHANNELS[message.channel.id]
            logger.warning(f"Intercepted message from non-YPT user {message.author} (ID: {message.author.id}) in channel #{channel_name}")
            
            author_info = f"{message.author} (ID: {message.author.id})"
            channel_info = f"#{channel_name} (ID: {message.channel.id})"
            content = message.content or "[Attachment/Embed only]"
            timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            
            # Delete the message
            try:
                await message.delete()
                logger.info(f"Deleted message successfully.")
            except Exception as e:
                logger.error(f"Failed to delete message in moderated channel: {e}")
                
            # Report the deletion via DM (looking up local cache first)
            for r_user_id in REPORT_USERS:
                try:
                    user = bot.get_user(r_user_id)
                    if not user:
                        user = await bot.fetch_user(r_user_id)
                    if user:
                        embed = discord.Embed(
                            title="🚨 Task Bot Moderation: Message Deleted",
                            description="A message was deleted because only the YPT Bot is permitted to post in this channel.",
                            color=discord.Color.red()
                        )
                        embed.add_field(name="Sender", value=author_info, inline=False)
                        embed.add_field(name="Channel", value=channel_info, inline=True)
                        embed.add_field(name="Timestamp", value=timestamp, inline=True)
                        embed.add_field(name="Message Content", value=content, inline=False)
                        
                        await user.send(embed=embed)
                        logger.info(f"Report DM sent to user {r_user_id}.")
                except Exception as e:
                    logger.error(f"Could not send DM report to user {r_user_id}: {e}")

async def main():
    token = os.getenv("TASK_BOT_TOKEN")
    if not token:
        logger.critical("TASK_BOT_TOKEN is not set in environment variables. Exiting.")
        return

    # Start keep-alive web server before starting bot connection attempts
    bot.keepalive_runner = await start_keepalive_server()

    bot.retry_delay = 5  # Initial backoff delay in seconds
    max_delay = 300  # Maximum backoff delay (5 minutes)

    async with bot:
        while True:
            try:
                logger.info("Attempting to connect task bot to Discord gateway...")
                await bot.start(token)
                # If bot.start completes cleanly, break loop
                break
            except (discord.LoginFailure, discord.PrivilegedIntentsRequired) as e:
                logger.critical(f"Unrecoverable error in task bot main: {e}. Exiting.", exc_info=True)
                raise
            except (discord.HTTPException, aiohttp.ClientResponseError) as e:
                status = getattr(e, "status", None)
                # Cloudflare Error 1015 also returns HTTP 429
                if status == 429 or "429" in str(e) or "1015" in str(e):
                    logger.warning(
                        f"Discord gateway rate limit hit (HTTP 429 / Error 1015). "
                        f"Retrying in {bot.retry_delay} seconds... Error: {e}"
                    )
                else:
                    logger.warning(
                        f"Discord connection failed with HTTP exception: {e}. "
                        f"Retrying in {bot.retry_delay} seconds..."
                    )
                await asyncio.sleep(bot.retry_delay)
                bot.retry_delay = min(bot.retry_delay * 2, max_delay)
            except Exception as e:
                logger.warning(
                    f"Unexpected connection/network error in task bot main: {e}. "
                    f"Retrying in {bot.retry_delay} seconds...",
                    exc_info=True
                )
                await asyncio.sleep(bot.retry_delay)
                bot.retry_delay = min(bot.retry_delay * 2, max_delay)

# Entry point
if __name__ == "__main__":
    token = os.getenv("TASK_BOT_TOKEN")
    if not token:
        logger.critical("TASK_BOT_TOKEN is not set in environment variables. Exiting.")
    else:
        logger.info("Starting Task Bot...")
        asyncio.run(main())
