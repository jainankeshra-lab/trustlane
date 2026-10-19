"""Push notifications to the native app (Expo push service), so nudges reach a phone whose app
is in the background or locked. The web app keeps using its WebSocket.

The app registers its Expo push token per trip; only these note kinds are pushed.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("trustlane.push")
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
PUSHED = {
    "nudge": "Did you forget to scan?",
    "confirm": "Quick check before you leave",
    "staff": "A team member is coming",
    "payment_failed": "Payment didn't go through",
    "receipt": "Paid · gate open",
    "info": "TrustLane",
}


def message_for(token: str, kind: str, data: dict) -> dict | None:
    if kind not in PUSHED:
        return None
    if kind == "receipt":
        body = f"Charged Rs {data.get('total', 0) / 100:.2f}. Thanks for shopping!"
    elif kind == "confirm":
        body = f"{len(data.get('items', []))} item(s) need a quick confirmation at the gate."
    else:
        body = data.get("text", "")
    return {"to": token, "title": PUSHED[kind], "body": body, "sound": "default",
            "priority": "high", "data": {"kind": kind, **({"pick_id": data["pick_id"]} if "pick_id" in data else {})}}


class PushSender:
    """Sends to Expo's push service. Expo answers with one ticket per message; a ticket saying
    DeviceNotRegistered means the app was uninstalled or notifications were turned off, so the
    token is dropped (on_dead_token) instead of being retried on every nudge."""

    def __init__(self, client: httpx.AsyncClient | None = None, url: str = EXPO_PUSH_URL, on_dead_token=None):
        self.client = client
        self.url = url
        self.on_dead_token = on_dead_token
        self.sent: list[dict] = []
        self.errors: list[dict] = []

    async def send(self, messages: list[dict]) -> list[dict]:
        if not messages:
            return []
        client = self.client or httpx.AsyncClient(timeout=10)
        tickets: list[dict] = []
        try:
            resp = await client.post(self.url, json=messages, headers={"Accept": "application/json"})
            if resp.status_code >= 400:
                log.warning("Expo push rejected (%s): %s", resp.status_code, resp.text[:200])
                return [{"status": "error", "message": f"HTTP {resp.status_code}"}]
            try:
                tickets = resp.json().get("data", [])
            except ValueError:
                tickets = []
            if isinstance(tickets, dict):  # a single message gets a single ticket
                tickets = [tickets]
            for msg, ticket in zip(messages, tickets):
                if ticket.get("status") == "error":
                    self.errors.append({"to": msg["to"], **ticket})
                    del self.errors[:-100]
                    if (ticket.get("details") or {}).get("error") == "DeviceNotRegistered" and self.on_dead_token:
                        self.on_dead_token(msg["to"])
            self.sent.extend(messages)
            del self.sent[:-200]
        except httpx.HTTPError as err:
            log.warning("Expo push failed: %s", err)
            return [{"status": "error", "message": str(err)}]
        finally:
            if self.client is None:
                await client.aclose()
        return tickets
