"""Verlauf: Messwerte fuer die spaetere Anfeuer-Empfehlung aufzeichnen.

SQLite-Datei DATA_DIR/history.db (Python-Standardbibliothek, keine Pakete).
Alle 5 min (auf 5 min ausgerichtet) werden aufgezeichnet:
  - hz:       ALLE Werte des esp-p4d (/api/v1/values, Schluessel = Adresse im Kessel)
  - p4d:      Vitalwerte des esp-p4d selbst (Signal, Chip-Temperatur, Speicher, COM1-Runde)
  - innen:    Temperatur/Feuchte vom Infoscreen (DHT22)
  - spessart: Temperatur, Wind, Boeen
  - owm:      Temperatur, Luftfeuchte
Zustandstexte (Betriebsart, Kesselzustand, Stoerung) bei jeder Aenderung in
"states" - daraus werden spaeter die Abbraende erkannt. Die stuendliche
Vorhersage (Open-Meteo, alle 3 h) landet in "forecast" mit Zeitpunkt des Laufs,
damit sich spaeter pruefen laesst, wie gut sie war.

Platz sparen: ein Wert wird nur geschrieben, wenn er sich geaendert hat, sonst
spaetestens alle 30 min. Damit Treppen im Diagramm stimmen, wird vor einer
Aenderung nach laengerer Pause der alte Wert 5 min vorher noch einmal gesetzt.
Rohdaten aelter als 90 Tage werden nachts zu Stundenmitteln verdichtet.

Welche Adresse "Puffer oben" usw. ist, steht in der Zuordnung (config hz_roles,
JSON {rolle: schluessel}); aufgezeichnet wird unabhaengig davon alles.
"""
import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time

from . import config, device, heizung, net, poller

log = logging.getLogger("history")

DB_FILE = os.path.join(config.DATA_DIR, "history.db")
STEP = 300                 # Aufzeichnungsraster (s)
KEEPALIVE = 1800           # unveraenderte Werte spaetestens so oft schreiben
RAW_DAYS = 90              # aelter -> Stundenmittel
MAX_AGE_HZ = 2 * STEP      # aelter gemeldete Kesselwerte nicht aufzeichnen

# Rollen fuer Diagramme und die spaetere Empfehlung: (id, Titel, Suchbegriffe)
# Keine Raum-Rolle: die S3200-Werte "Raumtemperatur" (je Heizkreis) sind Sollwerte
# (0x0018 konstant 20 °C, die anderen 0/-0,5), die echte Innentemperatur liefert der
# Infoscreen (Quelle "innen").
ROLES = [
    ("aussen", "Außentemperatur", ["außentemp", "aussentemp", "außen", "aussen"]),
    ("kessel", "Kesseltemperatur", ["kesseltemp", "kessel"]),
    ("abgas", "Abgastemperatur", ["abgastemp", "abgas"]),
    ("puffer_oben", "Puffer/Boiler oben", ["puffer oben", "pufferoben", "puffertemp oben", "boiler oben", "puffer o"]),
    ("puffer_mitte", "Puffer/Boiler mitte", ["puffer mitte", "puffermitte", "boiler mitte", "puffer m"]),
    ("puffer_unten", "Puffer/Boiler unten", ["puffer unten", "pufferunten", "boiler unten", "puffer u"]),
    ("warmwasser", "Warmwasser", ["warmwasser", "brauchwasser", "boilertemp oben", "boilertemp", "boiler"]),
    ("vorlauf_1", "Vorlauf Heizkreis 1", ["vorlauftemp 1", "vorlauf 1", "vorlauf hk1", "vorlauf hk 1", "vorlauf"]),
    ("vorlauf_2", "Vorlauf Heizkreis 2", ["vorlauftemp 2", "vorlauf 2", "vorlauf hk2", "vorlauf hk 2"]),
    ("mischer_1", "Mischer Heizkreis 1", ["mischer 1", "mischer hk1", "mischer hk 1", "mischer"]),
    ("mischer_2", "Mischer Heizkreis 2", ["mischer 2", "mischer hk2", "mischer hk 2"]),
    ("kollektor", "Solar-Kollektor", ["kollektortemp", "kollektor"]),
    ("solar_speicher", "Solar Speicher", ["solar speicher", "solarspeicher", "solar unten", "solar"]),
    ("solar_pumpe", "Solarpumpe", ["solarpumpe", "kollektorpumpe", "solar pumpe"]),
]
ROLE_IDS = [r[0] for r in ROLES]
# Vorschlag nur aus Werten mit passender Einheit (Anfang), sonst landet z. B. "Warmwasser"
# auf "Strömungsschalter an der Brauchwasser Leitung", "Solar" auf der WMZ-Leistung oder
# "Solarpumpe" auf "Laufzeit Kollektorpumpe" (h). esp-p4d: "°C", vor 0.2.2 nur "°".
ROLE_UNITS = {rid: "°" for rid in ("aussen", "kessel", "abgas", "puffer_oben", "puffer_mitte",
                                   "puffer_unten", "warmwasser", "vorlauf_1", "vorlauf_2", "kollektor",
                                   "solar_speicher")}
