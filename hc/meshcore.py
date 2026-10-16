"""MeshCore-Gateway: Kanaele abonnieren und an Telegram (optional ntfy) weiterleiten.

Gegenstelle ist die Kanal-API des esp-meshcore-gateway (ab 0.10.0, dort E43):
GET /api/v1/channels und /api/v1/messages?since=N mit Header X-API-Key. Das
Gateway haelt die letzten 200 Kanal-Zeilen im RAM; wir fragen alle 15 s mit dem
Zeiger "next" der letzten Antwort nach. Zeiger + Boot-Kennung des Gateways
liegen in secrets.json, damit ein Neustart von home-central nichts verliert
(solange das Gateway die Zeilen noch hat). Aendert sich "boot", wurde das
Gateway neu gestartet -> ab since=0 (alles danach ist neu).
Beim allerersten Abruf wird der vorhandene Verlauf uebersprungen.
"""
import logging
from collections import deque

from . import config, net, ntfy, telegram

log = logging.getLogger("meshcore")

# Selbst ins Mesh geschickte Texte (wie vom Gateway bestaetigt): beim
# Weiterleiten Mesh -> Telegram ueberspringen, sonst kaeme jede Warnung doppelt.
_pushed: deque = deque(maxlen=50)

STATE_KEY = "mc_state"       # secret_obj: {"url": ..., "boot": ..., "next": ...}
PAGE = 50                    # max. Nachrichten je Antwort (wie im Gateway)
MAX_PAGES = 20
TG_MAX = 3800                # Telegram erlaubt 4096 Zeichen je Nachricht

# Fuer die Einstellungsseite: zuletzt vom Gateway gelesene Kanaele
last = {"gateway": "", "channels": [], "forwarded": 0, "gap": False}


def base_url() -> str:
    u = str(config.get("mc_url")).strip().rstrip("/")
    if u and not u.startswith(("http://", "https://")):
        u = "http://" + u
    return u


def enabled() -> bool:
    return config.get("mc_on") == "1" and bool(base_url()) and bool(str(config.get("mc_key")).strip())


def selected() -> set[str]:
    return {c for c in str(config.get("mc_channels")).split("\n") if c.strip()}


async def _get(path: str, params: dict | None = None) -> dict:
    r = await net.client.get(base_url() + path, params=params, timeout=10,
                             headers={"X-API-Key": str(config.get("mc_key")).strip()})
    if r.status_code in (401, 403):
        try:
            msg = r.json().get("error", "")
        except ValueError:
            msg = ""
        raise RuntimeError(f"HTTP {r.status_code}: {msg or 'Zugriff verweigert'}")
    r.raise_for_status()
    return r.json()


def format_batch(gateway: str, msgs: list[dict], gap: bool) -> list[str]:
    """Nachrichten zu Telegram-Texten (mehrere Nachrichten je Text, max. TG_MAX)."""
    lines = []
    if gap:
        lines.append("⚠️ MeshCore: Nachrichten verpasst (Gateway-Puffer übergelaufen)")
    for m in msgs:
        who = m.get("sender") or "?"
        lines.append(f"📡 {m.get('channel', '?')} · {who}:\n{m.get('text', '')}")
    out, cur = [], ""
    for ln in lines:
        if cur and len(cur) + 2 + len(ln) > TG_MAX:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{ln}" if cur else ln[:TG_MAX]
    if cur:
        out.append(cur)
    return out


def pick(msgs: list[dict], channels: set[str], own: bool) -> list[dict]:
    return [m for m in msgs if m.get("channel") in channels and (own or m.get("dir") == "rx")
            and not (m.get("dir") == "tx" and m.get("text") in _pushed)]


# --- Warnungen ins Mesh (Gateway E44: POST /api/v1/send) -------------------------
def push_enabled(kind: str = "") -> bool:
    """Senden ins Mesh eingeschaltet (und fuer diese Art von Warnung)."""
    if not (config.get("mc_push") == "1" and str(config.get("mc_push_ch")).strip()
            and base_url() and str(config.get("mc_key")).strip()):
        return False
    return not kind or config.get(f"mc_push_{kind}") == "1"


def mesh_text(text: str) -> str:
    """Kurz halten (jede Nachricht kostet Funkzeit, max. ~140 Byte): Zeilen zusammenziehen."""
    return " ".join(text.split())


async def push(text: str) -> bool:
    """Text in den freigegebenen Mesh-Kanal senden. True = vom Gateway angenommen."""
    if not push_enabled():
        return False
    try:
        r = await net.client.post(base_url() + "/api/v1/send", timeout=10,
                                  headers={"X-API-Key": str(config.get("mc_key")).strip()},
                                  data={"channel": str(config.get("mc_push_ch")).strip(), "text": mesh_text(text)})
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            log.warning("Senden ins Mesh abgelehnt: HTTP %s %s", r.status_code, j.get("error", ""))
            return False
    except Exception as e:  # noqa: BLE001
        log.warning("Senden ins Mesh fehlgeschlagen: %s", e)
        return False
    _pushed.append(j.get("text", mesh_text(text)))
    if j.get("truncated"):
        log.info("Mesh-Text gekuerzt: %s", j.get("text"))
    return True


async def poll():
    if not enabled():
        return None
    ch = await _get("/api/v1/channels")
    boot = ch.get("boot", "")
    last.update(gateway=ch.get("gateway", ""), channels=[c.get("name", "") for c in ch.get("channels", [])])

    st = config.secret_obj(STATE_KEY) or {}
    if st.get("url") != base_url():
        st = {}                                  # anderes Gateway -> von vorn, ohne Verlauf
    first = not st
    since = int(st.get("next", 0)) if st.get("boot") == boot else 0
    if st and st.get("boot") != boot:
        log.info("Gateway neu gestartet (boot %s -> %s), lese ab Anfang", st.get("boot"), boot)

    msgs, gap = [], False
    for _ in range(MAX_PAGES):
        j = await _get("/api/v1/messages", {"since": since})
        if j.get("boot") != boot:                # zwischendurch neu gestartet -> naechste Runde
            break
        msgs += j.get("messages", [])
        gap = gap or bool(j.get("gap"))
        nxt = int(j.get("next", since))
        if nxt == since:
            break
        since = nxt

    sent = 0
    if first:
        log.info("Erster Abruf von %s: %d vorhandene Nachrichten übersprungen", base_url(), len(msgs))
    else:
        todo = pick(msgs, selected(), config.get("mc_own") == "1")
        if todo or (gap and selected()):
            if not telegram.configured():
                raise RuntimeError("Telegram nicht eingerichtet (Token/Gruppe fehlt)")
            for text in format_batch(last["gateway"], todo, gap):
                # Zeiger bleibt stehen -> naechster Versuch schickt alles nochmal
                if not await telegram.send(text, fwd=False):
                    raise RuntimeError("Telegram-Versand fehlgeschlagen, wird wiederholt")
            if config.get("mc_ntfy") == "1":
                for m in todo:
                    await ntfy.publish(m.get("text", ""), title=f"{m.get('channel')} · {m.get('sender') or '?'}",
                                       tags="satellite_antenna")
            sent = len(todo)
    last["forwarded"] += sent
    last["gap"] = gap
    new = {"url": base_url(), "boot": boot, "next": since}
    if new != st:
        config.set_secret_obj(STATE_KEY, new)
    return {"gateway": last["gateway"], "channels": last["channels"], "forwarded": last["forwarded"]}
