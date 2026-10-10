"""Shelf station delivery: loose produce lanes, drift re-zeroing, the on-disk outbox, and MQTT end to end."""
import asyncio
import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from server.main import create_app
from server.mqtt_bridge import parse_message
from shelf_station.station import HttpTransport, LaneTracker, MqttTransport, Outbox
from tests.helpers import Phone, staff
from tests.test_api import make_settings


def settle(tracker, grams, n=10, quiet=True):
    return sum(tracker.add(grams, quiet) for _ in range(n))


def test_loose_produce_lane_reports_grams():
    t = LaneTracker(100, by_weight=True)
    settle(t, 5000)
    assert settle(t, 4470) == 1 and round(t.last_delta_g) == 530
    assert settle(t, 4670) == -1 and round(t.last_delta_g) == -200


def test_drift_is_reported_and_rezeroed_only_when_no_hand_is_near():
    t = LaneTracker(52)
    settle(t, 520)
    settle(t, 528, quiet=False)                # creep while a shopper is at the shelf
    assert t.health(0)["drift_g"] == 8.0 and not t.rezeroed
    settle(t, 528, quiet=True)                 # shelf quiet: re-zero
    assert t.rezeroed and t.health(0)["drift_g"] == 0.0
    assert settle(t, 476) == 1                 # and a real take is still counted from the new zero


def test_outbox_survives_a_station_restart(tmp_path):
    path = tmp_path / "outbox.jsonl"
    dead = HttpTransport("http://127.0.0.1:9", None, Outbox(path))  # nothing listens on port 9
    dead.send("events", {"station_id": "s", "shelf_id": "S7-A", "lane": 0, "kind": "take"})
    assert len(Outbox(path).items) == 1                              # still there after a "restart"


def test_mqtt_bridge_parses_topics():
    assert parse_message("trustlane/shelf/S7-A/events", b'{"kind":"take"}') == ("events", {"kind": "take", "shelf_id": "S7-A"})
    assert parse_message("trustlane/shelf/S7-A/unknown", b"{}") is None
    assert parse_message("other/topic/x/events", b"{}") is None
    assert parse_message("trustlane/shelf/S7-A/events", b"not json") is None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def broker():
    amqtt = pytest.importorskip("amqtt.broker")
    port = _free_port()
    loop = asyncio.new_event_loop()
    started = threading.Event()
    holder = {}

    async def run():
        b = amqtt.Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
                          "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
        await b.start()
        holder["b"] = b
        started.set()

    thread = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_until_complete(run()),
                                              loop.run_forever()), daemon=True)
    thread.start()
    if not started.wait(10):
        pytest.skip("MQTT test broker did not start")
    yield f"mqtt://127.0.0.1:{port}"
    asyncio.run_coroutine_threadsafe(holder["b"].shutdown(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)


def test_station_event_over_mqtt_reaches_the_engine(tmp_path, broker):
    app = create_app(make_settings(tmp_path, mqtt_url=broker, station_key="k1"))
    with TestClient(app) as c:
        p = Phone(c, aisle="7")
        deadline = time.time() + 10
        while not (app.state.mqtt and app.state.mqtt.connected) and time.time() < deadline:
            time.sleep(0.1)
        assert app.state.mqtt.connected, "server bridge did not connect to the broker"
        station = MqttTransport(broker, "S7-A", "k1", Outbox(None))
        station.send("events", {"station_id": "mqtt-st", "shelf_id": "S7-A", "lane": 0, "kind": "take",
                                "ts": time.time(), "weight_delta_g": -52})
        intruder = MqttTransport(broker, "S7-A", "wrong-key", Outbox(None))
        intruder.send("events", {"station_id": "fake", "shelf_id": "S7-A", "lane": 1, "kind": "take"})
        while time.time() < deadline:
            picks = c.get(f"/api/ops/trips/{p.sid}", headers=staff(app)).json()["picks"]
            if picks:
                break
            time.sleep(0.1)
        time.sleep(0.5)
        picks = c.get(f"/api/ops/trips/{p.sid}", headers=staff(app)).json()["picks"]
        assert [x["sku"] for x in picks] == ["razor"]  # the wrong-key message was dropped
        station.client.loop_stop()
        intruder.client.loop_stop()
