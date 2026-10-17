"""OpenWeatherMap: aktuelle Werte (20 min) + Vorhersage (~3 h). Port von owm.c.

Liefert dieselben Felder wie owm_data_t/owm_fc_day_t. Unterschied zum ESP:
Slots werden nach LOKALEM Datum (aus "dt") den Tagen zugeordnet, nicht nach
dt_txt (UTC).
"""
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from .. import config, net
from ..tz import TZ

CUR_URL = "https://api.openweathermap.org/data/2.5/weather"
FC_URL = "https://api.openweathermap.org/data/2.5/forecast"
FC_EVERY_S = 3 * 3600
WD = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

_state = {"cur": None, "fc": None, "fc_ts": 0.0}


def _loc_params() -> dict:
    """Ortsname, sonst der allgemeine Standort (Breite/Laenge)."""
    loc = str(config.get("owm_loc")).strip()
    if loc:
        return {"q": loc}
    lat, lon = str(config.get("planes_lat")).strip(), str(config.get("planes_lon")).strip()
    if not lat or not lon:
        raise ValueError("nicht eingerichtet: Wetter-Ort oder Standort fehlt (Einstellungen)")
    return {"lat": lat, "lon": lon}


def _iround(v: float) -> int:
    return int(v + 0.5) if v >= 0 else int(v - 0.5)


async def _current(key: str) -> dict:
    j = await net.get_json(CUR_URL, params={**_loc_params(), "units": "metric", "lang": "de", "appid": key})
    w0 = (j.get("weather") or [{}])[0]
    main, wind = j.get("main") or {}, j.get("wind") or {}
    return {
        "valid": "temp" in main,
        "desc": w0.get("description", "")[:47],
        "icon": w0.get("icon", "")[:3],
        "temp": _iround(main.get("temp", 0)),
        "feels": _iround(main.get("feels_like", 0)),
        "humidity": int(main.get("humidity", 0)),
        "wind_kmh": _iround(wind.get("speed", 0) * 3.6),
        "fog_now": w0.get("id") == 741 or j.get("visibility", 99999) < 1000,
    }


def _empty_day(label: str) -> dict:
    return {"valid": False, "label": label, "tmin": 999, "tmax": -999, "desc": "", "icon": "",
            "rain_mm": 0.0, "snow_mm": 0.0, "gust_kmh": 0, "pop": 0, "thunder": False,
            "freezing": False, "fog": False, "tmin_morning": 999, "rain_from": -1, "_hd": 99}


async def _forecast(key: str) -> dict:
    j = await net.get_json(FC_URL, params={**_loc_params(), "units": "metric", "lang": "de", "appid": key})
    today = datetime.now(TZ).date()
    days = []
    for k in range(4):
        d = today + timedelta(days=k)
        label = ["Heute", "Morgen", "Übermorgen"][k] if k < 3 else WD[d.weekday()]
        days.append((d, _empty_day(label)))

    for e in j.get("list", []):
        main = e.get("main") or {}
        if "dt" not in e or not main:
            continue
        lt = datetime.fromtimestamp(e["dt"], TZ)
        for d, fc in days:
            if lt.date() != d:
                continue
            if "temp_min" in main:
                fc["tmin"] = min(fc["tmin"], _iround(main["temp_min"]))
            if "temp_max" in main:
                fc["tmax"] = max(fc["tmax"], _iround(main["temp_max"]))
            r3 = (e.get("rain") or {}).get("3h")
            s3 = (e.get("snow") or {}).get("3h")
            if r3 is not None:
                fc["rain_mm"] += r3
                if r3 >= 0.3 and fc["rain_from"] < 0:
                    fc["rain_from"] = lt.hour
            if s3 is not None:
                fc["snow_mm"] += s3
            if lt.hour <= 8 and "temp_min" in main:
                fc["tmin_morning"] = min(fc["tmin_morning"], _iround(main["temp_min"]))
            if e.get("visibility", 99999) < 1000:
                fc["fog"] = True
            gust = (e.get("wind") or {}).get("gust")
            if gust is not None:
                fc["gust_kmh"] = max(fc["gust_kmh"], _iround(gust * 3.6))
            if e.get("pop") is not None:
                fc["pop"] = max(fc["pop"], _iround(e["pop"] * 100))
            for w in e.get("weather") or []:
                wid = w.get("id", 0)
                if 200 <= wid < 300:
                    fc["thunder"] = True
                if wid == 511 or 611 <= wid <= 616:
                    fc["freezing"] = True
                if wid == 741:
                    fc["fog"] = True
            hd = abs(lt.hour - 13)   # Beschreibung/Icon vom Slot nahe Mittag
            if hd < fc["_hd"] and e.get("weather"):
                fc["_hd"] = hd
                fc["desc"] = e["weather"][0].get("description", "")[:31]
                fc["icon"] = e["weather"][0].get("icon", "")[:3]
            fc["valid"] = True
            break

    out = []
    for _, fc in days:
        fc.pop("_hd")
        fc["rain_mm"] = round(fc["rain_mm"], 1)
        fc["snow_mm"] = round(fc["snow_mm"], 1)
        out.append(fc)
    return {"fc_valid": any(fc["valid"] for fc in out[1:]), "today": out[0], "fc": out[1:]}


async def poll(force_fc: bool = False) -> dict:
    key = str(config.get("owm_key")).strip()
    if not key:
        _state.update(cur=None, fc=None, fc_ts=0.0)
        return {"has_key": False, "valid": False, "fc_valid": False}
    _state["cur"] = await _current(key)
    if force_fc or _state["fc"] is None or time.time() - _state["fc_ts"] >= FC_EVERY_S:
        try:
            _state["fc"] = await _forecast(key)
            _state["fc_ts"] = time.time()
        except Exception:
            if _state["fc"] is None:
                raise
    return {"has_key": True, **_state["cur"],
            **(_state["fc"] or {"fc_valid": False, "today": _empty_day("Heute"), "fc": []})}


async def poll_full() -> dict:
    """Aktuell + Vorhersage sofort (6-Uhr-Bericht, Bot "vorhersage")."""
    return await poll(force_fc=True)
