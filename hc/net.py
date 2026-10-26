"""Gemeinsamer HTTP-Client + Download-Waechter (Port von http_util.c)."""
import asyncio
import logging
import socket
import ssl
import time
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("net")

UA = "home-central (esp-infoscreen data host)"
client: httpx.AsyncClient | None = None

# Eigener TLS-Kontext: Server, die das Zwischenzertifikat nicht mitschicken
# (warnung.bund.de seit 2026-10-26), werden per AIA nachgeladen. Ohne
# PARTIAL_CHAIN (ab Python 3.13 Standard) wird ein nachgeladenes Zertifikat nie
# selbst zum Vertrauensanker - die Kette muss weiter bis zu einer Wurzel reichen.
_ctx = ssl.create_default_context()
_ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
_aia_tried: dict[str, float] = {}

# Download-Waechter: je Host letzter Erfolg/Versuch. Ein Host gilt als
# gestoert, wenn er in der letzten Stunde versucht wurde, aber seit >1 h nicht
# mehr erfolgreich war. Jeder Ausfall/jede Erholung wird einmal gemeldet.
HH_FAIL_S = 3600
_health: dict[str, dict] = {}


def start() -> None:
    global client
    client = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": UA}, follow_redirects=True, verify=_ctx)


async def stop() -> None:
    if client:
        await client.aclose()


def _report(url: str, ok: bool) -> None:
    host = urlsplit(url).hostname or ""
    if not host or "telegram" in host:
        return
    now = time.time()
    e = _health.setdefault(host, {"last_ok": now, "last_try": now, "alerted": False})
    e["last_try"] = now
    if ok:
        e["last_ok"] = now


def health() -> dict[str, dict]:
    return {h: dict(e) for h, e in _health.items()}


def health_alert() -> str | None:
    now = time.time()
    fail, rec = [], 0
    for host, e in _health.items():
        down = now - e["last_ok"] > HH_FAIL_S
        tried = now - e["last_try"] < HH_FAIL_S
        if down and tried and not e["alerted"]:
            fail.append(host)
            e["alerted"] = True
        elif not down and e["alerted"]:
            e["alerted"] = False
            rec += 1
    if not fail and not rec:
        return None
    if fail:
        msg = "⚠ Download-Problem (>1h):" + "".join(f"\n- {h}" for h in fail)
        if rec:
            msg += f"\n({rec} Quelle(n) wieder ok)"
        return msg
    return "✅ Downloads wieder ok"


def _peer_cert(host: str, port: int) -> bytes:
    """Serverzertifikat ohne Pruefung holen (nur um die AIA-Adresse zu lesen)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=10) as s, ctx.wrap_socket(s, server_hostname=host) as t:
        return t.getpeercert(binary_form=True)


async def _fetch_intermediate(url: str) -> bool:
    """Fehlendes Zwischenzertifikat ueber AIA (caIssuers) nachladen. Je Host
    hoechstens einmal pro Stunde; True, wenn eines hinzugefuegt wurde."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if time.time() - _aia_tried.get(host, 0) < 3600:
        return False
    _aia_tried[host] = time.time()
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

        leaf = x509.load_der_x509_certificate(await asyncio.to_thread(_peer_cert, host, parts.port or 443))
        aia = leaf.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
        src = next(d.access_location.value for d in aia
                   if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS)
        r = await client.get(src, timeout=10)
        r.raise_for_status()
        try:
            ca = x509.load_der_x509_certificate(r.content)
        except ValueError:
            ca = x509.load_pem_x509_certificate(r.content)
        bc = ca.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value
        if not bc.ca or ca.subject != leaf.issuer:
            raise ValueError("kein passendes CA-Zertifikat")
        _ctx.load_verify_locations(cadata=ca.public_bytes(serialization.Encoding.DER))
    except Exception as e:
        log.warning("%s: Zwischenzertifikat nicht nachladbar: %s", host, e)
        return False
    log.info("%s: Zwischenzertifikat nachgeladen (%s)", host, ca.subject.rfc4514_string())
    return True


def _missing_issuer(e: Exception) -> bool:
    return isinstance(e, httpx.ConnectError) and "unable to get local issuer" in str(e)


async def get(url: str, **kw) -> httpx.Response:
    """GET mit Waechter. Wirft bei Netzfehler oder Status != 200."""
    try:
        try:
            r = await client.get(url, **kw)
        except httpx.ConnectError as e:
            if not (_missing_issuer(e) and await _fetch_intermediate(url)):
                raise
            r = await client.get(url, **kw)
        r.raise_for_status()
    except Exception:
        _report(url, False)
        raise
    _report(url, True)
    return r


async def get_json(url: str, **kw):
    return (await get(url, **kw)).json()
