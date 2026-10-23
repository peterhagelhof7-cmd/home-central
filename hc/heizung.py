"""Heizung: Werte vom ESP-P4d (Froeling Lambdatronic S3200 ueber COM1) abholen.

Gegenstelle ist die API des esp-p4d (ab Firmware 0.2.0):
GET /api/v1/summary mit Header X-API-Key ->
  {"ts": <unix|null>, "name": "...", "online": <Kessel antwortet auf COM1>,
   "mode": "Automatik", "state": "Heizen", "error": "<Stoerungstext>"|null,
   "values": [{"k": "0x0000", "n": "Kesseltemperatur", "v": 72.5|null, "u": "°C"}]}
Welche Werte kommen, wird im esp-p4d per Haken festgelegt (ohne Auswahl: alle).

Meldungen (Telegram/ntfy ueber telegram._warn):
  - Stoerung: neuer Stoerungstext -> einmal melden; weg -> "behoben"
  - nicht erreichbar: esp-p4d antwortet nicht ODER Kessel antwortet dem esp-p4d
    nicht, jeweils laenger als OFFLINE_AFTER -> einmal melden; zurueck -> melden
  - Feuer aus: Zustand war "Anheizen"/"Heizen" und ist jetzt "Feuer Aus"
    (Zwischenzustaende wie "Ausbrand" oder "Tür offen" dazwischen sind egal)
"""
import logging
import time

from . import config, net, poller

log = logging.getLogger("heizung")

OFFLINE_AFTER = 10 * 60      # so lange ohne Daten bzw. Kessel stumm -> Meldung
MAX_LINES = 30               # Werte je Telegram-Antwort

# Fuer Einstellungsseite/Bot: letzter Verbindungstest bzw. Abruf
last = {"name": "", "count": 0}


def base_url() -> str:
    u = str(config.get("hz_url")).strip().rstrip("/")
    if u and not u.startswith(("http://", "https://")):
        u = "http://" + u
    return u


def enabled() -> bool:
    return config.get("hz_on") == "1" and bool(base_url()) and bool(str(config.get("hz_key")).strip())


async def _get(path: str) -> dict:
    r = await net.client.get(base_url() + path, timeout=10,
                             headers={"X-API-Key": str(config.get("hz_key")).strip()})
    if r.status_code in (401, 403):
        raise RuntimeError(f"HTTP {r.status_code}: API-Schlüssel falsch (im esp-p4d unter Einstellungen)")
    r.raise_for_status()
    return r.json()


async def poll():
    if not enabled():
        return None
    j = await _get("/api/v1/summary")
    if not isinstance(j, dict) or "values" not in j:
        raise RuntimeError("Antwort ist keine esp-p4d-Zusammenfassung")
    last.update(name=j.get("name", ""), count=len(j.get("values") or []))
    _track(j)
    return j


# --- Zustand fuer die Meldungen ------------------------------------------------
# kessel_off_since: seit wann der esp-p4d "online: false" meldet (0 = Kessel ok)
# fire_since: seit wann Feuer (0 = keins); fire_known: Beginn gesehen (nicht erst
# nach einem Neustart von home-central mitten im Heizen)
# fire_events: abgeschlossene Heizphasen [(beginn, ende, beginn_bekannt)] fuer telegram
_st = {"kessel_off_since": 0.0, "fire_since": 0.0, "fire_known": False, "state_seen": False}
fire_events: list[tuple[float, float, bool]] = []


def is_burning(state: str) -> bool:
    """S3200-Zustandstext: "Anheizen", "Heizen" (Gross-/Kleinschreibung egal)."""
    return "heizen" in (state or "").lower()


def is_fire_off(state: str) -> bool:
    return (state or "").strip().lower() == "feuer aus"


def _track(j: dict, now_ts: float | None = None):
    now_ts = now_ts or time.time()
    if j.get("online"):
        _st["kessel_off_since"] = 0.0
    elif not _st["kessel_off_since"]:
        _st["kessel_off_since"] = now_ts
    if not j.get("online"):
        return
    state = str(j.get("state") or "")
    if is_burning(state) and not _st["fire_since"]:
        _st["fire_since"] = now_ts
        _st["fire_known"] = _st["state_seen"]   # erster Abruf nach Start: Beginn unbekannt
    elif is_fire_off(state) and _st["fire_since"]:
        fire_events.append((_st["fire_since"], now_ts, _st["fire_known"]))
        del fire_events[:-5]
        _st["fire_since"] = 0.0
    _st["state_seen"] = True


