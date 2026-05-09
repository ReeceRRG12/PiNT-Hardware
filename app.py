from flask import Flask, render_template, jsonify
from scapy.all import sniff, Ether, DNS, IP, UDP, BOOTP, DHCP, sendp, get_if_hwaddr, get_if_addr
import threading
import struct
import socket
import re
import subprocess
import os
import time

IFACE = "eth0"

app = Flask(__name__)

_lock = threading.Lock()
neighbours = []       # LLDP + CDP entries
mdns_devices = {}     # keyed by raw PTR rdata string

_mdns_host_ip = {}    # hostname -> IP (from A records)
_mdns_srv = {}        # instance -> hostname (from SRV records)

_dhcp_cache = {"options": [], "fetched_at": 0}
_DHCP_CACHE_TTL = 300  # seconds

_stats_lock = threading.Lock()
_prev_stats = {}
_prev_stats_time = 0.0

# ── LLDP ─────────────────────────────────────────────────────────────────────

def parse_lldp(packet):
    if not packet.haslayer(Ether):
        return
    if packet[Ether].type != 0x88cc:
        return

    raw = bytes(packet)
    result = {"protocol": "LLDP"}
    pos = 14
    port_from_type2 = None
    port_from_type4 = None

    while pos + 2 <= len(raw):
        header = struct.unpack_from("!H", raw, pos)[0]
        tlv_type = (header >> 9) & 0x7F
        tlv_len = header & 0x1FF
        pos += 2
        if tlv_type == 0:
            break
        if pos + tlv_len > len(raw):
            break
        value = raw[pos:pos + tlv_len]
        pos += tlv_len

        if tlv_type == 1:
            result["chassis_id"] = (
                ":".join(f"{b:02x}" for b in value[1:7])
                if len(value) >= 7
                else value[1:].decode(errors="replace")
            )
        elif tlv_type == 2:
            subtype = value[0] if value else 0
            port_str = value[1:].decode("utf-8", errors="ignore").strip()
            if subtype in (5, 7) and port_str:
                port_from_type2 = port_str
        elif tlv_type == 3:
            result["ttl"] = struct.unpack("!H", value)[0]
        elif tlv_type == 4:
            port_from_type4 = value.decode("utf-8", errors="ignore").strip()
        elif tlv_type == 5:
            result["system_name"] = value.decode("utf-8", errors="ignore").strip()
        elif tlv_type == 6:
            result["system_description"] = value.decode("utf-8", errors="ignore").strip().splitlines()[0][:80]
        elif tlv_type == 8:
            if len(value) >= 6 and value[1] == 1:
                result["mgmt_ip"] = f"{value[2]}.{value[3]}.{value[4]}.{value[5]}"
        elif tlv_type == 127 and len(value) >= 4:
            oui = value[0:3]
            subtype = value[3]
            if oui == b"\x00\x80\xc2" and subtype == 0x01 and len(value) >= 6:
                result["vlan_id"] = struct.unpack_from("!H", value, 4)[0]

    result["port_id"] = port_from_type4 or port_from_type2 or "Unknown"

    if result.get("chassis_id"):
        with _lock:
            for n in neighbours:
                if n.get("chassis_id") == result["chassis_id"] and n.get("protocol") == "LLDP":
                    n.update(result)
                    return
            neighbours.append(result)


# ── CDP ──────────────────────────────────────────────────────────────────────

