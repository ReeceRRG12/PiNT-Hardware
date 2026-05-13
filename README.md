# Pi Network Tools - PiNT Hardware 🍺 

![Version](https://img.shields.io/badge/version-v0.3-1a7a4a?style=flat-square)
![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi%204-c51a4a?style=flat-square&logo=raspberrypi&logoColor=white)
[![Website](https://img.shields.io/badge/website-pinetworktools.com-0077cc?style=flat-square)](https://pinetworktools.com)

A Raspberry Pi network appliance that passively listens on a switch port and displays live network information via a web dashboard. Optionally runs as a standalone kiosk with an attached touchscreen display.

Plug it into any switch port, connect to the PiNT WiFi hotspot (or attach a display), and instantly see switch details, DHCP scope, local devices, and cable health. No laptop required.

## What it does

- **Layer 2 Neighbours:** captures LLDP and CDP packets on eth0, displays switch/AP name, port, chassis ID, VLAN, management IP, and description
- **mDNS Discovery:** passively listens for mDNS announcements and resolves device names and IPs across common service types
- **Interface & Cable Test:** shows link speed, duplex, RX/TX rates, and cumulative error/drop counters
- **IP Info:** displays the Pi's own IP, subnet, gateway, DNS, and MAC on eth0
- **DHCP Scope:** sends a DHCP DISCOVER and shows all options returned by the server (lease time, DNS, NTP, TFTP, vendor options, etc.)
- Serves a web UI at `http://pint.local:5000`
- Broadcasts its own WiFi hotspot (SSID: PiNT) for access on isolated VLANs
- Optional kiosk mode with a 320x480 touchscreen (MPI3501) for standalone use
- Auto-starts on boot with no interaction needed

## Hardware

- Raspberry Pi 4 (2GB+ RAM)
- SD card (16GB+)
- USB-C power supply or battery bank
- Ethernet cable
- Optional: MPI3501 3.5" SPI touchscreen (ILI9486, 320x480)

## Software

- Raspberry Pi OS Lite 64-bit (Bookworm)
- Python 3
- Flask
- Scapy

---

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

Clone this repo or copy the following files into `~/pint/`:
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

---

## Optional: MPI3501 Display Setup (Kiosk Mode)

These steps configure a 3.5" MPI3501 SPI touchscreen and run Chromium in kiosk mode so PiNT operates as a fully standalone device with no phone or laptop needed.

### 1. Install the display driver

The MPI3501 uses the ILI9486 controller and requires the `goodtft LCD-show` driver package.

    git clone https://github.com/goodtft/LCD-show.git
    chmod -R 755 LCD-show
    cd LCD-show
    sudo ./LCD35-show

The Pi will reboot automatically. After rebooting the display should show a console.

### 2. Set display rotation

If the display orientation is wrong, edit `/boot/firmware/config.txt` and find the `dtoverlay` line added by the driver. Set it to:

    dtoverlay=tft35a:rotate=0

Adjust the value (0, 90, 180, 270) to match how your display is mounted, then reboot.

### 3. Calibrate the touchscreen

Install the calibration tool:

    sudo apt install xinput-calibrator -y

Run it on the display and note the output values, then create `/etc/X11/xorg.conf.d/99-calibration.conf`:

    Section "InputClass"
        Identifier      "calibration"
        MatchProduct    "ADS7846 Touchscreen"
        Option "Calibration" "227 3936 268 3880"
        Option "SwapAxes"    "1"
    EndSection

Adjust the `Calibration` values to match your own calibration output.

### 4. Configure eth0 DHCP at boot

The kiosk uses eth0 directly (no NetworkManager) so create a dedicated systemd service to bring it up. Create `/etc/systemd/system/eth0-dhcp.service`:

    [Unit]
    Description=DHCP for eth0
    After=network.target
    Wants=network.target

    [Service]
    Type=oneshot
    ExecStartPre=/sbin/ip link set eth0 up
    ExecStart=/sbin/dhcpcd eth0
    RemainAfterExit=yes

    [Install]
    WantedBy=multi-user.target

Enable it:

    sudo systemctl enable eth0-dhcp
    sudo systemctl start eth0-dhcp

### 5. Install kiosk packages

    sudo apt install xserver-xorg xinit chromium xdotool -y

### 6. Configure Xorg to use the framebuffer display

Create `/etc/X11/xorg.conf.d/99-fbdev.conf`:

    Section "Device"
        Identifier "fb1"
        Driver "fbdev"
        Option "fbdev" "/dev/fb1"
    EndSection

    Section "Screen"
        Identifier "Screen0"
        Device "fb1"
    EndSection

Allow any user to start X (needed for auto-login on tty1):

    sudo nano /etc/X11/Xwrapper.config

Set:

    allowed_users=anybody

Disable the LightDM display manager if it is installed (it conflicts with manual xinit):

    sudo systemctl disable lightdm

### 7. Auto-launch the kiosk on boot

Append the following to `~/.bash_profile` (create it if it does not exist):

    export FRAMEBUFFER=/dev/fb1
    if [ -z "$DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
        xinit /usr/bin/chromium \
            --no-sandbox \
            --kiosk \
            --disable-infobars \
            --window-size=320,480 \
            --window-position=0,0 \
            http://localhost:5000 \
            -- :0 vt1 2>/tmp/log_output.txt
    fi

Enable auto-login on tty1 so the script runs on boot without a keyboard:

    sudo systemctl edit getty@tty1

Add:

    [Service]
    ExecStart=
    ExecStart=-/sbin/agetty --autologin YOUR_USERNAME --noclear %I $TERM

Replace `YOUR_USERNAME` with your actual user (e.g. `admin`). Reboot and the display should launch directly into the PiNT UI.

---

## Usage

1. Plug PiNT into any switch port
2. Connect your phone or laptop to the **PiNT** WiFi network using the password you set, then browse to `http://pint.local:5000`
3. Or, if a display is attached, read it directly
4. Layer 2 neighbour information appears within 30-60 seconds
5. mDNS devices populate as announcements are heard

---

## Project structure

    pint/
    ├── app.py
    └── templates/
        └── index.html

## Built with

PiNT Hardware was built by vibe coding with [Claude](https://claude.ai) by Anthropic.

## Security note

The web UI has no authentication. It is intended for use on the isolated PiNT hotspot or attached display only. Do not expose port 5000 on a production or shared network. The app runs as root to allow raw packet capture via Scapy.

## License

MIT. See [LICENSE](LICENSE).

---

## Changelog

### v0.3
- **New:** MPI3501 3.5" SPI touchscreen support (ILI9486, 320x480)
- **New:** Kiosk mode -- Chromium runs fullscreen on the display via xinit, auto-launches on tty1 at boot
- **New:** Touch calibration config for ADS7846
- **New:** eth0-dhcp systemd service to bring up the wired interface reliably on boot
- **New:** Completely rewritten `index.html` for 320x480 -- tabbed single-panel navigation (DEVICE / IFACE / L2 / mDNS / DHCP), live clock, pulsing indicators, tab counts (e.g. L2(2), mDNS(31))
- **Fix:** mDNS sniffer now binds explicitly to eth0 to avoid capturing on wlan0
- **Fix:** Added 15s startup delay before first DHCP probe so eth0 is fully up
- **Fix:** Added 20s startup delay before first mDNS query for the same reason

### v0.2.1
- Update project name to Pi Network Tools - PiNT Hardware

### v0.2
- DHCP scope discovery via forged DISCOVER packet
- CDP neighbour capture
- Interface RX/TX rate and error counters
- mDNS IP resolution from SRV and A record correlation

### v0.1
- Initial release: LLDP capture, mDNS passive listen, IP info, Flask web UI, WiFi hotspot