def offline_reason(now_ts: float | None = None) -> str:
    """'' = alles da; sonst Grund, warum seit > OFFLINE_AFTER keine Heizungsdaten."""
    if not enabled():
        return ""
    now_ts = now_ts or time.time()
    p = poller.POLLERS.get("heizung")
    if p and p.try_ts:
        since = p.ok_ts or p.try_ts   # nie erfolgreich: ab erstem Versuch zaehlen
        if p.error and now_ts - since > OFFLINE_AFTER:
            return "esp-p4d nicht erreichbar"
    off = _st["kessel_off_since"]
    if off and now_ts - off > OFFLINE_AFTER:
        return "Heizung antwortet dem esp-p4d nicht (COM1)"
    return ""


SCREEN_VALUES = 12           # so viele Werte zeigt der Infoscreen-Slide (2 Spalten x 6)


def screen_block() -> dict | None:
    """Block "heizung" fuer /api/v1/screen (Infoscreen-Slide, ab Firmware 1.4.0).
    Werte schon als Text formatiert ("72,5 °C") - der Infoscreen zeigt nur an.
    None = nicht eingerichtet (dann fehlt der Block)."""
    if not enabled():
        return None
    p = poller.POLLERS.get("heizung")
    j = poller.data("heizung") or {}
    age = int(time.time() - p.ok_ts) if p and p.ok_ts else -1
    return {
        "on": True,
        "reach": bool(p and p.ok_ts and not p.error),
        "online": bool(j.get("online")),
        "age": age,
        "mode": str(j.get("mode") or "")[:31],
        "state": str(j.get("state") or "")[:47],
        "error": str(j.get("error") or "")[:63],
        "values": [{"n": str(v.get("n") or v.get("k") or "")[:39], "v": fmt_value(v)[:23]}
                   for v in (j.get("values") or [])[:SCREEN_VALUES]],
    }


def fmt_value(v: dict) -> str:
    val = v.get("v")
    if val is None:
        return "–"
    if isinstance(val, float):
        # Nachkommastellen liefert das esp-p4d passend zum Faktor (z. B. 7.25) - nur ",0" weglassen
        s = f"{val:.2f}".rstrip("0").rstrip(".")
    else:
        s = str(val)
    s = s.replace(".", ",")
    u = (v.get("u") or "").strip()
    return f"{s} {u}" if u else s


def build_text(j: dict | None, age: int = -1, err: str = "") -> str:
    """Antwort fuer den Bot-Befehl 'heizung'."""
    if not enabled():
        return "\U0001F525 Heizung: nicht eingerichtet (home-central → Einstellungen → Heizung)."
    if not j:
        return "\U0001F525 Heizung: noch keine Daten" + (f" ({err})" if err else "") + "."
    lines = ["\U0001F525 Heizung" + (f" ({j['name']})" if j.get("name") else "")]
    if err:
        lines.append(f"⚠ esp-p4d gerade nicht erreichbar ({err}) – Stand von vor {age // 60} min")
    if not j.get("online"):
        lines.append("⚠ Der Kessel antwortet dem esp-p4d nicht (COM1).")
    state = " / ".join(x for x in (j.get("mode"), j.get("state")) if x)
    if state:
        lines.append(f"Zustand: {state}")
    lines.append(f"\U0001F6A8 Störung: {j['error']}" if j.get("error") else "Störung: keine")
    vals = j.get("values") or []
    for v in vals[:MAX_LINES]:
        lines.append(f"• {v.get('n') or v.get('k')}: {fmt_value(v)}")
    if len(vals) > MAX_LINES:
        lines.append(f"… und {len(vals) - MAX_LINES} weitere (Auswahl im esp-p4d unter „Werte“)")
    if not vals:
        lines.append("(noch keine Werte – Werte-Katalog wird gelesen oder Kessel stumm)")
    return "\n".join(lines)
