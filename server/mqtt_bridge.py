"""MQTT bridge: shelf stations publish to the store gateway's broker, the server subscribes.

Topics (JSON payloads, the same fields as the HTTP endpoints):
    trustlane/shelf/<shelf_id>/events      take / return
    trustlane/shelf/<shelf_id>/heartbeat
    trustlane/shelf/<shelf_id>/presence    Bluetooth IDs heard near the shelf

Set TRUSTLANE_MQTT_URL=mqtt://host:1883 (optionally mqtt://user:pass@host:1883) to enable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Awaitable, Callable
from urllib.parse import urlparse

log = logging.getLogger("trustlane.mqtt")
TOPIC = "trustlane/shelf/+/+"
KINDS = {"events", "heartbeat", "presence"}


def parse_message(topic: str, payload: bytes) -> tuple[str, dict] | None:
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "trustlane" or parts[1] != "shelf" or parts[3] not in KINDS:
        return None
    try:
        body = json.loads(payload)
    except ValueError:
        log.warning("Ignored non-JSON MQTT message on %s", topic)
        return None
    if not isinstance(body, dict):
        return None
    body.setdefault("shelf_id", parts[2])
    return parts[3], body


class MqttBridge:
    def __init__(self, url: str, loop: asyncio.AbstractEventLoop,
                 handler: Callable[[str, dict], Awaitable[None]]):
        self.url = urlparse(url)
        self.loop = loop
        self.handler = handler
        self.client = None
        self.connected = False

    def start(self) -> None:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"trustlane-server-{secrets.token_hex(3)}")
        if self.url.username:
            client.username_pw_set(self.url.username, self.url.password)

        def on_connect(c, _userdata, _flags, reason_code, _props):
            self.connected = not reason_code.is_failure
            if self.connected:
                c.subscribe(TOPIC, qos=1)
                log.warning("MQTT bridge connected to %s, subscribed to %s", self.url.hostname, TOPIC)

        def on_disconnect(_c, _userdata, _flags, _reason_code, _props):
            self.connected = False

        def on_message(_c, _userdata, msg):
            parsed = parse_message(msg.topic, msg.payload)
            if parsed:
                asyncio.run_coroutine_threadsafe(self.handler(*parsed), self.loop)

        client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
        client.connect_async(self.url.hostname or "localhost", self.url.port or 1883)
        client.loop_start()
        self.client = client

    def stop(self) -> None:
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
