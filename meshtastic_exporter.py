import sys
import time
from typing import Optional

import meshtastic
import meshtastic.ble_interface
import meshtastic.serial_interface
import meshtastic.tcp_interface
import typer
from meshtastic import BROADCAST_NUM
from meshtastic.mesh_interface import MeshInterface
from prometheus_client import start_http_server, Gauge, Counter
from pubsub import pub

INCOMING_MESSAGES = Counter("incoming_messages", "Number of messages received by the radio.", ["type"])
NODE_INFO = Gauge("node_info", "Basic information about the nodes. Value is the last seen timestamp.", ["num", "id", "long_name", "short_name", "macaddr", "hw_model", "is_licensed"])
NODE_LATITUDE = Gauge("node_latitude", "Latitude of the node, if it exposes position.", ["num"])
NODE_LONGITUDE = Gauge("node_longitude", "Longitude of the node, if it exposes position.", ["num"])
NODE_ALTITUDE = Gauge("node_altitude", "Altitude of the node, if it exposes position.", ["num"])
NODE_SNR = Gauge("node_snr", "Signal to noise ratio for messages received directly from the node", ["num"])
NODE_RSSI = Gauge("node_rssi", "RSSI for messages received directly from the node", ["num"])
NODE_HOP_LIMIT = Gauge("node_hop_limit", "Hop limit of messages sent by the node", ["num"])
NODE_HOP_COUNT = Gauge("node_hop_count", "How many hops from the node we are", ["num"])
DEVICE_METRICS = Gauge("device_metric", "Metric exposed by the device, together with its value.", ["num", "metric", "type"])
MESSAGES = Counter("message_count", "Messages sent between nodes", ["src", "dest", "type"])

# latest node information in a dict form digestible for scripts and correlation
nodes = {}

app = typer.Typer()


def onReceive(packet, interface):  # pylint: disable=unused-argument
    """called when a packet arrives"""
    message_type = packet["decoded"]["portnum"]
    INCOMING_MESSAGES.labels(type=message_type).inc()
    sending_node = packet["from"]
    if "rx_time" in packet["decoded"]:
        set_last_heard(sending_node, nodes[sending_node]['user'], packet["decoded"]["rx_time"])
    MESSAGES.labels(src=sending_node,dest=packet["to"] if packet["to"] != BROADCAST_NUM else "all",type=message_type).inc()
    raw_data = packet["raw"]
    parse_signal_and_hops(raw_data, sending_node)
    if message_type == "TELEMETRY_APP":
        parse_telemetry_packet(packet, sending_node)
    elif message_type == "POSITION_APP":
        parse_position_packet(packet, sending_node)
    elif message_type == "NODEINFO_APP":
        parse_nodeinfo_packet(packet, sending_node)
    print(f"Received: {packet}")


def parse_nodeinfo_packet(packet, sending_node):
    # TODO consider: in case of label renames, should we remove the old instance of this node_info?
    # if yes, maybe a node_renames counter should be there? definitely a log line.
    rx_time = packet['rx_time'] if 'rx_time' in packet else time.time()
    set_last_heard(sending_node, packet['decoded']['user'], rx_time)
    if sending_node not in nodes.keys():
        # we update the internal object with limited info we got in the node
        # in case of a restart, it is anyway in radio's internal memory
        nodes[sending_node] = {
            'num': sending_node,
            'user': packet['decoded']['user'],
            'lastHeard': rx_time,
        }


def parse_signal_and_hops(raw_data, sending_node):
    if raw_data.hop_start:
        NODE_HOP_LIMIT.labels(num=sending_node).set(raw_data.hop_start)
    if raw_data.hop_limit and raw_data.hop_start:
        hops = raw_data.hop_start - raw_data.hop_limit
        NODE_HOP_COUNT.labels(num=sending_node).set(hops)
        if hops == 0:
            # we have a direct message, SNR and RSSI values (if present) are reliable.
            if raw_data.rx_snr:
                NODE_SNR.labels(num=sending_node).set(raw_data.rx_snr)
            if raw_data.rx_rssi:
                NODE_RSSI.labels(num=sending_node).set(raw_data.rx_rssi)


