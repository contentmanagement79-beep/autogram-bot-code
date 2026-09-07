import asyncio
import logging
import time

from aiohttp import web

from app import db, crypto, config
from app.bot import TenantBot

log = logging.getLogger("manager")


class BotManager:
    def __init__(self):
        self.bots: dict = {}  # user_id -> TenantBot
        self._last_purge = 0

    async def sync(self):
        """Start bots for newly-connected tenants, stop removed ones."""
        try:
            tenants = await db.get_connected_tenants()
        except Exception as e:
            log.error(f"sync db error: {e}")
            return
        current = set()
        for t in tenants:
            uid = t["user_id"]
            current.add(uid)
            if uid in self.bots:
                continue
            try:
                bot = TenantBot(
                    uid,
                    t["api_id"],
                    crypto.decrypt(t["api_hash_enc"]),
                    crypto.decrypt(t["session_string_enc"]),
                )
                ok = await bot.start()
                if ok:
                    self.bots[uid] = bot
            except Exception as e:
                log.error(f"start bot {uid} failed: {e}")

        # stop tenants no longer connected
        for uid in list(self.bots.keys()):
            if uid not in current:
                await self.bots[uid].stop()
                del self.bots[uid]
                log.info(f"[{uid}] bot stopped (disconnected)")

    async def loop(self):
        await db.init_pool()
        await self.sync()
        while True:
            await asyncio.sleep(config.SYNC_INTERVAL)
            await self.sync()
            # purge old memory roughly once a day
            if time.time() - self._last_purge > 86400:
                try:
                    await db.purge_old_messages()
                    self._last_purge = time.time()
                    log.info("purged old messages")
                except Exception as e:
                    log.warning(f"purge error: {e}")


# ── keep-alive web server (Render needs a bound port) ──────
async def _health(_req):
    return web.json_response({"ok": True, "service": "autogram-engine"})


async def start_web(manager: BotManager):
    from app.api import setup_internal_routes
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    setup_internal_routes(app, manager)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info(f"health server on :{config.PORT}")


async def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    manager = BotManager()
    await start_web(manager)
    await manager.loop()
