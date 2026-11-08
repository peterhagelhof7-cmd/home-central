"""Medikamente (therapy): Bestand, Reichweite, Nachbestell-Hinweis.

Der Bestand wird nicht taeglich heruntergezaehlt, sondern berechnet:
Rest = gezaehlter Bestand am Stichtag - Dosis x Einnahmetage seit dem Stichtag.
Der Bestand am Stichtag gilt NACH der Einnahme dieses Tages; der heutige Rest
ebenso (heutige Einnahme schon abgezogen).

Gesundheitsdaten: eigene Datei therapy.json (nicht config.json -> nicht im
Infoscreen-Spiegel, nicht im Einstellungs-Export). Meldungen nur an Telegram.
"""
import json
import math
import os
import secrets
import threading
from datetime import date, timedelta

from .config import DATA_DIR, _atomic_write
from .tz import now

FILE = os.path.join(DATA_DIR, "therapy.json")
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
DEFAULT_LIMITS = {"pct": 30, "days": 14, "urgent": 7}
LIMIT_RANGES = {"pct": (1, 90), "days": (1, 120), "urgent": (0, 60)}
EPS = 1e-9

_lock = threading.RLock()
_data: dict | None = None


# --- Speicher -------------------------------------------------------------------
def _load() -> dict:
    global _data
    if _data is None:
        try:
            with open(FILE, encoding="utf-8") as f:
                _data = json.load(f)
        except FileNotFoundError:
            _data = {}
        _data.setdefault("meds", [])
        _data.setdefault("limits", dict(DEFAULT_LIMITS))
    return _data


def _save() -> None:
    _atomic_write(FILE, _data)


def meds() -> list[dict]:
    with _lock:
        return json.loads(json.dumps(_load()["meds"]))


def limits() -> dict:
    with _lock:
        return {**DEFAULT_LIMITS, **_load()["limits"]}


def set_limits(vals: dict) -> None:
    with _lock:
        lim = limits()
        for k, (lo, hi) in LIMIT_RANGES.items():
            try:
                lim[k] = max(lo, min(hi, int(str(vals.get(k, lim[k])).strip())))
            except ValueError:
                pass
        _load()["limits"] = lim
        _save()


def _find(mid: str) -> dict | None:
    return next((m for m in _load()["meds"] if m.get("id") == mid), None)


def _num(v, lo: float, hi: float, step: float) -> float:
    """Zahl mit Komma oder Punkt, auf step gerundet; ValueError bei Unsinn."""
    x = float(str(v).strip().replace(",", "."))
    if not math.isfinite(x):
        raise ValueError
    x = round(x / step) * step
    if not lo <= x <= hi:
        raise ValueError
    return x


def validate(form: dict) -> dict:
    """Formularwerte -> Medikament (ohne id/stand). ValueError mit Text bei Fehler."""
    name = str(form.get("name", "")).strip()[:60]
    if not name:
        raise ValueError("Name fehlt.")
    try:
        packung = int(_num(form.get("packung", ""), 1, 10000, 1))
    except ValueError:
        raise ValueError("Packungsgröße: ganze Zahl ab 1.") from None
    try:
        dosis = _num(form.get("dosis", ""), 0.5, 50, 0.5)
    except ValueError:
        raise ValueError("Dosis: Zahl in halben Schritten, z. B. 1 oder 0,5 oder 1,5.") from None
    try:
        bestand = _num(form.get("bestand", ""), 0, 100000, 0.5)
    except ValueError:
        raise ValueError("Bestand: Zahl in halben Schritten.") from None
    rhythmus = "woechentlich" if form.get("rhythmus") == "woechentlich" else "taeglich"
    try:
        wtag = int(form.get("wochentag", 0))
    except (TypeError, ValueError):
        wtag = 0
    return {"name": name, "person": str(form.get("person", "")).strip()[:40],
            "einheit": str(form.get("einheit", "")).strip()[:20] or "Stück",
            "packung": packung, "dosis": dosis, "rhythmus": rhythmus,
            "wochentag": wtag if 0 <= wtag <= 6 else 0, "bestand": bestand,
            "notiz": str(form.get("notiz", "")).strip()[:120]}


