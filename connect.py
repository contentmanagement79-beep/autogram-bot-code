"""
One-time setup for a tenant. Run locally:

    python connect.py

You'll need:
  - your Supabase user id (uuid) — from the dashboard/Supabase → Authentication → Users
  - Telegram api_id + api_hash — from my.telegram.org
  - the phone number of the account the assistant should run on
  - (optional) a Gemini API key — from aistudio.google.com/apikey

This logs in, encrypts the session, and stores it in Supabase so the engine
can run the bot on that account.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

from app import db, crypto


async def main():
    print("\n=== Autogram — connect a Telegram account ===\n")
    user_id = input("Supabase user id (uuid): ").strip()
    api_id = int(input("Telegram api_id: ").strip())
    api_hash = input("Telegram api_hash: ").strip()
    phone = input("Phone (e.g. +8801XXXXXXXXX): ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    await client.send_code_request(phone)
    code = input("Enter the login code Telegram sent you: ").strip()
    try:
        await client.sign_in(phone, code)
    except Exception as e:
        if "password" in str(e).lower() or "2fa" in str(e).lower():
            pw = input("Two-step password: ").strip()
            await client.sign_in(password=pw)
        else:
            raise

    session_string = client.session.save()
    await client.disconnect()

    await db.init_pool()
    await db.store_session(
        user_id,
        api_id,
        crypto.encrypt(api_hash),
        phone,
        crypto.encrypt(session_string),
    )
    print("✅ Telegram connected and stored.")

    key = input("\nGemini API key (leave blank to skip): ").strip()
    if key:
        await db.store_ai_key(user_id, crypto.encrypt(key))
        print("✅ Gemini key stored.")

    print("\nDone. Start the engine with:  python main.py\n")


if __name__ == "__main__":
    asyncio.run(main())
