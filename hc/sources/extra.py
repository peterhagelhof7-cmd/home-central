"""Kino (kino.de-Backend), Flugzeuge (adsb.lol + adsbdb), Weltraum (N2YO + NASA NeoWs).
Ports von kino.c, planes.c, space.c.
"""
import asyncio
import time
import logging
import math
from datetime import datetime, timedelta

from .. import config, net
from ..tz import TZ, now, today_iso

log = logging.getLogger("extra")

# --- Kino ---------------------------------------------------------------------
async def kino() -> dict:
    cid = str(config.get("kino_id")).strip() or ""
    j = await net.get_json(f"https://kntkapi.apps.stroeermb.de/api/cinema/{cid}/")
    movies = {}
    for m in j.get("movies") or []:
        if m.get("id") is None or not isinstance(m.get("title"), str):
            continue
        fsk = m.get("fsk")
        movies[str(m["id"]) if not isinstance(m["id"], float) else f"{m['id']:.0f}"] = {
            "title": m["title"][:79], "fsk": fsk if isinstance(fsk, int) else -1,
            "min_date": "", "max_date": ""}
    for s in j.get("showtimes") or []:
        mid = (s.get("movie") or {}).get("id")
        st = s.get("showtime")
        if mid is None or not isinstance(st, str) or str(mid) not in movies:
            continue
        try:
            d = datetime.fromisoformat(st.replace("Z", "+00:00")).astimezone(TZ).strftime("%Y-%m-%d")
        except ValueError:
            continue
        mv = movies[str(mid)]
        if not mv["min_date"] or d < mv["min_date"]:
            mv["min_date"] = d
        if not mv["max_date"] or d > mv["max_date"]:
            mv["max_date"] = d
    return {"valid": True, "cinema": j.get("name") or "Kino",
            "movies": [m for m in movies.values() if m["max_date"]]}


def _next_monday():
    n = now()
    nm = (n + timedelta(days=7 - n.weekday())).date()
    return nm.isoformat(), nm.strftime("%d.%m.")


def kino_text(d: dict | None, preview: bool = False) -> str:
    if not d or not d.get("valid"):
        return "\U0001F3AC Kinoprogramm noch nicht geladen."
    if preview:
        iso, human = _next_monday()
        head, frm = f"\U0001F3AC {d['cinema']} — ab Mo {human}:", iso
    else:
        head, frm = f"\U0001F3AC {d['cinema']} — Programm:", today_iso()
    lines = [head]
    shown = 0
    for m in d["movies"]:
        if m["max_date"] < frm:
            continue
        if sum(len(x) for x in lines) > 600:
            lines.append("…")
            break
        lines.append(f"• {m['title']}" + (f" (FSK{m['fsk']})" if m["fsk"] >= 0 else ""))
        shown += 1
    if not shown:
        lines.append("(keine Vorstellungen im Zeitraum)")
    return "\n".join(lines)


# --- Flugzeuge ----------------------------------------------------------------
PLANES_MAX = 8
_routes: dict[str, tuple[str, str]] = {}   # Callsign -> (von, nach), tagesstabil
_routes_day = ""


def _loc():
    return (str(config.get("planes_lat")).strip() or "",
            str(config.get("planes_lon")).strip() or "")


def _hav_nm(lat1, lon1, lat2, lon2):
    r = math.radians
    a = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _place(o) -> str:
    if not isinstance(o, dict):
        return ""
    return (o.get("municipality") or o.get("iata_code") or "")[:27]


async def _route(cs: str) -> tuple[str, str]:
    try:
        j = await net.get_json(f"https://api.adsbdb.com/v0/callsign/{cs}")
    except Exception:
        # 404 = unbekanntes Callsign -> auch merken (nicht jedes Mal neu fragen)
        return ("", "")
    fr = (j.get("response") or {}).get("flightroute") if isinstance(j.get("response"), dict) else None
    return (_place(fr.get("origin")), _place(fr.get("destination"))) if isinstance(fr, dict) else ("", "")


