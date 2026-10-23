"""Telegram-Bot (Port von telegram.c): Message Board, @-Kommandos, Warnungs-
Pushes, 6-Uhr-Bericht, Sonntags-Kino, Download-Waechter, Infoscreen offline/online.

Auf dem Server per Long-Polling (getUpdates timeout=25) -> Antworten sofort.
"""
import asyncio
import logging
import re
import time
from collections import deque
from datetime import date, datetime

from . import config, device, heizchart, heizung, meshcore, net, ntfy, poller
from .history import roles as hz_roles   # "history" ist hier der Chatverlauf
from .sources import extra, kalender, simple
from .tz import TZ, now, today_iso

log = logging.getLogger("telegram")
API = "https://api.telegram.org/bot"

TG_MAX_MSGS = 16
TG_TERMINE = 5
SUMMARY_HOUR = 6
KINO_SUNDAY_HOUR = 10
BOE_WARN_KMH = 50.0
HW_GLAETTE_C, HW_REGEN_MM, HW_SCHNEE_MM, HW_BOEEN_KMH = 1, 10.0, 1.0, 60
HW_HITZE_C, HW_NACHTFROST_C, HW_REGEN_AB_MM = 30, 0, 1.0

history: deque = deque(maxlen=TG_MAX_MSGS)
version = 0
_st = {"botname": "", "offset": 0, "primed": False, "greeted": False,
       "summary_day": None, "kino_day": None, "dev_online": None}
# Zuletzt gemeldete Warnung je Kanal ("tg"/"ntfy") und Art ("dwd"/"nina"/"boe")
_warned: dict[tuple[str, str], str] = {}


def configured() -> bool:
    return bool(str(config.get("tg_token")).strip() and str(config.get("tg_chat")).strip())


def _hist_add(frm: str, text: str):
    global version
    history.append({"from": (frm or "?")[:23], "text": (text or "")[:191]})
    version += 1


