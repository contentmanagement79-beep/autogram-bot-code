# Autogram — Bot Engine (Phase 3B)

The Python service that actually runs the assistant on each seller's Telegram
account. It reads config (persona, products, keys) from the same Supabase your
website uses, replies to customers, understands photos/voice/documents via
Gemini, sends voice notes on request, and obeys the take-over commands.

This is a **separate project** from the website. Deploy it on Render (to start)
or a small always-on VPS.

## How it fits together
```
Website (Vercel) ──writes persona/products──►  Supabase  ◄──reads config── Bot Engine (Render)
                                                   ▲                              │
                                          connect.py stores                 Telethon ⇄ Telegram
                                          encrypted session + key                Gemini (vision/voice)
```

## 1. Prerequisites
- Run the SQL in **`schema-phase3b.sql`** in Supabase (after the website's `phase3-schema.sql`).
- Have your Supabase **DATABASE_URL** (Settings → Database → Connection string → URI, "Session pooler").
- Generate a **FERNET_KEY**:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

## 2. Install & configure
```bash
cd engine
python -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
cp .env.example .env      # fill DATABASE_URL, FERNET_KEY, GEMINI_MODELS
```

## 3. Connect a Telegram account (one-time, per seller)
```bash
python connect.py
```
Enter the Supabase user id, Telegram `api_id`/`api_hash` (my.telegram.org), phone,
the login code, and a Gemini key. This stores an **encrypted** session in Supabase.

> The seller should already have set their **persona** and **products** in the
> website dashboard — the engine reads those.

## 4. Run the engine
```bash
python main.py
```
It connects every seller with a stored session and starts replying. Message the
account from another Telegram account to test.

## Commands (typed by the account owner, in any chat)
Defaults (change them in the dashboard → settings):
- `//stop` / `//start` — pause / resume the bot **for that one customer** (take-over)
- `//stopall` / `//startall` — pause / resume **everywhere**

While paused the bot keeps reading and remembering — it just doesn't reply.

## Deploy on Render (free tier)
- New **Web Service** from this repo (root = `engine`).
- Build: `pip install -r requirements.txt` · Start: `python main.py`
- Add env vars (`DATABASE_URL`, `FERNET_KEY`, `GEMINI_MODELS`).
- Free web services sleep after 15 min idle — ping `/health` with an external
  uptime service to keep it awake. As sellers grow, move to a small VPS
  (Telethon holds ~20–35 MB per account; 512 MB fits ~10–15).

## Notes / honesty
- **Inbound-only:** it replies to people who message the account; it never cold-DMs.
- **No hallucinated prices:** it quotes only the products/store info you provide.
- **Honest assistant:** it doesn't pretend to be a specific human.
- Model names change — set `GEMINI_MODELS` to models available in your Google AI
  Studio account. The engine tries them in order and rotates keys on quota.
- This code hasn't been run in your exact environment; test with one account
  first and adjust. Managed mode + ElevenLabs voice are future work.