async def planes(route_cap: int = PLANES_MAX) -> dict:
    global _routes_day
    lat, lon = _loc()
    rad = str(config.get("planes_radius")).strip() or "20"
    url = f"https://api.adsb.lol/v2/point/{lat}/{lon}/{rad}"
    try:
        j = await net.get_json(url)
    except Exception:
        await asyncio.sleep(1.5)   # adsb.lol setzt gelegentlich zurueck -> ein Retry
        j = await net.get_json(url)
    olat, olon = float(lat), float(lon)
    acs = []
    for a in j.get("ac") or j.get("aircraft") or []:
        alt = a.get("alt_baro")
        if isinstance(a.get("dst"), (int, float)):
            dst = float(a["dst"])
        elif isinstance(a.get("lat"), (int, float)) and isinstance(a.get("lon"), (int, float)):
            dst = _hav_nm(olat, olon, a["lat"], a["lon"])
        else:
            dst = 9999.0
        acs.append({"flight": (a.get("flight") or "").strip()[:9], "type": (a.get("t") or "")[:7],
                    "reg": (a.get("r") or "")[:9],
                    "alt_ft": alt if isinstance(alt, int) else (-1 if isinstance(alt, str) else 0),
                    "gs_kt": int((a.get("gs") or 0) + 0.5),
                    "track": int(a["track"] + 0.5) if isinstance(a.get("track"), (int, float)) else -1,
                    "dst_nm": round(dst, 1), "from": "", "to": ""})
    acs.sort(key=lambda x: x["dst_nm"])
    acs = acs[:PLANES_MAX]
    if _routes_day != today_iso():
        _routes.clear()
        _routes_day = today_iso()
    fetched = 0
    for a in acs:
        cs = a["flight"]
        if not cs:
            continue
        if cs not in _routes and fetched < route_cap:
            _routes[cs] = await _route(cs)
            fetched += 1
        a["from"], a["to"] = _routes.get(cs, ("", ""))
    return {"valid": True, "count": len(acs), "ac": acs}


# --- Weltraum -----------------------------------------------------------------
CURATED = [25544, 48274, 20580, 23705, 28353]   # ISS, Tiangong, Hubble, 2x SL-16 R/B
_neo_state = {"data": None, "ts": 0.0}
NEO_EVERY_S = 6 * 3600


async def _passes() -> dict:
    key = str(config.get("n2yo_key")).strip()
    if not key:
        return {"has_key": False, "valid": False, "count": 0, "p": []}
    lat, lon = _loc()
    alt = str(config.get("space_alt")).strip() or "200"
    out, any_ok = [], False
    for sid in CURATED:
        try:
            j = await net.get_json(f"https://api.n2yo.com/rest/v1/satellite/visualpasses/"
                                   f"{sid}/{lat}/{lon}/{alt}/2/60/&apiKey={key}")
        except Exception:
            continue
        any_ok = True
        name = ((j.get("info") or {}).get("satname") or "?")[:23]
        for p in j.get("passes") or []:
            out.append({"name": name, "max_utc": int(p.get("maxUTC") or 0),
                        "max_el": int((p.get("maxEl") or 0) + 0.5), "dir": (p.get("maxAzCompass") or "")[:3],
                        "mag": float(p["mag"]) if isinstance(p.get("mag"), (int, float)) else 99.0})
    out.sort(key=lambda p: p["max_utc"])
    return {"has_key": True, "valid": any_ok, "count": min(len(out), 8), "p": out[:8]}


async def _neos() -> dict:
    key = str(config.get("nasa_key")).strip() or "DEMO_KEY"
    today = today_iso()
    j = await net.get_json("https://api.nasa.gov/neo/rest/v1/feed", timeout=40.0,
                           params={"start_date": today, "end_date": today, "api_key": key})
    out = []
    for e in (j.get("near_earth_objects") or {}).get(today, []):
        m = (e.get("estimated_diameter") or {}).get("meters") or {}
        cad = (e.get("close_approach_data") or [{}])[0]
        try:
            miss = float((cad.get("miss_distance") or {}).get("kilometers") or 0)
        except ValueError:
            miss = 0.0
        out.append({"name": (e.get("name") or "")[:27], "hazard": bool(e.get("is_potentially_hazardous_asteroid")),
                    "diam_m": round(((m.get("estimated_diameter_min") or 0) + (m.get("estimated_diameter_max") or 0)) / 2, 1),
                    "miss_km": miss if miss > 0 else 1e12,
                    "approach_utc": int((cad.get("epoch_date_close_approach") or 0) / 1000)})
    out.sort(key=lambda o: o["miss_km"])
    return {"has_key": True, "valid": True, "count": min(len(out), 8), "n": out[:8]}


async def space() -> dict:
    passes = await _passes()
    if _neo_state["data"] is None or time.time() - _neo_state["ts"] >= NEO_EVERY_S:
        try:
            _neo_state["data"] = await _neos()
            _neo_state["ts"] = time.time()
        except Exception as e:
            log.warning("NeoWs: %s: %s", type(e).__name__, e)
            _neo_state["ts"] = time.time() - NEO_EVERY_S + 15 * 60   # in 15 min erneut
            if _neo_state["data"] is None:
                _neo_state["data"] = {"has_key": True, "valid": False, "count": 0, "n": []}
    return {"passes": passes, "neos": _neo_state["data"]}


def space_wake_neos():
    _neo_state["ts"] = 0.0