def parse_position_packet(packet, sending_node):
    if "position" in packet['decoded']:
        if "latitude" in packet['decoded']['position']:
            NODE_LATITUDE.labels(num=sending_node).set(packet['decoded']['position']['latitude'])
        if "longitude" in packet['decoded']['position']:
            NODE_LONGITUDE.labels(num=sending_node).set(packet['decoded']['position']['longitude'])
        if "altitude" in packet['decoded']['position']:
            NODE_ALTITUDE.labels(num=sending_node).set(packet['decoded']['position']['altitude'])


def parse_telemetry_packet(packet, sending_node):
    if "deviceMetrics" in packet['decoded']['telemetry']:
        for key, value in packet['decoded']['telemetry']['deviceMetrics'].items():
            DEVICE_METRICS.labels(num=sending_node, metric=key, type='device').set(value)
    if "environmentMetrics" in packet['decoded']['telemetry']:
        for key, value in packet['decoded']['telemetry']['environmentMetrics'].items():
            DEVICE_METRICS.labels(num=sending_node, metric=key, type='environment').set(value)


def onConnection(interface, topic=pub.AUTO_TOPIC):  # pylint: disable=unused-argument
    """called when we (re)connect to the radio"""
    print("radio connected")


def server_loop(interface: MeshInterface, port: int):
    pub.subscribe(onReceive, "meshtastic.receive")
    pub.subscribe(onConnection, "meshtastic.connection.established")
    start_http_server(port)
    cached_node_info = interface.nodesByNum
    for id, entry in cached_node_info.items():
        nodes[id] = entry
        set_last_heard(id, entry['user'], entry['lastHeard'] if 'lastHeard' in entry else '0')
    print(f"Nodes: {nodes}")
    while True:
        time.sleep(100)


def set_last_heard(num, user, last_heard):
    NODE_INFO.labels(
        num=num,
        id=user['id'],
        long_name=user['longName'],
        short_name=user['shortName'],
        macaddr=user['macaddr'],
        hw_model=user['hwModel'],
        is_licensed=user['isLicensed'] if 'isLicensed' in user else "False",
    ).set(last_heard)


@app.command()
def tcp(host: str = "meshtastic.local", port: int = 8000):
    try:
        server_loop(meshtastic.tcp_interface.TCPInterface(hostname=host), port)
    except Exception as ex:
        print(f"Error: Could not connect to {host} {ex}")
        sys.exit(1)


@app.command()
def ble(address: Optional[str] = "any", port: int = 8000):
    # TODO seems like I need to figure out how to pair a Bluetooth radio with a computer first
    if address == "any":
        potential_devices = meshtastic.ble_interface.BLEClient().discover()
        print(f"Potential devices: {potential_devices}")
        for device in potential_devices:
            try:
                server_loop(meshtastic.ble_interface.BLEInterface(address=device.address), port)
            except Exception as ex:
                print(f"Could not connect to {device.address}, likely not a Meshtastic device. Exception: {ex}")
                continue
        print("Error: No Meshtastic-compatible devices found. Exiting.")
        sys.exit(1)
    try:
        server_loop(meshtastic.ble_interface.BLEInterface(address=address), port)
    except Exception as ex:
        print(f"Error: Could not connect to {address} {ex}")
        sys.exit(1)

@app.command()
def serial(path: Optional[str] = None, port: int = 8000):
    try:
        server_loop(meshtastic.serial_interface.SerialInterface(devPath=path), port)
    except Exception as ex:
        print(f"Error: Could not connect to serial {ex}")
        sys.exit(1)


if __name__ == "__main__":
    app()