def upsert(form: dict, today: date | None = None) -> str:
    """Anlegen (ohne id) oder bearbeiten. Bestand gilt ab heute (Stichtag)."""
    today = today or now().date()
    m = validate(form)
    with _lock:
        mid = str(form.get("id", "")).strip()
        cur = _find(mid) if mid else None
        if cur is None:
            m.update(id=secrets.token_hex(4), aktiv=True, stand=today.isoformat(), urgent_sent="")
            _load()["meds"].append(m)
            mid = m["id"]
        else:
            # Bestand unveraendert gelassen -> Stichtag nicht verschieben (sonst
            # waere der Verbrauch seit dem alten Stichtag "vergessen")
            same_use = all(m[k] == cur.get(k) for k in ("dosis", "rhythmus", "wochentag"))
            if same_use and abs(m["bestand"] - rest(cur, today)) < EPS:
                m["bestand"], m["stand"] = cur["bestand"], cur["stand"]
            else:
                m["stand"] = today.isoformat()
            cur.update(m)
            cur["urgent_sent"] = ""
            mid = cur["id"]
        _save()
        return mid


def bought(mid: str, packs: int = 1, today: date | None = None) -> bool:
    today = today or now().date()
    with _lock:
        m = _find(mid)
        if not m:
            return False
        m["bestand"] = rest(m, today) + m["packung"] * packs
        m["stand"] = today.isoformat()
        m["urgent_sent"] = ""
        _save()
        return True


def counted(mid: str, value, today: date | None = None) -> bool:
    today = today or now().date()
    with _lock:
        m = _find(mid)
        if not m:
            return False
        m["bestand"] = _num(value, 0, 100000, 0.5)
        m["stand"] = today.isoformat()
        m["urgent_sent"] = ""
        _save()
        return True


def toggle(mid: str, today: date | None = None) -> bool:
    """Pausieren friert den Rest ein; Fortsetzen zaehlt ab heute weiter."""
    today = today or now().date()
    with _lock:
        m = _find(mid)
        if not m:
            return False
        m["bestand"] = rest(m, today)
        m["stand"] = today.isoformat()
        m["aktiv"] = not m.get("aktiv", True)
        m["urgent_sent"] = ""
        _save()
        return True


def delete(mid: str) -> bool:
    with _lock:
        lst = _load()["meds"]
        n = len(lst)
        lst[:] = [m for m in lst if m.get("id") != mid]
        if len(lst) != n:
            _save()
        return len(lst) != n


def mark_urgent(mid: str, day: date) -> None:
    with _lock:
        m = _find(mid)
        if m:
            m["urgent_sent"] = day.isoformat()
            _save()


def export_json() -> str:
    with _lock:
        return json.dumps(_load(), ensure_ascii=False, indent=1)


def import_json(raw: str) -> int:
    """Sicherung einspielen (ersetzt alles). Jeder Eintrag wird geprueft."""
    d = json.loads(raw)
    if not isinstance(d, dict) or not isinstance(d.get("meds"), list):
        raise ValueError("Keine therapy-Sicherung.")
    out = []
    for e in d["meds"]:
        if not isinstance(e, dict):
            continue
        m = validate(e)
        try:
            stand = date.fromisoformat(str(e.get("stand", "")))
        except ValueError:
            stand = now().date()
        m.update(id=str(e.get("id") or secrets.token_hex(4))[:16], aktiv=bool(e.get("aktiv", True)),
                 stand=stand.isoformat(), urgent_sent=str(e.get("urgent_sent", ""))[:10])
        out.append(m)
    with _lock:
        _load()["meds"] = out
        _data["limits"] = dict(DEFAULT_LIMITS)
        set_limits(d.get("limits") if isinstance(d.get("limits"), dict) else {})   # speichert
    return len(out)


# --- Rechnen --------------------------------------------------------------------
def _intake_days(m: dict, a: date, b: date) -> int:
    """Anzahl Einnahmetage d mit a < d <= b."""
    n = (b - a).days
    if n <= 0:
        return 0
    if m.get("rhythmus") != "woechentlich":
        return n
    first = a + timedelta(days=1)
    off = (m.get("wochentag", 0) - first.weekday()) % 7
    return 0 if off >= n else (n - 1 - off) // 7 + 1


def rest(m: dict, today: date) -> float:
    if not m.get("aktiv", True):
        return float(m["bestand"])
    stand = date.fromisoformat(m["stand"])
    return max(0.0, float(m["bestand"]) - m["dosis"] * _intake_days(m, stand, today))


def _next_intake(m: dict, after: date) -> date:
    """Erster Einnahmetag nach 'after'."""
    if m.get("rhythmus") != "woechentlich":
        return after + timedelta(days=1)
    return after + timedelta(days=(m.get("wochentag", 0) - after.weekday() - 1) % 7 + 1)


