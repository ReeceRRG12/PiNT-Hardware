# PiNT Hardware 🍺

A Raspberry Pi network appliance that passively listens on a switch port and displays live network information via a mobile-friendly web dashboard.

Plug it into any switch port, connect to the PiNT WiFi hotspot, and instantly see switch details, DHCP scope, local devices, and cable health — no laptop required.

## What it does

- **Layer 2 Neighbours** — captures LLDP and CDP packets on eth0, displays switch/AP name, port, chassis ID, VLAN, management IP, and description
- **mDNS Discovery** — passively listens for mDNS announcements and resolves device names and IPs across common service types
- **Interface & Cable Test** — shows link speed, duplex, RX/TX rates, and cumulative error/drop counters
- **IP Info** — displays the Pi's own IP, subnet, gateway, DNS, and MAC on eth0
- **DHCP Scope** — sends a DHCP DISCOVER and shows all options returned by the server (lease time, DNS, NTP, TFTP, vendor options, etc.)
- Serves a mobile-friendly web UI at `http://pint.local:5000`
- Broadcasts its own WiFi hotspot (SSID: PiNT) for access on isolated VLANs
- Auto-starts on boot — no interaction needed

## Hardware

- Raspberry Pi 4 (2GB+ RAM)
- SD card (16GB+)
- USB-C power supply or battery bank
- Ethernet cable

## Software

- Raspberry Pi OS Lite 64-bit (Bookworm)
- Python 3
- Flask
- Scapy

## Setup

### 1. Flash the SD card

Use Raspberry Pi Imager with Raspberry Pi OS Lite (64-bit). In the settings configure:
- Hostname: `PiNT`
- Enable SSH
- Set a username and password of your choice
- Optional: WiFi credentials for initial setup

### 2. Update the system

    sudo apt update && sudo apt upgrade -y

### 3. Install dependencies

    sudo apt install python3-pip python3-venv -y
    mkdir pint && cd pint
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

### 4. Deploy the app

Copy the following files into `~/pint/`:
- `app.py`
- `templates/index.html`

### 5. Set up the systemd service

Create `/etc/systemd/system/pint.service`:

    [Unit]
    Description=PiNT Network Tester
    After=network.target

    [Service]
    ExecStart=/home/admin/pint/venv/bin/python /home/admin/pint/app.py
    WorkingDirectory=/home/admin/pint
    StandardOutput=journal
    StandardError=journal
    Restart=always
    User=root

    [Install]
    WantedBy=multi-user.target

Enable and start it:

    sudo systemctl daemon-reload
    sudo systemctl enable pint
    sudo systemctl start pint

### 6. Set up the WiFi hotspot

Install hostapd and dnsmasq:

    sudo apt install hostapd dnsmasq -y

Configure `/etc/hostapd/hostapd.conf`:

    interface=wlan0
    driver=nl80211
    ssid=PiNT
    hw_mode=g
    channel=6
    wmm_enabled=0
    macaddr_acl=0
    auth_algs=1
    wpa=2
    wpa_passphrase=YourPasswordHere
    wpa_key_mgmt=WPA-PSK
    wpa_pairwise=TKIP
    rsn_pairwise=CCMP

Update `/etc/default/hostapd`:

    DAEMON_CONF="/etc/hostapd/hostapd.conf"

Replace `/etc/dnsmasq.conf`:

    interface=wlan0
    dhcp-range=192.168.50.10,192.168.50.50,255.255.255.0,24h
    domain=local
    address=/pint.local/192.168.50.1

Tell NetworkManager to leave wlan0 alone by adding to `/etc/NetworkManager/NetworkManager.conf`:

    [keyfile]
    unmanaged-devices=interface-name:wlan0

Create `/etc/NetworkManager/dispatcher.d/pre-up.d/wlan0-static`:

    #!/bin/bash
    ip addr add 192.168.50.1/24 dev wlan0

Make it executable:

    sudo chmod +x /etc/NetworkManager/dispatcher.d/pre-up.d/wlan0-static

Enable everything and reboot:

    sudo systemctl unmask hostapd
    sudo systemctl enable hostapd
    sudo systemctl enable dnsmasq
    sudo reboot

## Usage

1. Plug PiNT into any switch port
2. Connect your phone or laptop to the **PiNT** WiFi network using the password you set
3. Browse to `http://pint.local:5000`
4. Layer 2 neighbour information appears within 30–60 seconds; mDNS devices populate as announcements are heard

## Project structure

    pint/
    ├── app.py
    └── templates/
        └── index.html

## Security note

The web UI has no authentication. It is intended for use on the isolated PiNT hotspot only — do not expose port 5000 on a production or shared network. The app runs as root to allow raw packet capture via Scapy.

## License

MIT — see [LICENSE](LICENSE).

## Roadmap

- Small e-paper display integration
- Session export / report generation
- Favicon