def parse_cdp(packet):
    raw = bytes(packet)
    if len(raw) < 30:
        return

    # Ethernet(14) + LLC/SNAP(8) + CDP version/TTL/checksum(4) = 26
    offset = 26
    device = {"protocol": "CDP"}

    try:
        while offset + 4 <= len(raw):
            tlv_type = struct.unpack("!H", raw[offset:offset + 2])[0]
            tlv_len = struct.unpack("!H", raw[offset + 2:offset + 4])[0]
            if tlv_len < 4:
                break
            value = raw[offset + 4:offset + tlv_len]

            if tlv_type == 0x0001:
                device["system_name"] = value.decode("utf-8", errors="ignore").strip()
            elif tlv_type == 0x0002:
                if len(value) >= 9:
                    ip = value[5:9]
                    device["mgmt_ip"] = f"{ip[0]}.{ip[1]}.{ip[2]}.{ip[3]}"
            elif tlv_type == 0x0003:
                device["port_id"] = value.decode("utf-8", errors="ignore").strip()
            elif tlv_type == 0x0005:
                device["system_description"] = value.decode("utf-8", errors="ignore").split("\n")[0][:80]
            elif tlv_type == 0x0006:
                device["model"] = value.decode("utf-8", errors="ignore").strip()
            elif tlv_type == 0x000a:
                if len(value) >= 2:
                    device["vlan_id"] = struct.unpack("!H", value[0:2])[0]

            offset += tlv_len
    except Exception as e:
        print(f"CDP parse error: {e}")

    if not device.get("system_name") and not device.get("mgmt_ip"):
        return

    device["chassis_id"] = device.get("system_name", device.get("mgmt_ip", "unknown-cdp"))
    with _lock:
        for n in neighbours:
            if n.get("chassis_id") == device["chassis_id"] and n.get("protocol") == "CDP":
                n.update(device)
                return
        neighbours.append(device)


# ── mDNS ─────────────────────────────────────────────────────────────────────

MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353

_HIDDEN_STYPES = {
    "_services._dns-sd._udp", "_sleep-proxy._udp",
    "_meshcop._udp", "_asquic._udp",
}
_SIMPLE_STYPES = {
    "_airplay._tcp", "_raop._tcp", "_googlecast._tcp",
    "_spotify-connect._tcp", "_http._tcp", "_printer._tcp",
    "_ipp._tcp", "_smb._tcp", "_ssh._tcp",
}


def _strip_local(name):
    return re.sub(r"\.local\.?$", "", name.rstrip("."), flags=re.IGNORECASE)


def _normalise(name):
    name = name.lower()
    name = re.sub(r"\s*\((\d+)\)", r" \1", name)
    return name.replace("-", " ").strip()


def _parse_mdns_rr_chain(rr):
    while rr and hasattr(rr, "type"):
        try:
            rrname = _strip_local(
                rr.rrname.decode("utf-8", errors="ignore")
                if isinstance(rr.rrname, bytes) else str(rr.rrname)
            )
            if rr.type == 1 and hasattr(rr, "rdata"):
                _mdns_host_ip[rrname] = rr.rdata
            elif rr.type == 33 and hasattr(rr, "target"):
                target = _strip_local(
                    rr.target.decode("utf-8", errors="ignore")
                    if isinstance(rr.target, bytes) else str(rr.target)
                )
                _mdns_srv[rrname] = target
            rr = rr.payload if hasattr(rr, "payload") else None
        except Exception:
            break


def _resolve_mdns_ip(instance, friendly):
    if instance in _mdns_srv:
        host = _mdns_srv[instance]
        if host in _mdns_host_ip:
            return _mdns_host_ip[host]
    if instance in _mdns_host_ip:
        return _mdns_host_ip[instance]
    norm = _normalise(friendly)
    for k, v in _mdns_host_ip.items():
        if _normalise(k) == norm:
            return v
    return "Unknown"