def status(m: dict, today: date, lim: dict | None = None) -> dict:
    """Rest, Reichweite (Tage bis zum ersten Einnahmetag ohne volle Dosis), Ampel."""
    lim = lim or limits()
    r = rest(m, today)
    pct = 100.0 * r / m["packung"] if m["packung"] else 0.0
    st = {"rest": r, "pct": pct, "aktiv": m.get("aktiv", True), "low": False, "urgent": False,
          "leer": None, "days": None, "reason": ""}
    if not st["aktiv"]:
        return st
    left = int(r / m["dosis"] + EPS)          # volle Einnahmen, die noch gehen
    step = 7 if m.get("rhythmus") == "woechentlich" else 1
    leer = _next_intake(m, today) + timedelta(days=step * left)   # erster Tag ohne volle Dosis
    st["leer"] = leer
    st["days"] = (leer - today).days - 1       # voll gedeckte Tage ab morgen
    low_pct = pct < lim["pct"]
    low_days = st["days"] < lim["days"]
    st["low"] = low_pct or low_days
    st["urgent"] = st["days"] < lim["urgent"]
    st["reason"] = " · ".join(x for x in (f"unter {lim['pct']} %" if low_pct else "",
                                          f"reicht unter {lim['days']} Tage" if low_days else "") if x)
    return st


# --- Texte ----------------------------------------------------------------------
def fmt_n(x: float) -> str:
    return (f"{x:.1f}".rstrip("0").rstrip(".")).replace(".", ",")


def dosis_text(m: dict) -> str:
    if m.get("rhythmus") == "woechentlich":
        return f"{fmt_n(m['dosis'])} {m['einheit']} jeden {WEEKDAYS[m.get('wochentag', 0)]}"
    return f"{fmt_n(m['dosis'])} {m['einheit']} täglich"


def _line(m: dict, st: dict) -> str:
    if not st["aktiv"]:
        return f"• {m['name']} – pausiert ({fmt_n(st['rest'])} {m['einheit']})"
    if st["rest"] < m["dosis"] - EPS:
        return f"• {m['name']} – aufgebraucht ({fmt_n(st['rest'])} {m['einheit']} übrig)"
    return (f"• {m['name']} – noch {fmt_n(st['rest'])} {m['einheit']} ({st['pct']:.0f} %), "
            f"reicht {days_text(st['days'])}, leer ab {st['leer'].strftime('%d.%m.')}")


def days_text(n: int) -> str:
    return "noch 1 Tag" if n == 1 else f"noch {n} Tage"


def _grouped(items: list[tuple[dict, dict]]) -> list[str]:
    out, persons = [], sorted({m.get("person", "") for m, _ in items}, key=lambda p: (p == "", p.lower()))
    for p in persons:
        sub = [(m, s) for m, s in items if m.get("person", "") == p]
        if len(persons) > 1 or p:
            out.append(f"{p or 'ohne Person'}:")
        out += [_line(m, s) for m, s in sorted(sub, key=lambda x: x[0]["name"].lower())]
    return out


def overview(today: date | None = None) -> list[dict]:
    """Fuer die Webseite: Medikamente mit Status, nach Person sortiert."""
    today = today or now().date()
    lim = limits()
    rows = [{**m, "st": status(m, today, lim), "dosis_txt": dosis_text(m)} for m in meds()]
    rows.sort(key=lambda r: (r.get("person", "") == "", r.get("person", "").lower(), r["name"].lower()))
    return rows


def bot_text(today: date | None = None) -> str:
    today = today or now().date()
    lim = limits()
    items = [(m, status(m, today, lim)) for m in meds()]
    if not items:
        return "\U0001F48A Keine Medikamente angelegt (Weboberfläche → Medikamente)."
    lines = ["\U0001F48A Medikamente – aktueller Bestand:"] + _grouped(items)
    low = [m["name"] for m, s in items if s["low"]]
    if low:
        lines.append(f"⚠ Nachbestellen: {', '.join(low)}")
    return "\n".join(lines)


def digest_text(today: date | None = None) -> str:
    """Sonntags-Meldung; leer, wenn nichts nachzubestellen ist."""
    today = today or now().date()
    lim = limits()
    items = [(m, s) for m in meds() if (s := status(m, today, lim))["low"]]
    if not items:
        return ""
    return "\n".join(["\U0001F48A Medikament nachbestellen:"] + _grouped(items))


def urgent_due(today: date | None = None) -> list[tuple[dict, str]]:
    """Medikamente, die sofort gemeldet werden muessen (einmal je Engpass)."""
    today = today or now().date()
    lim = limits()
    out = []
    for m in meds():
        s = status(m, today, lim)
        if s["urgent"] and not m.get("urgent_sent"):
            who = f" ({m['person']})" if m.get("person") else ""
            out.append((m, f"\U0001F6A8 Medikament nachbestellen: {m['name']}{who} reicht "
                           f"{days_text(s['days'])} (leer ab {s['leer'].strftime('%d.%m.')}), "
                           f"noch {fmt_n(s['rest'])} {m['einheit']}."))
    return out
