import asyncio
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("voice")

VOICE_DIR = "voice_tmp"
Path(VOICE_DIR).mkdir(exist_ok=True)

# Bengali + English + Banglish phrases that mean "send a voice message"
_VOICE_KW = [
    "voice dao", "voice de", "voice pathao", "voice ta dao", "voice ta pathao",
    "voice message", "voice note", "send voice", "send a voice", "voice reply",
    "audio message", "speak", "bolo", "voice a bolo", "voice chai", "voice lagbe",
    "audio dao", "audio de", "audio pathao", "audio chai",
    # বাংলা হরফের ট্রিগার
    "ভয়েস দাও", "ভয়েস দে", "ভয়েস পাঠাও", "ভয়েস মেসেজ", "ভয়েস নোট", "ভয়েস রিপ্লাই",
    "ভয়েসে বলো", "মুখে বলো", "ভয়েস চাই", "ভয়েস লাগবে", "অডিও দাও", 
    "অডিও পাঠাও", "অডিও মেসেজ", "কথা বলো",
    # 'য়' এর বদলে 'য়' (অনেক কিবোর্ডে আলাদা হয়) দিয়ে ট্রিগার
    "ভয়েস দাও", "ভয়েস দে", "ভয়েস পাঠাও", "ভয়েস মেসেজ", "ভয়েস নোট", "ভয়েসে বলো", "ভয়েস চাই"
]


def wants_voice(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    
    # ১. সরাসরি কিওয়ার্ড মিলে গেলে
    if any(kw in t for kw in _VOICE_KW):
        return True
        
    # ২. বাক্যের মধ্যে "voice/audio" এবং "দাও/পাঠাও" আলাদা থাকলেও যেন ধরে
    voice_words = ["voice", "audio", "ভয়েস", "ভয়েস", "অডিও"]
    action_words = ["dao", "de", "pathao", "send", "chai", "lagbe", "note", "bolo", "reply", "দাও", "দে", "পাঠাও", "বলো", "বল", "চাই", "লাগবে", "করো", "দিন"]
    
    if any(v in t for v in voice_words) and any(a in t for a in action_words):
        return True
        
    return False


def _clean(text: str) -> str:
    out = []
    for c in text:
        if ord(c) < 128 or c.isalpha() or c.isspace() or c in ".,!?-'":
            out.append(c)
    return " ".join("".join(out).split()).strip()


async def tts(text: str, voice: str, uid) -> str | None:
    try:
        import edge_tts
    except ImportError:
        return None
    script = _clean(text) or "Hello!"
    path = os.path.join(VOICE_DIR, f"v_{uid}_{int(time.time())}.mp3")
    try:
        await asyncio.wait_for(edge_tts.Communicate(script, voice).save(path), timeout=25.0)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    except Exception as e:
        log.warning(f"tts error: {e}")
    return None


def extract_document_text(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                return "\n".join((pg.extract_text() or "") for pg in pdf.pages[:6])[:3000]
        if ext in (".doc", ".docx"):
            from docx import Document
            return "\n".join(p.text for p in Document(file_path).paragraphs)[:3000]
        if ext in (".xlsx", ".xls"):
            import openpyxl
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            rows = [str([c.value for c in row]) for row in wb.active.iter_rows(max_row=30)]
            return "\n".join(rows)[:2500]
        if ext == ".txt":
            return Path(file_path).read_text(encoding="utf-8", errors="ignore")[:3000]
    except Exception as e:
        log.warning(f"doc extract error: {e}")
    return ""
