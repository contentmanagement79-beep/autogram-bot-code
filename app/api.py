import asyncio
import logging
import secrets
import time

from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError

from app import config, crypto, db

log = logging.getLogger("api")

# token -> in-progress login {client, phone, hash, api_id, api_hash, user_id, ts}
_pending: dict = {}


def _authorized(request) -> bool:
    tok = config.INTERNAL_API_TOKEN
    return bool(tok) and request.headers.get("x-internal-token") == tok


def _cleanup():
    now = time.time()
    for t in list(_pending):
        if now - _pending[t]["ts"] > 600:
            try:
                asyncio.create_task(_pending[t]["client"].disconnect())
            except Exception:
                pass
            _pending.pop(t, None)


async def send_code(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    _cleanup()
    try:
        data = await request.json()
        api_id = int(data["api_id"])
        api_hash = str(data["api_hash"]).strip()
        phone = str(data["phone"]).strip()
        user_id = str(data["user_id"])
    except Exception:
        return web.json_response({"error": "api_id, api_hash and phone are required."}, status=400)

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        return web.json_response({"error": f"Could not send code: {e}"}, status=400)

    token = secrets.token_urlsafe(24)
    _pending[token] = {
        "client": client, "phone": phone, "hash": sent.phone_code_hash,
        "api_id": api_id, "api_hash": api_hash, "user_id": user_id, "ts": time.time(),
    }
    log.info(f"send-code ok for {user_id}")
    return web.json_response({"pending_token": token})


async def verify_code(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    data = await request.json()
    token = data.get("pending_token")
    code = (data.get("code") or "").strip()
    password = data.get("password")

    p = _pending.get(token)
    if not p:
        return web.json_response({"error": "Session expired. Please start again."}, status=400)
    client: TelegramClient = p["client"]

    try:
        if password:
            try:
                await client.sign_in(password=password)
            except Exception as e:
                return web.json_response({"error": f"Wrong password: {e}"}, status=400)
        else:
            try:
                await client.sign_in(phone=p["phone"], code=code, phone_code_hash=p["hash"])
            except SessionPasswordNeededError:
                return web.json_response({"needs_password": True})
            except PhoneCodeInvalidError:
                return web.json_response({"error": "Invalid code."}, status=400)
            except PhoneCodeExpiredError:
                _pending.pop(token, None)
                return web.json_response({"error": "Code expired. Please start again."}, status=400)

        session_string = client.session.save()
        await db.store_session(
            p["user_id"], p["api_id"], crypto.encrypt(p["api_hash"]), p["phone"], crypto.encrypt(session_string)
        )
        try:
            await client.disconnect()
        except Exception:
            pass
        _pending.pop(token, None)

        mgr = request.app.get("manager")
        if mgr:
            try:
                await mgr.sync()  # start the bot immediately
            except Exception as e:
                log.warning(f"sync after connect: {e}")

        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def store_ai_key(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    data = await request.json()
    user_id = data.get("user_id")
    key = (data.get("key") or "").strip()
    if not user_id or not key:
        return web.json_response({"error": "Missing key."}, status=400)
    try:
        hint = key[-4:] if len(key) >= 4 else key
        await db.store_ai_key(user_id, crypto.encrypt(key), hint=hint)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


def setup_internal_routes(app, manager):
    app["manager"] = manager
    app.router.add_post("/internal/telegram/send-code", send_code)
    app.router.add_post("/internal/telegram/verify-code", verify_code)
    app.router.add_post("/internal/ai-key", store_ai_key)