def handle_mdns(pkt):
    try:
        if not pkt.haslayer(DNS):
            return
        dns = pkt[DNS]
        for section in ("an", "ar", "ns"):
            try:
                _parse_mdns_rr_chain(getattr(dns, section))
            except Exception:
                pass
        if dns.ancount == 0:
            return
        for i in range(dns.ancount):
            try:
                rr = dns.an[i]
                if rr.type != 12:
                    continue
                rdata = (
                    rr.rdata.decode("utf-8", errors="ignore")
                    if isinstance(rr.rdata, bytes) else str(rr.rdata)
                )
                rrname = (
                    rr.rrname.decode("utf-8", errors="ignore")
                    if isinstance(rr.rrname, bytes) else str(rr.rrname)
                )
                stype = rrname.rstrip(".").replace(".local.", "").replace(".local", "")
                if stype in _HIDDEN_STYPES:
                    continue
                friendly_raw = rdata.replace(f".{rrname.rstrip('.')}", "").strip(".")
                friendly = re.sub(r"\.local\.?$", "", (friendly_raw or rdata), flags=re.IGNORECASE).strip(".")
                if "@" in friendly:
                    friendly = friendly.split("@", 1)[1]
                instance = _strip_local(rdata)
                ip = _resolve_mdns_ip(instance, friendly)
                with _lock:
                    if rdata in mdns_devices:
                        mdns_devices[rdata]["ip"] = ip
                    else:
                        mdns_devices[rdata] = {
                            "friendly": friendly,
                            "type": stype,
                            "ip": ip,
                            "instance": instance,
                            "simple": stype in _SIMPLE_STYPES,
                        }
            except Exception:
                pass
    except Exception as e:
        print(f"mDNS error: {e}")


def _send_mdns_queries():
    queries = [
        "_airplay._tcp.local", "_raop._tcp.local", "_googlecast._tcp.local",
        "_companion-link._tcp.local", "_hap._tcp.local", "_http._tcp.local",
        "_printer._tcp.local", "_smb._tcp.local", "_sftp-ssh._tcp.local",
    ]
    for q in queries:
        try:
            qname = b"".join(
                bytes([len(p)]) + p.encode() for p in q.rstrip(".").split(".")
            ) + b"\x00"
            pkt = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x0c\x00\x01"
            for _ in range(2):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
                s.sendto(pkt, (MDNS_ADDR, MDNS_PORT))
                s.close()
        except Exception as e:
            print(f"mDNS query error for {q}: {e}")


def _mdns_query_loop():
    while True:
        _send_mdns_queries()
        time.sleep(60)


# ── Interface stats ───────────────────────────────────────────────────────────

def _read_net_stat(name):
    try:
        with open(f"/sys/class/net/{IFACE}/statistics/{name}") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def get_interface_info():
    global _prev_stats, _prev_stats_time
    result = {}

    for attr, key in (("speed", "speed_mbps"), ("duplex", "duplex"), ("operstate", "link")):
        try:
            with open(f"/sys/class/net/{IFACE}/{attr}") as f:
                val = f.read().strip()
                result[key] = int(val) if attr == "speed" else val
        except Exception:
            result[key] = None

    stat_keys = ["rx_packets", "tx_packets", "rx_errors", "tx_errors",
                 "rx_dropped", "tx_dropped", "rx_bytes", "tx_bytes"]
    stats = {k: _read_net_stat(k) for k in stat_keys}
    now = time.time()

    with _stats_lock:
        elapsed = now - _prev_stats_time if _prev_stats_time else 0
        if _prev_stats and elapsed > 0:
            result["rx_bps"] = round((stats["rx_bytes"] - _prev_stats.get("rx_bytes", 0)) * 8 / elapsed)
            result["tx_bps"] = round((stats["tx_bytes"] - _prev_stats.get("tx_bytes", 0)) * 8 / elapsed)
            result["rx_error_rate"] = round(
                (stats["rx_errors"] - _prev_stats.get("rx_errors", 0)) / elapsed, 4
            )
            result["tx_error_rate"] = round(
                (stats["tx_errors"] - _prev_stats.get("tx_errors", 0)) / elapsed, 4
            )
        else:
            result["rx_bps"] = result["tx_bps"] = 0
            result["rx_error_rate"] = result["tx_error_rate"] = 0
        _prev_stats = stats
        _prev_stats_time = now

    result["rx_errors_total"] = stats["rx_errors"]
    result["tx_errors_total"] = stats["tx_errors"]
    result["rx_dropped_total"] = stats["rx_dropped"]
    result["tx_dropped_total"] = stats["tx_dropped"]
    result["rx_packets_total"] = stats["rx_packets"]
    result["tx_packets_total"] = stats["tx_packets"]

    return result


# ── IP config (Linux) ─────────────────────────────────────────────────────────

