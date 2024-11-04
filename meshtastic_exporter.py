from typing import Optional

import meshtastic
import meshtastic.tcp_interface
import meshtastic.ble_interface
import yaml
from meshtastic.mesh_interface import MeshInterface

import sys
import time
import typer

from pubsub import pub

from prometheus_client import start_http_server, Gauge, Counter
from meshtastic import BROADCAST_NUM, BROADCAST_ADDR
from meshtastic.protobuf.portnums_pb2 import PortNum

INCOMING_MESSAGES = Counter("incoming_messages", "Number of messages received by the radio.", ["type"])
NODE_INFO = Gauge("node_info", "Basic information about the nodes. Value is the last seen timestamp.", ["num", "id", "long_name", "short_name", "macaddr", "hw_model", "is_licensed"])
NODE_LATITUDE = Gauge("node_latitude", "Latitude of the node, if it exposes position.", ["num"])
NODE_LONGITUDE = Gauge("node_longitude", "Longitude of the node, if it exposes position.", ["num"])
NODE_ALTITUDE = Gauge("node_altitude", "Altitude of the node, if it exposes position.", ["num"])
NODE_SNR = Gauge("node_snr", "Signal to noise ratio for messages received directly from the node", ["num"])
NODE_RSSI = Gauge("node_rssi", "RSSI for messages received directly from the node", ["num"])
NODE_HOP_LIMIT = Gauge("node_hop_limit", "Hop limit of messages sent by the node", ["num"])
NODE_HOP_COUNT = Gauge("node_hop_count", "How many hops from the node we are", ["num"])
DEVICE_METRICS = Gauge("device_metric", "Metric exposed by the device, together with its value.", ["num", "metric"])
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
        set_last_heard(nodes[sending_node], packet["decoded"]["rx_time"])
    MESSAGES.labels(src=packet["from"],dest=packet["to"] if packet["to"] != BROADCAST_NUM else "all",type=message_type).inc()
    raw_data = packet["raw"]
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
    if message_type == "TELEMETRY_APP":
        # TODO fill for existing message types
        pass
    print(f"Received: {packet}")


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
        set_last_heard(entry, entry['lastHeard'] if 'lastHeard' in entry else '0')
    print(f"Nodes: {nodes}")
    while True:
        time.sleep(100)


def set_last_heard(entry, last_heard):
    NODE_INFO.labels(
        num=entry['num'],
        id=entry['user']['id'],
        long_name=entry['user']['longName'],
        short_name=entry['user']['shortName'],
        macaddr=entry['user']['macaddr'],
        hw_model=entry['user']['hwModel'],
        is_licensed=entry['user']['isLicensed'] if 'isLicensed' in entry['user'] else "False"
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


if __name__ == "__main__":
    app()
