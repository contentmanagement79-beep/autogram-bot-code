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
async def get_connected_tenants():
    """Users with a connected Telegram account."""
    p = await pool()
    rows = await p.fetch(
        """
        select t.user_id, t.api_id, t.api_hash_enc, t.session_string_enc
        from telegram_accounts t
        where t.status = 'connected'
          and t.session_string_enc is not null
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
        insert into telegram_accounts (user_id, api_id, api_hash_enc, phone, session_string_enc, status, updated_at)
        values ($1,$2,$3,$4,$5,'connected', now())
        on conflict (user_id) do update set
          api_id = $2, api_hash_enc = $3, phone = $4, session_string_enc = $5, status = 'connected', updated_at = now()
        """,
        user_id, api_id, api_hash_enc, phone, session_enc,
    )


async def store_ai_key(user_id, key_enc: str, label: str = "primary"):
    p = await pool()
    await p.execute(
        "insert into ai_keys (user_id, provider, label, key_enc, status) values ($1,'gemini',$2,$3,'active')",
        user_id, label, key_enc,
    )
