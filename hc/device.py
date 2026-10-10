"""Zustand des Infoscreens: was er per POST /api/v1/report meldet (DHT22,
WLAN, Status, Stacks, Slide-Katalog), Kommandos fuer den naechsten Abruf und
die Kopplung (Infoscreen findet home-central per mDNS und fragt per
/api/v1/pair an; Freigabe einmalig in der Weboberflaeche).
"""
import time

from . import config

_report: dict = {}
_last_seen = 0.0
_pending_cmd: str | None = None
_pending_pair: dict[str, dict] = {}   # MAC -> {name, fw, ip, ts}
OFFLINE_S = 10 * 60
PAIR_PENDING_S = 15 * 60


def update(rep: dict, ip: str) -> None:
    global _last_seen
    _report.clear()
    _report.update(rep)
    _report["ip"] = ip
    _last_seen = time.time()
    cat = rep.get("slides")
    if isinstance(cat, list) and cat:
        cat = [{"id": str(s.get("id", ""))[:12], "title": str(s.get("title", ""))[:40]}
               for s in cat if isinstance(s, dict) and s.get("id")]
        if cat and cat != config.get("slide_catalog"):
            config.set_many({"slide_catalog": cat})


def seen() -> None:
    """Abruf von /screen zaehlt ebenfalls als Lebenszeichen."""
    global _last_seen
    _last_seen = time.time()


def report() -> dict:
    return dict(_report)


def last_seen() -> float:
    return _last_seen


def online() -> bool:
    return _last_seen > 0 and time.time() - _last_seen < OFFLINE_S


def queue_cmd(cmd: str) -> None:
    global _pending_cmd
    _pending_cmd = cmd


def take_cmd() -> str | None:
    global _pending_cmd
    c, _pending_cmd = _pending_cmd, None
    return c


# --- Kopplung --------------------------------------------------------------------
def paired() -> dict:
    p = config.secret_obj("paired")
    return p if isinstance(p, dict) else {}


def pair_request(mac: str, name: str, fw: str, ip: str) -> bool:
    """True = freigegeben (Token darf ausgeliefert werden)."""
    p = paired()
    if mac in p:
        p[mac].update(name=name, fw=fw, ip=ip, last=time.time())
        config.set_secret_obj("paired", p)
        _pending_pair.pop(mac, None)
        return True
    _pending_pair[mac] = {"name": name, "fw": fw, "ip": ip, "ts": time.time()}
    return False


def pending() -> dict:
    now = time.time()
    for m in [m for m, e in _pending_pair.items() if now - e["ts"] > PAIR_PENDING_S]:
        _pending_pair.pop(m)
    return dict(_pending_pair)


def approve(mac: str) -> None:
    e = _pending_pair.pop(mac, None) or {}
    p = paired()
    p[mac] = {"name": e.get("name", "?"), "fw": e.get("fw", ""), "ip": e.get("ip", ""), "since": time.time()}
    config.set_secret_obj("paired", p)


def deny(mac: str) -> None:
    _pending_pair.pop(mac, None)


def unpair(mac: str) -> None:
    p = paired()
    if p.pop(mac, None) is not None:
        config.set_secret_obj("paired", p)