def get_ip_config():
    result = {
        "hostname": socket.gethostname(),
        "ip": "Unknown", "subnet": "Unknown", "gateway": "Unknown",
        "mac": "Unknown", "dns": [], "domain": "Unknown",
        "dhcp_enabled": False, "dhcp_server": "Unknown",
    }

    try:
        out = subprocess.check_output(
            ["ip", "addr", "show", IFACE], text=True, stderr=subprocess.DEVNULL
        )
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", out)
        if m:
            result["ip"] = m.group(1)
            prefix = int(m.group(2))
            mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
            result["subnet"] = socket.inet_ntoa(struct.pack("!I", mask))
        m = re.search(r"link/ether ([\w:]+)", out)
        if m:
            result["mac"] = m.group(1)
    except Exception:
        pass

    try:
        out = subprocess.check_output(
            ["ip", "route", "show", "default"], text=True, stderr=subprocess.DEVNULL
        )
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            result["gateway"] = m.group(1)
    except Exception:
        pass

    try:
        with open("/etc/resolv.conf") as f:
            content = f.read()
        result["dns"] = re.findall(r"nameserver (\S+)", content)
        m = re.search(r"(?:domain|search) (\S+)", content)
        if m:
            result["domain"] = m.group(1)
    except Exception:
        pass

    # dhcpcd lease detection
    for lease_path in (
        f"/var/lib/dhcpcd/{IFACE}.lease",
        f"/run/dhcpcd/{IFACE}.lease",
        f"/var/lib/dhcp/dhclient.{IFACE}.leases",
    ):
        if os.path.exists(lease_path):
            result["dhcp_enabled"] = True
            try:
                out = subprocess.check_output(
                    ["dhcpcd", "-U", IFACE], text=True, stderr=subprocess.DEVNULL
                )
                m = re.search(r"dhcp_server_identifier=(\S+)", out)
                if m:
                    result["dhcp_server"] = m.group(1)
            except Exception:
                pass
            break

    return result


# ── DHCP options ─────────────────────────────────────────────────────────────

DHCP_OPTIONS = {
    1:   ("Subnet Mask",             "standard"),
    3:   ("Router / Gateway",        "standard"),
    6:   ("DNS Servers",             "standard"),
    12:  ("Hostname",                "standard"),
    15:  ("Domain Name",             "standard"),
    28:  ("Broadcast Address",       "standard"),
    42:  ("NTP Servers",             "notable"),
    43:  ("Vendor Specific",         "notable"),
    44:  ("WINS Servers",            "notable"),
    51:  ("Lease Time",              "standard"),
    54:  ("DHCP Server",             "standard"),
    58:  ("Renewal Time (T1)",       "standard"),
    59:  ("Rebinding Time (T2)",     "standard"),
    66:  ("TFTP Server",             "notable"),
    67:  ("Bootfile Name",           "notable"),
    119: ("Domain Search List",      "standard"),
    121: ("Classless Static Routes", "notable"),
    252: ("WPAD (Proxy)",            "notable"),
}

_SCAPY_NAME_MAP = {
    "subnet_mask": 1, "router": 3, "name_server": 6, "hostname": 12,
    "domain": 15, "broadcast_address": 28, "NTP_server": 42,
    "vendor_specific": 43, "NetBIOS_name_server": 44, "lease_time": 51,
    "server_id": 54, "renewal_time": 58, "rebinding_time": 59,
    "TFTP_server_name": 66, "bootfile_name": 67,
}


def _fmt_single(v):
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8").strip("\x00")
        except Exception:
            return v.hex()
    try:
        return socket.inet_ntoa(v.to_bytes(4, "big") if isinstance(v, int) else v)
    except Exception:
        pass
    return str(v)


def _fmt_dhcp_val(opt_num, raw):
    try:
        if isinstance(raw, (list, tuple)):
            return ", ".join(_fmt_single(v) for v in raw)
        if opt_num in (51, 58, 59):
            secs = int(raw)
            if secs >= 86400:
                return f"{secs // 86400}d {(secs % 86400) // 3600}h"
            if secs >= 3600:
                return f"{secs // 3600}h {(secs % 3600) // 60}m"
            return f"{secs // 60}m"
        return _fmt_single(raw)
    except Exception:
        return str(raw)