async def send(text: str, fwd: bool = True) -> bool:
    """In die Gruppe senden. fwd=True: auch an ntfy (Bot-Antworten, Berichte);
    False fuer Technik-Meldungen, Warnungen (eigener Weg) und Message Board."""
    if not configured():
        return False
    try:
        r = await net.client.post(f"{API}{config.get('tg_token')}/sendMessage",
                                  data={"chat_id": config.get("tg_chat"), "text": text[:4000]}, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log.warning("sendMessage fehlgeschlagen: %s", e)
        return False
    if fwd:
        await ntfy.bot_message(_st["botname"] or "Bot", text)
    return True


async def send_photo(png: bytes, caption: str) -> bool:
    """Bild in die Gruppe (nicht an ntfy - dort nur Text)."""
    if not configured():
        return False
    try:
        r = await net.client.post(f"{API}{config.get('tg_token')}/sendPhoto",
                                  data={"chat_id": config.get("tg_chat"), "caption": caption[:1000]},
                                  files={"photo": ("heizung.png", png, "image/png")}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log.warning("sendPhoto fehlgeschlagen: %s", e)
        return False
    return True


# --- Bausteine ----------------------------------------------------------------
def build_weather() -> str:
    s = poller.data("spessart")
    if s and s.get("valid"):
        return f"\U0001F321 Spessartwetter: {s.get('temp') or '?'} °C, Wind {s.get('wind') or '?'} km/h"
    return "Wetterdaten noch nicht verfügbar."


def build_internet() -> str:
    p = poller.POLLERS.get("fritzbox")
    d = p.data if p else None
    if not d or (p and p.error):
        return "Fritzbox nicht erreichbar."
    dn, up = d["down_rate_Bps"] * 8 / 1e6, d["up_rate_Bps"] * 8 / 1e6
    return (f"\U0001F310 Internet: IP {d['external_ip'] or '-'} | Down {dn:.1f}/{d['down_max_bps'] // 1000000} Mbit/s"
            f" | Up {up:.1f}/{d['up_max_bps'] // 1000000} Mbit/s")


def termine_merged() -> list[tuple[str, str]]:
    """(Datum, Label) aller Termine: eigene + Muell + Feiertage, ab heute, sortiert."""
    today = today_iso()
    ev = []
    for e in kalender.termine():
        if e.get("d", "") >= today:
            ev.append((e["d"], f"{e['t']} {e['ti']}" if e.get("t") else e["ti"]))
    for m in (poller.data("muell") or {}).get("entries", []):
        ev.append((m["day"], f"Abfuhr: {m['title']}"))
    for f in kalender.feiertage():
        if f["date"] >= today:
            ev.append((f["date"], f["name"]))
    return sorted(ev, key=lambda e: e[0])


def build_termine(maxn: int = TG_TERMINE) -> str:
    ev = termine_merged()
    if not ev:
        return "\U0001F4C5 Keine Termine."
    return "\U0001F4C5 Nächste Termine:" + "".join(
        f"\n{d[8:10]}.{d[5:7]}. {lbl}" for d, lbl in ev[:maxn])


def _days_until(d: str) -> int:
    return (date.fromisoformat(d) - now().date()).days


def _when(du: int) -> str:
    return "heute" if du == 0 else "morgen" if du == 1 else f"in {du} Tagen"


def next_feiertag_line() -> str | None:
    today = today_iso()
    fe = [f for f in kalender.feiertage() if f["date"] >= today]
    if not fe:
        return None
    f = fe[0]
    return f"\U0001F389 Nächster Feiertag: {f['date'][8:10]}.{f['date'][5:7]}. {f['name']} ({_when(_days_until(f['date']))})"


def wetter_hinweise(d: dict) -> list[str]:
    h = d.get("today") or {}
    if not h.get("valid"):
        return []
    out = []
    tmin = h["tmin"]
    if d.get("valid") and d.get("temp", 999) < tmin:
        tmin = d["temp"]
    nieder = h["rain_mm"] >= 0.5 or h["snow_mm"] >= 0.5
    if h["thunder"]:
        out.append("⛈ Heute ist mit Gewitter zu rechnen")
    if h["freezing"] or (tmin <= HW_GLAETTE_C and nieder):
        out.append(f"\U0001F9CA Heute ist mit Eis und Glätte zu rechnen (bis {tmin}°C)")
    elif tmin <= 0:
        out.append(f"\U0001F9CA Heute Frost (bis {tmin}°C) – Vorsicht, Glättegefahr")
    if h["snow_mm"] >= HW_SCHNEE_MM:
        out.append(f"❄️ Heute Schneefall (ca. {h['snow_mm']:.0f} mm)")
    if h["rain_mm"] >= HW_REGEN_MM:
        out.append(f"\U0001F327 Heute viel Regen (ca. {h['rain_mm']:.0f} mm)"
                   + (f", ab ca. {h['rain_from']} Uhr" if h["rain_from"] >= 0 else ""))
    elif h["rain_mm"] >= HW_REGEN_AB_MM and h["rain_from"] >= 0:
        out.append(f"\U0001F302 Regen ab ca. {h['rain_from']} Uhr (ca. {h['rain_mm']:.0f} mm)")
    if h["gust_kmh"] >= HW_BOEEN_KMH:
        out.append(f"\U0001F4A8 Heute stürmische Böen bis {h['gust_kmh']} km/h")
    if h["tmax"] >= HW_HITZE_C:
        out.append(f"\U0001F975 Heute wird es heiß (bis {h['tmax']}°C)")
    if d.get("fog_now"):
        out.append("\U0001F32B Nebel – Sicht eingeschränkt")
    elif h["fog"]:
        out.append("\U0001F32B Heute zeitweise Nebel möglich")
    fc = d.get("fc") or []
    if d.get("fc_valid") and fc and fc[0].get("valid") and fc[0]["tmin_morning"] <= HW_NACHTFROST_C:
        out.append(f"\U0001F976 Heute Nacht Frost (bis {fc[0]['tmin_morning']}°C) – Pflanzen/Auto abdecken")
    return out


async def _owm_fresh() -> dict:
    from .sources import owm
    p = poller.POLLERS["owm"]
    try:
        p.data = await owm.poll_full()
        p.ok_ts, p.error = time.time(), ""
    except Exception as e:
        log.warning("OWM-Refresh: %s", e)
    return p.data or {}


async def send_daily_summary():
    d = await _owm_fresh()
    msg = ["☀️ Guten Morgen! Deine Tagesübersicht"]
    if d.get("has_key") and d.get("valid"):
        msg.append(f"\U0001F324 Jetzt: {d['temp']}°C (gefühlt {d['feels']}), {d['desc'] or '-'}")
        t = d.get("today") or {}
        if t.get("valid"):
            msg.append(f"Heute: {t['tmin']}–{t['tmax']}°C, {t['desc'] or '-'}")
        msg += wetter_hinweise(d)
    else:
        msg.append(build_weather())
    dw = poller.data("dwd") or {}
    if dw.get("valid") and dw.get("count", 0) > 0:
        msg.append(f"⚠️ DWD: {dw['headline'] or 'Warnung aktiv'}"
                   + (f" (+{dw['count'] - 1} weitere)" if dw["count"] > 1 else ""))
    fl = next_feiertag_line()
    if fl:
        msg.append(fl)
    await send("\n".join(msg))
    await send(build_termine())


# --- Kommandos ----------------------------------------------------------------
async def cmd_new_termin(rest: str):
    tok, _, title = rest.strip().partition(" ")
    title = title.strip()
    d = None
    for pat, f in ((r"^(\d{4})-(\d{1,2})-(\d{1,2})$", lambda m: (m[1], m[2], m[3])),
                   (r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", lambda m: (m[3], m[2], m[1])),
                   (r"^(\d{1,2})\.(\d{1,2})\.?$", lambda m: (now().year, m[2], m[1]))):
        m = re.match(pat, tok)
        if m:
            y, mo, da = (int(x) for x in f(m))
            try:
                d = date(y, mo, da).isoformat()
            except ValueError:
                d = None
            break
    if not d or not title or not kalender.termin_add(d, "", title):
        await send("Konnte Termin nicht anlegen. Format: neu termin JJJJ-MM-TT Text")
    else:
        await send(f"✅ Termin gespeichert: {d[8:10]}.{d[5:7]}. {title}")


def build_innen() -> str:
    r = device.report().get("dht22") or {}
    if r.get("valid"):
        return f"\U0001F3E0 Innen: {r['temp_c']:.1f} °C, {r['hum_pct']:.0f}% rel. Luftfeuchte"
    return "\U0001F3E0 Innenraum-Sensor: noch kein Messwert."


def build_status() -> str:
    r = device.report()
    lines = []
    if device.online():
        lines.append(r.get("status") or "Infoscreen online")
    else:
        ls = device.last_seen()
        lines.append("Infoscreen: offline" + (f" (zuletzt {time.strftime('%d.%m. %H:%M', time.localtime(ls))})" if ls else ""))
    bad = [p.title for p in poller.POLLERS.values() if p.error]
    lines.append("home-central: " + ("alle Quellen ok" if not bad else "Fehler: " + ", ".join(bad)))
    return "\n".join(lines)


async def cmd_vorhersage():
    d = await _owm_fresh()
    if not d.get("has_key"):
        return await send("\U0001F324 Vorhersage: kein OpenWeatherMap-Key konfiguriert.")
    if not d.get("valid") and not d.get("fc_valid"):
        return await send("\U0001F324 Vorhersage noch nicht verfügbar.")
    msg = ["\U0001F324 Wettervorhersage"]
    if d.get("valid"):
        msg.append(f"Jetzt: {d['temp']}°C (gefühlt {d['feels']}), {d['desc'] or '-'}, "
                   f"{d['humidity']}% rF, Wind {d['wind_kmh']} km/h")
    for f in d.get("fc") or []:
        if f.get("valid"):
            msg.append(f"{f['label']}: {f['tmin']}–{f['tmax']}°C, {f['desc'] or '-'}")
    await send("\n".join(msg))


async def cmd_planes():
    p = poller.POLLERS["planes"]
    if p.pause_until <= time.time():   # Kontingent gesperrt -> letzten Stand zeigen
        try:
            p.data = await extra.planes(route_cap=extra.PLANES_MAX)
            p.ok_ts, p.error = time.time(), ""
        except Exception:
            pass
    d = p.data
    if not d or not d.get("valid"):
        return await send("✈ Flugzeuge: Abruf fehlgeschlagen.")
    if not d["count"]:
        return await send("✈ Zurzeit keine Flugzeuge in der Nähe.")
    msg = [f"✈ Flugzeuge in der Nähe ({d['count']})"]
    for a in d["ac"][:6]:
        cs = a["flight"] or a["reg"] or "?"
        route = f" {a['from'] or '?'}→{a['to'] or '?'}" if a["from"] or a["to"] else ""
        msg.append(f"{cs}{route}{' ' + a['type'] if a['type'] else ''}  {a['dst_nm']:.0f}nm")
    await send("\n".join(msg))


async def cmd_feiertag():
    if not kalender.feiertage():
        return await send("\U0001F389 Keine Feiertage – Bundesland in den Einstellungen wählen.")
    today = today_iso()
    fe = [f for f in kalender.feiertage() if f["date"] >= today][:3]
    if not fe:
        return await send("\U0001F389 Keine kommenden Feiertage.")
    await send("\U0001F389 Nächste Feiertage:" + "".join(
        f"\n{f['date'][8:10]}.{f['date'][5:7]}. {f['name']} ({_when(_days_until(f['date']))})" for f in fe))


async def cmd_alarm():
    msg = ["\U0001F6A8 Aktive Warnungen:"]
    d = poller.data("dwd") or {}
    if d.get("valid") and d.get("count") and d.get("headline"):
        msg.append(f"⚠ DWD ({d['severity'] or '?'}): {d['headline']}"
                   + (f" (+{d['count'] - 1} weitere)" if d["count"] > 1 else ""))
    n = poller.data("nina") or {}
    if n.get("valid") and n.get("count") and n.get("headline"):
        msg.append(f"\U0001F6A8 {n['provider'] or 'BBK'} ({n['severity'] or '?'}): {n['headline']}"
                   + (f" (+{n['count'] - 1} weitere)" if n["count"] > 1 else ""))
    s = poller.data("spessart")
    if s and simple.gust_kmh(s) >= BOE_WARN_KMH:
        msg.append(f"\U0001F4A8 Windböen: {s['gust'] or '?'} km/h (spessartwetter)")
    await send("\n".join(msg) if len(msg) > 1 else "✅ Keine aktiven Warnungen.")


async def cmd_nacht():
    sp = (poller.data("space") or {}).get("passes") or {}
    msg = ["\U0001F6F0 Sichtbare Überflüge (heute Nacht):"]
    if not sp.get("has_key"):
        msg.append("(N2YO-Key fehlt – in den Einstellungen setzen)")
    elif not sp.get("valid") or not sp.get("count"):
        msg.append("keine sichtbaren Überflüge")
    else:
        for a in sp["p"][:6]:
            hm = time.strftime("%H:%M", time.localtime(a["max_utc"])) if a["max_utc"] else "--:--"
            msg.append(f"{hm} {a['name']} – {a['max_el']}° {a['dir']}"
                       + (f", mag {a['mag']:.1f}" if a["mag"] < 90 else ""))
    await send("\n".join(msg))
    ne = (poller.data("space") or {}).get("neos") or {}
    msg = ["☄ Erdnahe Objekte heute:"]
    if not ne.get("valid") or not ne.get("count"):
        msg.append("keine")
    else:
        for o in ne["n"][:5]:
            msg.append(f"{o['name']}{' ⚠' if o['hazard'] else ''} – {o['miss_km'] / 1e6:.2f} Mio km, ⌀ {o['diam_m']:.0f} m")
    await send("\n".join(msg))


async def cmd_kurse():
    k = poller.data("kurse")
    if not k or not k.get("valid"):
        return await send("\U0001F4B1 Kurse noch nicht verfügbar.")
    arrow = lambda c, t: "↑" if c > t else "↓" if c < -t else "→"
    await send(f"\U0001F4B1 Kurse (24h):\n1 BTC: {k['btc_eur']:.0f} € {arrow(k['btc_chg'], 0.05)} {k['btc_chg']:+.1f} %\n"
               f"1 EUR: {k['eur_usd']:.4f} $ {arrow(k['eur_chg'], 0.05)} {k['eur_chg']:+.2f} %")


async def cmd_heizung():
    if heizung.enabled():
        await poller.refresh_now("heizung")
    p = poller.POLLERS.get("heizung")
    age = int(time.time() - p.ok_ts) if p and p.ok_ts else -1
    await send(heizung.build_text(poller.data("heizung"), age, p.error if p else ""))


async def cmd_heizung_verlauf(low: str):
    """'heizung verlauf [stunden]' -> Diagramm aus der Verlaufs-Datenbank."""
    m = re.search(r"verlauf\s+(\d{1,3})", low)
    hours = int(m.group(1)) if m else heizchart.DEFAULT_HOURS
    try:
        png, cap = await asyncio.to_thread(heizchart.render, hours)
    except heizchart.NoMatplotlib as e:
        return await send(f"\U0001F525 Heizungsverlauf: Diagramm nicht möglich ({e}).")
    except ValueError as e:
        return await send(f"\U0001F525 Heizungsverlauf: {e}.")
    if not await send_photo(png, cap):
        await send("\U0001F525 Heizungsverlauf: Bild konnte nicht gesendet werden.")


async def send_help():
    await send(
        f"\U0001F916 Kommandos (@{_st['botname'] or 'bot'} ...):\n"
        "• Wetter – aktuelle Spessartwetter-Werte\n"
        "• vorhersage – Wettervorhersage (OWM)\n"
        "• planes – Flugzeuge in der Nähe\n"
        "• feiertag – nächste Feiertage\n"
        "• innen – Innenraum-Temperatur/-Feuchte (DHT22)\n"
        "• internet – Internet-/Fritzbox-Daten\n"
        "• termin – die nächsten 5 Termine\n"
        "• status – Infoscreen + home-central\n"
        "• stacks – Stack-Reserve je Task (Infoscreen)\n"
        "• reboot – Infoscreen neu starten\n"
        "• alarm – aktive Warnungen (DWD/NINA/Böen)\n"
        "• nacht – sichtbare Satelliten + Asteroiden\n"
        "• kurse – BTC und EUR-USD (24h-Trend)\n"
        "• heizung – Zustand, Störung und Werte der Heizung (esp-p4d)\n"
        f"• heizung verlauf [Stunden] – Temperatur-Diagramm (ohne Angabe {heizchart.DEFAULT_HOURS} h)\n"
        "• kino – aktuelles Kinoprogramm\n"
        "• kino preview – Programm ab nächster Woche\n"
        "• neu termin JJJJ-MM-TT Text – Termin anlegen")


async def handle_command(text: str):
    bot = _st["botname"]
    low = text.lower()
    if not bot or f"@{bot}" not in low:
        return
    i = low.find("neu termin")
    if i >= 0:
        return await cmd_new_termin(text[i + len("neu termin"):])
    if "kino" in low:
        return await send(extra.kino_text(poller.data("kino"), preview="kino preview" in low))
    if "vorhersage" in low:
        return await cmd_vorhersage()
    if "planes" in low:
        return await cmd_planes()
    if "feiertag" in low:
        return await cmd_feiertag()
    if "stacks" in low:
        return await send(device.report().get("stacks") or "Keine Stack-Daten vom Infoscreen.", fwd=False)
    if "alarm" in low:
        return await cmd_alarm()
    if "nacht" in low:
        return await cmd_nacht()
    if "kurse" in low:
        return await cmd_kurse()
    if "heizung" in low and "verlauf" in low:
        return await cmd_heizung_verlauf(low)
    if "heizung" in low:
        return await cmd_heizung()
    if "reboot" in low:
        device.queue_cmd("reboot")
        return await send("\U0001F504 Infoscreen startet beim nächsten Abruf neu …")
    parts = []
    if "wetter" in low:
        parts.append(build_weather())
    if "innen" in low:
        parts.append(build_innen())
    if "internet" in low:
        parts.append(build_internet())
    if "termin" in low:
        parts.append(build_termine())
    if "status" in low:
        parts.append("\U0001F4CA " + build_status())
    await (send("\n".join(parts)) if parts else send_help())


# --- Bot-Schleife -------------------------------------------------------------
async def _get_updates(timeout: int) -> list:
    r = await net.client.get(f"{API}{config.get('tg_token')}/getUpdates",
                             params={"timeout": timeout, "limit": 20, "offset": _st["offset"]},
                             timeout=timeout + 10)
    r.raise_for_status()
    return r.json().get("result") or []


async def _process(updates: list, execute: bool):
    chat = str(config.get("tg_chat")).strip()
    for u in updates:
        _st["offset"] = max(_st["offset"], u.get("update_id", 0) + 1)
        m = u.get("message")
        if not isinstance(m, dict) or not isinstance(m.get("text"), str):
            continue
        if str((m.get("chat") or {}).get("id")) != chat:
            continue
        sender = (m.get("from") or {}).get("first_name", "?")
        _hist_add(sender, m["text"])
        if execute and not _is_command(m["text"]):
            await ntfy.group_message(sender, m["text"])
        if execute:
            try:
                await handle_command(m["text"])
            except Exception:
                log.exception("Kommando fehlgeschlagen")


def _is_command(text: str) -> bool:
    """Slash-Befehle (/start ...) nicht an ntfy weiterleiten. @bot-Fragen schon,
    damit die (ebenfalls weitergeleitete) Antwort verstaendlich ist."""
    return text.lstrip().startswith("/")


async def _fetch_botname():
    try:
        r = await net.client.get(f"{API}{config.get('tg_token')}/getMe", timeout=10)
        _st["botname"] = (r.json().get("result") or {}).get("username", "").lower()
        log.info("Bot-Username: @%s", _st["botname"])
    except Exception as e:
        log.warning("getMe: %s", e)


async def bot_loop():
    token_seen = None
    while True:
        if not configured():
            await asyncio.sleep(5)
            continue
        if config.get("tg_token") != token_seen:   # Token geaendert -> neu abgleichen
            token_seen = config.get("tg_token")
            _st.update(primed=False, botname="", offset=0)
        try:
            if not _st["botname"]:
                await _fetch_botname()
            if not _st["primed"]:
                # Startabgleich: vorhandene Updates nur in den Verlauf (keine alten Befehle ausfuehren)
                await _process(await _get_updates(0), execute=False)
                _st["primed"] = True
            await _process(await _get_updates(25), execute=True)
        except Exception as e:
            log.warning("getUpdates: %s", e)
            await asyncio.sleep(10)


async def _warn(kind: str, key: str, text: str):
    """Warnung je Kanal genau einmal melden; bei Fehlschlag spaeter erneut."""
    if configured() and _warned.get(("tg", kind)) != key:
        if await send(text, fwd=False):
            _warned[("tg", kind)] = key
    if ntfy.enabled() and config.get("ntfy_warn") == "1" and _warned.get(("ntfy", kind)) != key:
        if await ntfy.warning(text):
            _warned[("ntfy", kind)] = key
    if meshcore.push_enabled(kind) and _warned.get(("mesh", kind)) != key:
        if await meshcore.push(text):
            _warned[("mesh", kind)] = key


async def _clear(kind: str, text: str):
    """Entwarnung dort senden, wo vorher gewarnt wurde."""
    if _warned.pop(("tg", kind), None) is not None and configured():
        await send(text, fwd=False)
    if _warned.pop(("ntfy", kind), None) is not None and ntfy.enabled():
        await ntfy.warning(text)
    _warned.pop(("mesh", kind), None)


def _dur(sec: float) -> str:
    m = int(sec // 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


async def fire_off_text(start: float, end: float, known: bool) -> str:
    """Meldung 'Feuer aus' mit Heizdauer, Puffer und den Kessel-Werten dazu."""
    if not known:   # home-central lief beim Anheizen nicht: Beginn aus dem Verlauf
        spans = await asyncio.to_thread(heizchart.fire_spans, int(end) - 86400, int(end))
        if spans and spans[-1][0] < start:
            start, known = spans[-1][0], True
    def t(ts):
        return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")
    lines = ["\U0001F525 Heizung: Feuer aus.",
             f"Heizphase {t(start)}–{t(end)} ({'' if known else 'mind. '}{_dur(end - start)})"]
    try:
        vals = (await heizung._get("/api/v1/values")).get("values") or []
    except Exception:  # noqa: BLE001 - Meldung dann ohne Werte
        vals = []
    by_key = {v.get("k"): v for v in vals}
    roles = hz_roles()
    puffer = [by_key.get(roles.get(r)) for r in ("puffer_oben", "puffer_mitte", "puffer_unten")]
    if all(p and p.get("v") is not None for p in puffer):
        lines.append("Puffer oben/mitte/unten: " + " / ".join(f"{p['v']:.0f}" for p in puffer) + " °C")
    for word in ("pufferladezustand", "holzmenge"):
        v = next((x for x in vals if word in str(x.get("n") or "").lower() and x.get("v") is not None), None)
        if v:
            lines.append(f"{v['n']}: {heizung.fmt_value(v)}")
    return "\n".join(lines)


async def _check_heizung():
    if not heizung.enabled():
        heizung.fire_events.clear()
        return
    while heizung.fire_events:
        start, end, known = heizung.fire_events.pop(0)
        if config.get("hz_push_fire") == "1" and time.time() - end < 3600:
            await _warn("hz_fire", str(int(end)), await fire_off_text(start, end, known))
    j = poller.data("heizung") or {}
    if config.get("hz_push_err") == "1":
        err = j.get("error")
        if err:
            await _warn("hz_err", err, f"\U0001F525\U0001F6A8 Heizungsstörung: {err}")
        elif j:
            await _clear("hz_err", "✅ Heizungsstörung behoben.")
    if config.get("hz_push_off") == "1":
        reason = heizung.offline_reason()
        if reason:
            await _warn("hz_off", "on", f"⚠ Heizung: {reason} (seit über {heizung.OFFLINE_AFTER // 60} min).")
        else:
            await _clear("hz_off", "✅ Heizung: Daten kommen wieder.")


async def _check_pushes():
    d = poller.data("dwd") or {}
    if d.get("valid") and d.get("count") and d.get("headline"):
        await _warn("dwd", d["headline"], f"⚠ Wetterwarnung: {d['headline']}")
    n = poller.data("nina") or {}
    if n.get("valid") and n.get("count") and n.get("headline"):
        await _warn("nina", n["headline"], f"🚨 Warnung ({n['provider'] or 'BBK'}): {n['headline']}")
    s = poller.data("spessart")
    if s and s.get("valid"):
        g = simple.gust_kmh(s)
        if g >= BOE_WARN_KMH:
            await _warn("boe", "on", f"💨 Windböen: {s['gust'] or '?'} km/h (spessartwetter)")
        elif g < BOE_WARN_KMH - 5:
            _warned.pop(("tg", "boe"), None)
            _warned.pop(("ntfy", "boe"), None)
            _warned.pop(("mesh", "boe"), None)
    await _check_heizung()


async def _check_schedules():
    n = now()
    today = n.date()
    # 6-Uhr-Bericht; kein Nachholen, wenn der Dienst nach 6 Uhr startet
    if _st["summary_day"] is None:
        _st["summary_day"] = today if n.hour >= SUMMARY_HOUR else False
    elif n.hour >= SUMMARY_HOUR and _st["summary_day"] != today:
        _st["summary_day"] = today
        await send_daily_summary()
    if _st["kino_day"] is None:
        _st["kino_day"] = today if (n.weekday() == 6 and n.hour >= KINO_SUNDAY_HOUR) else False
    elif n.weekday() == 6 and n.hour >= KINO_SUNDAY_HOUR and _st["kino_day"] != today:
        _st["kino_day"] = today
        await send(extra.kino_text(poller.data("kino")))


async def push_loop():
    await asyncio.sleep(20)   # Quellen erst einmal laufen lassen
    while True:
        if (ntfy.enabled() or meshcore.push_enabled()) and not configured():
            try:
                await _check_pushes()   # nur Warnungen an ntfy / ins Mesh
            except Exception:
                log.exception("Push-Pruefung fehlgeschlagen")
        if configured():
            try:
                if not _st["greeted"]:
                    await send(f"\U0001F504 home-central gestartet.\n{build_weather()}\n{build_internet()}", fwd=False)
                    _st["greeted"] = True
                await _check_pushes()
                await _check_schedules()
                alert = net.health_alert()
                if alert:
                    await send(alert, fwd=False)
                on = device.online()
                if _st["dev_online"] is not None and on != _st["dev_online"]:
                    await send("✅ Infoscreen wieder online." if on else
                               "⚠ Infoscreen seit >10 min nicht mehr gemeldet.", fwd=False)
                _st["dev_online"] = on
            except Exception:
                log.exception("Push-Pruefung fehlgeschlagen")
        await asyncio.sleep(30)
