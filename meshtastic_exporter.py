from typing import Optional

import meshtastic
import meshtastic.tcp_interface
import meshtastic.ble_interface

import sys
import time
import typer

from pubsub import pub

from prometheus_client import start_http_server, Gauge

INCOMING_MESSAGES = Gauge("incoming_messages", "Number of messages received by the radio.")

app = typer.Typer()

def onReceive(packet, interface):  # pylint: disable=unused-argument
    """called when a packet arrives"""
    INCOMING_MESSAGES.inc()
    print(f"Received: {packet}")


def onConnection(interface, topic=pub.AUTO_TOPIC):  # pylint: disable=unused-argument
    """called when we (re)connect to the radio"""
    # defaults to broadcast, specify a destination ID if you wish
    print("radio connected")

@app.command()
def tcp(host: str = "meshtastic.local", port: int = 8000):
    pub.subscribe(onReceive, "meshtastic.receive")
    pub.subscribe(onConnection, "meshtastic.connection.established")
    start_http_server(port)
    try:
        iface = meshtastic.tcp_interface.TCPInterface(hostname=host)
        while True:
            time.sleep(100)
    except Exception as ex:
        print(f"Error: Could not connect to {host} {ex}")
        sys.exit(1)

@app.command()
def ble(address: Optional[str] = "any", port: int = 8000):
    # TODO seems like I need to figure out how to pair a Bluetooth radio with a computer first
    pub.subscribe(onReceive, "meshtastic.receive")
    pub.subscribe(onConnection, "meshtastic.connection.established")
    start_http_server(port)
    if address == "any":
        potential_devices = meshtastic.ble_interface.BLEClient().discover()
        print(f"Potential devices: {potential_devices}")
        for device in potential_devices:
            try:
                iface = meshtastic.ble_interface.BLEInterface(address=device.address)
                while True:
                    time.sleep(100)
            except Exception as ex:
                print(f"Could not connect to {device.address}, likely not a Meshtastic device. Exception: {ex}")
                continue
        print("Error: No Meshtastic-compatible devices found. Exiting.")
        sys.exit(1)
    try:
        iface = meshtastic.ble_interface.BLEInterface(address=address)
        while True:
            time.sleep(100)
    except Exception as ex:
        print(f"Error: Could not connect to {address} {ex}")
        sys.exit(1)


if __name__ == "__main__":
    app()
