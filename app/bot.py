import asyncio
import logging
import os
import random
import time
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, DocumentAttributeFilename

from app import db, crypto, config
from app.ai import GeminiClient
from app.prompt import build_system_prompt
from app.integration import call_integration
from app import voice as voicelib

log = logging.getLogger("bot")
DOWNLOADS = "downloads"
Path(DOWNLOADS).mkdir(exist_ok=True)


class TenantBot:
    def __init__(self, user_id, mode="user", api_id=None, api_hash=None, session_string=None, bot_token=None):
        self.user_id = user_id
        self.mode = mode  # 'user' (personal account) | 'bot' (BotFather bot)
        self._api_id = api_id
        self._api_hash = api_hash
        self._session = session_string
        self._bot_token = bot_token
        self.client = None
        self._cfg = None
        self._cfg_at = 0
        self._buffers = {}        # customer_id -> {texts, media, count, task}
        self._last_key_alert = 0

    async def config(self):
        """Cache persona/products/keys for ~60s."""
        if self._cfg and time.time() - self._cfg_at < 60:
            return self._cfg
        persona = await db.get_persona(self.user_id)
        products = await db.get_products(self.user_id)
        key_rows = await db.get_active_ai_keys(self.user_id)
        keys = [{"id": r["id"], "key": crypto.decrypt(r["key_enc"])} for r in key_rows]
        self._cfg = {"persona": persona, "products": products, "gemini": GeminiClient(keys)}
        self._cfg_at = time.time()
        return self._cfg

    async def start(self):
        if self.mode == "bot":
            if not config.PLATFORM_API_ID or not config.PLATFORM_API_HASH:
                log.error(f"[{self.user_id}] PLATFORM_API_ID/HASH not set — cannot run bot mode")
                await db_safe_status(self.user_id, "error")
                return False
            self.client = TelegramClient(StringSession(), config.PLATFORM_API_ID, config.PLATFORM_API_HASH)
            try:
                await self.client.start(bot_token=self._bot_token)
            except Exception as e:
                log.warning(f"[{self.user_id}] bot token failed: {e}")
                await db_safe_status(self.user_id, "error")
                return False
        else:
            self.client = TelegramClient(StringSession(self._session), int(self._api_id), self._api_hash)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                log.warning(f"[{self.user_id}] session not authorized — skipping")
                await db_safe_status(self.user_id, "error")
                return False
        self.client.add_event_handler(self._on_message, events.NewMessage)
        log.info(f"[{self.user_id}] bot started ({self.mode} mode)")
        return True

    async def stop(self):
        try:
            await self.client.disconnect()
        except Exception:
            pass

    async def _on_message(self, event):
        try:
            await self._handle(event)
        except Exception as e:
            log.exception(f"[{self.user_id}] handler error: {e}")

    async def _handle(self, event):
        # Only private 1-to-1 chats — ignore groups & channels entirely.
        if not event.is_private:
            return

        text = (event.raw_text or "").strip()
        customer_id = event.chat_id
        cfg = await self.config()
        persona = cfg["persona"]
        if not persona:
            return

        # ── Owner commands (personal-account mode only; typed by the owner) ──
        if self.mode == "user" and event.out:
            cmds = {
                persona["cmd_takeover_stop"]: ("cust", True),
                persona["cmd_takeover_start"]: ("cust", False),
                persona["cmd_global_stop"]: ("glob", True),
                persona["cmd_global_start"]: ("glob", False),
            }
            if text in cmds:
                scope, on = cmds[text]
                if scope == "glob":
                    await db.set_bot_running(self.user_id, not on)  # stop => running False
                    note = "⏸️ Bot paused everywhere." if on else "▶️ Bot resumed everywhere."
                else:
                    await db.set_customer_paused(self.user_id, customer_id, on)
                    note = "⏸️ Bot paused for this chat." if on else "▶️ Bot resumed for this chat."
                try:
                    await event.respond(note)
                except Exception:
                    pass
            return  # never treat own messages as customer input

        if event.out:
            return  # ignore our own outgoing messages (bot mode / owner manual reply)

        if not text and not event.message.media:
            return

        # ── Store customer message ──
        stored = text or "[sent media]"
        await db.add_message(self.user_id, customer_id, "user", stored)

        # ── Track activity (persistent) + reset any follow-up sequence ──
        try:
            await db.touch_customer(self.user_id, customer_id)
            await db.clear_followup_sends(self.user_id, customer_id)
        except Exception as e:
            log.warning(f"customer touch error: {e}")

        # ── Mark the customer's message as read ──
        try:
            await self.client.send_read_acknowledge(customer_id)
        except Exception:
            pass

        # ── Paused? (still listened + stored above, just no reply) ──
        if not await db.is_bot_running(self.user_id):
            return
        if await db.is_customer_paused(self.user_id, customer_id):
            return

        gemini: GeminiClient = cfg["gemini"]
        if not gemini.keys:
            await self._alert_no_keys()
            return

        # ── Media understanding (per message) ──
        media_ctx = ""
        if event.message.media:
            media_ctx = await self._analyze_media(event, gemini, text)

        # ── Buffer + debounce: merge rapid messages into ONE reply ──
        buf = self._buffers.setdefault(customer_id, {"texts": [], "media": [], "count": 0, "task": None})
        if text:
            buf["texts"].append(text)
        if media_ctx:
            buf["media"].append(media_ctx)
        buf["count"] += 1
        if buf["task"]:
            buf["task"].cancel()
        buf["task"] = asyncio.create_task(self._flush(customer_id))

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

        # merged customer turn (text + media notes)
        parts = list(buf["texts"])
        for m in buf["media"]:
            parts.append(f"[media: {m}]")
        enriched = "\n".join(parts).strip() or "[sent media]"

        # ── Live data from the tenant's own website API (optional) ──
        try:
            integ = await db.get_integration(self.user_id)
            if integ and integ.get("enabled") and integ.get("api_url"):
                live = await call_integration(integ, " ".join(buf["texts"]))
                if live:
                    enriched += f"\n[live data: {live}]"
        except Exception as e:
            log.warning(f"integration error: {e}")

        # ── Generate reply ──
        system_prompt = build_system_prompt(persona, cfg["products"])
        history = await db.recent_messages(self.user_id, customer_id, config.MEMORY_TURNS)
        n = buf["count"]
        ctx = history[:-n] if 0 < n <= len(history) else (history if n == 0 else [])
        reply = await gemini.reply(system_prompt, ctx, enriched)
        if not reply:
            if not gemini.keys:
                await self._alert_no_keys()
            return

        await db.add_message(self.user_id, customer_id, "assistant", reply)

        # ── Smart media: if a customer keyword matches a library item, send it ──
        try:
            await self._maybe_send_media(customer_id, " ".join(buf["texts"]))
        except Exception as e:
            log.warning(f"media send error: {e}")

        # ── Voice or text ──
        want_voice = persona.get("voice_enabled", True) and any(voicelib.wants_voice(t) for t in buf["texts"])
        if want_voice:
            path = await voicelib.tts(reply, persona.get("voice_name", "en-US-JennyNeural"), self.user_id)
            if path:
                try:
                    async with self.client.action(customer_id, "record-audio"):
                        await asyncio.sleep(random.uniform(1.0, 2.0))
                    await self.client.send_file(customer_id, path, voice_note=True)
                    os.remove(path)
                    return
                except Exception as e:
                    log.warning(f"voice send failed: {e}")

        # text (with typing simulation)
        try:
            async with self.client.action(customer_id, "typing"):
                await asyncio.sleep(min(1.0 + len(reply) / 60.0, 6.0))
            await self.client.send_message(customer_id, reply)
        except Exception as e:
            log.warning(f"send failed: {e}")

    async def _alert_no_keys(self):
        """Tell the owner (Saved Messages) once/hour when no AI key works."""
        if time.time() - self._last_key_alert < 3600:
            return
        self._last_key_alert = time.time()
        log.warning(f"[{self.user_id}] no working AI keys")
        try:
            await self.client.send_message(
                "me",
                "⚠️ Autogram: your Gemini API key isn't working (limit reached or invalid). "
                "Add or update your key in the dashboard to keep the assistant replying.",
            )
        except Exception:
            pass

    async def _maybe_send_media(self, customer_id, customer_text):
        text = (customer_text or "").lower()
        if not text:
            return
        items = await db.get_media_items(self.user_id)
        for it in items:
            kw = (it.get("keyword") or "").strip().lower()
            if not kw:
                continue
            words = [w.strip() for w in kw.split(",") if w.strip()]
            if any(w in text for w in words):
                await self._send_media(customer_id, it)
                return  # at most one media per reply

    async def _send_media(self, customer_id, item):
        url = item["url"]
        if item.get("blur") and item.get("kind") == "image":
            url = url.replace("/upload/", "/upload/e_blur:1500/", 1)  # Cloudinary blur
        caption = item.get("caption") or ""
        kwargs = {}
        if caption:
            kwargs["caption"] = caption
        if item.get("spoiler"):
            kwargs["spoiler"] = True
        if item.get("self_destruct"):
            kwargs["ttl_seconds"] = 30
        try:
            await self.client.send_file(customer_id, url, **kwargs)
        except Exception as e:
            log.warning(f"media send failed, retrying plain: {e}")
            try:
                await self.client.send_file(customer_id, url, caption=caption)
            except Exception as e2:
                log.warning(f"media send plain failed: {e2}")

    async def run_followups(self):
        """Send drip follow-ups to customers idle >= each rule's delay_days."""
        from datetime import datetime, timezone
        try:
            followups = await db.get_enabled_followups(self.user_id)
            if not followups:
                return
            customers = await db.get_customers(self.user_id)
            if not customers:
                return
            sent = await db.get_followup_sends(self.user_id)
            now = datetime.now(timezone.utc)
            for c in customers:
                last = c.get("last_msg_at")
                if not last:
                    continue
                idle_days = (now - last).total_seconds() / 86400.0
                for f in followups:  # ascending by delay_days
                    if idle_days >= f["delay_days"] and (c["customer_id"], f["id"]) not in sent:
                        await self._send_followup(c["customer_id"], f)
                        await db.record_followup_send(self.user_id, c["customer_id"], f["id"])
                        break  # at most one follow-up per customer per cycle
        except Exception as e:
            log.warning(f"[{self.user_id}] followup error: {e}")

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
                            await self.client.send_message(customer_id, msg)
                        await self._send_media(customer_id, media)
                    return
            if msg:
                await self.client.send_message(customer_id, msg)
        except Exception as e:
            log.warning(f"send followup failed: {e}")

    async def _analyze_media(self, event, gemini: GeminiClient, text: str):
        msg = event.message
        ctx = f'Customer wrote: "{text}"' if text else ""
        try:
            mtype = _media_type(msg)
            if mtype == "document_text":
                fp = await self._download(msg, "doc")
                if fp:
                    content = voicelib.extract_document_text(fp)
                    _rm(fp)
                    if content:
                        return f"customer shared a document. Content preview: {content[:800]}"
                    return "customer shared a document"
            if mtype == "photo":
                fp = await self._download(msg, "img.jpg")
                if fp:
                    data = Path(fp).read_bytes()
                    _rm(fp)
                    r = await gemini.see_image(data, ctx)
                    return r or "customer sent a photo"
            if mtype == "voice":
                fp = await self._download(msg, "voice.ogg")
                if fp:
                    r = await gemini.hear_audio(fp, ctx)
                    _rm(fp)
                    return r or "customer sent a voice message"
        except Exception as e:
            log.warning(f"media analyze error: {e}")
        return ""

    async def _download(self, msg, name):
        try:
            path = os.path.join(DOWNLOADS, f"{int(time.time()*1000)}_{name}")
            await asyncio.wait_for(self.client.download_media(msg, path), timeout=45.0)
            return path
        except Exception as e:
            log.warning(f"download error: {e}")
            return None


def _media_type(msg):
    m = msg.media
    if isinstance(m, MessageMediaPhoto):
        return "photo"
    if isinstance(m, MessageMediaDocument):
        doc = m.document
        mime = doc.mime_type or ""
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("audio/"):
            for a in doc.attributes:
                if getattr(a, "voice", False):
                    return "voice"
            return "voice"
        return "document_text"
    return None


def _rm(p):
    try:
        os.remove(p)
    except Exception:
        pass


async def db_safe_status(user_id, status):
    try:
        p = await db.pool()
        await p.execute("update telegram_accounts set status=$2 where user_id=$1", user_id, status)
    except Exception:
        pass
