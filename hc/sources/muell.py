"""Muellabfuhr: MyMuell/jumomind ODER AWIDO (cubefour). Port von muell.c (v1.2.12).

Auf dem Server gibt es keinen Neustart-Zwang: ein Quellenwechsel greift beim
naechsten Abruf (die Einstellungsseite stoesst ihn sofort an).
"""
import re

from .. import config, net
from ..tz import now, today_iso

MAX_ENTRIES = 48
AWIDO = "https://awido.cubefour.de"

# AWIDO-Mandanten (keine Listen-API; Stand HA waste_collection_schedule, 09.10.2026 geprueft)
AWIDO_CATALOG = [
    ("awld", "Abfallwirtschaft Lahn-Dill-Kreis"), ("awb-altenburg", "Abfallwirtschaft Altenburger Land"),
    ("awv-isar-inn", "Abfallwirtschaft Isar-Inn"), ("rmk", "Abfallwirtschaft Rems-Murr (AWRM)"),
    ("awv-nordschwaben", "Abfall-Wirtschafts-Verband Nordschwaben"), ("azv-hef-rof", "AZV Hersfeld-Rotenburg"),
    ("awb-ak", "AWB Landkreis Altenkirchen"), ("awb-duerkheim", "AWB Landkreis Bad Dürkheim"),
    ("ffb", "AWB Landkreis Fürstenfeldbruck"), ("ebu", "EBU Ulm"),
    ("unterhaching", "Gemeinde Unterhaching"), ("ansbach", "Landkreis Ansbach"),
    ("lra-ab", "Landkreis Aschaffenburg"), ("bgl", "Landkreis Berchtesgadener Land"),
    ("coburg", "Landkreis Coburg"), ("ebe", "Landkreis Ebersberg"), ("erding", "Landkreis Erding"),
    ("fulda", "Landkreis Fulda"), ("lkgi", "Landkreis Gießen"), ("gifhorn", "Landkreis Gifhorn"),
    ("gotha", "Landkreis Gotha"), ("kaw-guenzburg", "Landkreis Günzburg"),
    ("kelheim", "Landkreis Kelheim"), ("kronach", "Landkreis Kronach"), ("kulmbach", "Landkreis Kulmbach"),
    ("lichtenfels", "Landkreis Lichtenfels"), ("lra-mue", "Landkreis Mühldorf a. Inn"),
    ("rosenheim", "Landkreis Rosenheim"), ("roth", "Landkreis Roth"),
    ("lra-schweinfurt", "Landkreis Schweinfurt"), ("eww-suew", "Landkreis Südliche Weinstraße"),
    ("kreis-tir", "Landkreis Tirschenreuth"), ("tuebingen", "Landkreis Tübingen"),
    ("landkreisbetriebe", "Landkreisbetriebe Neuburg-Schrobenhausen"),
    ("aic-fdb", "Landratsamt Aichach-Friedberg"), ("lra-dah", "Landratsamt Dachau"),
    ("lra-regensburg", "Landratsamt Regensburg"), ("neustadt", "Neustadt a.d. Waldnaab"),
    ("pullach", "Pullach im Isartal"), ("fulda-stadt", "Stadt Fulda"), ("kaufbeuren", "Stadt Kaufbeuren"),
    ("koenigstein", "Stadt Königstein im Taunus"), ("memmingen", "Stadt Memmingen"),
    ("regensburg", "Stadt Regensburg"), ("unterschleissheim", "Stadt Unterschleißheim"),
    ("wgv", "WGV Recycling GmbH"), ("zaso", "Zweckverband Abfallwirtschaft Saale-Orla"),
    ("zv-muc-so", "Zweckverband München-Südost"),
]
_CLIENT_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_GUID_RE = re.compile(r"^[0-9a-fA-F-]{1,40}$")


def awido_valid(client: str, oid: str) -> bool:
    return bool(_CLIENT_RE.match(client or "")) and bool(_GUID_RE.match(oid or ""))


