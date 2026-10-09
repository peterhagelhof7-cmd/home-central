"""Feiertage (lokal berechnet, Port von feiertage.c) + eigene Termine."""
from datetime import date, timedelta

from .. import config
from ..tz import now, today_iso

BL_CODES = ["BW", "BY", "BE", "BB", "HB", "HH", "HE", "MV", "NI", "NW", "RP", "SL", "SN", "ST", "SH", "TH"]
BL_NAMES = {
    "": "Aus", "BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin", "BB": "Brandenburg",
    "HB": "Bremen", "HH": "Hamburg", "HE": "Hessen", "MV": "Mecklenburg-Vorpommern",
    "NI": "Niedersachsen", "NW": "Nordrhein-Westfalen", "RP": "Rheinland-Pfalz", "SL": "Saarland",
    "SN": "Sachsen", "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein", "TH": "Thüringen",
}
ALL = 0xFFFF
FIX = [(1, 1, ALL, "Neujahr"), (1, 6, 0x2003, "Heilige Drei Könige"), (3, 8, 0x0084, "Frauentag"),
       (5, 1, ALL, "Tag der Arbeit"), (8, 15, 0x0802, "Mariä Himmelfahrt"),
       (9, 20, 0x8000, "Weltkindertag"), (10, 3, ALL, "Tag der Deutschen Einheit"),
       (10, 31, 0xF1B8, "Reformationstag"), (11, 1, 0x0E03, "Allerheiligen"),
       (12, 25, ALL, "1. Weihnachtstag"), (12, 26, ALL, "2. Weihnachtstag")]
MOV = [(-2, ALL, "Karfreitag"), (1, ALL, "Ostermontag"), (39, ALL, "Christi Himmelfahrt"),
       (50, ALL, "Pfingstmontag"), (60, 0x0E43, "Fronleichnam")]


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e, f = b // 4, b % 4, (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(y, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)


def feiertage() -> list[dict]:
    """Feiertage des Bundeslandes fuer dieses + naechstes Jahr, sortiert."""
    bl = str(config.get("feiertage_bl"))
    if bl not in BL_CODES:
        return []
    bit = 1 << BL_CODES.index(bl)
    out = []
    for y in (now().year, now().year + 1):
        out += [{"date": date(y, m, d).isoformat(), "name": n} for m, d, mask, n in FIX if mask & bit]
        es = _easter(y)
        out += [{"date": (es + timedelta(days=o)).isoformat(), "name": n} for o, mask, n in MOV if mask & bit]
        if 0x1000 & bit:   # Buss- und Bettag (SN): Mittwoch vor dem 23. November
            d = date(y, 11, 22)
            while d.weekday() != 2:
                d -= timedelta(days=1)
            out.append({"date": d.isoformat(), "name": "Buß- und Bettag"})
    return sorted(out, key=lambda e: e["date"])


# --- eigene Termine (Config "termine") ----------------------------------------
MAX_TERMINE = 50


def termine() -> list[dict]:
    return [e for e in config.get("termine") if isinstance(e, dict)]


def termin_add(d: str, t: str, ti: str) -> bool:
    lst = termine()
    if not d or not ti or len(lst) >= MAX_TERMINE:
        return False
    lst.append({"d": d, "t": t or "", "ti": ti[:39]})
    config.set_many({"termine": lst})
    return True


def termin_delete(idx: int) -> None:
    lst = termine()
    if 0 <= idx < len(lst):
        lst.pop(idx)
        config.set_many({"termine": lst})


def termine_purge_past() -> int:
    today = today_iso()
    lst = termine()
    keep = [e for e in lst if e.get("d", "") >= today]
    if len(keep) != len(lst):
        config.set_many({"termine": keep})
    return len(lst) - len(keep)
