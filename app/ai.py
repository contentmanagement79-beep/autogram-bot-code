import asyncio
import logging
from typing import List, Optional

from google import genai
from google.genai import types

from app import config, db

log = logging.getLogger("ai")


class GeminiClient:
    """Per-tenant Gemini access with multiple-key rotation and model fallback."""

    def __init__(self, keys: List[dict]):
        # keys: [{"id": uuid, "key": "decrypted"}]
        self.keys = keys

    def _client_for(self, key: str):
        return genai.Client(api_key=key)

    async def _run(self, make_call):
        """Try each active key against each model until one works."""
        if not self.keys:
            return None
        for k in list(self.keys):
            client = self._client_for(k["key"])
            invalid_key = False
            for model in config.GEMINI_MODELS:
                try:
                    return await asyncio.to_thread(make_call, client, model)
                except Exception as e:
                    msg = str(e).lower()
                    if "api key not valid" in msg or "api_key_invalid" in msg or "invalid api key" in msg:
                        invalid_key = True
                        break
                    if "429" in msg or "quota" in msg or "exhaust" in msg or "resource_exhausted" in msg:
                        # transient (rate/quota): skip this key now, don't mark permanently
                        log.warning("Gemini key hit its limit, rotating to next.")
                        self.keys = [x for x in self.keys if x["id"] != k["id"]]
                        break
                    log.warning(f"Gemini model {model} error: {str(e)[:120]}")
                    continue  # try next model with same key
            if invalid_key:
                log.warning("Gemini key invalid — marking it.")
                try:
                    await db.mark_key_invalid(k["id"], k.get("source", "user"))
                except Exception:
                    pass
                self.keys = [x for x in self.keys if x["id"] != k["id"]]
        return None

    async def reply(self, system_prompt: str, history: list, user_msg: str) -> Optional[str]:
        contents = []
        for m in history:
            role = "user" if m["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=m["content"])]))
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_msg)]))

        def call(client, model):
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.85,
                    max_output_tokens=600,
                ),
            )
            return (resp.text or "").strip()

        return await self._run(call)

    async def see_image(self, image_bytes: bytes, ctx: str) -> Optional[str]:
        def call(client, model):
            resp = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    f"A customer sent this image to a store. {ctx} "
                    f"In 1-2 sentences, say what it shows and what the customer likely wants.",
                ],
            )
            return (resp.text or "").strip()

        return await self._run(call)

    async def hear_audio(self, file_path: str, ctx: str) -> Optional[str]:
        def call(client, model):
            uploaded = client.files.upload(file=file_path)
            resp = client.models.generate_content(
                model=model,
                contents=[uploaded, f"Transcribe this voice message. {ctx} If unclear, describe what you hear."],
            )
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
            return (resp.text or "").strip()

        return await self._run(call)

    async def summarize(self, transcript: str) -> Optional[str]:
        def call(client, model):
            resp = client.models.generate_content(
                model=model,
                contents=(
                    "Summarize this customer chat in 2 short lines: what they want, key details "
                    "(products, prices, quantities), and where things stand. Be concise, no preamble.\n\n"
                    + transcript[:4000]
                ),
                config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=160),
            )
            return (resp.text or "").strip()

        return await self._run(call)