ROLE_UNITS["solar_pumpe"] = "%"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_series: dict[tuple[str, str], int] = {}            # (src, key) -> id
_last: dict[int, tuple[int, float]] = {}            # id -> (ts, v) zuletzt geschrieben
_last_seen: dict[int, tuple[int, float]] = {}       # id -> (ts, v) zuletzt gemessen
_last_state: dict[tuple[str, str], str] = {}
stats = {"last_run": 0.0, "last_rows": 0, "last_error": ""}


# --- Datenbank -------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
        c = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript("""
CREATE TABLE IF NOT EXISTS series(id INTEGER PRIMARY KEY, src TEXT NOT NULL, key TEXT NOT NULL,
  name TEXT, unit TEXT, UNIQUE(src, key));
CREATE TABLE IF NOT EXISTS samples(sid INTEGER NOT NULL, ts INTEGER NOT NULL, v REAL NOT NULL,
  PRIMARY KEY(sid, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS states(ts INTEGER NOT NULL, src TEXT NOT NULL, key TEXT NOT NULL, value TEXT);
CREATE INDEX IF NOT EXISTS states_ts ON states(ts);
CREATE TABLE IF NOT EXISTS forecast(var TEXT NOT NULL, target INTEGER NOT NULL, run INTEGER NOT NULL,
  v REAL, PRIMARY KEY(var, target, run)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
""")
        for sid, src, key in c.execute("SELECT id, src, key FROM series"):
            _series[(src, key)] = sid
        for sid, ts, v in c.execute(
                "SELECT s.sid, s.ts, s.v FROM samples s JOIN (SELECT sid, MAX(ts) m FROM samples GROUP BY sid) x "
                "ON s.sid = x.sid AND s.ts = x.m"):
            _last[sid] = _last_seen[sid] = (ts, v)
        for src, key, value in c.execute(
                "SELECT st.src, st.key, st.value FROM states st JOIN (SELECT src, key, MAX(ts) m FROM states "
                "GROUP BY src, key) x ON st.src = x.src AND st.key = x.key AND st.ts = x.m"):
            _last_state[(src, key)] = value or ""
        _conn = c
    return _conn


def _sid(c: sqlite3.Connection, src: str, key: str, name: str, unit: str) -> int:
    sid = _series.get((src, key))
    if sid is None:
        cur = c.execute("INSERT INTO series(src, key, name, unit) VALUES(?,?,?,?)", (src, key, name, unit))
        sid = cur.lastrowid
        _series[(src, key)] = sid
    else:
        c.execute("UPDATE series SET name=?, unit=? WHERE id=? AND (name IS NOT ? OR unit IS NOT ?)",
                  (name, unit, sid, name, unit))
    return sid


