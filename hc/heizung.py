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
  - Board (esp-p4d ab 0.3.0, /api/v1/status jede Minute): Neustart nach Absturz,
    Watchdog oder Spannungseinbruch; WLAN-Signal laenger schwach; Chip zu warm;
    Speicher knapp; COM1-Fehlerquote hoch (vital_problems())
"""
import logging
import time

from . import config, net, poller

log = logging.getLogger("heizung")

OFFLINE_AFTER = 10 * 60      # so lange ohne Daten bzw. Kessel stumm -> Meldung
MAX_LINES = 30               # Werte je Telegram-Antwort

# Board-Vitalwerte: Abruf und Schwellen (Warnung / Entwarnung mit Abstand)
VITAL_EVERY = 60
RSSI_WARN, RSSI_OK, RSSI_AFTER = -80, -75, 10 * 60   # dBm; erst nach 10 min schwach melden
TEMP_WARN, TEMP_OK = 75.0, 70.0                       # °C Chip (liegt ueber Raumtemperatur)
HEAP_WARN = 20 * 1024                                 # kleinster freier Speicher seit Start
COM_FAIL_WARN, COM_FAIL_OK = 0.10, 0.02               # Anteil Fehler je Werte-Runde
RESET_TEXT = {"poweron": "Strom an", "ext": "Reset-Taste", "sw": "Software/Update", "panic": "Absturz",
              "int_wdt": "Watchdog", "task_wdt": "Watchdog", "wdt": "Watchdog", "brownout": "Spannungseinbruch"}

# Fuer Einstellungsseite/Bot: letzter Verbindungstest bzw. Abruf
last = {"name": "", "count": 0}
# Letzter /api/v1/status (device + kessel), ts = Abrufzeit; leer bei Firmware < 0.3.0
vital: dict = {"ts": 0.0, "device": {}, "kessel": {}}


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
    if time.time() - vital["ts"] >= VITAL_EVERY:
        try:
            s = await _get("/api/v1/status")
            vital.update(ts=time.time(), device=s.get("device") or {}, kessel=s.get("kessel") or {})
        except Exception as e:  # noqa: BLE001 - Kesseldaten sind wichtiger; naechster Versuch in 1 min
            vital["ts"] = time.time()
            log.warning("esp-p4d /api/v1/status: %s", e)
    return j


def vital_fresh(now_ts: float | None = None) -> bool:
    return bool(vital["device"].get("vital")) and (now_ts or time.time()) - vital["ts"] < 3 * VITAL_EVERY


def vital_values() -> list[tuple[str, str, str, float]]:
    """Fuer den Verlauf: [(schluessel, name, einheit, wert)] - nur mit frischen Daten."""
    if not vital_fresh():
        return []
    d, k = vital["device"], vital["kessel"]
    v, w = d.get("vital") or {}, d.get("wlan") or {}
    out = [("rssi", "esp-p4d WLAN-Signal", "dBm", w.get("rssi") if w.get("connected") else None),
           ("chip_temp", "esp-p4d Chip-Temperatur", "°C", v.get("chipTemp")),
           ("heap_free", "esp-p4d freier Speicher", "KB", (v.get("heapFree") or 0) / 1024 or None),
           ("heap_min", "esp-p4d kleinster freier Speicher", "KB", (v.get("heapMin") or 0) / 1024 or None),
           ("loop_max", "esp-p4d längster Schleifendurchlauf", "ms", v.get("loopMaxMs")),
           ("com_round", "esp-p4d COM1-Runde", "s", k.get("roundSec")),
           ("com_fail", "esp-p4d COM1-Fehler je Runde", "", k.get("roundFailures"))]
    return [(key, name, unit, float(val)) for key, name, unit, val in out if isinstance(val, (int, float))]


_vst = {"boots": None, "weak_since": 0.0}


def vital_problems(now_ts: float | None = None) -> dict[str, tuple[str, str] | None]:
    """{art: (schluessel, text) | None}. None = Entwarnung bzw. nichts. Arten:
    p4d_reset (nur Ereignis, keine Entwarnung), p4d_rssi, p4d_temp, p4d_heap, p4d_com.
    Leer, solange keine frischen Vitalwerte da sind."""
    now_ts = now_ts or time.time()
    if not vital_fresh(now_ts):
        return {}
    d, k = vital["device"], vital["kessel"]
    v, w = d.get("vital") or {}, d.get("wlan") or {}
    out: dict[str, tuple[str, str] | None] = {}

    boots = v.get("boots")
    prev, _vst["boots"] = _vst["boots"], boots
    if prev is not None and boots and boots != prev and v.get("abnormalReset"):
        why = RESET_TEXT.get(v.get("resetReason"), v.get("resetReason") or "?")
        out["p4d_reset"] = (str(boots), f"⚠ esp-p4d neu gestartet nach {why} (Start Nr. {boots}).")

    rssi = w.get("rssi") if w.get("connected") else None
    if isinstance(rssi, (int, float)) and rssi < RSSI_WARN:
        _vst["weak_since"] = _vst["weak_since"] or now_ts
        if now_ts - _vst["weak_since"] >= RSSI_AFTER:
            out["p4d_rssi"] = ("on", f"\U0001F4F6 esp-p4d: WLAN-Signal schwach ({rssi} dBm, seit über "
                                     f"{RSSI_AFTER // 60} min, Access Point {w.get('bssid') or '?'}).")
    elif isinstance(rssi, (int, float)) and rssi >= RSSI_OK:
        _vst["weak_since"] = 0.0
        out["p4d_rssi"] = None

    t = v.get("chipTemp")
    if isinstance(t, (int, float)):
        if t >= TEMP_WARN:
            out["p4d_temp"] = ("on", f"\U0001F321 esp-p4d: Chip sehr warm ({t:.0f} °C).")
        elif t < TEMP_OK:
            out["p4d_temp"] = None

    hm = v.get("heapMin")
    if isinstance(hm, (int, float)):
        out["p4d_heap"] = ((str(boots), f"⚠ esp-p4d: Speicher knapp (kleinster freier Wert {hm / 1024:.0f} KB) "
                                        "– Neustart empfohlen.") if hm < HEAP_WARN else None)

    polls, fails = k.get("roundPolls") or 0, k.get("roundFailures") or 0
    if polls >= 20:
        rate = fails / polls
        if rate >= COM_FAIL_WARN:
            out["p4d_com"] = ("on", f"⚠ esp-p4d: COM1 gestört ({fails} von {polls} Abfragen der letzten "
                                    "Runde fehlgeschlagen) – Kabel/Stecker prüfen.")
        elif rate <= COM_FAIL_OK:
            out["p4d_com"] = None
    return out


def vital_line() -> str:
    """Eine Zeile fuer die Bot-Antwort 'heizung'."""
    if not vital_fresh():
        return ""
    d = vital["device"]
    v, w = d.get("vital") or {}, d.get("wlan") or {}
    up = int(d.get("uptime") or 0)
    parts = [f"Signal {w.get('rssi')} dBm" if w.get("connected") else "WLAN getrennt"]
    if isinstance(v.get("chipTemp"), (int, float)):
        parts.append(f"Chip {v['chipTemp']:.0f} °C")
    parts.append(f"läuft seit {up // 86400} d {up % 86400 // 3600} h" if up >= 86400 else
                 f"läuft seit {up // 3600} h {up % 3600 // 60} min")
    parts.append(f"Start Nr. {v.get('boots')} ({RESET_TEXT.get(v.get('resetReason'), v.get('resetReason'))})")
    return "\U0001F4DF esp-p4d: " + ", ".join(parts)


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


def buffer_charge(temps: list[float]) -> int | None:
    """Eigener Pufferladezustand in % aus Puffer oben/mitte/unten: je Fuehler linear zwischen
    hz_buf_min (0 %) und hz_buf_max (100 %), gemittelt. None = Werte fehlen/Grenzen unsinnig."""
    try:
        lo, hi = float(config.get("hz_buf_min")), float(config.get("hz_buf_max"))
    except (TypeError, ValueError):
        return None
    if not temps or hi <= lo:
        return None
    parts = [max(0.0, min(1.0, (t - lo) / (hi - lo))) for t in temps]
    return round(100 * sum(parts) / len(parts))


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
    if vital_line():
        lines.append(vital_line())
    return "\n".join(lines)
