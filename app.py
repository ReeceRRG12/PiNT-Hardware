from flask import Flask, render_template, jsonify
from scapy.all import sniff, Ether
import threading
import struct
import socket
app = Flask(__name__)
neighbours = []
def parse_mgmt_ip(value):
    if len(value) < 2:
        return None
    addr_len = value[0]
    addr_subtype = value[1]
    if addr_subtype == 1 and addr_len == 5:  # IPv4
        return socket.inet_ntoa(value[2:6])
    return None
def parse_lldp(packet):
    if not packet.haslayer(Ether):
        return
    if packet[Ether].type != 0x88cc:
        return
    data = bytes(packet[Ether].payload)
    result = {}
    pos = 0
    while pos < len(data):
        if pos + 2 > len(data):
            break
        header = struct.unpack("!H", data[pos:pos+2])[0]
        tlv_type = (header >> 9) & 0x7F
        tlv_len = header & 0x1FF
        pos += 2
        value = data[pos:pos+tlv_len]
        pos += tlv_len
        if tlv_type == 0:
            break
        elif tlv_type == 1:
            result['chassis_id'] = ':'.join(f'{b:02x}' for b in value[1:7]) if len(value) >= 7 else value[1:].decode(errors='replace')
        elif tlv_type == 2:
            result['port_id'] = value[1:].decode(errors='replace')
        elif tlv_type == 3:
            result['ttl'] = struct.unpack("!H", value)[0]
        elif tlv_type == 5:
            result['system_name'] = value.decode(errors='replace')
        elif tlv_type == 6:
            result['system_description'] = value.decode(errors='replace')
        elif tlv_type == 8:
            mgmt_ip = parse_mgmt_ip(value)
            if mgmt_ip:
                result['mgmt_ip'] = mgmt_ip
        elif tlv_type == 127 and len(value) >= 4:
            oui = value[0:3]
            subtype = value[3]
            payload = value[4:]
            # IEEE 802.1 - VLAN ID (OUI 00:80:c2, subtype 3)
            if oui == b'\x00\x80\xc2' and subtype == 3 and len(payload) >= 2:
                result['vlan_id'] = struct.unpack("!H", payload[0:2])[0]
    if result:
        for n in neighbours:
            if n.get('chassis_id') == result.get('chassis_id'):
                n.update(result)
                return
        neighbours.append(result)
def start_sniff():
    sniff(iface="eth0", prn=parse_lldp, store=0)
@app.route('/')
def index():
    return render_template('index.html')
@app.route('/api/neighbours')
def api_neighbours():
    return jsonify(neighbours)
if __name__ == '__main__':
    t = threading.Thread(target=start_sniff, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=5000)
