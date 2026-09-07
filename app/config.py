import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
FERNET_KEY = os.getenv("FERNET_KEY", "")
GEMINI_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite,gemini-1.5-flash"
).split(",") if m.strip()]
PORT = int(os.getenv("PORT", "10000"))
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "120"))

MEMORY_TURNS = 16          # how many past messages to send to the model
MEMORY_RETENTION_DAYS = 7  # raw messages older than this are purged