async def _mymuell(today: str) -> list[dict]:
    mid = str(config.get("muell_id")).strip() or ""
    arr = await net.get_json("https://mymuell.jumomind.com/mmapp/api.php",
                             params={"r": "dates", "city_id": mid, "area_id": mid})
    out = [{"day": e["day"], "title": e.get("title", "")} for e in arr
           if isinstance(e, dict) and e.get("day", "") >= today]
    out.sort(key=lambda e: (e["day"], e["title"]))
    return out[:MAX_ENTRIES]


def _parse_ics(text: str, ort: str) -> list[dict]:
    text = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))   # Zeilen entfalten
    evs, cur = [], None
    for line in text.split("\n"):
        if line == "BEGIN:VEVENT":
            cur = {"day": "", "summary": "", "location": ""}
        elif line == "END:VEVENT" and cur is not None:
            if cur["day"] and cur["summary"]:
                evs.append(cur)
            cur = None
        elif cur is not None:
            key, _, val = line.partition(":")
            if key.startswith("DTSTART"):
                v = line.rsplit(":", 1)[-1]
                if len(v) >= 8:
                    cur["day"] = f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
            elif key.startswith("SUMMARY"):
                cur["summary"] = val
            elif key.startswith("LOCATION"):
                cur["location"] = val
    unesc = lambda s: re.sub(r"\\([nN])", " ", s).replace("\\;", ";").replace("\\,", ",").replace("\\\\", "\\")
    out = []
    for e in evs:
        title = unesc(e["summary"])
        cut = title.rfind(" in ")
        if cut > 0:
            title = title[:cut]
        out.append({"day": e["day"], "title": title,
                    "local": not ort or unesc(e["location"]).lower().startswith(ort.lower())})
    return out


async def _awido(today: str) -> list[dict]:
    cl = str(config.get("muell_aw_cl")).strip()
    oid = str(config.get("muell_aw_id")).strip()
    ort = str(config.get("muell_aw_ort")).strip()
    if not awido_valid(cl, oid):
        raise ValueError(f"ungueltiger AWIDO-Mandant/ID ({cl!r}/{oid!r})")
    year = now().year
    years = [year, year + 1] if now().month >= 10 else [year]
    evs = []
    for i, y in enumerate(years):
        try:
            r = await net.get(f"{AWIDO}/Customer/{cl}/KalenderICS.aspx",
                              params={"oid": oid, "jahr": y, "fraktionen": "", "reminder": "-1.17:00"})
        except Exception:
            if i == 0:
                raise
            continue   # Folgejahr evtl. noch nicht veroeffentlicht
        evs += _parse_ics(r.text, ort)
    if any(e["local"] for e in evs):   # Filter nur, wenn er etwas uebrig laesst
        evs = [e for e in evs if e["local"]]
    seen, out = set(), []
    for e in sorted(evs, key=lambda e: (e["day"], e["title"])):
        k = (e["day"], e["title"])
        if e["day"] >= today and k not in seen:
            seen.add(k)
            out.append({"day": e["day"], "title": e["title"]})
    return out[:MAX_ENTRIES]


async def poll() -> dict:
    src = "awido" if config.get("muell_src") == "awido" else "mymuell"
    today = today_iso()
    entries = await (_awido(today) if src == "awido" else _mymuell(today))
    return {"src": src, "entries": entries}


async def awido_lookup(step: str, client: str, oid: str = "") -> list:
    if not _CLIENT_RE.match(client or ""):
        raise ValueError("ungueltiger Mandant")
    base = f"{AWIDO}/WebServices/Awido.Service.svc/secure"
    if step == "places":
        return await net.get_json(f"{base}/getPlaces/client={client}")
    if not _GUID_RE.match(oid or ""):
        raise ValueError("ungueltige ID")
    if step == "streets":
        return await net.get_json(f"{base}/getGroupedStreets/{oid}", params={"client": client})
    if step == "addons":
        return await net.get_json(f"{base}/getStreetAddons/{oid}", params={"client": client})
    raise ValueError("ungueltiger Schritt")
