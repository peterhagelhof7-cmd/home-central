# home-central — Admin-Guide

Einrichtung und Betrieb von **home-central**, dem Datenlieferanten für den
[esp-infoscreen](https://github.com/peterhagelhof7-cmd/esp-infoscreen). Kurzfassung und API: siehe
[README](../README.md).

---

## 1. Überblick

home-central übernimmt alles, was der Infoscreen früher selbst aus dem Internet geholt hat, und
liefert es ihm aufbereitet als kompaktes JSON:

| Aufgabe | Wie |
|---|---|
| Datenquellen | OpenWeatherMap, Spessartwetter, DWD (Bright Sky), BBK/NINA, Bitstamp, Fritzbox (UPnP), Müll (MyMüll **oder** AWIDO), Kino, Flugzeuge, Satelliten/Asteroiden, Feiertage, eigene Termine – gleiche Intervalle und Auswertung wie früher im ESP |
| Telegram-Bot | komplett (Message Board, Kommandos, Warnungen, 6-Uhr-Bericht, Sonntags-Kino, Download-Wächter, „Infoscreen offline“) |
| Weiterleitung an ntfy | Gruppennachrichten und Warnungen für Familienmitglieder ohne Telegram (nur empfangen, siehe 3.5) |
| Einstellungen | alles außer WLAN/IP des Infoscreens, inkl. Anzeige (Helligkeit, Slides, Intervall, Drehung) |
| Uhrzeit | NTP-Server (chrony) für das LAN |
| Auffindbarkeit | mDNS-Dienst `_home-central._tcp` (Avahi) |

Der Infoscreen (Firmware ≥ v1.3.0) findet home-central selbst, koppelt sich nach einer Freigabe und
läuft dann im **Client-Modus**. Fällt home-central aus, arbeitet er mit den letzten Daten weiter und
schaltet nach 30 min in den Notbetrieb (Details im Admin-Guide des Infoscreens, Abschnitt 2.4).

**Referenzsystem:** Debian 13 (trixie) als VM unter Proxmox (2 vCPU, 4 GB RAM, 30 GB) mit fester IP.
Der Dienst selbst braucht ~30–40 MB RAM; ein Raspberry Pi mit Debian/Raspberry Pi OS (trixie) genügt.

**Lizenz:** GPL-3.0 (siehe [LICENSE](../LICENSE)).

---

## 2. Installation und Update

### 2.1 Voraussetzungen

- Debian 13 mit fester IP im selben Netz/Subnetz wie der Infoscreen (mDNS muss durchkommen:
  kein Gäste-WLAN, keine Client-Isolation).
- Root-Rechte (`sudo` oder `su`), Internetzugang für `apt`.
- Port 80 frei (kein anderer Webserver).

### 2.2 Erstinstallation

Vom Entwicklungsrechner aus dem Repo-Ordner:

```sh
tar -czf - hc deploy | ssh <user>@<host> 'rm -rf ~/hc-src && mkdir ~/hc-src && tar -xzf - -C ~/hc-src'
ssh <user>@<host> 'sudo sh ~/hc-src/deploy/install.sh <web-passwort>'
```

`deploy/install.sh` erledigt:

1. Pakete aus Debian: `python3-fastapi python3-uvicorn python3-httpx python3-jinja2
   python3-multipart python3-itsdangerous chrony avahi-daemon` (keine pip-Pakete).
2. Systembenutzer `home-central`, Datenverzeichnis `/var/lib/home-central` (0750).
3. Code nach `/opt/home-central` (die vorige Version bleibt als `/opt/home-central.old`).
4. chrony als LAN-Zeitserver (`/etc/chrony/conf.d/home-central.conf`, `allow <LAN>/24`,
   `local stratum 10` → liefert Zeit auch bei Internet-Ausfall).
5. Avahi-Dienst `/etc/avahi/services/home-central.service`.
6. systemd-Dienst `home-central` (Port 80 über `CAP_NET_BIND_SERVICE`, ohne root; `ProtectSystem=strict`).
7. Mit Argument: Web-Passwort setzen.

> **Netz anpassen:** In `deploy/chrony-lan.conf` steht als Beispiel `allow 192.168.79.0/24`. Vor der
> Installation auf das eigene LAN ändern (z. B. `allow 192.168.178.0/24`), sonst bekommt der
> Infoscreen keine Zeit von home-central.

### 2.3 Update

Gleicher Befehl wie bei der Installation, **ohne** Passwort-Argument – Einstellungen, Passwort,
API-Token und Kopplungen in `/var/lib/home-central` bleiben erhalten. Der Dienst startet neu; der
Infoscreen überbrückt die paar Sekunden mit seinen letzten Daten.

### 2.4 Passwort vergessen

```sh
cd /opt/home-central && sudo -u home-central env HC_DATA_DIR=/var/lib/home-central \
  python3 -m hc.main --set-password '<neues-passwort>' && sudo systemctl restart home-central
```

---

## 3. Weboberfläche

Erreichbar unter `http://<host>` (IP-Adresse des Servers oder `http://home-central.local`).
Anmeldung mit dem Web-Passwort; nach 5 Fehlversuchen ist das Login 5 min gesperrt.
**Startpasswort nach der Installation sofort ändern** (Einstellungen → System).

### 3.1 Übersicht

- **Kopplungsanfrage** (nur wenn vorhanden, orange umrandet): Name, IP, MAC, Firmware eines
  Infoscreens, der home-central nutzen möchte → **Freigeben** oder **Ablehnen**. Anfragen
  verfallen nach 15 min, der Infoscreen fragt alle 30 s erneut.
- **Infoscreen:** online/offline, letzter Abruf, Statusmeldung (Firmware, Uptime, RAM, WLAN),
  Innenraum-Werte; **Infoscreen neu starten**; aufklappbar die **gekoppelten Geräte** mit
  Entkoppeln-Knopf.
- **Datenquellen:** je Quelle Ampel, Zeitpunkt des letzten Erfolgs, letzte Fehlermeldung und
  ↻ zum sofortigen Abruf.
- **Live-Slides:** Nachbau aller Display-Slides (800×480, skaliert) mit den aktuellen Daten, im
  eingestellten Intervall rotierend; ◀ ▶, Pause, Punkte zum Springen. Reihenfolge und Titel kommen
  aus dem Slide-Katalog des Infoscreens – neue Slides erscheinen automatisch (ohne passende
  Vorschau mit Platzhalter).
- **Message Board:** Chatverlauf der Telegram-Gruppe mit Sendefeld.

### 3.2 Einstellungen

| Karte | Inhalt |
|---|---|
| **Anzeige** | Helligkeit, Untergrenze (Panel-Kalibrierung), 180°-Drehung, Slide-Auswahl, Intervall. Helligkeit wirkt sofort; Drehung/Slides/Intervall → automatischer Neustart des Infoscreens |
| **Termine** | eigene Termine anlegen/löschen (vergangene werden alle 30 min entfernt) |
| **Wetter** | OpenWeatherMap-Key (leer lassen = behalten), Ort (leer = Standort) |
| **Standort & Flugzeuge** | Breite/Länge (auch für DWD und Satelliten), Flugzeug-Radius |
| **Nachtobjekte** | N2YO-Key, NASA-Key (ohne = DEMO_KEY), Beobachterhöhe |
| **Warnungen** | Regionalschlüssel (ARS) für BBK/NINA |
| **Müll** | MyMüll-ID **oder** AWIDO mit Auswahl Mandant → Gemeinde → Ortsteil/Straße → Hausnummer; Wechsel greift sofort (kein Neustart) |
| **Kino** | Kino-ID (siehe 3.3) |
| **Feiertage** | Bundesland |
| **Fritzbox** | Adresse (leer = Gateway von home-central) |
| **Telegram** | Bot-Token (leer = behalten), Gruppen-Chat-ID |
| **Weiterleitung an ntfy** | Aktiv, Gruppennachrichten, Bot-Antworten/Berichte, Warnungen, Server, Kanal; **Testnachricht senden**, **Neuen Kanal erzeugen** (siehe 3.5) |
| **Infoscreen-Anbindung** | Server-Adresse, API-Token, NTP-Server; **Neuen API-Token erzeugen** (gekoppelte Geräte holen ihn automatisch) |
| **System** | Import des Infoscreen-Exports, Export der eigenen Einstellungen, Log-Download, Passwort ändern |

Nach dem Speichern werden die betroffenen Quellen sofort neu abgerufen.

### 3.3 Ersteinrichtung: Ort und Region

Ortsbezogene Werte haben **keinen Standard** – bis sie eingetragen sind, steht die Quelle in der Übersicht
rot mit „nicht eingerichtet: …“. Wer vorher den Infoscreen im Direkt-Modus genutzt hat, übernimmt alles
per Import (3.4).

| Wert | Karte | Wo finde ich ihn? |
|---|---|---|
| **Standort** (Breite/Länge, Dezimalgrad) | Standort & Flugzeuge | Karten-App: Rechtsklick/langes Tippen auf den Ort → Koordinaten, z. B. `49.79` / `9.95`. Wird für Wetter (ohne Ortsnamen), DWD-Warnungen, Flugzeuge und Satelliten genutzt. |
| **Wetter-Ort** (optional) | Wetter | Ortsname für OpenWeatherMap, z. B. `Würzburg,DE`; leer = Standort |
| **Warnregion (ARS)** | Warnungen | 12-stelliger Amtlicher Regionalschlüssel. Für einen ganzen Landkreis: 5-stelliger Kreisschlüssel + `0000000` (z. B. `096790000000` = Landkreis Würzburg). Kreis- und Gemeindeschlüssel stehen im Gemeindeverzeichnis von Destatis. |
| **Müll – MyMüll** | Müll | Ortsteil-ID: Liste aller Orte als JSON unter `https://mymuell.jumomind.com/mmapp/api.php?r=cities` (Feld `id`). Die Termine lassen sich mit `…api.php?r=dates&city_id=<id>&area_id=<id>` prüfen. |
| **Müll – AWIDO** | Müll | Datenquelle „Abfall-App (AWIDO)“, dann Mandant → Gemeinde → Ortsteil/Straße → Hausnummer wählen; die ID setzt die Auswahl. Fehlt der Mandant: Kürzel aus der Adresse des Web-Abfuhrkalenders (`awido.cubefour.de/Customer/<kürzel>/…`). |
| **Kino** (optional) | Kino | ID des Kinos im kino.de-Backend. Prüfen: `https://kntkapi.apps.stroeermb.de/api/cinema/<id>/` im Browser öffnen – `name` zeigt das Kino. Leer = Bot-Befehl `kino` meldet „nicht eingerichtet“. |
| **Feiertage** | Feiertage | Bundesland (Standard Bayern) |

Die Slide **Spessartwetter** zeigt eine feste regionale Wetterstation (spessartwetter.de, im
Spessart). Außerhalb der Region in der Karte *Anzeige* abwählen.

### 3.4 Einstellungen vom Infoscreen übernehmen

Im Infoscreen (Direkt-Modus) unter **System → Einstellungen herunterladen** die JSON-Datei holen und
in home-central unter **System → Importieren** hochladen. Übernommen wird alles außer `wifi_*` und
`dev_name`. **Telegram-Token:** Läuft der Bot noch im Infoscreen, den Token erst nach der Umstellung
auf den Client-Modus in home-central eintragen – sonst laufen zwei Bots mit demselben Token.

### 3.5 Weiterleitung an ntfy (Familie ohne Telegram)

Telegram ist im Play Store erst ab 18 freigegeben. Damit Kinder trotzdem mitbekommen, was in der
Gruppe passiert, leitet home-central Nachrichten an einen Kanal von [ntfy](https://ntfy.sh) weiter.
ntfy ist eine reine Benachrichtigungs-App: kein Konto, keine Kontakte, kein Chat – die Kinder können
nur empfangen, nicht in die Gruppe schreiben.

**Was weitergeleitet wird** (einzeln abschaltbar):

| Häkchen | Inhalt | Darstellung in ntfy |
|---|---|---|
| Gruppennachrichten | jede Textnachricht aus der Telegram-Gruppe (auch Fragen an den Bot), außerdem Nachrichten, die über das Message Board (home-central oder Infoscreen) in die Gruppe gehen | Titel = Vorname des Absenders bzw. „Message Board“/„Infoscreen“ |
| Antworten und Berichte des Bots | Antworten auf Befehle (`@<botname> wetter` …), 6-Uhr-Bericht mit Terminen, Sonntags-Kino | Titel = Bot-Name |
| Warnungen | DWD-Wetterwarnung, NINA/BBK-Warnung, Windböen ≥ 50 km/h – jede Warnung einmal | Titel „Warnung“, hohe Priorität |

**Nicht** weitergeleitet: Slash-Befehle (`/start` …) und technische Meldungen (home-central gestartet,
Download-Problem, Infoscreen offline/online, Stack-Daten). Warnungen kommen nur über das Häkchen
„Warnungen“, nie doppelt.

**Einrichten:**

1. Einstellungen → **Weiterleitung an ntfy** → „Weiterleitung aktiv“ anhaken → Speichern.
   Ist das Feld *Kanal* leer, wird ein zufälliger Name wie `hc-…` erzeugt.
2. **Testnachricht senden** – die Meldung muss in der ntfy-App (bzw. unter `https://ntfy.sh/<kanal>`)
   ankommen.
3. Auf jedem Kinder-Handy: ntfy aus dem Play Store installieren (Freigabe über Family Link),
   „+“ → Kanalname eintragen, Server ntfy.sh belassen → Abonnieren.

**Voraussetzung:** Der Bot muss alle Gruppennachrichten sehen dürfen (Datenschutz-Modus aus:
@BotFather → `/setprivacy` → Disable, danach den Bot einmal aus der Gruppe entfernen und neu
hinzufügen).

**Wichtig zum Kanalnamen:** Auf ntfy.sh ist ein Kanal öffentlich, aber nur über seinen Namen
auffindbar. Wer den Namen kennt, kann **mitlesen und selbst Nachrichten hineinschicken**. Deshalb:
Namen nur an die Familie geben, nicht verkürzen. Ist er in falsche Hände geraten → **Neuen Kanal
erzeugen** und auf allen Geräten neu abonnieren. Die Nachrichten laufen über den Server von ntfy.sh
(dort ~12 h zwischengespeichert); home-central schickt nur nach außen und bleibt selbst unerreichbar.
Der Kanalname steht in `config.json` und im Einstellungs-Export, er wird nicht an den Infoscreen
gespiegelt.

### 3.6 MeshCore-Kanäle an Telegram (ab 0.3.0)

home-central kann Nachrichten aus Kanälen eines **esp-meshcore-gateway** (Firmware ab 0.10.0) in die
Telegram-Gruppe weiterleiten, z. B. die Alarme der Überwachung. Das Gateway muss von home-central aus
**im Netz erreichbar** sein.

1. Im Gateway: **System → Kanal-API → Schlüssel erzeugen**, Schlüssel kopieren.
2. In home-central: **Einstellungen → MeshCore-Gateway**: Adresse (IP oder Hostname des Gateways) und
   API-Schlüssel eintragen, **Speichern**, dann **Verbindung testen**. Danach erscheinen die Kanäle des
   Gateways als Häkchen.
3. Gewünschte Kanäle anhaken, **Weiterleitung aktiv** setzen, speichern.

- **auch Meldungen des Gateways selbst**: Alarme, die das Gateway in einen Kanal sendet, kommen mit.
  Ohne Haken nur Nachrichten anderer Teilnehmer.
- **zusätzlich an ntfy**: dieselben Nachrichten auch in den ntfy-Kanal (wenn ntfy aktiv ist).
- In Telegram sieht eine Nachricht so aus: `📡 sps-alarm · DE-BY-SUL02-SPS-GW:` und darunter der Text.
  Mehrere Nachrichten aus einer Abfrage kommen gesammelt in einer Telegram-Nachricht.
- Abfrage alle 15 s. Beim **ersten** Abruf (oder nach Wechsel der Adresse) werden vorhandene Nachrichten
  übersprungen, damit kein alter Verlauf in die Gruppe kommt. Danach merkt sich home-central die
  Position, auch über einen eigenen Neustart hinweg.
- Startet das **Gateway** neu, liest home-central dessen Kanäle wieder von vorn: alles seit dem Neustart
  ist neu und wird weitergeleitet.
- Das Gateway hält nur die letzten 200 Kanal-Zeilen im RAM. War home-central länger nicht erreichbar,
  kommt der Hinweis **„Nachrichten verpasst“**.
- Kann Telegram nicht senden, bleibt die Position stehen: beim nächsten Abruf (1 min) wird nachgeliefert.

**Warnungen ins Mesh senden:** Dieselben Warnungen, die in die Telegram-Gruppe gehen, kann home-central auch in
einen MeshCore-Kanal schicken. So erreichen sie auch Leute ohne Internet.
1. Im Gateway: System → Kanal-API → **„Senden erlauben in“** den Kanal wählen (z. B. `warnungen`), „Übernehmen“.
2. In home-central, Karte MeshCore-Gateway: **Warnungen ins Mesh senden** aktivieren, denselben Kanal wählen und
   die Arten anhaken: **DWD** (Wetterwarnungen), **NINA** (Bevölkerungsschutz: MoWaS, KATWARN, BIWAPP, Polizei,
   Hochwasser), **Windböen** (Spessartwetter).

- Jede Warnung geht **einmal** ins Mesh (wie bei Telegram). Ein Neustart von home-central meldet eine noch
  laufende Warnung einmal neu.
- Mehrzeilige Texte werden zu einer Zeile, zu lange kürzt das Gateway (Platz ca. 140 Zeichen).
- Das Gateway nimmt höchstens **10 Nachrichten pro Stunde** an. Lehnt es ab (Limit, Kanal nicht freigegeben),
  versucht home-central es alle 30 s erneut; der Grund steht im Log (`Senden ins Mesh abgelehnt`).
- Was home-central selbst ins Mesh schickt, wird beim Weiterleiten Mesh → Telegram übersprungen, damit
  Warnungen nicht doppelt in der Gruppe landen.

---

## 4. Infoscreen koppeln

1. Infoscreen auf Firmware ≥ v1.3.0 bringen (OTA oder Web-Flasher); Datenquelle „Automatisch“.
2. Nach spätestens ~2 min erscheint in der Übersicht die **Kopplungsanfrage** → **Freigeben**.
3. Innerhalb von 30 s holt der Infoscreen den Token, startet neu und ist danach **online**
   (Übersicht → Infoscreen).
4. Telegram-Token eintragen (falls noch nicht geschehen).

Ab dann:
- Der Infoscreen ruft alle 15 s `/api/v1/screen` ab und meldet sich jede Minute mit Status.
- Ändert sich eine Einstellung, spiegelt der Infoscreen sie für den Notbetrieb in seinen Speicher.
- Bot-Kommando `reboot` bzw. **Infoscreen neu starten** wirken beim nächsten Abruf.

**Entkoppeln:** Übersicht → Gekoppelte Geräte → ✕. Dabei wird der API-Token erneuert: Der
entkoppelte Infoscreen bekommt 401 und fragt erneut an (neue Freigabe nötig); weiterhin gekoppelte
Geräte holen den neuen Token automatisch. Im Infoscreen selbst: Datenquelle → „Kopplung lösen“.

---

## 5. Betrieb

| Was | Befehl / Ort |
|---|---|
| Status | `systemctl status home-central` |
| Log | `journalctl -u home-central -f` oder Übersicht → Einstellungen → System → Log herunterladen |
| Neustart | `sudo systemctl restart home-central` |
| Einstellungen | `/var/lib/home-central/config.json` |
| Geheimnisse | `/var/lib/home-central/secrets.json` (Passwort-Hash, API-Token, Session-Schlüssel, Kopplungen) |
| Zeitserver | `chronyc tracking`, `chronyc clients` |
| mDNS | `avahi-browse -rt _home-central._tcp` (Paket `avahi-utils`) |

**Backup:** `/var/lib/home-central` sichern (enthält Keys und Token – vertraulich behandeln).
Alternativ Einstellungen → System → **Einstellungen herunterladen** (ohne Passwort/Token/Kopplungen).

**Abfrage-Intervalle:** Wetter 20 min (Vorhersage ~3 h), Spessart 30 min, DWD/NINA 15 min,
Kurse 5 min, Fritzbox 10 s, Müll 6 h, Kino 6 h, Flugzeuge 1 min, Satelliten 2 h / Asteroiden 6 h.
Nach einem Fehler wird früher erneut versucht; die letzten guten Daten bleiben erhalten.

---

## 6. Fehlersuche

**Keine Kopplungsanfrage.** Infoscreen-Webinterface → Datenquelle zeigt den Zustand. „Im Netz nicht
gefunden“ → `systemctl status avahi-daemon`, beide Geräte im selben Subnetz? Windows-Rechner sehen
mDNS-Antworten oft wegen der Firewall nicht – das sagt nichts über den Infoscreen aus.

**Quelle rot in der Übersicht.** Fehlermeldung steht darunter. Typisch: Key fehlt/ungültig (OWM,
N2YO), Fritzbox ohne UPnP-Statusinformationen, Zeitüberschreitung bei NASA (wird nach 15 min erneut
versucht). Meldet eine Quelle > 1 h Fehler, schickt der Bot einmal „Download-Problem“.

**Bot antwortet nicht / doppelt.** Läuft der Bot noch irgendwo anders mit demselben Token (alter
Infoscreen im Direkt-Modus, Notbetrieb)? Im Log nach `getUpdates`-Warnungen suchen.

**ntfy: nichts kommt an.** Weiterleitung aktiv und Häkchen gesetzt? **Testnachricht senden** – meldet
die Seite einen Fehler, steht der Grund im Log (`ntfy fehlgeschlagen: …`). Kommt der Test an, aber keine
Gruppennachrichten: Datenschutz-Modus des Bots prüfen (siehe 3.5). Im Handy ntfy vom
Akku-Sparen ausnehmen, sonst kommen Benachrichtigungen verspätet.

**MeshCore: nichts kommt an.** Übersicht → Zeile „MeshCore-Gateway“ zeigt den letzten Fehler.
`HTTP 401` = Schlüssel falsch (im Gateway neu erzeugt?), `HTTP 403` = Kanal-API im Gateway
ausgeschaltet, `ConnectTimeout` = Gateway von home-central aus nicht erreichbar (anderes Netz, keine
Route). Kanal angehakt? Kanalnamen müssen exakt wie im Gateway heißen; ein umbenannter Kanal steht als
„(fehlt am Gateway)“ in der Liste.

**Infoscreen „offline“.** Er ruft seit > 10 min nicht ab: WLAN, Stromversorgung, Firmware-Modus
(Datenquelle) prüfen. Nach 30 min ohne home-central wechselt er selbst in den Notbetrieb.

**Weboberfläche nicht erreichbar.** `systemctl status home-central`; belegt ein anderer Dienst Port 80?
`ss -tlnp | grep :80`.

---

## 7. Sicherheit

- Weboberfläche mit Passwort (scrypt-Hash) und Session-Cookie; ohne Login sind alle Seiten und
  internen APIs gesperrt.
- Die Infoscreen-API braucht den **API-Token** (`X-HC-Token`); neue Geräte bekommen ihn nur nach
  **Freigabe** in der Weboberfläche. Ohne Token sind nur `/api/v1/health` und die Kopplungsanfrage
  erreichbar.
- Keys, Token und Kopplungen liegen nur in `/var/lib/home-central` (0600, Benutzer `home-central`),
  nie im Repo.
- Kein HTTPS: für das Heimnetz gedacht, nicht ins Internet freigeben.
- ntfy-Weiterleitung: Wer den Kanalnamen kennt, kann lesen und schreiben (siehe 3.5).
- MeshCore-Gateway: Der API-Schlüssel erlaubt das Lesen von Kanälen und Nachrichten und – nur wenn im
  Gateway freigegeben – das Senden in genau einen Kanal (max. 10 pro Stunde). Er steht in
  `config.json` und im Einstellungs-Export (wie die übrigen Keys), wird aber nicht an den Infoscreen
  gespiegelt und nie im Klartext in der Weboberfläche angezeigt. Die Abfrage läuft per HTTP durchs LAN.
