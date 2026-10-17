"""Kleinere Quellen: Spessartwetter, DWD (Bright Sky), NINA, Kurse, Fritzbox.
Ports von spessart.c, dwd.c, nina.c, kurse.c, fritzbox.c.
"""
import re
import xml.etree.ElementTree as ET

from .. import config, net

# --- Spessartwetter ----------------------------------------------------------
SPESSART_URL = "https://www.spessartwetter.de/webimg/custom.html"
_MON = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
_NUM = re.compile(r"(-?[\d.,]+)\s*$")


def _number_before(html: str, marker: str, start: int = 0) -> tuple[str, int]:
    """Zahl direkt vor dem ersten marker ab start. Liefert (zahl, pos_marker)."""
    p = html.find(marker, start)
    if p < 0:
        return "", -1
    m = _NUM.search(html[max(0, p - 30):p])
    return (m.group(1) if m else ""), p


def _parse_stand(html: str) -> dict | None:
    # "... wetterlage in <ort> am Sonntag, 27. September 2026 um 11:10"
    p = html.find("wetterlage")
    if p < 0:
        return None
    m = re.search(r",\s*(\d{1,2})\.\s*(\S+)\s+(\d{4})\s+um\s+(\d{1,2}):(\d{2})", html[p:p + 300])
    if not m:
        return None
    mon = next((i + 1 for i, n in enumerate(_MON) if m.group(2).startswith(n)), 0)
    if not mon:
        return None
    return {"st_day": int(m.group(1)), "st_mon": mon, "st_year": int(m.group(3)),
            "st_hour": int(m.group(4)), "st_min": int(m.group(5))}


async def spessart() -> dict:
    html = (await net.get(SPESSART_URL)).content.decode("iso-8859-1", "replace")
    temp, _ = _number_before(html, "°C")
    wind, p1 = _number_before(html, "km/h")
    gust = _number_before(html, "km/h", p1 + 4)[0] if p1 >= 0 else ""
    if not temp and not wind:
        raise ValueError("keine Werte gefunden (Layout geaendert?)")
    stand = _parse_stand(html)
    return {"valid": True, "temp": temp, "wind": wind, "gust": gust,
            "has_stand": stand is not None, **(stand or {})}


def gust_kmh(d: dict | None) -> float:
    try:
        return float((d or {}).get("gust", "").replace(",", "."))
    except ValueError:
        return 0.0


# --- DWD-Warnungen ueber Bright Sky -------------------------------------------
async def dwd() -> dict:
    lat, lon = str(config.get("planes_lat")).strip(), str(config.get("planes_lon")).strip()
    if not lat or not lon:
        raise ValueError("nicht eingerichtet: Standort (Breite/Laenge) fehlt (Einstellungen)")
    j = await net.get_json("https://api.brightsky.dev/alerts", params={"lat": lat, "lon": lon})
    alerts = j.get("alerts") or []
    a0 = alerts[0] if alerts else {}
    return {"valid": True, "count": len(alerts),
            "headline": (a0.get("headline_de") or "")[:95],
            "severity": (a0.get("severity") or "")[:15]}


# --- NINA / BBK ---------------------------------------------------------------
_SEV = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1}


async def nina() -> dict:
    ars = str(config.get("nina_ars")).strip()
    if not ars:
        raise ValueError("nicht eingerichtet: NINA-Regionalschluessel (ARS) fehlt (Einstellungen)")
    arr = await net.get_json(f"https://warnung.bund.de/api31/dashboard/{ars}.json")
    count, best, out = 0, -1, {"headline": "", "severity": "", "provider": ""}
    for e in arr if isinstance(arr, list) else []:
        data = (e.get("payload") or {}).get("data") or {}
        if data.get("msgType") == "Cancel":
            continue
        count += 1
        r = _SEV.get(data.get("severity"), 0)
        if r > best:
            best = r
            out = {"severity": (data.get("severity") or "")[:15],
                   "provider": (data.get("provider") or "")[:11],
                   "headline": ((e.get("i18nTitle") or {}).get("de") or data.get("headline") or "")[:95]}
    return {"valid": True, "count": count, **out}


# --- Kurse (Bitstamp) ---------------------------------------------------------
async def kurse() -> dict:
    e = await net.get_json("https://www.bitstamp.net/api/v2/ticker/btceur/")
    u = await net.get_json("https://www.bitstamp.net/api/v2/ticker/btcusd/")
    e_last, e_open = float(e["last"]), float(e["open_24"])
    u_last, u_open = float(u["last"]), float(u["open_24"])
    eur_usd = u_last / e_last
    eur_usd_24 = u_open / e_open
    return {"valid": True, "btc_eur": e_last, "eur_usd": eur_usd, "has_trend": True,
            "btc_chg": float(e.get("percent_change_24") or 0.0),
            "eur_chg": (eur_usd - eur_usd_24) / eur_usd_24 * 100.0 if eur_usd_24 > 0 else 0.0}


# --- Fritzbox (UPnP/IGD, Port 49000, ohne Login) ------------------------------
def default_gateway() -> str:
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if p[1] == "00000000":
                    g = int(p[2], 16)
                    return ".".join(str((g >> s) & 255) for s in (0, 8, 16, 24))
    except OSError:
        pass
    return ""


async def _soap(host: str, control: str, service: str, action: str) -> dict:
    body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service}"></u:{action}></s:Body></s:Envelope>')
    r = await net.client.post(f"http://{host}:49000{control}", content=body, timeout=4.0,
                              headers={"Content-Type": 'text/xml; charset="utf-8"',
                                       "SOAPAction": f"{service}#{action}"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    return {el.tag.split("}")[-1]: (el.text or "") for el in root.iter() if el.tag.startswith("New")}


async def fritzbox() -> dict:
    host = str(config.get("fb_host")).strip() or default_gateway()
    if not host:
        raise ValueError("keine Fritzbox-Adresse")
    d = {"reachable": False, "host": host, "external_ip": "", "down_max_bps": 0, "up_max_bps": 0,
         "down_rate_Bps": 0, "up_rate_Bps": 0}
    wan = "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1"
    try:
        v = await _soap(host, "/igdupnp/control/WANIPConn1",
                        "urn:schemas-upnp-org:service:WANIPConnection:1", "GetExternalIPAddress")
        d["external_ip"], d["reachable"] = v.get("NewExternalIPAddress", ""), True
    except Exception:
        pass
    try:
        v = await _soap(host, "/igdupnp/control/WANCommonIFC1", wan, "GetCommonLinkProperties")
        d["down_max_bps"] = int(v.get("NewLayer1DownstreamMaxBitRate") or 0)
        d["up_max_bps"] = int(v.get("NewLayer1UpstreamMaxBitRate") or 0)
        d["reachable"] = True
    except Exception:
        pass
    try:
        v = await _soap(host, "/igdupnp/control/WANCommonIFC1", wan, "GetAddonInfos")
        d["down_rate_Bps"] = int(v.get("NewByteReceiveRate") or 0)
        d["up_rate_Bps"] = int(v.get("NewByteSendRate") or 0)
        d["reachable"] = True
    except Exception:
        pass
    if not d["reachable"]:
        raise ConnectionError(f"Fritzbox {host} nicht erreichbar (UPnP aktiv?)")
    return d
