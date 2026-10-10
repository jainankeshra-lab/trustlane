"""Alert delivery: when an ops alert starts or clears, post it to Slack and/or any webhook.

Alerts are de-duplicated by id, so a condition that stays true pages once, and its recovery
is reported once. Info-level alerts stay on the ops console and are not paged.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("trustlane.alerts")
PAGE_LEVELS = {"warn", "bad"}


class AlertNotifier:
    def __init__(self, slack_webhook: str = "", alert_webhook: str = "", ledger=None,
                 client: httpx.AsyncClient | None = None):
        self.slack_webhook = slack_webhook
        self.alert_webhook = alert_webhook
        self.ledger = ledger
        self.client = client
        self.active: dict[str, dict] = {}
        self.sent: list[dict] = []  # what was delivered, for the ops console and tests

    async def update(self, alerts: list[dict], now: float) -> list[dict]:
        current = {a["id"]: a for a in alerts if a.get("level") in PAGE_LEVELS}
        events = [{"state": "firing", **a} for aid, a in current.items() if aid not in self.active]
        events += [{"state": "resolved", **a} for aid, a in self.active.items() if aid not in current]
        self.active = current
        for event in events:
            await self._deliver(event, now)
        return events

    async def _deliver(self, event: dict, now: float) -> None:
        icon = {"bad": ":red_circle:", "warn": ":large_orange_circle:"}.get(event["level"], "")
        text = (f"{icon} TrustLane alert: {event['text']}" if event["state"] == "firing"
                else f":white_check_mark: Resolved: {event['text']}")
        log.warning(text)
        if self.ledger is not None:
            self.ledger.append(now, f"alert.{event['state']}", None, event)
        self.sent.append({**event, "ts": now})
        del self.sent[:-100]
        targets = []
        if self.slack_webhook:
            targets.append((self.slack_webhook, {"text": text}))
        if self.alert_webhook:
            targets.append((self.alert_webhook, {"source": "trustlane", "ts": now, **event}))
        if not targets:
            return
        client = self.client or httpx.AsyncClient(timeout=5)
        try:
            for url, body in targets:
                try:
                    await client.post(url, json=body)
                except httpx.HTTPError as err:
                    log.error("Alert delivery to %s failed: %s", url, err)
        finally:
            if self.client is None:
                await client.aclose()
