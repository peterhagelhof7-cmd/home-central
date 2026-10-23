"""home-central: Datenlieferant fuer den esp-infoscreen.

Startet die Datenquellen-Poller, den Telegram-Bot und die Weboberflaeche
(FastAPI/uvicorn). Start: python3 -m hc.main  (siehe deploy/home-central.service)
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import config, device, heizung, history, meshcore, net, ntfy, poller, telegram
from .sources import extra, kalender, muell, openmeteo, owm, simple
from .tz import now

__version__ = "0.9.2"
HERE = os.path.dirname(__file__)
log = logging.getLogger("hc")

# --- Log-Ringpuffer (Download in der Weboberflaeche) ---------------------------
LOGBUF: deque = deque(maxlen=2000)


class _RingHandler(logging.Handler):
    def emit(self, record):
        LOGBUF.append(self.format(record))


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), _RingHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# --- Poller-Definitionen (Intervalle wie im ESP) ------------------------------
def make_pollers():
    P = poller.Poller
    P("owm", "Wetter (OpenWeatherMap)", owm.poll, 20 * 60, retry=5 * 60)
    P("spessart", "Spessartwetter", simple.spessart, 30 * 60, retry=5 * 60)
    P("dwd", "DWD-Warnungen (Bright Sky)", simple.dwd, 15 * 60, retry=5 * 60)
    P("nina", "Warnungen (BBK/NINA)", simple.nina, 15 * 60, retry=5 * 60)
    P("kurse", "Kurse (Bitstamp)", simple.kurse, 5 * 60, retry=2 * 60)
    P("fritzbox", "Fritzbox (UPnP)", simple.fritzbox, 10, retry=30)
    P("muell", "Müllabfuhr", muell.poll, 6 * 3600, retry=15 * 60)
    P("kino", "Kino", extra.kino, 6 * 3600, retry=30 * 60)
    # adsb.lol: max. 500 Abfragen/Tag je Adresse -> alle 4 min (360/Tag, Rest für Bot/Neustarts)
    P("planes", "Flugzeuge (adsb.lol)", lambda: extra.planes(route_cap=3), 4 * 60, retry=5 * 60)
    P("space", "Nachtobjekte (N2YO/NASA)", extra.space, 2 * 3600, retry=15 * 60)
    P("meshcore", "MeshCore-Gateway (Kanäle → Telegram)", meshcore.poll, 15, retry=60)
    P("heizung", "Heizung (esp-p4d)", heizung.poll, 30, retry=60)
    P("openmeteo", "Stundenvorhersage (Open-Meteo)", openmeteo.poll, 3 * 3600, retry=30 * 60)


# Welche Poller nach einer Einstellungsaenderung sofort neu laufen sollen.
REFRESH_ON = {
    "owm_": ["owm"], "nina_": ["nina"], "fb_": ["fritzbox"], "muell_": ["muell"], "kino_": ["kino"],
    "planes_": ["planes", "space", "dwd", "openmeteo"], "space_": ["space"], "n2yo_": ["space"], "nasa_": ["space"],
    "mc_": ["meshcore"], "hz_": ["heizung"],
}


async def _housekeeping():
    while True:
        try:
            kalender.termine_purge_past()
        except Exception:
            log.exception("Termine aufraeumen")
        await asyncio.sleep(30 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    net.start()
    for p in poller.POLLERS.values():
        p.start()
    tasks = [asyncio.create_task(telegram.bot_loop()), asyncio.create_task(telegram.push_loop()),
             asyncio.create_task(_housekeeping()), asyncio.create_task(history.loop())]
    log.info("home-central %s gestartet", __version__)
    yield
    for t in tasks:
        t.cancel()
    await net.stop()


config.load()
make_pollers()
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.secret("session_key"),
                   session_cookie="hc_session", max_age=30 * 24 * 3600, same_site="lax")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


# --- Passwort / Login ----------------------------------------------------------
def hash_pw(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_pw(pw: str) -> bool:
    stored = config.secret("web_pw")
    if not stored:
        return False
    _, salt, h = stored.split("$")
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(salt)), stored)


def logged_in(req: Request) -> bool:
    return bool(req.session.get("auth"))


def need_login(req: Request):
    return None if logged_in(req) else RedirectResponse("/login", status_code=303)


def api_ok(req: Request) -> bool:
    tok = req.headers.get("x-hc-token") or req.query_params.get("token") or ""
    return hmac.compare_digest(tok, config.secret("api_token"))


_login_fail: dict[str, list[float]] = {}


@app.get("/login", response_class=HTMLResponse)
async def login_get(req: Request):
    return templates.TemplateResponse(req, "login.html", {"error": ""})


@app.post("/login")
async def login_post(req: Request, password: str = Form("")):
    ip = req.client.host if req.client else "?"
    fails = [t for t in _login_fail.get(ip, []) if time.time() - t < 300]
    if len(fails) >= 5:
        return templates.TemplateResponse(req, "login.html", {"error": "Zu viele Versuche – 5 min warten."})
    if check_pw(password):
        req.session["auth"] = True
        _login_fail.pop(ip, None)
        return RedirectResponse("/", status_code=303)
    _login_fail[ip] = fails + [time.time()]
    await asyncio.sleep(1)
    return templates.TemplateResponse(req, "login.html", {"error": "Falsches Passwort."})


@app.get("/logout")
async def logout(req: Request):
    req.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- Seiten --------------------------------------------------------------------
def _ago(ts: float) -> str:
    if not ts:
        return "nie"
    s = int(time.time() - ts)
    return f"vor {s} s" if s < 90 else f"vor {s // 60} min" if s < 5400 else f"vor {s // 3600} h"


@app.get("/", response_class=HTMLResponse)
async def index(req: Request):
    if (r := need_login(req)):
        return r
    sources = [{**p.status(), "ago": _ago(p.ok_ts)} for p in poller.POLLERS.values()]
    rep = device.report()
    hzp = poller.POLLERS["heizung"]
    hz = {"on": heizung.enabled(), "data": hzp.data, "ago": _ago(hzp.ok_ts), "error": hzp.error,
          "offline": heizung.offline_reason(), "fmt": heizung.fmt_value}
    return templates.TemplateResponse(req, "index.html", {
        "sources": sources, "dev": rep, "dev_online": device.online(), "dev_seen": _ago(device.last_seen()),
        "tg_ok": telegram.configured(), "version": __version__,
        "pending": device.pending(), "paired": device.paired(), "hz": hz})


@app.get("/settings", response_class=HTMLResponse)
async def settings_get(req: Request, msg: str = ""):
    if (r := need_login(req)):
        return r
    cfg = config.all_values()
    return templates.TemplateResponse(req, "settings.html", {
        "c": cfg, "slides": config.slide_catalog(), "termine": kalender.termine(), "bl": kalender.BL_NAMES,
        "catalog": muell.AWIDO_CATALOG, "msg": msg, "api_token": config.secret("api_token"),
        "host": req.url.hostname, "gw": simple.default_gateway(),
        "has": {k: bool(str(cfg.get(k, "")).strip()) for k in ("owm_key", "n2yo_key", "nasa_key", "tg_token", "mc_key", "hz_key")},
        "mc": meshcore.last, "mc_sel": meshcore.selected(), "mc_err": (poller.POLLERS["meshcore"].error if "meshcore" in poller.POLLERS else ""),
        "hz": heizung.last, "hz_err": poller.POLLERS["heizung"].error})


SECRET_KEYS = {"owm_key", "n2yo_key", "nasa_key", "tg_token", "mc_key", "hz_key"}
SLIDE_KEY = re.compile(r"^sl_[a-z0-9_]{1,12}$")
INT_LIMITS = {"brightness": (5, 100), "bl_floor": (0, 95), "slide_sec": (3, 120),
              "hz_buf_min": (0, 90), "hz_buf_max": (20, 100)}


@app.post("/settings/save")
async def settings_save(req: Request):
    if (r := need_login(req)):
        return r
    form = await req.form()
    vals = {}
    for cb in str(form.get("_checkboxes", "")).split(","):
        if cb in config.DEFAULTS or SLIDE_KEY.match(cb):
            vals[cb] = "1" if form.get(cb) else "0"
    if form.get("_mc_channels"):   # Kanal-Haken der MeshCore-Karte (Mehrfachauswahl)
        vals["mc_channels"] = "\n".join(c for c in form.getlist("mc_ch") if isinstance(c, str) and c.strip())
    for k, v in form.items():
        if k.startswith("_") or k not in config.DEFAULTS or k in ("termine", "mc_channels") or not isinstance(v, str):
            continue
        v = v.strip()
        if k in SECRET_KEYS and not v:
            continue   # leer = bisherigen Key behalten
        if k in INT_LIMITS:
            lo, hi = INT_LIMITS[k]
            try:
                v = str(max(lo, min(hi, int(v))))
            except ValueError:
                continue
        vals[k] = v
    if vals.get("ntfy_on") == "1" and not str(vals.get("ntfy_topic", config.get("ntfy_topic"))).strip():
        vals["ntfy_topic"] = ntfy.new_topic()
    if "ntfy_server" in vals and not vals["ntfy_server"].startswith(("https://", "http://")):
        return RedirectResponse("/settings?msg=ntfy-Server+muss+mit+https%3A%2F%2F+beginnen#ntfy", status_code=303)
    if vals.get("muell_src") == "awido" and not muell.awido_valid(vals.get("muell_aw_cl", ""), vals.get("muell_aw_id", "")):
        return RedirectResponse("/settings?msg=Ung%C3%BCltiger+AWIDO-Mandant+oder+-ID", status_code=303)
    changed = [k for k, v in vals.items() if config.get(k) != v]
    config.set_many(vals)
    todo = {p for k in changed for pre, ps in REFRESH_ON.items() if k.startswith(pre) for p in ps}
    if any(k in ("n2yo_key", "nasa_key") for k in changed):
        extra.space_wake_neos()
    poller.refresh(*todo)
    log.info("Einstellungen gespeichert: %s", ", ".join(changed) or "(keine Aenderung)")
    return RedirectResponse(f"/settings?msg=Gespeichert#{form.get('_card', '')}", status_code=303)


@app.post("/settings/termin/add")
async def termin_add(req: Request, date: str = Form(""), time_: str = Form("", alias="time"), title: str = Form("")):
    if (r := need_login(req)):
        return r
    kalender.termin_add(date.strip(), time_.strip(), title.strip())
    return RedirectResponse("/settings#termine", status_code=303)


@app.post("/settings/termin/del")
async def termin_del(req: Request, i: int = Form(-1)):
    if (r := need_login(req)):
        return r
    kalender.termin_delete(i)
    return RedirectResponse("/settings#termine", status_code=303)


@app.post("/settings/refresh/{name}")
async def refresh_source(req: Request, name: str):
    if (r := need_login(req)):
        return r
    if name == "owm":
        await telegram._owm_fresh()
    else:
        await poller.refresh_now(name)
    return RedirectResponse(req.headers.get("referer") or "/", status_code=303)


@app.get("/settings/export")
async def settings_export(req: Request):
    if (r := need_login(req)):
        return r
    data = config.all_values()
    data["termine"] = json.dumps(data["termine"], ensure_ascii=False, separators=(",", ":"))
    return Response(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="home-central-config.json"'})


@app.post("/settings/import")
async def settings_import(req: Request, file: UploadFile = File(...)):
    if (r := need_login(req)):
        return r
    try:
        data = json.loads((await file.read()).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return RedirectResponse("/settings?msg=Ung%C3%BCltige+JSON-Datei#system", status_code=303)
    n, skipped = config.import_infoscreen(data)
    log.info("Import: %d Werte, uebersprungen: %s", n, ", ".join(skipped))
    poller.refresh(*poller.POLLERS)
    extra.space_wake_neos()
    msg = f"{n} Einstellungen importiert" + (f" (ohne {', '.join(skipped)})" if skipped else "")
    return RedirectResponse(f"/settings?msg={msg}#system", status_code=303)


@app.post("/settings/password")
async def settings_password(req: Request, old: str = Form(""), new: str = Form(""), new2: str = Form("")):
    if (r := need_login(req)):
        return r
    if not check_pw(old):
        m = "Altes+Passwort+falsch"
    elif len(new) < 8 or new != new2:
        m = "Neues+Passwort+zu+kurz+(min.+8)+oder+ungleich"
    else:
        config.set_secret("web_pw", hash_pw(new))
        m = "Passwort+ge%C3%A4ndert"
    return RedirectResponse(f"/settings?msg={m}#system", status_code=303)


@app.post("/settings/token")
async def settings_token(req: Request):
    if (r := need_login(req)):
        return r
    config.set_secret("api_token", secrets.token_hex(16))
    return RedirectResponse("/settings?msg=Neuer+API-Token+(im+Infoscreen+eintragen)#system", status_code=303)


@app.post("/device/reboot")
async def device_reboot(req: Request):
    if (r := need_login(req)):
        return r
    device.queue_cmd("reboot")
    return RedirectResponse("/?msg=reboot", status_code=303)


@app.get("/log/download")
async def log_download(req: Request):
    if (r := need_login(req)):
        return r
    return PlainTextResponse("\n".join(LOGBUF) + "\n",
                             headers={"Content-Disposition": 'attachment; filename="home-central-log.txt"'})


# --- Message Board ---------------------------------------------------------------
@app.get("/tg/history.json")
async def tg_history(req: Request):
    if not logged_in(req):
        return JSONResponse([], status_code=401)
    return list(telegram.history)


@app.post("/tg/send")
async def tg_send(req: Request, text: str = Form("")):
    if not logged_in(req):
        return PlainTextResponse("login", status_code=401)
    if not text.strip():
        return PlainTextResponse("leer", status_code=400)
    ok = await telegram.send(text.strip()[:400], fwd=False)
    if ok:
        await ntfy.group_message("Message Board", text.strip()[:400])
    return PlainTextResponse("OK" if ok else "Telegram nicht konfiguriert oder Fehler", status_code=200 if ok else 503)


@app.post("/settings/ntfy/{action}")
async def ntfy_action(req: Request, action: str):
    if (r := need_login(req)):
        return r
    if action == "newtopic":
        config.set_many({"ntfy_topic": ntfy.new_topic()})
        msg = "Neuer Kanal erzeugt – in der ntfy-App neu abonnieren"
    elif action == "test":
        ok = await ntfy.publish("Test von home-central ✅", title="home-central", tags="white_check_mark", force=True)
        msg = "Testnachricht gesendet" if ok else "Senden fehlgeschlagen (Kanal/Server prüfen, Log)"
    else:
        msg = "?"
    return RedirectResponse(f"/settings?msg={quote(msg)}#ntfy", status_code=303)


@app.post("/settings/meshcore/test")
async def meshcore_test(req: Request):
    """Verbindung zum Gateway pruefen: Kanaele lesen (ohne etwas weiterzuleiten)."""
    if (r := need_login(req)):
        return r
    if not meshcore.base_url() or not str(config.get("mc_key")).strip():
        msg = "Adresse und API-Schlüssel eintragen"
    else:
        try:
            ch = await meshcore._get("/api/v1/channels")
            meshcore.last.update(gateway=ch.get("gateway", ""), channels=[c.get("name", "") for c in ch.get("channels", [])])
            msg = f"Verbunden mit {ch.get('gateway') or 'Gateway'}: {len(meshcore.last['channels'])} Kanäle"
        except Exception as e:  # noqa: BLE001
            msg = f"Fehler: {type(e).__name__}: {e or 'Gateway nicht erreichbar'}"[:200]
    return RedirectResponse(f"/settings?msg={quote(msg)}#meshcore", status_code=303)


# --- Verlauf (Aufzeichnung fuer die Anfeuer-Empfehlung) ---------------------------
@app.get("/heizung", response_class=HTMLResponse)
async def heizung_page(req: Request, msg: str = ""):
    if (r := need_login(req)):
        return r
    series = await asyncio.to_thread(history.series_list)
    hz_vals = [{"k": s["key"], "n": s["name"], "u": s["unit"]} for s in series if s["src"] == "hz"]
    return templates.TemplateResponse(req, "heizung.html", {
        "msg": msg, "series": series, "roles": history.ROLES, "assigned": history.roles(),
        "suggest": history.suggest_roles(hz_vals), "stats": await asyncio.to_thread(history.db_stats),
        "rec": history.stats, "hz_on": heizung.enabled(), "version": __version__})


@app.post("/heizung/rollen")
async def heizung_rollen(req: Request):
    if (r := need_login(req)):
        return r
    form = await req.form()
    roles = {rid: str(form.get(f"role_{rid}", "")).strip() for rid in history.ROLE_IDS}
    config.set_many({"hz_roles": json.dumps({k: v for k, v in roles.items() if v}, ensure_ascii=False)})
    log.info("Zuordnung der Heizungswerte gespeichert: %d Rollen", sum(1 for v in roles.values() if v))
    return RedirectResponse("/heizung?msg=Zuordnung+gespeichert", status_code=303)


@app.get("/ui/history.json")
async def ui_history(req: Request, ids: str = "", hours: int = 24):
    if not logged_in(req):
        return JSONResponse({}, status_code=401)
    hours = max(1, min(hours, 24 * 400))
    sids = [int(x) for x in ids.split(",") if x.strip().isdigit()][:20]
    until = int(time.time())
    since = until - hours * 3600
    data = await asyncio.to_thread(history.query, sids, since, until)
    states = await asyncio.to_thread(history.states_between, since, until)
    return {"since": since, "until": until, "series": data,
            "states": [s for s in states if s["key"] in ("state", "error", "online")]}


@app.get("/history/export.csv")
async def history_export(req: Request, days: int = 30):
    if (r := need_login(req)):
        return r
    days = max(1, min(days, 3650))
    until = int(time.time())
    lines = await asyncio.to_thread(lambda: list(history.csv_lines(until - days * 86400, until)))
    name = f"home-central-verlauf-{time.strftime('%Y%m%d')}-{days}d.csv"
    return StreamingResponse(iter(lines), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/settings/heizung/test")
async def heizung_test(req: Request):
    if (r := need_login(req)):
        return r
    if not heizung.base_url() or not str(config.get("hz_key")).strip():
        msg = "Erst Adresse und API-Schlüssel speichern."
    else:
        try:
            j = await heizung._get("/api/v1/summary")
            heizung.last.update(name=j.get("name", ""), count=len(j.get("values") or []))
            msg = (f"Verbunden mit {j.get('name') or 'esp-p4d'}: " +
                   ("Kessel antwortet" if j.get("online") else "Kessel antwortet NICHT auf COM1") +
                   f", {len(j.get('values') or [])} Werte")
        except Exception as e:  # noqa: BLE001
            msg = f"Fehler: {e}"
    return RedirectResponse(f"/settings?msg={quote(msg)}#heizung", status_code=303)


@app.get("/api/awido")
async def api_awido(req: Request, step: str = "", client: str = "", id: str = ""):
    if not logged_in(req):
        return JSONResponse([], status_code=401)
    try:
        return await muell.awido_lookup(step, client, id)
    except Exception as e:
        return PlainTextResponse(f"AWIDO-Abfrage fehlgeschlagen: {e}", status_code=502)


# --- Schnittstelle fuer den Infoscreen ---------------------------------------
def _src(name: str, max_age: float | None = None):
    """Daten + Alter (s) einer Quelle; None, wenn nie erfolgreich."""
    p = poller.POLLERS[name]
    return p.data, (int(time.time() - p.ok_ts) if p.ok_ts else -1)


def build_screen(take_cmd: bool) -> dict:
    """Gesamtpaket fuer den Infoscreen (und die Live-Slides im Web)."""
    c = config.all_values()
    out = {
        "v": 1, "server": __version__, "time": int(time.time()), "tz": "CET-1CEST,M3.5.0,M10.5.0/3",
        "cmd": device.take_cmd() if take_cmd else None,
        "cfg_hash": config_hash(),
        "display": {"brightness": int(c["brightness"]), "bl_floor": int(c["bl_floor"]),
                    "slide_sec": int(c["slide_sec"]), "rot180": c["rot180"] == "1",
                    "slides": {sid: c.get(f"sl_{sid}", "1") == "1" and (sid != "heizung" or heizung.enabled())
                               for sid, _ in config.slide_catalog()}},
        "age": {},
    }
    for name in ("owm", "spessart", "dwd", "nina", "kurse", "fritzbox", "planes", "space"):
        data, age = _src(name)
        out[name] = data
        out["age"][name] = age
    fb = poller.POLLERS["fritzbox"]
    if fb.error and out["fritzbox"]:
        out["fritzbox"] = {**out["fritzbox"], "reachable": False}
    m, out["age"]["muell"] = _src("muell")
    out["muell"] = (m or {}).get("entries", [])
    out["termine"] = [{"date": e["d"], "time": e.get("t", ""), "title": e["ti"]} for e in kalender.termine()]
    today = now().strftime("%Y-%m-%d")
    out["feiertage"] = [f for f in kalender.feiertage() if f["date"] >= today]
    # Heizung (esp-p4d): nur wenn eingerichtet; Infoscreen ab 1.4.0 zeigt sie als Slide
    hz = heizung.screen_block()
    if hz is not None:
        out["heizung"] = hz
    out["telegram"] = {"version": telegram.version, "msgs": list(telegram.history),
                       "configured": telegram.configured()}
    return out


@app.get("/api/v1/screen")
async def api_screen(req: Request):
    if not api_ok(req):
        return JSONResponse({"error": "token"}, status_code=401)
    device.seen()
    return build_screen(take_cmd=True)


@app.get("/ui/screen.json")
async def ui_screen(req: Request):
    """Wie /api/v1/screen, aber per Web-Login und ohne Kommandos zu verbrauchen;
    plus die Geraete-Meldung (Innenraum/WLAN) fuer die Live-Slides."""
    if not logged_in(req):
        return JSONResponse({}, status_code=401)
    out = build_screen(take_cmd=False)
    out["device"] = {**device.report(), "online": device.online()}
    out["catalog"] = [{"id": i, "title": t} for i, t in config.slide_catalog()]
    return out


@app.post("/api/v1/report")
async def api_report(req: Request):
    if not api_ok(req):
        return JSONResponse({"error": "token"}, status_code=401)
    try:
        rep = await req.json()
    except ValueError:
        return JSONResponse({"error": "json"}, status_code=400)
    device.update(rep if isinstance(rep, dict) else {}, req.client.host if req.client else "")
    return {"ok": True}


MIRROR_SKIP = {"slide_catalog", "brightness", "bl_floor", "slide_sec", "rot180"}


def config_mirror() -> dict:
    """Einstellungs-Kopie fuer den Infoscreen (Notbetrieb ohne home-central).
    Format wie sein eigener Export (alles Strings, termine als JSON-String).
    Anzeige-Werte laufen ueber /screen; leere Keys werden nicht gesendet, damit
    sie im Geraet nie einen vorhandenen Key loeschen."""
    out = {}
    for k, v in config.all_values().items():
        if k in MIRROR_SKIP or k.startswith(("sl_", "ntfy_", "mc_", "hz_")):
            continue
        if k == "termine":
            v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        v = str(v)
        if k in SECRET_KEYS and not v.strip():
            continue
        out[k] = v
    return out


def config_hash() -> str:
    return hashlib.sha1(json.dumps(config_mirror(), sort_keys=True).encode()).hexdigest()[:12]


@app.get("/api/v1/config")
async def api_config(req: Request):
    if not api_ok(req):
        return JSONResponse({"error": "token"}, status_code=401)
    return {"hash": config_hash(), "config": config_mirror()}


@app.post("/api/v1/pair")
async def api_pair(req: Request):
    """Kopplung: Infoscreen fragt mit seiner MAC an; nach Freigabe in der
    Weboberflaeche bekommt er den API-Token. Auch fuer Token-Erneuerung (401)."""
    try:
        j = await req.json()
    except ValueError:
        return JSONResponse({"error": "json"}, status_code=400)
    mac = str(j.get("mac", "")).lower()[:17]
    if not re.match(r"^[0-9a-f:]{17}$", mac):
        return JSONResponse({"error": "mac"}, status_code=400)
    ip = req.client.host if req.client else ""
    if device.pair_request(mac, str(j.get("name", "?"))[:31], str(j.get("fw", ""))[:31], ip):
        return {"status": "ok", "token": config.secret("api_token")}
    log.info("Kopplungsanfrage von %s (%s, %s)", j.get("name"), mac, ip)
    return {"status": "pending"}


@app.post("/api/v1/tg/send")
async def api_tg_send(req: Request):
    """Message Board des Infoscreens (Web) sendet ueber den Bot von home-central."""
    if not api_ok(req):
        return JSONResponse({"error": "token"}, status_code=401)
    try:
        text = str((await req.json()).get("text", "")).strip()[:400]
    except ValueError:
        text = ""
    if not text:
        return JSONResponse({"error": "leer"}, status_code=400)
    ok = await telegram.send(text, fwd=False)
    if ok:
        await ntfy.group_message("Infoscreen", text)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 503)


@app.post("/device/pair/{action}")
async def device_pair(req: Request, action: str, mac: str = Form("")):
    if (r := need_login(req)):
        return r
    if action == "approve":
        device.approve(mac)
        log.info("Infoscreen %s freigegeben", mac)
    elif action == "deny":
        device.deny(mac)
    elif action == "unpair":
        device.unpair(mac)
        # Token wechseln, damit das entkoppelte Geraet wirklich keinen Zugriff mehr hat;
        # weiterhin gekoppelte Geraete holen den neuen Token per /api/v1/pair.
        config.set_secret("api_token", secrets.token_hex(16))
        log.info("Kopplung %s entfernt, API-Token erneuert", mac)
    return RedirectResponse("/", status_code=303)


@app.get("/api/v1/health")
async def api_health():
    return {"ok": True, "version": __version__, "time": int(time.time())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set-password", metavar="PW", help="Web-Passwort setzen und beenden")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=80)
    a = ap.parse_args()
    if a.set_password:
        config.set_secret("web_pw", hash_pw(a.set_password))
        print("Passwort gesetzt.")
        return
    setup_logging()
    import uvicorn
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
