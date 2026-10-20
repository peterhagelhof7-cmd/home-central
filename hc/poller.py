"""Generischer Hintergrund-Abruf je Datenquelle.

Jede Quelle ist eine async-Funktion ohne Argumente, die die aufbereiteten Daten
(dict/list) liefert oder bei einem Fehler eine Exception wirft. Bei Fehlern
bleiben die letzten guten Daten erhalten (wie im ESP), es wird frueher erneut
versucht. refresh() weckt den Poller sofort (z.B. nach Einstellungsaenderung).
"""
import asyncio
import logging
import time

log = logging.getLogger("poller")

POLLERS: dict[str, "Poller"] = {}


class Poller:
    def __init__(self, name: str, title: str, func, interval: float,
                 retry: float = 300, start_delay: float = 2):
        self.name, self.title, self.func = name, title, func
        self.interval, self.retry, self.start_delay = interval, retry, start_delay
        self.data = None          # letzte gute Daten
        self.ok_ts = 0.0          # Zeitpunkt des letzten Erfolgs
        self.try_ts = 0.0         # Zeitpunkt des letzten Versuchs
        self.error = ""           # letzter Fehlertext ("" = ok)
        self.pause_until = 0.0    # vor diesem Zeitpunkt nicht erneut abrufen (Sperrzeit der Quelle)
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task = None
        POLLERS[name] = self

    async def run_once(self) -> bool:
        async with self._lock:
            self.try_ts = time.time()
            try:
                data = await self.func()
            except Exception as e:  # noqa: BLE001 - jede Quelle darf scheitern
                self.error = f"{type(e).__name__}: {e}"[:200]
                # Quelle meldet Sperrzeit (z.B. Tageskontingent aus) -> so lange nicht fragen
                ra = getattr(e, "retry_after", 0)
                self.pause_until = time.time() + ra if ra else 0.0
                log.warning("%s: %s", self.name, self.error)
                return False
            if data is not None:
                self.data = data
            self.ok_ts = time.time()
            self.error = ""
            self.pause_until = 0.0
            return True

    async def _loop(self):
        await asyncio.sleep(self.start_delay)
        while True:
            ok = await self.run_once()
            self._wake.clear()
            wait = self.interval if ok else max(self.retry, self.pause_until - time.time())
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
            except asyncio.TimeoutError:
                pass

    def start(self):
        self._task = asyncio.create_task(self._loop(), name=f"poll-{self.name}")

    def refresh(self):
        self._wake.set()

    def status(self) -> dict:
        return {"name": self.name, "title": self.title, "ok_ts": self.ok_ts,
                "try_ts": self.try_ts, "error": self.error, "interval": self.interval,
                "pause_until": self.pause_until if self.pause_until > time.time() else 0}


def data(name: str, default=None):
    p = POLLERS.get(name)
    return p.data if p and p.data is not None else default


def refresh(*names: str):
    for n in names:
        if n in POLLERS:
            POLLERS[n].refresh()


async def refresh_now(name: str) -> bool:
    """Sofort abrufen und auf das Ergebnis warten (Bot-Kommandos)."""
    p = POLLERS.get(name)
    return await p.run_once() if p else False