def write(ts: int, values: list[tuple[str, str, str, str, float]], states: list[tuple[str, str, str]]) -> int:
    """Ein Messzeitpunkt: values = [(src, key, name, unit, v)], states = [(src, key, text)].
    Liefert die Zahl geschriebener Zeilen."""
    n = 0
    with _lock:
        c = _db()
        with c:
            for src, key, name, unit, v in values:
                sid = _sid(c, src, key, name, unit)
                seen = _last_seen.get(sid)
                last = _last.get(sid)
                _last_seen[sid] = (ts, v)
                if last and last[1] == v and ts - last[0] < KEEPALIVE:
                    continue
                # Treppe: alten Wert kurz vor der Aenderung nochmal setzen
                if seen and last and seen[0] > last[0] and seen[1] != v:
                    c.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?)", (sid, seen[0], seen[1]))
                    n += 1
                c.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?)", (sid, ts, v))
                _last[sid] = (ts, v)
                n += 1
            for src, key, text in states:
                if _last_state.get((src, key)) == text:
                    continue
                c.execute("INSERT INTO states VALUES(?,?,?,?)", (ts, src, key, text))
                _last_state[(src, key)] = text
                n += 1
    return n


def store_forecast(run: int, rows: list[tuple[str, int, float]]) -> int:
    with _lock:
        c = _db()
        with c:
            c.executemany("INSERT OR REPLACE INTO forecast VALUES(?,?,?,?)",
                          [(var, target, run, v) for var, target, v in rows if v is not None])
    return len(rows)


def downsample(now: float | None = None) -> int:
    """Rohwerte aelter als RAW_DAYS zu Stundenmitteln verdichten (einmal je Bereich)."""
    cutoff = (int(now or time.time()) - RAW_DAYS * 86400) // 3600 * 3600
    with _lock:
        c = _db()
        row = c.execute("SELECT v FROM meta WHERE k='downsampled_until'").fetchone()
        done = int(row[0]) if row else 0
        if cutoff <= done:
            return 0
        with c:
            agg = c.execute("SELECT sid, ts / 3600 * 3600, AVG(v) FROM samples WHERE ts >= ? AND ts < ? "
                            "GROUP BY sid, ts / 3600", (done, cutoff)).fetchall()
            c.execute("DELETE FROM samples WHERE ts >= ? AND ts < ?", (done, cutoff))
            c.executemany("INSERT OR REPLACE INTO samples VALUES(?,?,?)", agg)
            c.execute("INSERT OR REPLACE INTO meta VALUES('downsampled_until', ?)", (str(cutoff),))
    log.info("Verlauf verdichtet bis %s: %d Stundenwerte", time.strftime("%d.%m.%Y", time.localtime(cutoff)), len(agg))
    return len(agg)


# --- Abfragen --------------------------------------------------------------------
def series_list() -> list[dict]:
    with _lock:
        c = _db()
        rows = c.execute("SELECT s.id, s.src, s.key, s.name, s.unit, COUNT(x.ts), MIN(x.ts), MAX(x.ts) "
                         "FROM series s LEFT JOIN samples x ON x.sid = s.id GROUP BY s.id ORDER BY s.src, s.name").fetchall()
    return [{"id": r[0], "src": r[1], "key": r[2], "name": r[3] or r[2], "unit": r[4] or "",
             "count": r[5], "first": r[6] or 0, "last": r[7] or 0} for r in rows]


