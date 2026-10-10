"""Test client helpers that behave like the real phone app and staff console."""
import time

from server.security import sign_request, sign_scan


class Phone:
    """A shopper's phone: starts a trip and signs every request with the trip key."""

    def __init__(self, client, aisle=None, shopper=None):
        body = {"consent": True, **(shopper or {})}
        r = client.post("/api/sessions", json=body)
        assert r.status_code == 200, r.text
        data = r.json()
        self.c, self.sid, self.key, self.seq = client, data["session_id"], data["key"], 0
        self.shopper = data.get("shopper", shopper)
        if aisle:
            self.go(aisle)

    def headers(self, method, path):
        return {"X-Trip-Auth": sign_request(self.key, method, path, int(time.time() * 1000))}

    def get(self, sub=""):
        path = f"/api/sessions/{self.sid}{sub}"
        return self.c.get(path, headers=self.headers("GET", path))

    def post(self, sub, json=None):
        path = f"/api/sessions/{self.sid}{sub}"
        return self.c.post(path, json=json or {}, headers=self.headers("POST", path))

    def go(self, aisle):
        return self.post("/location", {"aisle": aisle})

    def scan(self, barcode, offline=False):
        self.seq += 1
        ts = int(time.time() * 1000)
        return self.c.post(f"/api/sessions/{self.sid}/scans", json={
            "seq": self.seq, "barcode": barcode, "ts": ts, "offline": offline,
            "sig": sign_scan(self.key, self.sid, self.seq, barcode, ts)})

    def exit(self, basket_weight_g=None):
        token = self.post("/checkout").json()["token"]
        return self.c.post("/api/gate/G1/tap", json={"token": token, "basket_weight_g": basket_weight_g})

    def cart(self):
        return self.get().json()


def staff(app):
    return {"X-Staff-Pin": app.state.staff_pin}


def shelf(client, kind="take", lane=0, shelf_id="S7-A", **extra):
    return client.post("/api/shelf/events", json={"station_id": f"st-{shelf_id}", "shelf_id": shelf_id, "lane": lane,
                                                  "kind": kind, "ts": time.time(), **extra})
