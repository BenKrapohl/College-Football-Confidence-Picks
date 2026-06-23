import discord
from discord.ext import commands
import logging
import os
from config import DISCORD_TOKEN, GUILD_ID, SEASON_YEAR, validate_config
from database import init_db, get_active_season, get_db

os.makedirs("data", exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/cfcp_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("cfcp_bot")

# ── Intents ───────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.guilds         = True
intents.guild_messages = True
intents.members        = True
intents.message_content = True


class CFCPBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!cfcp ",
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self):
        init_db()
        self._ensure_season()

        cog_files = [
            "cogs.admin",
            "cogs.picks",
            "cogs.scoring",
            "cogs.notifications",
            "cogs.stats",
            "cogs.setup",
        ]
        for cog in cog_files:
            try:
                await self.load_extension(cog)
                log.info(f"Loaded cog: {cog}")
            except Exception as exc:
                log.error(f"Failed to load cog {cog}: {exc}", exc_info=True)
                
        # Auto-sync removed to avoid Discord rate limiting.
        # Run !cfcp sync in Discord to manually update slash commands.

    @commands.command(name="sync")
    @commands.has_permissions(administrator=True)
    async def sync_tree(self, ctx: commands.Context):
        """Manually sync slash commands (Admin only)"""
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            await ctx.send(f"✅ Slash commands synced to guild {GUILD_ID}.")
        else:
            await self.tree.sync()
            await ctx.send("✅ Slash commands synced globally.")

    def _ensure_season(self):
        season = get_active_season()
        if not season:
            with get_db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO seasons(year, poll_type, is_active) VALUES (?,?,1)",
                    (SEASON_YEAR, "ap"),
                )
            log.info(f"Created {SEASON_YEAR} season record.")

    async def on_ready(self):
        assert self.user is not None
        log.info(f"Logged in as {self.user} ({self.user.id})")
        guild = self.get_guild(GUILD_ID)
        if not guild:
            log.error(f"Guild {GUILD_ID} not found — check GUILD_ID in .env")
            return

        from cogs.setup import setup_channels
        await setup_channels(self, guild)

        self._register_persistent_views()
        log.info("CFCP Bot is ready.")

    def _register_persistent_views(self):
        from cogs.admin import AdminPanelView
        from cogs.picks import PicksHubView
        self.add_view(AdminPanelView())
        self.add_view(PicksHubView())

    async def on_guild_channel_pins_update(self, channel, last_pin):
        pass  


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    errors = validate_config()
    if errors:
        for err in errors:
            log.critical(f"Config error: {err}")
        log.critical("Fix the above errors in your .env file before starting.")
        return

    bot = CFCPBot()
    bot.run(DISCORD_TOKEN, log_handler=None)

if __name__ == "__main__":
    main()