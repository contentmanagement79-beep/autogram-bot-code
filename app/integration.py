import logging
import aiohttp
from app import crypto

log = logging.getLogger("integration")


async def call_integration(integ: dict, query: str) -> str:
    """POST the customer's query to the tenant's own API and return text context."""
    url = integ.get("api_url")
    if not url:
        return ""
    headers = {"Content-Type": "application/json"}
    hn = integ.get("header_name")
    hv_enc = integ.get("header_value_enc")
    if hn and hv_enc:
        try:
            headers[hn] = crypto.decrypt(hv_enc)
        except Exception:
            pass
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json={"query": query}, headers=headers) as r:
                if r.status != 200:
                    log.warning(f"integration HTTP {r.status}")
                    return ""
                try:
                    data = await r.json(content_type=None)
                    if isinstance(data, dict):
                        for k in ("context", "answer", "result", "data", "text"):
                            if data.get(k):
                                return str(data[k])[:1500]
                        return str(data)[:1500]
                    return str(data)[:1500]
                except Exception:
                    return (await r.text())[:1500]
    except Exception as e:
        log.warning(f"integration call error: {e}")
        return ""