def query(sids: list[int], since: int, until: int, points: int = 400) -> list[dict]:
    """Je Reihe hoechstens ~points Punkte (Mittel je Zeitfenster, mind. STEP)."""
    step = max(STEP, ((until - since) // max(points, 1)) // STEP * STEP)
    out = []
    with _lock:
        c = _db()
        meta = {r[0]: r for r in c.execute(
            f"SELECT id, name, unit, src, key FROM series WHERE id IN ({','.join('?' * len(sids))})", sids)} if sids else {}
        for sid in sids:
            if sid not in meta:
                continue
            pts = c.execute("SELECT ts / ? * ?, AVG(v) FROM samples WHERE sid = ? AND ts >= ? AND ts <= ? "
                            "GROUP BY ts / ? ORDER BY 1", (step, step, sid, since, until, step)).fetchall()
            m = meta[sid]
            out.append({"id": sid, "name": m[1] or m[4], "unit": m[2] or "", "src": m[3], "key": m[4],
                        "points": [[t, round(v, 2)] for t, v in pts]})
    return out


def states_between(since: int, until: int) -> list[dict]:
    with _lock:
        rows = _db().execute("SELECT ts, src, key, value FROM states WHERE ts >= ? AND ts <= ? ORDER BY ts",
                             (since, until)).fetchall()
    return [{"ts": r[0], "src": r[1], "key": r[2], "value": r[3]} for r in rows]


def csv_lines(since: int, until: int):
    """CSV fuer Excel (Semikolon, Dezimalkomma): Zeit;Quelle;Schluessel;Name;Einheit;Wert."""
    yield "Zeit;Quelle;Schlüssel;Name;Einheit;Wert\n"
    with _lock:
        c = _db()
        rows = c.execute("SELECT x.ts, s.src, s.key, s.name, s.unit, x.v FROM samples x JOIN series s ON s.id = x.sid "
                         "WHERE x.ts >= ? AND x.ts <= ? ORDER BY x.ts, s.src, s.key", (since, until)).fetchall()
        st = c.execute("SELECT ts, src, key, value FROM states WHERE ts >= ? AND ts <= ? ORDER BY ts",
                       (since, until)).fetchall()
    def clean(s):
        return str(s or "").replace(";", ",").replace("\n", " ")
    merged = [(r[0], r[1], r[2], r[3], r[4], f"{r[5]:g}".replace(".", ",")) for r in rows]
    merged += [(r[0], r[1], r[2], r[2], "", r[3]) for r in st]
    merged.sort(key=lambda r: r[0])
    for ts, src, key, name, unit, v in merged:
        yield f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))};{src};{clean(key)};{clean(name)};{clean(unit)};{clean(v)}\n"


def db_stats() -> dict:
    with _lock:
        c = _db()
        n = c.execute("SELECT COUNT(*), MIN(ts) FROM samples").fetchone()
        f = c.execute("SELECT COUNT(*) FROM forecast").fetchone()[0]
        s = c.execute("SELECT COUNT(*) FROM series").fetchone()[0]
    size = sum(os.path.getsize(DB_FILE + ext) for ext in ("", "-wal") if os.path.exists(DB_FILE + ext))
    return {"samples": n[0], "since": n[1] or 0, "series": s, "forecast": f, "size": size}


# --- Rollen ----------------------------------------------------------------------
def roles() -> dict[str, str]:
    try:
        r = json.loads(str(config.get("hz_roles") or "{}"))
        return {k: str(v) for k, v in r.items() if k in ROLE_IDS and v} if isinstance(r, dict) else {}
    except ValueError:
        return {}


def _norm(s: str) -> str:
    s = s.lower().replace("ß", "ss").replace("-", " ").replace("_", " ").replace(".", " ")
    return re.sub(r"\s+", " ", s).strip()


def _matches(words: str, name_tokens: list[str]) -> bool:
    """Jedes Suchwort ist der Anfang eines (anderen) Wortes im Namen:
    "puffer oben" passt auf "Puffertemperatur oben", "raum" nicht auf "Brennraum"."""
    free = list(name_tokens)
    for w in words.split():
        hit = next((t for t in free if t.startswith(w)), None)
        if hit is None:
            return False
        free.remove(hit)
    return True


