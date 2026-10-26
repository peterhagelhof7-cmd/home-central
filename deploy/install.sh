#!/bin/sh
# home-central installieren/aktualisieren (als root auf Debian 13).
# Aufruf aus dem entpackten Quellverzeichnis:  sudo sh deploy/install.sh [WEB-PASSWORT]
set -e
SRC=$(cd "$(dirname "$0")/.." && pwd)
export DEBIAN_FRONTEND=noninteractive

apt-get -y -q install python3-fastapi python3-uvicorn python3-httpx python3-jinja2 \
    python3-multipart python3-itsdangerous python3-cryptography chrony avahi-daemon >/dev/null
# Diagramme fuer den Bot (heizung verlauf); ohne Recommends ~70 MB statt ~110 Pakete
apt-get -y -q install --no-install-recommends python3-matplotlib >/dev/null

id home-central >/dev/null 2>&1 || useradd --system --home-dir /var/lib/home-central --shell /usr/sbin/nologin home-central
install -d -o home-central -g home-central -m 0750 /var/lib/home-central

# Code nach /opt/home-central (alte Version ersetzen, Daten bleiben in /var/lib)
rm -rf /opt/home-central.new
mkdir -p /opt/home-central.new
cp -r "$SRC/hc" /opt/home-central.new/
find /opt/home-central.new -name '__pycache__' -prune -exec rm -rf {} +
rm -rf /opt/home-central.old
[ -d /opt/home-central ] && mv /opt/home-central /opt/home-central.old
mv /opt/home-central.new /opt/home-central
chown -R root:root /opt/home-central

# NTP-Server fuer das LAN
install -m 0644 "$SRC/deploy/chrony-lan.conf" /etc/chrony/conf.d/home-central.conf
systemctl restart chrony

# mDNS-Ankuendigung (_home-central._tcp), damit der Infoscreen den Server findet
install -m 0644 "$SRC/deploy/avahi-home-central.service" /etc/avahi/services/home-central.service
systemctl enable --now avahi-daemon >/dev/null 2>&1
systemctl reload avahi-daemon 2>/dev/null || systemctl restart avahi-daemon

install -m 0644 "$SRC/deploy/home-central.service" /etc/systemd/system/home-central.service
systemctl daemon-reload

if [ -n "$1" ]; then
    (cd /opt/home-central && HC_DATA_DIR=/var/lib/home-central runuser -u home-central -- python3 -m hc.main --set-password "$1")
fi

systemctl enable home-central >/dev/null 2>&1
systemctl restart home-central
sleep 2
systemctl --no-pager --lines=5 status home-central | head -12
