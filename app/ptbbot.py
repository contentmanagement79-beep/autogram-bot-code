import asyncio
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from app import db, crypto, config
from app.ai import GeminiClient
from app.prompt import build_system_prompt
from app.integration import call_integration
from app import voice as voicelib

log = logging.getLogger("ptbbot")
DOWNLOADS = "downloads"
Path(DOWNLOADS).mkdir(exist_ok=True)


class PTBBot:
    """Bot-mode adapter using python-telegram-bot (no api_id/hash needed).
    Reuses the exact same brain as the Telethon bot."""

    mode = "bot"

    def __init__(self, user_id, token):
        self.user_id = user_id
        self.token = token
        self.app = None
        self._cfg = None
        self._cfg_at = 0
        self._buffers = {}
        self._last_key_alert = 0
        self._away_sent = {}
        self._media_cooldown = {}

    async def config(self):
        if self._cfg and time.time() - self._cfg_at < 60:
            return self._cfg
        persona = await db.get_persona(self.user_id)
        products = await db.get_products(self.user_id)
        key_rows = await db.get_gemini_keys_for(self.user_id)
        keys = [{"id": r["id"], "key": crypto.decrypt(r["key_enc"]), "source": r["source"]} for r in key_rows]
        access = await db.get_access(self.user_id)
        self._cfg = {"persona": persona, "products": products, "gemini": GeminiClient(keys), "access": access}
        self._cfg_at = time.time()
        return self._cfg

    async def start(self):
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(MessageHandler(filters.ChatType.PRIVATE, self._on_message))
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info(f"[{self.user_id}] bot started (bot mode / PTB)")
        return True

    async def stop(self):
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception:
            pass

    # ── incoming ──
    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            msg = update.effective_message
            if not msg:
                return
            customer_id = update.effective_user.id
            text = (msg.text or msg.caption or "").strip()
            has_media = bool(msg.photo or msg.voice or msg.audio or msg.document or msg.video)
            if not text and not has_media:
                return

            cfg = await self.config()
            persona = cfg["persona"]
            if not persona:
                return

            await db.add_message(self.user_id, customer_id, "user", text or "[sent media]")
            try:
                await db.touch_customer(self.user_id, customer_id, via="bot")
                await db.clear_followup_sends(self.user_id, customer_id)
            except Exception:
                pass

            if not await db.is_bot_running(self.user_id):
                return
            if await db.is_customer_paused(self.user_id, customer_id):
                return

            # business hours
            if persona.get("hours_enabled"):
                local_hour = (datetime.now(timezone.utc) + timedelta(hours=int(persona.get("tz_offset", 0) or 0))).hour
                start = int(persona.get("hours_start", 0) or 0)
                end = int(persona.get("hours_end", 24) or 24)
                within = (start <= local_hour < end) if start <= end else (local_hour >= start or local_hour < end)
                if not within:
                    away = (persona.get("away_message") or "").strip()
                    if away and time.time() - self._away_sent.get(customer_id, 0) > 7200:
                        self._away_sent[customer_id] = time.time()
                        try:
                            await self.app.bot.send_message(customer_id, away)
                        except Exception:
                            pass
                    return

            # instant keyword media — if matched, send ONLY the media (no AI reply)
            if text and cfg["access"].get("media", True):
                try:
                    if await self._maybe_send_media(customer_id, text):
                        return
                except Exception as e:
                    log.warning(f"media send error: {e}")

            gemini: GeminiClient = cfg["gemini"]
            if not gemini.keys:
                await self._alert_no_keys()
                return

            media_ctx = ""
            if has_media:
                media_ctx = await self._analyze(msg, context, gemini, text)

            buf = self._buffers.setdefault(customer_id, {"texts": [], "media": [], "count": 0, "task": None})
            if text:
                buf["texts"].append(text)
            if media_ctx:
                buf["media"].append(media_ctx)
            buf["count"] += 1
            if buf["task"]:
                buf["task"].cancel()
            buf["task"] = asyncio.create_task(self._flush(customer_id))
        except Exception as e:
            log.exception(f"[{self.user_id}] ptb handler error: {e}")

    async def _flush(self, customer_id):
        try:
            await asyncio.sleep(config.DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        buf = self._buffers.pop(customer_id, None)
        if not buf:
            return
        cfg = await self.config()
        persona = cfg["persona"]
        gemini: GeminiClient = cfg["gemini"]
        if not persona:
            return
        if not gemini.keys:
            await self._alert_no_keys()
            return

        parts = list(buf["texts"])
        for m in buf["media"]:
            parts.append(f"[media: {m}]")
        enriched = "\n".join(parts).strip() or "[sent media]"

        try:
            integ = await db.get_integration(self.user_id)
            if integ and integ.get("enabled") and integ.get("api_url") and cfg["access"].get("integration", True):
                live = await call_integration(integ, " ".join(buf["texts"]))
                if live:
                    enriched += f"\n[live data: {live}]"
        except Exception as e:
            log.warning(f"integration error: {e}")

        system_prompt = build_system_prompt(persona, cfg["products"])
        try:
            profile = await db.get_customer_profile(self.user_id, customer_id)
            if profile:
                system_prompt += f"\n\nWHAT YOU ALREADY KNOW ABOUT THIS CUSTOMER (from earlier chats):\n{profile}"
        except Exception:
            pass
        history = await db.recent_messages(self.user_id, customer_id, config.MEMORY_TURNS)
        n = buf["count"]
        ctx = history[:-n] if 0 < n <= len(history) else (history if n == 0 else [])
        reply = await gemini.reply(system_prompt, ctx, enriched)
        if not reply:
            if not gemini.keys:
                await self._alert_no_keys()
            return

        await db.add_message(self.user_id, customer_id, "assistant", reply)

        want_voice = persona.get("voice_enabled", True) and cfg["access"].get("voice", True) and any(voicelib.wants_voice(t) for t in buf["texts"])
        if want_voice:
            path = await voicelib.tts(reply, persona.get("voice_name", "en-US-JennyNeural"), self.user_id)
            if path:
                try:
                    with open(path, "rb") as f:
                        await self.app.bot.send_voice(customer_id, voice=f)
                    os.remove(path)
                    return
                except Exception as e:
                    log.warning(f"voice send failed: {e}")

        try:
            await self.app.bot.send_chat_action(customer_id, "typing")
            await self.app.bot.send_message(customer_id, reply)
        except Exception as e:
            log.warning(f"send failed: {e}")

    # ── media ──
    async def _maybe_send_media(self, customer_id, customer_text) -> bool:
        text = (customer_text or "").lower()
        if not text:
            return False
        items = await db.get_media_items(self.user_id)
        for it in items:
            kw = (it.get("keyword") or "").strip().lower()
            if not kw:
                continue
            words = [w.strip() for w in kw.split(",") if w.strip()]
            if any(w in text for w in words):
                ck = (customer_id, it.get("name"))
                if time.time() - self._media_cooldown.get(ck, 0) < 60:
                    return True
                self._media_cooldown[ck] = time.time()
                await self._send_media(customer_id, it)
                return True
        return False

    async def _send_media(self, customer_id, item):
        url = item["url"]
        if item.get("blur") and item.get("kind") == "image":
            url = url.replace("/upload/", "/upload/e_blur:1500/", 1)
        caption = item.get("caption") or None
        spoiler = bool(item.get("spoiler"))
        try:
            if item.get("kind") == "video":
                await self.app.bot.send_video(customer_id, video=url, caption=caption, has_spoiler=spoiler)
            else:
                await self.app.bot.send_photo(customer_id, photo=url, caption=caption, has_spoiler=spoiler)
        except Exception as e:
            log.warning(f"media send failed: {e}")

    async def _analyze(self, msg, context, gemini, text):
        ctx = f'Customer wrote: "{text}"' if text else ""
        try:
            if msg.photo:
                f = await context.bot.get_file(msg.photo[-1].file_id)
                path = os.path.join(DOWNLOADS, f"{int(time.time()*1000)}_p.jpg")
                await f.download_to_drive(path)
                data = Path(path).read_bytes()
                _rm(path)
                return await gemini.see_image(data, ctx) or "customer sent a photo"
            if msg.voice or msg.audio:
                fid = (msg.voice or msg.audio).file_id
                f = await context.bot.get_file(fid)
                path = os.path.join(DOWNLOADS, f"{int(time.time()*1000)}_v.ogg")
                await f.download_to_drive(path)
                r = await gemini.hear_audio(path, ctx)
                _rm(path)
                return r or "customer sent a voice message"
            if msg.document:
                f = await context.bot.get_file(msg.document.file_id)
                ext = os.path.splitext(msg.document.file_name or "file.bin")[1] or ".bin"
                path = os.path.join(DOWNLOADS, f"{int(time.time()*1000)}_d{ext}")
                await f.download_to_drive(path)
                content = voicelib.extract_document_text(path)
                _rm(path)
                return f"customer shared a document. Content preview: {content[:800]}" if content else "customer shared a document"
        except Exception as e:
            log.warning(f"ptb media analyze error: {e}")
        return ""

    async def _alert_no_keys(self):
        # Bots can't message the owner's Saved Messages; the dashboard shows key status.
        if time.time() - self._last_key_alert < 3600:
            return
        self._last_key_alert = time.time()
        log.warning(f"[{self.user_id}] (bot) no working AI keys")

    # ── follow-ups (bot-mode customers only) ──
    async def run_followups(self):
        try:
            cfg = await self.config()
            if not cfg["access"].get("followups", True):
                return
            followups = await db.get_enabled_followups(self.user_id)
            if not followups:
                return
            customers = await db.get_customers(self.user_id)
            if not customers:
                return
            sent = await db.get_followup_sends(self.user_id)
            now = datetime.now(timezone.utc)
            for c in customers:
                if (c.get("via") or "user") != "bot":
                    continue
                last = c.get("last_msg_at")
                if not last:
                    continue
                idle_days = (now - last).total_seconds() / 86400.0
                for f in followups:
                    if idle_days >= f["delay_days"] and (c["customer_id"], f["id"]) not in sent:
                        await self._send_followup(c["customer_id"], f)
                        await db.record_followup_send(self.user_id, c["customer_id"], f["id"])
                        break
        except Exception as e:
            log.warning(f"[{self.user_id}] (bot) followup error: {e}")

    async def _send_followup(self, customer_id, f):
        msg = (f.get("message") or "").strip()
        try:
            if f.get("media_id"):
                media = await db.get_media_by_id(f["media_id"])
                if media:
                    if msg and not media.get("caption"):
                        media = dict(media)
                        media["caption"] = msg
                        await self._send_media(customer_id, media)
                    else:
                        if msg:
                            await self.app.bot.send_message(customer_id, msg)
                        await self._send_media(customer_id, media)
                    return
            if msg:
                await self.app.bot.send_message(customer_id, msg)
        except Exception as e:
            log.warning(f"send followup failed: {e}")


def _rm(p):
    try:
        os.remove(p)
    except Exception:
        pass
