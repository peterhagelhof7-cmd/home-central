"""Zustand des Infoscreens: was er per POST /api/v1/report meldet (DHT22,
Status, Stacks, Firmware) und Kommandos, die er beim naechsten Abruf erhaelt.
"""
import time

_report: dict = {}
_last_seen = 0.0
_pending_cmd: str | None = None
OFFLINE_S = 10 * 60


def update(rep: dict, ip: str) -> None:
    global _last_seen
    _report.clear()
    _report.update(rep)
    _report["ip"] = ip
    _last_seen = time.time()


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
