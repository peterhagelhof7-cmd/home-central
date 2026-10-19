"""Stuendliche Vorhersage von Open-Meteo (kostenlos, ohne Schluessel).

Fuer die Anfeuer-Empfehlung zaehlen Temperatur, Sonneneinstrahlung (Sonnen-
gewinn durch die Fenster), Bewoelkung, Wind und Luftfeuchte - OWM liefert uns
nur Tageswerte. Jeder Lauf (alle 3 h) wird mit seinem Zeitpunkt im Verlauf
gespeichert (history.forecast), damit spaeter die Trefferquote messbar ist.
Standort: planes_lat/planes_lon (wie Flugzeuge/DWD).
"""
import asyncio
import time

from .. import config, history, net

URL = "https://api.open-meteo.com/v1/forecast"
VARS = ["temperature_2m", "shortwave_radiation", "cloud_cover", "wind_speed_10m", "relative_humidity_2m"]


def parse(j: dict) -> list[tuple[str, int, float]]:
    h = j.get("hourly") or {}
    times = h.get("time") or []
    rows = []
    for var in VARS:
        for t, v in zip(times, h.get(var) or []):
            if v is not None:
                rows.append((var, int(t), float(v)))
    return rows


async def poll() -> dict:
    lat, lon = str(config.get("planes_lat")).strip(), str(config.get("planes_lon")).strip()
    if not lat or not lon:
        raise RuntimeError("Standort (Breiten-/Längengrad) nicht eingerichtet")
    j = await net.get_json(URL, params={"latitude": lat, "longitude": lon, "hourly": ",".join(VARS),
                                        "forecast_days": 3, "timeformat": "unixtime", "timezone": "UTC"},
                           timeout=20)
    rows = parse(j)
    if not rows:
        raise RuntimeError("Antwort ohne Stundenwerte")
    run = int(time.time()) // 3600 * 3600
    await asyncio.to_thread(history.store_forecast, run, rows)
    now = time.time()
    temps = [v for var, t, v in rows if var == "temperature_2m" and now <= t < now + 24 * 3600]
    sun = [v for var, t, v in rows if var == "shortwave_radiation" and now <= t < now + 24 * 3600]
    return {"valid": True, "hours": len({t for _, t, _ in rows}),
            "tmin24": min(temps) if temps else None, "tmax24": max(temps) if temps else None,
            "sun24_kwh_m2": round(sum(sun) / 1000, 2) if sun else None}