def _fetch_dhcp_options(timeout=10):
    options = []
    try:
        import random
        my_mac = get_if_hwaddr(IFACE)
        mac_bytes = bytes.fromhex(my_mac.replace(":", ""))

        # Probe with a slightly different MAC so the server treats this as a
        # new client — works even when eth0 has a static IP (INFORM is ignored
        # by many servers when the IP isn't in their lease table)
        probe_bytes = mac_bytes[:5] + bytes([(mac_bytes[5] ^ 0xFF) & 0xFE])
        probe_mac = ":".join(f"{b:02x}" for b in probe_bytes)
        xid = random.randint(1, 0xFFFFFFFE)

        discover = (
            Ether(dst="ff:ff:ff:ff:ff:ff", src=probe_mac) /
            IP(src="0.0.0.0", dst="255.255.255.255") /
            UDP(sport=68, dport=67) /
            BOOTP(op=1, xid=xid, chaddr=probe_bytes + b"\x00" * 10) /
            DHCP(options=[
                ("message-type", "discover"),
                ("param_req_list", [1, 3, 6, 12, 15, 28, 42, 43, 44,
                                    51, 54, 58, 59, 66, 67, 119, 121, 252]),
                "end",
            ])
        )

        def _is_offer(pkt):
            return (
                pkt.haslayer(DHCP) and pkt[BOOTP].xid == xid and
                any(opt[0] == "message-type" and opt[1] == 2
                    for opt in pkt[DHCP].options if isinstance(opt, tuple))
            )

        sendp(discover, iface=IFACE, verbose=False)
        result = sniff(iface=IFACE, lfilter=_is_offer, count=1, timeout=timeout, store=True)
        if not result:
            return options

        for opt in result[0][DHCP].options:
            if not isinstance(opt, tuple) or opt[0] == "end":
                continue
            try:
                opt_num = int(opt[0])
            except (ValueError, TypeError):
                opt_num = _SCAPY_NAME_MAP.get(opt[0])
                if opt_num is None:
                    continue
            label, flag = DHCP_OPTIONS.get(opt_num, (f"Option {opt_num}", "unknown"))
            options.append({
                "option": opt_num,
                "label": label,
                "value": _fmt_dhcp_val(opt_num, opt[1] if len(opt) > 1 else ""),
                "flag": flag,
            })
        options.sort(key=lambda x: x["option"])
    except Exception as e:
        options.append({"option": 0, "label": "Error", "value": str(e), "flag": "unknown"})
    return options


def _dhcp_refresh_loop():
    while True:
        options = _fetch_dhcp_options()
        _dhcp_cache["options"] = options
        _dhcp_cache["fetched_at"] = time.time()
        time.sleep(_DHCP_CACHE_TTL)


# ── Background threads ────────────────────────────────────────────────────────

def _start_lldp():
    sniff(iface=IFACE, prn=parse_lldp, store=0)


def _start_cdp():
    sniff(iface=IFACE, filter="ether host 01:00:0c:cc:cc:cc", prn=parse_cdp, store=0)


def _start_mdns():
    sniff(filter="udp port 5353", prn=handle_mdns, store=0)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/neighbours")
def api_neighbours():
    with _lock:
        return jsonify(list(neighbours))


@app.route("/api/mdns")
def api_mdns():
    with _lock:
        return jsonify(list(mdns_devices.values()))


@app.route("/api/interface")
def api_interface():
    return jsonify(get_interface_info())


@app.route("/api/ip")
def api_ip():
    return jsonify(get_ip_config())


@app.route("/api/dhcp")
def api_dhcp():
    return jsonify({
        "options": _dhcp_cache["options"],
        "fetched_at": _dhcp_cache["fetched_at"],
    })


if __name__ == "__main__":
    for target in (_start_lldp, _start_cdp, _start_mdns, _mdns_query_loop, _dhcp_refresh_loop):
        threading.Thread(target=target, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
