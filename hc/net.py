"""Gemeinsamer HTTP-Client + Download-Waechter (Port von http_util.c)."""
import logging
import time
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("net")

UA = "home-central (esp-infoscreen data host)"
client: httpx.AsyncClient | None = None

# Download-Waechter: je Host letzter Erfolg/Versuch. Ein Host gilt als
# gestoert, wenn er in der letzten Stunde versucht wurde, aber seit >1 h nicht
# mehr erfolgreich war. Jeder Ausfall/jede Erholung wird einmal gemeldet.
HH_FAIL_S = 3600
_health: dict[str, dict] = {}


def start() -> None:
    global client
    client = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": UA}, follow_redirects=True)


async def stop() -> None:
    if client:
        await client.aclose()


def _report(url: str, ok: bool) -> None:
    host = urlsplit(url).hostname or ""
    if not host or "telegram" in host:
        return
    now = time.time()
    e = _health.setdefault(host, {"last_ok": now, "last_try": now, "alerted": False})
    e["last_try"] = now
    if ok:
        e["last_ok"] = now


def health() -> dict[str, dict]:
    return {h: dict(e) for h, e in _health.items()}


def health_alert() -> str | None:
    now = time.time()
    fail, rec = [], 0
    for host, e in _health.items():
        down = now - e["last_ok"] > HH_FAIL_S
        tried = now - e["last_try"] < HH_FAIL_S
        if down and tried and not e["alerted"]:
            fail.append(host)
            e["alerted"] = True
        elif not down and e["alerted"]:
            e["alerted"] = False
            rec += 1
    if not fail and not rec:
        return None
    if fail:
        msg = "⚠ Download-Problem (>1h):" + "".join(f"\n- {h}" for h in fail)
        if rec:
            msg += f"\n({rec} Quelle(n) wieder ok)"
        return msg
    return "✅ Downloads wieder ok"


async def get(url: str, **kw) -> httpx.Response:
    """GET mit Waechter. Wirft bei Netzfehler oder Status != 200."""
    try:
        r = await client.get(url, **kw)
        r.raise_for_status()
    except Exception:
        _report(url, False)
        raise
    _report(url, True)
    return r


async def get_json(url: str, **kw):
    return (await get(url, **kw)).json()