def suggest_roles(values: list[dict]) -> dict[str, str]:
    """Vorschlag {rolle: schluessel} aus den Namen der Kesselwerte. Spezifische
    Begriffe zuerst; jeder Wert wird hoechstens einer Rolle zugeordnet.
    Rollen aus ROLE_UNITS nehmen nur Werte mit passender Einheit."""
    taken, out = set(), {}
    names = [(v.get("k"), _norm(v.get("n") or "").split(), str(v.get("u") or "")) for v in values if v.get("k")]
    for depth in range(max(len(r[2]) for r in ROLES)):   # erst die genauesten Begriffe aller Rollen, dann allgemeinere
        for rid, _, words in ROLES:
            if rid in out or depth >= len(words):
                continue
            w = _norm(words[depth])
            unit = ROLE_UNITS.get(rid, "")
            for k, n, u in names:
                if k not in taken and u.startswith(unit) and _matches(w, n):
                    out[rid] = k
                    taken.add(k)
                    break
    return out


# --- Recorder ----------------------------------------------------------------------
def _num(s) -> float | None:
    try:
        return float(str(s).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


async def collect(ts: int) -> tuple[list, list]:
    values, states = [], []
    if heizung.enabled():
        try:
            j = await heizung._get("/api/v1/values")
            if j.get("connected"):
                for v in j.get("values") or []:
                    age = v.get("age")
                    # nc = Fuehler nicht angeschlossen (esp-p4d ab 0.2.2, Kessel meldet dann 127 °C)
                    if v.get("v") is None or v.get("nc") or age is None or age > MAX_AGE_HZ:
                        continue
                    values.append(("hz", v["k"], v.get("n") or v["k"], v.get("u") or "", float(v["v"])))
        except Exception as e:  # noqa: BLE001 - dann eben ohne Kesselwerte
            stats["last_error"] = f"esp-p4d: {e}"[:200]
        s = poller.data("heizung") or {}
        p = poller.POLLERS.get("heizung")
        if s and p and p.ok_ts and ts - p.ok_ts < 2 * STEP:
            states += [("hz", "online", "1" if s.get("online") else "0"), ("hz", "mode", s.get("mode") or ""),
                       ("hz", "state", s.get("state") or ""), ("hz", "error", s.get("error") or "")]
        values += [("p4d", key, name, unit, v) for key, name, unit, v in heizung.vital_values()]
    r = device.report().get("dht22") or {}
    if device.online() and r.get("valid"):
        values += [("innen", "temp", "Innenraum Temperatur", "°C", float(r["temp_c"])),
                   ("innen", "hum", "Innenraum Luftfeuchte", "%", float(r["hum_pct"]))]
    sp, ps = poller.data("spessart"), poller.POLLERS.get("spessart")
    if sp and sp.get("valid") and ps and ps.ok_ts and ts - ps.ok_ts < 3600:
        for key, name, unit in (("temp", "Spessartwetter Temperatur", "°C"), ("wind", "Spessartwetter Wind", "km/h"),
                                ("gust", "Spessartwetter Böen", "km/h")):
            x = _num(sp.get(key))
            if x is not None:
                values.append(("spessart", key, name, unit, x))
    ow, po = poller.data("owm"), poller.POLLERS.get("owm")
    if ow and ow.get("valid") and po and po.ok_ts and ts - po.ok_ts < 3600:
        values += [("owm", "temp", "OWM Temperatur", "°C", float(ow.get("temp", 0))),
                   ("owm", "humidity", "OWM Luftfeuchte", "%", float(ow.get("humidity", 0)))]
    return values, states


async def run_once(ts: int | None = None) -> int:
    ts = int(ts or time.time()) // STEP * STEP
    stats["last_error"] = ""
    values, states = await collect(ts)
    n = await asyncio.to_thread(write, ts, values, states)
    stats.update(last_run=time.time(), last_rows=n)
    return n


async def loop():
    while True:
        now = time.time()
        await asyncio.sleep(STEP - now % STEP + 5)   # kurz nach der vollen 5. Minute
        try:
            await run_once()
            if time.localtime().tm_hour == 3 and time.localtime().tm_min < 10:
                await asyncio.to_thread(downsample)
        except Exception as e:  # noqa: BLE001
            stats["last_error"] = f"{type(e).__name__}: {e}"[:200]
            log.exception("Aufzeichnung fehlgeschlagen")
