import asyncpg
from typing import Optional
from app import config

_pool: Optional[asyncpg.Pool] = None


async def init_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(config.DATABASE_URL, ssl="require", min_size=1, max_size=5)
    return _pool


async def pool() -> asyncpg.Pool:
    return await init_pool()


# ── Tenants to run ─────────────────────────────────────────
async def get_connected_connections():
    """Each connected Telegram connection (a user may have one 'user' + one 'bot')."""
    p = await pool()
    rows = await p.fetch(
        """
        select id, user_id, coalesce(mode,'user') as mode,
               api_id, api_hash_enc, session_string_enc, bot_token_enc
        from telegram_accounts
        where status = 'connected'
          and (
            (coalesce(mode,'user') = 'user' and session_string_enc is not null)
            or (mode = 'bot' and bot_token_enc is not null)
          )
        """
    )
    return [dict(r) for r in rows]


async def get_persona(user_id):
    p = await pool()
    row = await p.fetchrow("select * from personas where user_id = $1", user_id)
    return dict(row) if row else None


async def get_products(user_id):
    p = await pool()
    rows = await p.fetch(
        "select name, category, price, description from products where user_id = $1 and is_active = true",
        user_id,
    )
    return [dict(r) for r in rows]


async def get_active_ai_keys(user_id):
    p = await pool()
    rows = await p.fetch(
        "select id, key_enc from ai_keys where user_id = $1 and provider = 'gemini' and status = 'active' order by created_at",
        user_id,
    )
    return [dict(r) for r in rows]


async def mark_ai_key(key_id, status: str):
    p = await pool()
    await p.execute("update ai_keys set status = $2 where id = $1", key_id, status)


# ── Global / per-customer pause ────────────────────────────
async def is_bot_running(user_id) -> bool:
    p = await pool()
    row = await p.fetchrow("select bot_running from personas where user_id = $1", user_id)
    return bool(row["bot_running"]) if row else True


async def set_bot_running(user_id, running: bool):
    p = await pool()
    await p.execute("update personas set bot_running = $2 where user_id = $1", user_id, running)


async def is_customer_paused(user_id, customer_id) -> bool:
    p = await pool()
    row = await p.fetchrow(
        "select paused from chat_states where user_id = $1 and customer_id = $2", user_id, customer_id
    )
    return bool(row["paused"]) if row else False


async def set_customer_paused(user_id, customer_id, paused: bool):
    p = await pool()
    await p.execute(
        """
        insert into chat_states (user_id, customer_id, paused, updated_at)
        values ($1, $2, $3, now())
        on conflict (user_id, customer_id) do update set paused = $3, updated_at = now()
        """,
        user_id, customer_id, paused,
    )


# ── Conversation memory ────────────────────────────────────
async def add_message(user_id, customer_id, role: str, content: str):
    p = await pool()
    await p.execute(
        "insert into conversations (user_id, customer_id, role, content) values ($1,$2,$3,$4)",
        user_id, customer_id, role, content[:2000],
    )


