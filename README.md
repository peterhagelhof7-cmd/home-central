# home-central

Datenlieferant für den [esp-infoscreen](https://github.com/peterhagelhof7-cmd/esp-infoscreen):
Alle externen Abfragen laufen hier statt auf dem ESP32. Der Infoscreen holt sich nur
noch ein kompaktes JSON ab und stellt seine Uhr über home-central.

**Ausführliche Anleitung: [docs/admin-guide.md](docs/admin-guide.md)** (Installation, Weboberfläche,
Kopplung, Betrieb, Fehlersuche).

- **Datenquellen** (Intervalle wie bisher im ESP): OpenWeatherMap, Spessartwetter, DWD (Bright Sky),
  BBK/NINA, Bitstamp-Kurse, Fritzbox (UPnP), Müllabfuhr (MyMüll **oder** AWIDO), Kino,
  Flugzeuge (adsb.lol + adsbdb), Satelliten/Asteroiden (N2YO + NASA), Feiertage, eigene Termine
- **Telegram-Bot** komplett (Message Board, @-Kommandos, Warnungen, 6-Uhr-Bericht, Sonntags-Kino,
  Download-Wächter, Meldung wenn der Infoscreen offline geht)
- **Weiterleitung an ntfy** (ntfy.sh): Gruppennachrichten und Warnungen für Familienmitglieder ohne
  Telegram – reine Empfangs-App ohne Konto/Chat (Admin-Guide 3.5)
- **MeshCore-Kanäle → Telegram**: Nachrichten aus gewählten Kanälen eines
  [esp-meshcore-gateway](https://github.com/peterhagelhof7-cmd/esp-meshcore-gateway) (ab 0.10.0, Kanal-API
  mit API-Schlüssel) in die Telegram-Gruppe, optional auch an ntfy; umgekehrt Warnungen (DWD, NINA/KATWARN,
  Windböen) in einen freigegebenen Mesh-Kanal (Admin-Guide 3.6)
- **Heizung (esp-p4d)**: Zustand, Störung und Werte einer Fröling-Heizung (Lambdatronic S3200) vom
  esp-p4d (ESP32 an COM1, API mit Schlüssel) – Karte auf der Startseite, `@bot heizung`, Meldung bei
  Störung oder wenn keine Daten mehr kommen (Admin-Guide 3.7)
- **Weboberfläche** im Stil des Infoscreens mit Login: Übersicht (Quellen-Status, Infoscreen-Status,
  **Live-Slides** = Nachbau aller 13 Display-Slides mit den aktuellen Daten, Message Board) und alle Einstellungen; **Import** des Infoscreen-Exports (ohne WLAN/Gerätename)
- **NTP-Server** (chrony) für das LAN, **mDNS-Ankündigung** (Avahi), damit der Infoscreen den Server
  selbst findet; Kopplung neuer Infoscreens per Freigabe in der Weboberfläche
- **Ausfallsicherheit** für den Infoscreen: Einstellungs-Spiegel (`/api/v1/config`), dort zusätzlich
  Daten-Cache und automatischer Notbetrieb (Firmware ≥ v1.3.1)

Ort und Region (Standort, Warnregion, Müll, Kino) haben keinen Standard und werden nach der Installation
eingetragen – wo man die Werte findet, steht im Admin-Guide, Abschnitt 3.3.

## Installation (Debian 13)

```sh
tar -czf - hc deploy | ssh user@host 'mkdir -p ~/hc-src && tar -xzf - -C ~/hc-src'
ssh user@host 'sudo sh ~/hc-src/deploy/install.sh <web-passwort>'
```

Das Skript installiert die Python-Pakete aus Debian (fastapi, uvicorn, httpx, jinja2, multipart,
itsdangerous), chrony und avahi-daemon, legt den Systembenutzer `home-central` an, kopiert den Code nach
`/opt/home-central` und startet den systemd-Dienst auf Port 80. Daten liegen in
`/var/lib/home-central` (`config.json` = Einstellungen, `secrets.json` = Passwort-Hash, API-Token,
Session-Schlüssel). Ohne Passwort-Argument bleibt ein vorhandenes Passwort erhalten.

## Kopplung mit dem Infoscreen

home-central kündigt sich per mDNS (Avahi) als `_home-central._tcp` an. Der Infoscreen (ab Firmware
v1.3.0, Datenquelle „Automatisch") findet ihn darüber und fragt per `POST /api/v1/pair` mit seiner MAC
an. In der Übersicht erscheint dann „Kopplungsanfrage – Freigeben". Nach der Freigabe bekommt der
Infoscreen den API-Token, startet neu und läuft ab dann im Client-Modus (keine eigenen Abrufe, kein Bot,
Zeit per NTP von home-central). Gekoppelte Geräte stehen in der Übersicht unter „Infoscreen" und lassen
sich dort wieder entkoppeln. Ein neu erzeugter API-Token wird von gekoppelten Geräten automatisch über
`/api/v1/pair` nachgeholt.

## Schnittstelle für den Infoscreen

Alle Aufrufe mit Header `X-HC-Token: <API-Token>` (steht in den Einstellungen unter „Infoscreen-Anbindung").

| Aufruf | Inhalt |
|---|---|
| `GET /api/v1/screen` | alle Daten (`owm`, `spessart`, `dwd`, `nina`, `kurse`, `fritzbox`, `planes`, `space`, `muell`, `termine`, `feiertage`, `telegram`), `display` (Helligkeit, Slides, Intervall, Drehung), `age` je Quelle in s (-1 = nie), `time` (Unix), `cmd` (z. B. `"reboot"`, einmalig), `cfg_hash` (Prüfwert der Einstellungen) |
| `GET /api/v1/config` | Einstellungs-Kopie für den Notbetrieb des Infoscreens (Format wie sein Export; ohne Anzeige-Werte und ohne leere Keys) + `hash` |
| `POST /api/v1/report` | JSON vom Infoscreen: `dht22` {valid, temp_c, hum_pct}, `dht22_hist` {temp[], hum[]} (30-min-Punkte), `ssid`, `rssi`, `rssi_avg`, `status` (Text), `stacks` (Text), `fw`, `slides` [{id, title}] (Slide-Katalog → Auswahl + Live-Slides passen sich an neue Slides an) |
| `POST /api/v1/pair` | ohne Token: {mac, name, fw} → `{"status":"pending"}` bzw. nach Freigabe `{"status":"ok","token":…}` |
| `POST /api/v1/tg/send` | {text} → über den Bot an die Gruppe (Message Board im Infoscreen-Web) |
| `GET /api/v1/health` | ohne Token, für Erreichbarkeitstests |

Die Datenfelder entsprechen den C-Strukturen der Firmware (`owm_data_t`, `planes_data_t`, …).

## Hinweise

- Der Telegram-Bot darf nur an **einer** Stelle laufen. Im Client-Modus des Infoscreens läuft er nur
  hier; im Notbetrieb (home-central > 30 min weg) übernimmt der Infoscreen.
- Entkoppeln eines Infoscreens erneuert den API-Token; weiterhin gekoppelte Geräte holen ihn automatisch.
- Startpasswort nach der Installation in der Weboberfläche ändern (Einstellungen → System).
- Für das Heimnetz gedacht (HTTP ohne TLS) – nicht ins Internet freigeben.

## Lizenz

GPL-3.0, siehe [LICENSE](LICENSE).
