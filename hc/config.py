"""Persistente Einstellungen (JSON-Datei) mit denselben Schluesseln wie der
esp-infoscreen, damit dessen Export 1:1 importiert werden kann.

Werte sind - wie im ESP-NVS - Strings. Ausnahme: "termine" ist eine Liste von
{"d": "YYYY-MM-DD", "t": "HH:MM"|"", "ti": "Titel"}.
"""
import json
import os
import secrets
import threading

DATA_DIR = os.environ.get("HC_DATA_DIR", "/var/lib/home-central")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
SECRETS_FILE = os.path.join(DATA_DIR, "secrets.json")

# Slide-Katalog des Infoscreens (Reihenfolge wie slides.c ALL_SLIDES).
SLIDES = [
    ("clock", "Uhrzeit & Datum"), ("owm", "Wetter"), ("planes", "Flugzeuge"),
    ("night", "Nachtobjekte"), ("cal", "Termine"), ("dwd", "Warnungen"),
    ("spessart", "Spessartwetter"), ("innen", "Innenraum"), ("kurse", "Kurse"),
    ("fritz", "Internet (Fritzbox)"), ("net", "Netzwerk"), ("wifi", "WLAN-Empfang"),
    ("msg", "Message Board"),
]

DEFAULTS = {
    # Anzeige (wird an den Infoscreen ausgeliefert)
    "brightness": "100", "bl_floor": "80", "slide_sec": "10", "rot180": "0",
    **{f"sl_{sid}": "1" for sid, _ in SLIDES},
    # Datenquellen
    "owm_key": "", "owm_loc": "",
    "nina_ars": "",
    "fb_host": "",
    "muell_src": "mymuell", "muell_id": "",
    "muell_aw_cl": "", "muell_aw_id": "",
    "muell_aw_ort": "",
    "muell_aw_lbl": "",
    "kino_id": "",
    "planes_lat": "", "planes_lon": "", "planes_radius": "20",
    "space_alt": "200", "n2yo_key": "", "nasa_key": "",
    "feiertage_bl": "BY",
    "tg_token": "", "tg_chat": "",
    "termine": [],
}

# Beim Import aus dem Infoscreen ignoriert (WLAN/IP bleiben im Geraet).
IMPORT_SKIP_PREFIX = ("wifi_",)
IMPORT_SKIP = {"dev_name"}

_lock = threading.RLock()
_cfg: dict = {}
_secrets: dict = {}


def _atomic_write(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load() -> None:
    global _cfg, _secrets
    os.makedirs(DATA_DIR, exist_ok=True)
    with _lock:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                _cfg = json.load(f)
        except FileNotFoundError:
            _cfg = {}
        try:
            with open(SECRETS_FILE, encoding="utf-8") as f:
                _secrets = json.load(f)
        except FileNotFoundError:
            _secrets = {}
        changed = False
        if "api_token" not in _secrets:
            _secrets["api_token"] = secrets.token_hex(16)
            changed = True
        if "session_key" not in _secrets:
            _secrets["session_key"] = secrets.token_hex(32)
            changed = True
        if changed:
            _atomic_write(SECRETS_FILE, _secrets)


def get(key: str):
    with _lock:
        if key in _cfg:
            return _cfg[key]
        v = DEFAULTS.get(key, "")
        return list(v) if isinstance(v, list) else v


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(str(get(key)).strip())
    except ValueError:
        return default


def set_many(values: dict) -> None:
    with _lock:
        _cfg.update(values)
        _atomic_write(CONFIG_FILE, _cfg)


def all_values() -> dict:
    """Alle Werte inkl. Defaults (fuer Export/Anzeige)."""
    with _lock:
        out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
        out.update(_cfg)
        return out


def secret(key: str) -> str:
    with _lock:
        return _secrets.get(key, "")


def set_secret(key: str, value: str) -> None:
    with _lock:
        _secrets[key] = value
        _atomic_write(SECRETS_FILE, _secrets)


def import_infoscreen(data: dict) -> tuple[int, list[str]]:
    """Export des esp-infoscreen uebernehmen (ohne WLAN). Liefert (Anzahl, uebersprungen)."""
    take, skipped = {}, []
    for k, v in data.items():
        if k.startswith(IMPORT_SKIP_PREFIX) or k in IMPORT_SKIP:
            skipped.append(k)
            continue
        if k == "termine":
            # im ESP als JSON-String gespeichert
            try:
                lst = json.loads(v) if isinstance(v, str) else v
                take[k] = [{"d": str(e.get("d", "")), "t": str(e.get("t", "")),
                            "ti": str(e.get("ti", ""))} for e in lst if isinstance(e, dict)]
            except (ValueError, TypeError):
                skipped.append(k)
            continue
        take[k] = str(v)
    set_many(take)
    return len(take), skipped