async def recent_messages(user_id, customer_id, limit: int):
    p = await pool()
    rows = await p.fetch(
        """
        select role, content from conversations
        where user_id = $1 and customer_id = $2
        order by created_at desc limit $3
        """,
        user_id, customer_id, limit,
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def purge_old_messages():
    p = await pool()
    await p.execute(
        "delete from conversations where created_at < now() - ($1 || ' days')::interval",
        str(config.MEMORY_RETENTION_DAYS),
    )


# ── Writes used by connect.py (onboarding) ─────────────────
async def store_session(user_id, api_id: int, api_hash_enc: str, phone: str, session_enc: str):
    p = await pool()
    await p.execute(
        """
        insert into telegram_accounts (user_id, mode, api_id, api_hash_enc, phone, session_string_enc, status, updated_at)
        values ($1,'user',$2,$3,$4,$5,'connected', now())
        on conflict (user_id, mode) do update set
          api_id = $2, api_hash_enc = $3, phone = $4, session_string_enc = $5,
          status = 'connected', updated_at = now()
        """,
        user_id, api_id, api_hash_enc, phone, session_enc,
    )


async def store_bot(user_id, bot_token_enc: str, label: str = ""):
    p = await pool()
    await p.execute(
        """
        insert into telegram_accounts (user_id, mode, bot_token_enc, phone, status, updated_at)
        values ($1,'bot',$2,$3,'connected', now())
        on conflict (user_id, mode) do update set
          bot_token_enc=$2, phone=$3, status='connected', updated_at = now()
        """,
        user_id, bot_token_enc, label,
    )


async def store_ai_key(user_id, key_enc: str, hint: str = "", label: str = "primary"):
    p = await pool()
    await p.execute(
        "insert into ai_keys (user_id, provider, label, key_enc, hint, status) values ($1,'gemini',$2,$3,$4,'active')",
        user_id, label, key_enc, hint,
    )


async def set_single_ai_key(user_id, key_enc: str, label: str = "primary"):
    """Replace the user's Gemini key(s) with one fresh active key."""
    p = await pool()
    async with p.acquire() as con:
        async with con.transaction():
            await con.execute("delete from ai_keys where user_id = $1 and provider = 'gemini'", user_id)
            await con.execute(
                "insert into ai_keys (user_id, provider, label, key_enc, status) values ($1,'gemini',$2,$3,'active')",
                user_id, label, key_enc,
            )


# ── Website API integration ────────────────────────────────
async def get_integration(user_id):
    p = await pool()
    row = await p.fetchrow(
        "select api_url, header_name, header_value_enc, enabled, note from integrations where user_id = $1",
        user_id,
    )
    return dict(row) if row else None


async def store_integration(user_id, api_url, header_name, header_value_enc, enabled, note):
    """header_value_enc = None keeps the existing secret unchanged."""
    p = await pool()
    await p.execute(
        """
        insert into integrations (user_id, api_url, header_name, header_value_enc, enabled, note, updated_at)
        values ($1, $2, $3, $4, $5, $6, now())
        on conflict (user_id) do update set
          api_url = $2,
          header_name = $3,
          header_value_enc = coalesce($4, integrations.header_value_enc),
          enabled = $5,
          note = $6,
          updated_at = now()
        """,
        user_id, api_url, header_name, header_value_enc, enabled, note,
    )


# ── Media library ──────────────────────────────────────────
async def get_media_items(user_id):
    p = await pool()
    rows = await p.fetch(
        "select name, keyword, caption, url, kind, blur, spoiler, self_destruct from media_items where user_id = $1",
        user_id,
    )
    return [dict(r) for r in rows]


# ── Customers (persistent last-activity) + follow-ups ──────
async def touch_customer(user_id, customer_id, via="user"):
    p = await pool()
    await p.execute(
        """insert into customers (user_id, customer_id, last_msg_at, via) values ($1,$2, now(), $3)
           on conflict (user_id, customer_id) do update set last_msg_at = now(), via = $3""",
        user_id, customer_id, via,
    )


async def get_customers(user_id):
    p = await pool()
    rows = await p.fetch("select customer_id, last_msg_at, via from customers where user_id = $1", user_id)
    return [dict(r) for r in rows]


async def get_enabled_followups(user_id):
    p = await pool()
    rows = await p.fetch(
        "select id, name, delay_days, message, media_id from followups where user_id = $1 and enabled = true order by delay_days",
        user_id,
    )
    return [dict(r) for r in rows]


async def get_followup_sends(user_id):
    p = await pool()
    rows = await p.fetch("select customer_id, followup_id from followup_sends where user_id = $1", user_id)
    return {(r["customer_id"], r["followup_id"]) for r in rows}


async def record_followup_send(user_id, customer_id, followup_id):
    p = await pool()
    await p.execute(
        "insert into followup_sends (user_id, customer_id, followup_id) values ($1,$2,$3) on conflict do nothing",
        user_id, customer_id, followup_id,
    )


async def clear_followup_sends(user_id, customer_id):
    p = await pool()
    await p.execute("delete from followup_sends where user_id = $1 and customer_id = $2", user_id, customer_id)


async def get_media_by_id(media_id):
    p = await pool()
    row = await p.fetchrow(
        "select name, caption, url, kind, blur, spoiler, self_destruct from media_items where id = $1", media_id
    )
    return dict(row) if row else None


# ── Persistent per-customer summary ────────────────────────
async def get_customer_profile(user_id, customer_id):
    p = await pool()
    row = await p.fetchrow(
        "select summary from customer_profiles where user_id = $1 and customer_id = $2", user_id, customer_id
    )
    return row["summary"] if row and row["summary"] else None


async def upsert_customer_profile(user_id, customer_id, summary: str):
    p = await pool()
    await p.execute(
        """insert into customer_profiles (user_id, customer_id, summary, updated_at)
           values ($1,$2,$3, now())
           on conflict (user_id, customer_id) do update set summary = $3, updated_at = now()""",
        user_id, customer_id, summary,
    )


# ── Feature access (monetization) ──────────────────────────
async def get_access(user_id):
    """Return {feature: bool} — is each feature usable for this user right now."""
    from datetime import datetime, timezone
    p = await pool()
    try:
        mon = await p.fetchval("select monetization_on from platform_settings where id = 1")
    except Exception:
        mon = False
    if not mon:
        return _all_true()
    try:
        flags = {r["key"]: r["tier"] for r in await p.fetch("select key, tier from feature_flags")}
    except Exception:
        flags = {}
    row = await p.fetchrow("select plan, expires_at from plans where user_id = $1", user_id)
    is_pro = bool(row and row["plan"] == "pro" and (row["expires_at"] is None or row["expires_at"] > datetime.now(timezone.utc)))

    def allowed(feature):
        if flags.get(feature, "free") != "pro":
            return True
        return is_pro

    return {k: allowed(k) for k in _FEATURES}


_FEATURES = ["media", "voice", "followups", "integration", "conversations", "customers", "business_hours", "bot_mode"]


def _all_true():
    return {k: True for k in _FEATURES}
