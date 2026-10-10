"""Weiterleitung an ntfy (z. B. ntfy.sh) fuer Familienmitglieder ohne Telegram.

Reine Empfangs-Richtung: Gruppennachrichten (Menschen, keine Bot-Befehle) und
Warnungen werden in ein ntfy-Thema gepostet, das die Kinder in der ntfy-App
abonnieren. Wer den Themennamen kennt, kann lesen UND schreiben -> der Name
wird zufaellig und lang erzeugt.
"""
import logging
import secrets

from . import config, net

log = logging.getLogger("ntfy")


def new_topic() -> str:
    return "hc-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")


def enabled() -> bool:
    return config.get("ntfy_on") == "1" and bool(str(config.get("ntfy_topic")).strip())


def url() -> str:
    server = (str(config.get("ntfy_server")).strip() or "https://ntfy.sh").rstrip("/")
    return f"{server}/{str(config.get('ntfy_topic')).strip()}"


async def publish(text: str, title: str = "", priority: str = "", tags: str = "", force: bool = False) -> bool:
    if not (force or enabled()) or not str(config.get("ntfy_topic")).strip():
        return False
    # Titel/Prioritaet/Tags als Query-Parameter (UTF-8-sicher, anders als Header)
    params = {k: v for k, v in (("title", title), ("priority", priority), ("tags", tags)) if v}
    try:
        r = await net.client.post(url(), content=text[:4000].encode(), params=params, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("ntfy fehlgeschlagen: %s", e)
        return False


async def group_message(sender: str, text: str) -> bool:
    if enabled() and config.get("ntfy_grp") == "1":
        return await publish(text, title=sender, tags="speech_balloon")
    return False


async def bot_message(botname: str, text: str) -> bool:
    if enabled() and config.get("ntfy_bot") == "1":
        return await publish(text, title=botname, tags="robot")
    return False


async def warning(text: str) -> bool:
    if enabled() and config.get("ntfy_warn") == "1":
        return await publish(text, title="Warnung", priority="high", tags="warning")
    return False
