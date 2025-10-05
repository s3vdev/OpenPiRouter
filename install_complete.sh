#!/usr/bin/env bash
set -euo pipefail

# ---------- Helpers ----------
ask(){ local p="$1" d="$2" v=""; read -rp "$p [$d]: " v; echo "${v:-$d}"; }
ask_yn(){ local p="$1" def="${2:-J}" a=""; read -rp "$p [$def]: " a; a="${a:-$def}"; [[ "$a" =~ ^([JjYy]|Yes|yes)$ ]]; }
need_root(){ [[ $EUID -eq 0 ]] || { echo "Bitte mit sudo starten."; exit 1; }; }
ip_ok(){ local ip="$1"; [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1; IFS=. read -r a b c d <<<"$ip"; for o in $a $b $c $d; do ((o>=0&&o<=255))||return 1; done; }

# Logging function
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ---------- Start ----------
need_root
log "==> Pi Repeater v6 (COMPLETE)  |  WAN=USB->wlan0, AP=intern->wlan1"

# Komplette Bereinigung
log "Bereinige vorherige Installation..."
systemctl stop hostapd dnsmasq pi-config 2>/dev/null || true
systemctl disable hostapd dnsmasq pi-config 2>/dev/null || true
systemctl mask hostapd dnsmasq 2>/dev/null || true

# Entferne alte Konfigurationen
rm -f /etc/hostapd/hostapd.conf
rm -f /etc/dnsmasq.d/pi-repeater.conf
rm -f /etc/systemd/system/pi-*.service
rm -f /etc/systemd/system/hostapd.service.d/override.conf
rm -f /etc/systemd/system/dnsmasq.service.d/override.conf
rm -f /etc/iptables/rules.v4
rm -f /etc/sysctl.d/99-pi-repeater.conf

systemctl daemon-reload

log "Aktualisiere Paketlisten..."
apt-get update

log "Installiere benötigte Pakete..."
apt-get install -y hostapd dnsmasq iptables-persistent netfilter-persistent \
                   iw iproute2 curl bc python3-flask python3-yaml network-manager \
                   python3-pip python3-venv python3-psutil

# ---- Eingaben ----
log "Konfiguriere Pi-Repeater..."
CC=$(ask "Ländercode (country_code)" "DE")
SSID=$(ask "AP-SSID" "PiRepeater")
PASS=$(ask "AP-Passwort (8..63 Zeichen)" "BitteAendern123")

# Validate password length
while [[ ${#PASS} -lt 8 || ${#PASS} -gt 63 ]]; do
    echo "Passwort muss 8-63 Zeichen lang sein!"
    PASS=$(ask "AP-Passwort (8..63 Zeichen)" "BitteAendern123")
done

BAND=$(ask "AP-Band (5G|2G)" "5G"); BAND=$(echo "$BAND"|tr a-z A-Z); [[ "$BAND" =~ ^(5G|2G)$ ]] || BAND="5G"
if [[ "$BAND" == "5G" ]]; then CH5=$(ask "5-GHz-Kanal (36/40/44/48)" "36"); CH2="6"; else CH2=$(ask "2.4-GHz-Kanal (1/6/11)" "6"); CH5="36"; fi
SUBNET=$(ask "AP-Subnetz (CIDR)" "192.168.50.0/24"); PREF="${SUBNET#*/}"; [[ "$PREF" =~ ^[0-9]+$ ]] && ((PREF>=8&&PREF<=30)) || PREF=24
AP_IP=$(ask "AP-IP im Subnetz" "192.168.50.1"); ip_ok "$AP_IP" || { log "Ungültige IP."; exit 1; }
DHCP_S=$(ask "DHCP-Start" "192.168.50.10"); DHCP_E=$(ask "DHCP-Ende" "192.168.50.200"); ip_ok "$DHCP_S" && ip_ok "$DHCP_E" || { log "Ungültiger DHCP-Bereich."; exit 1; }
WEB_PORT=$(ask "Web-UI Port" "8080")
WAN_SSID=$(ask "WAN-WLAN SSID (für wlan0)" "FRITZ!Box 6890 MO")
WAN_PASS=$(ask "WAN-WLAN Passwort" "29496274130902329708")
PIHOLE=1; ask_yn "Pi-hole unattended installieren?" "J" || PIHOLE=0

# ---- Interfaces jetzt ermitteln ----
log "Ermittle WLAN-Interfaces..."

# Finde WLAN-Interfaces
mapfile -t WIFS < <(ls /sys/class/net | grep -E '^wlan[0-9]+$' || true)
log "Gefundene WLAN-Interfaces: ${WIFS[*]}"

if [[ ${#WIFS[@]} -lt 2 ]]; then
    log "FEHLER: Brauche mindestens 2 WLAN-Adapter. Gefunden: ${WIFS[*]}"
    log "Bitte USB-WLAN-Adapter anschließen und neu starten."
    exit 1
fi

USB_IF=""; INT_IF=""
for IF in "${WIFS[@]}"; do
  # Check if interface is USB-based
  if readlink -f /sys/class/net/$IF | grep -q "/usb"; then 
      USB_IF="$IF"
      log "USB-WLAN gefunden: $IF"
  else 
      INT_IF="$IF"
      log "Internes WLAN gefunden: $IF"
  fi
done

if [[ -z "$USB_IF" || -z "$INT_IF" ]]; then
    log "FEHLER: Zuordnung fehlgeschlagen. USB: $USB_IF, Intern: $INT_IF"
    log "Bitte USB-WLAN-Adapter anschließen und neu starten."
    exit 1
fi

WAN_MAC=$(cat /sys/class/net/${USB_IF}/address)  # USB -> WAN -> wlan0
AP_MAC=$(cat /sys/class/net/${INT_IF}/address)   # intern -> AP -> wlan1
log "WAN MAC (USB): $WAN_MAC -> wlan0"
log "AP MAC (intern): $AP_MAC -> wlan1"

log "=== ZUSAMMENFASSUNG (aktiv NACH Reboot) ==="
log "WAN=USB -> wlan0 (MAC ${WAN_MAC}) | AP=intern -> wlan1 (MAC ${AP_MAC})"
log "WAN-WLAN: $WAN_SSID"
log "AP: SSID=$SSID  Band=$BAND  Kanal=$( [[ $BAND == 5G ]] && echo $CH5 || echo $CH2 )"
log "Netz: $SUBNET (/ $PREF)  AP-IP=$AP_IP  DHCP=$DHCP_S..$DHCP_E"
log "Web-UI: Port $WEB_PORT  | Pi-hole unattended: $PIHOLE"
echo
ask_yn "Fortfahren (Konfig schreiben, Dienste nur ENABLEN)?" "J" || { log "Abgebrochen."; exit 1; }

# ---------- udev: USB->wlan0, intern->wlan1 ----------
log "Erstelle udev-Regeln für Interface-Zuordnung..."
cat >/etc/udev/rules.d/70-persistent-wlan-names.rules <<EOF
SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="${WAN_MAC}", NAME="wlan0"
SUBSYSTEM=="net", ACTION=="add", ATTR{address}=="${AP_MAC}",  NAME="wlan1"
EOF

# Regdom fix + Service
log "Konfiguriere Wireless Regulatory Domain..."
mkdir -p /etc/default
echo "REGDOMAIN=${CC}" >/etc/default/crda
cat >/etc/systemd/system/pi-regdom.service <<EOF
[Unit]
Description=Set wireless regulatory domain
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target
[Service]
Type=oneshot
ExecStart=/usr/sbin/iw reg set ${CC}
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF

# NM: wlan1 unmanaged (AP gehört hostapd)
log "Konfiguriere NetworkManager..."
mkdir -p /etc/NetworkManager/conf.d
cat >/etc/NetworkManager/conf.d/99-unmanaged-wlan1.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:wlan1
EOF

# wpa_supplicant vom AP fernhalten
systemctl mask wpa_supplicant@wlan1.service 2>/dev/null || true

# WAN-WLAN-Verbindung für wlan0
log "Erstelle WAN-Verbindungsservice..."
cat >/etc/systemd/system/pi-wan-connect.service <<EOF
[Unit]
Description=Connect wlan0 to WAN WiFi
After=network-pre.target
Wants=network-pre.target
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'rfkill unblock wifi; ip link set wlan0 up; sleep 3; nmcli dev wifi connect "${WAN_SSID}" password "${WAN_PASS}" ifname wlan0'
RemainAfterExit=yes
Restart=on-failure
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF

# AP-Prep Service
log "Erstelle AP-Vorbereitungsservice..."
cat >/etc/systemd/system/pi-ap-prep.service <<'EOF'
[Unit]
Description=Prep AP interface (rfkill unblock, stop wpa_supplicant on wlan1)
After=pi-regdom.service
Wants=pi-regdom.service
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'rfkill unblock all || true; pkill -f wpa_supplicant.*wlan1 || true; ip link set wlan1 down || true; sleep 2'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF

# Statische IP auf wlan1
log "Erstelle IP-Konfigurationsservice..."
cat >/etc/systemd/system/pi-repeater-ip.service <<EOF
[Unit]
Description=Static IP on wlan1 (AP)
After=pi-ap-prep.service network-pre.target
Wants=pi-ap-prep.service network-pre.target
ConditionPathExists=/sys/class/net/wlan1
[Service]
Type=oneshot
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 30); do ip link show wlan1 >/dev/null 2>&1 && exit 0; sleep 1; done; echo "wlan1 not present"; exit 1'
ExecStart=/usr/sbin/ip link set wlan1 up
ExecStart=/usr/sbin/ip addr flush dev wlan1
ExecStart=/usr/sbin/ip addr add ${AP_IP}/${PREF} dev wlan1
ExecStart=/bin/sh -c 'sleep 3'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF

# ---------- hostapd/dnsmasq (AP = wlan1) ----------
log "Konfiguriere hostapd (Access Point)..."
mkdir -p /etc/hostapd
MODE="a"; CH="${CH5}"
if [[ "$BAND" != "5G" ]]; then MODE="g"; CH="${CH2}"; fi
cat >/etc/hostapd/hostapd.conf <<EOF
interface=wlan1
driver=nl80211
ssid=${SSID}
country_code=${CC}
ieee80211d=1
hw_mode=${MODE}
channel=${CH}
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_passphrase=${PASS}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
beacon_int=100
dtim_period=2
max_num_sta=255
macaddr_acl=0
ignore_broadcast_ssid=0
EOF
sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

log "Konfiguriere dnsmasq (DHCP/DNS)..."
mkdir -p /etc/dnsmasq.d
cat >/etc/dnsmasq.d/pi-repeater.conf <<EOF
interface=wlan1
bind-interfaces
dhcp-range=${DHCP_S},${DHCP_E},12h
dhcp-option=3,${AP_IP}
dhcp-option=6,${AP_IP}
port=0
domain=lan
expand-hosts
EOF

# ---------- sysctl + Firewall ----------
log "Konfiguriere IP-Forwarding und Firewall..."
echo "net.ipv4.ip_forward=1" >/etc/sysctl.d/99-pi-repeater.conf

# ---------- Ethernet Bridge Setup ----------
log "Konfiguriere Ethernet Bridge für RJ45-Port..."
# Install bridge-utils if not present
apt-get install -y bridge-utils

# Create bridge configuration script
cat >/opt/pi-config/eth-bridge-setup.sh <<'EOF'
#!/bin/bash
# Ethernet Bridge Configuration Script

BRIDGE_MODE="/etc/pi-config/eth-bridge-mode"

# Default to client mode
if [[ ! -f "$BRIDGE_MODE" ]]; then
    echo "client" > "$BRIDGE_MODE"
fi

MODE=$(cat "$BRIDGE_MODE")

case "$MODE" in
    "client")
        # Client mode: eth0 gets internet from wlan0
        # Remove eth0 from bridge if it exists
        brctl delif br0 eth0 2>/dev/null || true
        # Configure eth0 as client interface
        ip addr flush dev eth0 2>/dev/null || true
        ip link set eth0 up
        # Enable internet routing for eth0 clients
        iptables -t nat -A POSTROUTING -o wlan0 -j MASQUERADE 2>/dev/null || true
        iptables -A FORWARD -i eth0 -o wlan0 -j ACCEPT 2>/dev/null || true
        iptables -A FORWARD -i wlan0 -o eth0 -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
        ;;
    "bridge")
        # Bridge mode: eth0 becomes part of AP network
        # Create bridge if it doesn't exist
        if ! ip link show br0 >/dev/null 2>&1; then
            ip link add name br0 type bridge
            ip link set br0 up
            ip addr add 192.168.50.1/24 dev br0
        fi
        # Add eth0 to bridge
        brctl addif br0 eth0 2>/dev/null || true
        ip link set eth0 up
        ;;
esac
EOF

chmod +x /opt/pi-config/eth-bridge-setup.sh

# Create systemd service for ethernet bridge
cat >/etc/systemd/system/pi-eth-bridge.service <<EOF
[Unit]
Description=Pi-Repeater Ethernet Bridge
After=network.target
Before=hostapd.service

[Service]
Type=oneshot
ExecStart=/opt/pi-config/eth-bridge-setup.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# Enable the service
systemctl enable pi-eth-bridge.service

# Create config directory
mkdir -p /etc/pi-config
echo "client" > /etc/pi-config/eth-bridge-mode
mkdir -p /etc/iptables
cat >/etc/iptables/rules.v4 <<'EOF'
*nat
:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
-A POSTROUTING -o wlan0 -j MASQUERADE
-A POSTROUTING -o eth0 -j MASQUERADE
COMMIT
*filter
:INPUT ACCEPT [0:0]
:FORWARD DROP [0:0]
:OUTPUT ACCEPT [0:0]
-A FORWARD -i wlan1 -o wlan0 -j ACCEPT
-A FORWARD -i wlan1 -o eth0 -j ACCEPT
-A FORWARD -i wlan0 -o wlan1 -m state --state ESTABLISHED,RELATED -j ACCEPT
-A FORWARD -i eth0 -o wlan1 -m state --state ESTABLISHED,RELATED -j ACCEPT
COMMIT
EOF

# Boot-Order: WAN-Verbindung vor AP-Services
log "Konfiguriere Service-Dependencies..."
mkdir -p /etc/systemd/system/hostapd.service.d
cat >/etc/systemd/system/hostapd.service.d/override.conf <<EOF
[Unit]
After=pi-repeater-ip.service pi-wan-connect.service
Wants=pi-repeater-ip.service pi-wan-connect.service
Requires=pi-repeater-ip.service
[Service]
ExecStartPre=/bin/sh -c 'sleep 2'
Restart=on-failure
RestartSec=5
EOF
mkdir -p /etc/systemd/system/dnsmasq.service.d
cat >/etc/systemd/system/dnsmasq.service.d/override.conf <<EOF
[Unit]
After=pi-repeater-ip.service pi-wan-connect.service
Wants=pi-repeater-ip.service pi-wan-connect.service
Requires=pi-repeater-ip.service
[Service]
ExecStartPre=/bin/sh -c 'sleep 1'
Restart=on-failure
RestartSec=3
EOF

# ---------- Web-UI ----------
log "Erstelle modernes Web-Interface..."
mkdir -p /opt/pi-config

# Install additional Python packages
log "Installiere Python-Abhängigkeiten..."
pip3 install psutil flask-socketio python-dotenv --break-system-packages 2>/dev/null || true

# Copy modern dashboard from same directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/modern_dashboard.py" ]]; then
    log "Kopiere modernes Dashboard..."
    cp "${SCRIPT_DIR}/modern_dashboard.py" /opt/pi-config/web.py
    chmod +x /opt/pi-config/web.py
else
    log "FEHLER: modern_dashboard.py nicht gefunden!"
    log "Bitte stelle sicher, dass modern_dashboard.py im gleichen Verzeichnis liegt."
    exit 1
fi

# Copy or create .env file
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
    log "Kopiere .env Datei..."
    cp "${SCRIPT_DIR}/.env" /opt/pi-config/.env
    chmod 600 /opt/pi-config/.env
else
    log "Erstelle .env aus .env.example..."
    if [[ -f "${SCRIPT_DIR}/.env.example" ]]; then
        cp "${SCRIPT_DIR}/.env.example" /opt/pi-config/.env
        chmod 600 /opt/pi-config/.env
        log "⚠️  WICHTIG: Bitte passe /opt/pi-config/.env mit deinen Passwörtern an!"
    else
        log "Erstelle Standard-.env Datei..."
        cat > /opt/pi-config/.env << 'ENVEOF'
# OpenPiRouter Configuration
DASHBOARD_PASSWORD=admin
PIHOLE_PASSWORD=admin
WEB_PORT=8080
DEFAULT_AP_SSID=OpenPiRouter
DEFAULT_AP_PASSWORD=raspberry123
DEFAULT_AP_CHANNEL=36
DEFAULT_AP_BAND=a
WIFI_COUNTRY=DE
TIMEZONE=Europe/Berlin
HOSTNAME=openpirouter
ENVEOF
        chmod 600 /opt/pi-config/.env
        log "⚠️  WICHTIG: Standard-Passwörter wurden gesetzt! Bitte ändere sie in /opt/pi-config/.env"
    fi
fi
cat >/etc/systemd/system/pi-config.service <<EOF
[Unit]
Description=OpenPiRouter Web Dashboard
After=network.target
[Service]
Environment=WEB_PORT=${WEB_PORT}
ExecStart=/usr/bin/python3 /opt/pi-config/web.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF

# UI-Defaults
log "Erstelle Standard-Konfiguration..."
cat >/etc/pi-repeater.yaml <<EOF
ap_ssid: ${SSID}
ap_pass: ${PASS}
ap_band: ${BAND}
wan_ssid: ""
wan_pass: ""
EOF

# Pi-hole optional
if [[ "$PIHOLE" -eq 1 ]]; then
  log "Installiere Pi-hole..."
  curl -sSL https://install.pi-hole.net | bash /dev/stdin --unattended || true
  [ -f /etc/pihole/setupVars.conf ] && sed -i 's/^#\?LISTENING_BEHAVIOUR=.*/LISTENING_BEHAVIOUR=0/' /etc/pihole/setupVars.conf || true
  log "Pi-hole Installation abgeschlossen."
fi

# Enable (nur aktivieren, nicht starten)
log "Aktiviere Services..."
systemctl daemon-reload
systemctl unmask hostapd dnsmasq || true

systemctl enable pi-regdom.service pi-wan-connect.service pi-ap-prep.service pi-repeater-ip.service
systemctl enable hostapd dnsmasq netfilter-persistent pi-config.service
systemctl enable pihole-FTL || true

log "============================================================"
log "Konfiguration geschrieben. Nichts wurde jetzt gestartet."
log "Nach REBOOT aktiv:"
log "  WAN = USB -> wlan0 -> ${WAN_SSID}  |  AP = intern -> wlan1"
log "  AP: ${SSID} / ${PASS}  |  IP: ${AP_IP}/${PREF}"
log "  Web-UI: http://${AP_IP}:${WEB_PORT}  |  Pi-hole: http://${AP_IP}/admin"
log "Jetzt neu starten: sudo reboot"
log "============================================================"
