import asyncio
import logging
import time

from aiohttp import web

from app import db, crypto, config
from app.bot import TenantBot
from app.ai import GeminiClient

log = logging.getLogger("manager")


class BotManager:
    def __init__(self):
        self.bots: dict = {}  # user_id -> TenantBot
        self._last_purge = 0
        self._last_followup = 0
        self._last_summary = 0

    async def sync(self):
        """Start runners for newly-connected connections, stop removed ones."""
        try:
            conns = await db.get_connected_connections()
        except Exception as e:
            log.error(f"sync db error: {e}")
            return
        current = set()
        for t in conns:
            cid = t["id"]            # connection id (a user may have user + bot)
            current.add(cid)
            if cid in self.bots:
                continue
            try:
                mode = t.get("mode") or "user"
                if mode == "bot":
                    from app.ptbbot import PTBBot
                    runner = PTBBot(t["user_id"], crypto.decrypt(t["bot_token_enc"]))
                else:
                    runner = TenantBot(
                        t["user_id"],
                        mode="user",
                        api_id=t["api_id"],
                        api_hash=crypto.decrypt(t["api_hash_enc"]),
                        session_string=crypto.decrypt(t["session_string_enc"]),
                    )
                ok = await runner.start()
                if ok:
                    self.bots[cid] = runner
            except Exception as e:
                log.error(f"start connection {cid} failed: {e}")

        # stop connections no longer present
        for cid in list(self.bots.keys()):
            if cid not in current:
                await self.bots[cid].stop()
                del self.bots[cid]
                log.info(f"connection {cid} stopped (disconnected)")

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

            # run follow-up automation periodically
            if time.time() - self._last_followup > config.FOLLOWUP_INTERVAL:
                self._last_followup = time.time()
                for bot in list(self.bots.values()):
                    await bot.run_followups()

            # build persistent per-customer summaries ~once a day
            if time.time() - self._last_summary > 86400:
                self._last_summary = time.time()
                await self.run_summaries()

    async def run_summaries(self):
        """Summarise recent conversations per customer so the bot stays
        consistent even after raw messages are purged (7 days)."""
        from datetime import datetime, timezone
        seen = set()
        for runner in list(self.bots.values()):
            uid = runner.user_id
            if uid in seen:
                continue
            seen.add(uid)
            try:
                key_rows = await db.get_gemini_keys_for(uid)
                if not key_rows:
                    continue
                gem = GeminiClient([{"id": r["id"], "key": crypto.decrypt(r["key_enc"]), "source": r["source"]} for r in key_rows])
                customers = await db.get_customers(uid)
                now = datetime.now(timezone.utc)
                count = 0
                for c in customers:
                    if count >= 50:  # cap per user per day
                        break
                    last = c.get("last_msg_at")
                    if not last or (now - last).total_seconds() > 2 * 86400:
                        continue  # only recently-active customers
                    msgs = await db.recent_messages(uid, c["customer_id"], 30)
                    if len(msgs) < 4:
                        continue
                    transcript = "\n".join(f'{m["role"]}: {m["content"]}' for m in msgs)
                    summary = await gem.summarize(transcript)
                    if summary:
                        await db.upsert_customer_profile(uid, c["customer_id"], summary)
                        count += 1
            except Exception as e:
                log.warning(f"summary error for {uid}: {e}")


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
