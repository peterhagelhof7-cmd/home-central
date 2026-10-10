# home-central

Datenlieferant für den [esp-infoscreen](https://github.com/peterhagelhof7-cmd/esp-infoscreen):
Alle externen Abfragen laufen hier statt auf dem ESP32. Der Infoscreen holt sich nur
noch ein kompaktes JSON ab und stellt seine Uhr über home-central.

- **Datenquellen** (Intervalle wie bisher im ESP): OpenWeatherMap, Spessartwetter, DWD (Bright Sky),
  BBK/NINA, Bitstamp-Kurse, Fritzbox (UPnP), Müllabfuhr (MyMüll **oder** AWIDO), Kino,
  Flugzeuge (adsb.lol + adsbdb), Satelliten/Asteroiden (N2YO + NASA), Feiertage, eigene Termine
- **Telegram-Bot** komplett (Message Board, @-Kommandos, Warnungen, 6-Uhr-Bericht, Sonntags-Kino,
  Download-Wächter, Meldung wenn der Infoscreen offline geht)
- **Weboberfläche** im Stil des Infoscreens mit Login: Übersicht (Quellen-Status, Infoscreen-Status,
  **Live-Slides** = Nachbau aller 13 Display-Slides mit den aktuellen Daten, Message Board) und alle Einstellungen; **Import** des Infoscreen-Exports (ohne WLAN/Gerätename)
- **NTP-Server** (chrony) für das LAN

## Installation (Debian 13)

```sh
tar -czf - hc deploy | ssh user@host 'mkdir -p ~/hc-src && tar -xzf - -C ~/hc-src'
ssh user@host 'sudo sh ~/hc-src/deploy/install.sh <web-passwort>'
```

Das Skript installiert die Python-Pakete aus Debian (fastapi, uvicorn, httpx, jinja2, multipart,
itsdangerous) und chrony, legt den Systembenutzer `home-central` an, kopiert den Code nach
`/opt/home-central` und startet den systemd-Dienst auf Port 80. Daten liegen in
`/var/lib/home-central` (`config.json` = Einstellungen, `secrets.json` = Passwort-Hash, API-Token,
Session-Schlüssel). Ohne Passwort-Argument bleibt ein vorhandenes Passwort erhalten.

## Schnittstelle für den Infoscreen

Alle Aufrufe mit Header `X-HC-Token: <API-Token>` (steht in den Einstellungen unter „Infoscreen-Anbindung").

| Aufruf | Inhalt |
|---|---|
| `GET /api/v1/screen` | alle Daten (`owm`, `spessart`, `dwd`, `nina`, `kurse`, `fritzbox`, `planes`, `space`, `muell`, `termine`, `feiertage`, `telegram`), `display` (Helligkeit, Slides, Intervall, Drehung), `age` je Quelle in s (-1 = nie), `time` (Unix), `cmd` (z. B. `"reboot"`, einmalig) |
| `POST /api/v1/report` | JSON vom Infoscreen: `dht22` {valid, temp_c, hum_pct}, `dht22_hist` {temp[], hum[]} (30-min-Punkte), `ssid`, `rssi`, `rssi_avg`, `status` (Text), `stacks` (Text), `fw` |
| `GET /api/v1/health` | ohne Token, für Erreichbarkeitstests |

Die Datenfelder entsprechen den C-Strukturen der Firmware (`owm_data_t`, `planes_data_t`, …).

## Hinweise

- Der Telegram-Bot darf nur an **einer** Stelle laufen. Sobald home-central den Token hat, muss er
  im Infoscreen entfernt sein (bzw. der Infoscreen im Client-Modus laufen).
- `brightness`, `bl_floor` und `slide_sec` speichert der Infoscreen als Zahl im NVS; sein Export
  enthält nur Strings, deshalb werden diese drei beim Import nicht übernommen.
