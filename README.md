# TrustLane prototype

Scan-and-go loss prevention: shelf sensors (load cells + camera) record what a shopper takes, the
shopper's phone records what they scan, and a fusion engine cross-checks the two in real time.
Honest shoppers walk straight out and are charged automatically; checks happen only where shelf
and phone disagree.

## Run it

Windows, Python 3.11+:

```powershell
cd trustlane
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run.ps1
```

Open <http://localhost:8000>. It links to the four screens:

| Screen | URL | Plays the part of |
|---|---|---|
| Phone app | `/phone` | Shopper's scan-and-go app |
| Shelf simulator | `/shelf` | Shelf station (camera + load cells) when no hardware is attached |
| Exit gate | `/gate` | Gate screen and light at the door |
| Ops console | `/ops` | Monitoring dashboard and staff console |

`run.ps1` sets a 20-second match window so nudges arrive quickly in a demo (`-Window 60` for the real value).

**Phone on Wi-Fi:** run `.\run.ps1 -Lan`, allow Python through Windows Firewall when asked, and open the
printed `http://<laptop-ip>:8000/phone` on the phone. On plain http the phone uses the product
picker; real camera barcode scanning needs https or `localhost` (browser rule).

## Demo script (about 4 minutes)

Open the phone app, shelf simulator, gate and ops console side by side.

1. **Honest shopper.** Phone: *start trip*, pick **Aisle 7**. Shelf: **Take 1** razor. Phone: tap the razor
   (it shows *Verified ✓*). **Go to exit → Tap at gate G1**: gate goes green, phone shows a test-mode UPI receipt.
2. **Forgot to scan.** New trip, Aisle 7. Shelf: take a chocolate. Don't scan. After 20 s the phone shows
   *"Oops! Did you pick up…"*. Tap **Scan it now**: the nudge clears and the exit is green.
3. **Barcode swap.** New trip, Aisle 7. Shelf: take razor blades. Phone: tap **Spearmint gum**. The gum line is
   flagged and the phone asks for the blades. (Or use *Package check → Holding: …* to show the on-phone check.)
4. **Pocketed item.** New trip, Aisle 7. Shelf: take blades, ignore the nudge, go to the exit: gate turns red and
   the ops console shows a staff check with reasons. Staff click **Add ticked items & charge**.
5. **Network down.** Ops console: **switch offline**. Scan milk (buffered at the gateway), exit: the gate still
   opens after checking the signed token on its own. Switch back online: buffered events replay and the trip is charged.

Also try: **Put back 1** on the shelf (cancels the expected scan, or removes a scanned item), switching a
shelf station off (ops alert: shelf unverified), and **Verify audit log**.

## How it maps to the architecture

| Layer (deck slide 4) | Code |
|---|---|
| Shelf station: camera + load cell | `shelf_station/station.py`, `shelf_station/firmware/esp32_hx711/`; simulator at `/shelf` |
| Phone app: scan, package check, signed log | `web/phone.html`, `web/sha256.js` (HMAC-signed scans) |
| Aisle beacons | Simulated by the aisle buttons in the phone app |
| Store edge gateway (offline buffer, offline token check) | `Gateway` in `server/main.py` |
| Fusion engine, risk engine, virtual cart | `server/engine.py` |
| Catalog | `data/catalog.json`, `server/catalog.py` |
| Payment service | `server/payments.py`: sandbox, or Razorpay test mode |
| Exit gate (Ed25519-signed tokens) | `server/security.py`, `web/gate.html` |
| Monitoring: metrics, dashboards, alerts, staff console, audit log | `server/metrics.py` (`/metrics`), `web/ops.html`, `server/ledger.py` (hash-chained SQLite) |

### Decision rules

* A shelf pick is attributed to shoppers whose phone is in that aisle. A scan of the same product within the
  match window verifies it; otherwise the phone is nudged.
* Risk points: unscanned pick (low 10 / medium 20 / high 35, split between shoppers when shared),
  ignored nudge +20, nudge +5, suspected barcode swap +45 (10 if the real item is scanned later),
  package mismatch +60, bad signature +100.
* Exit: **green** under 25 points (35 for shoppers with 5+ clean trips), **amber** under 60, **red** above.
  An unscanned item only this shopper could have taken always means at least amber, so nothing is
  auto-charged past it. Amber asks the shopper to confirm; disputing a medium/high-value pick goes to staff.

All thresholds are in `server/config.py` and can be set with `TRUSTLANE_*` environment variables.

## Real hardware

Per lane: one bar load cell + HX711 into an ESP32.

| HX711 | ESP32 (lanes 0 / 1 / 2) |
|---|---|
| VCC | 3V3 |
| GND | GND |
| DT | GPIO 16 / 18 / 21 |
| SCK | GPIO 17 / 19 / 22 |

1. Flash `shelf_station/firmware/esp32_hx711/esp32_hx711.ino` (Arduino IDE, "HX711" library by Bogdan Necula).
   Calibrate `CAL[]` with a known weight; send `t` over serial to zero the empty shelf.
2. `pip install -r shelf_station/requirements.txt`
3. `python -m shelf_station.station --shelf S7-A --serial COM5`. Add `--camera 0 --model ...` for product
   recognition (next section). Without hardware: `--stdin`, then type `take 0`.

Lanes map to products through `shelf_id` and `lane` in `data/catalog.json`.

## Shelf camera: YOLO product recognition

The camera watches the shelf front. MediaPipe finds hands; a YOLO model recognises the product in
each hand. On every take, the station reports what the camera saw, and the server fuses it with the
load cell:

| Camera says | Weight change | Result |
|---|---|---|
| Same product as the lane | any | Pick confirmed by camera |
| A different product, confident | fits that product better than the lane's | **Camera wins**: logged as a misplaced item on the shelf |
| A different product | fits the lane's product as well or better | Lane wins, disagreement logged |
| Unsure (below 0.6) or nothing | | Weight only |

The ops console shows how often camera and weight agree and alerts above 10% disagreement (camera
moved, shelf re-arranged, or the model needs retraining).

**Train a model for your products (about 20 minutes):**

```powershell
.\.venv\Scripts\python.exe -m pip install -r shelf_station\requirements-vision.txt
# 1. Hold each product at the shelf, press its number, then B for burst. ~150 photos per class.
.\.venv\Scripts\python.exe -m shelf_station.capture --classes razor blades chocolate empty
# 2. Fine-tune YOLO11n-cls (downloads the base weights once); saves models\shelf-classify.pt
.\.venv\Scripts\python.exe -m shelf_station.train --data datasets\shelf-products --epochs 30
# 3. Run the station with the camera
.\.venv\Scripts\python.exe -m shelf_station.station --shelf S7-A --serial COM5 --camera 0 --model models\shelf-classify.pt --show
```

`--camera` also takes a video file, which is handy for a demo: record the shelf once, replay it on stage.
YOLO runs only while MediaPipe sees a hand, so products resting on the shelf never count;
`--no-hands` recognises the whole frame instead (for a camera that only sees lifted items). The
MediaPipe hand model (about 8 MB) is downloaded to `models/` on first use.

Folder and class names must be catalog SKUs (plus `empty`). A detection model also works: label
boxes in Roboflow or Label Studio, export the YOLO dataset YAML, and train with `--task detect`.
For the Raspberry Pi, `--export ncnn` or `--export onnx` gives faster CPU inference; running on the
Hailo-8L needs the model compiled to HEF with Hailo's Dataflow Compiler (not included here).

No camera handy? The shelf simulator's **Camera (YOLO) sees** menu sends the same camera evidence,
including a misplaced item.

## Payments: Razorpay in test mode

By default the prototype uses a built-in sandbox that charges instantly at the gate. To use a real
gateway, add Razorpay **test** keys:

1. Sign up at the Razorpay Dashboard, switch to **Test Mode**, and generate API keys
   (Account & Settings → API Keys).
2. Copy `.env.example` to `.env` and paste the two keys. Restart the server. The ops console's payment
   tile and the phone's start screen now say Razorpay.

At the exit, a green decision creates a Razorpay order and the phone opens Razorpay Checkout. Pick
UPI and enter `success@razorpay` (or `failure@razorpay` to see the failure path). The server then:

* checks Razorpay's `order_id|payment_id` signature with your key secret,
* fetches the payment from Razorpay's API to confirm it belongs to this order,
* captures it if it is only authorised,

and only then opens the gate and sends the receipt. If the phone closes mid-payment, the
`/api/payments/razorpay/webhook` endpoint (signature-checked) finishes the trip instead; Razorpay
needs a public URL for webhooks, so use a tunnel if you want to try that locally.

Only `rzp_test_…` keys are accepted; the server refuses to start with live keys. Phone-side UPI
Intent and QR flows can't be tested in Razorpay test mode, so the test flow uses a UPI ID.

**Why the shopper approves at the gate instead of being charged silently:** UPI Autopay requires a
pre-debit notification 24 hours before each debit, so it can't charge at the moment of exit. True
zero-touch billing needs **UPI Reserve Pay** (funds blocked at entry, debited at exit), currently
offered by some providers for selected merchant categories up to ₹10,000. The sandbox provider shows
that experience; `server/payments.py` is the place to add a Reserve Pay provider.

## Monitoring

`/metrics` serves Prometheus metrics (`trustlane_*`). Minimal scrape config:

```yaml
scrape_configs:
  - job_name: trustlane
    scrape_interval: 5s
    static_configs: [{ targets: ["localhost:8000"] }]
```

The ops console computes the deck's alert rules live: match rate below 95%, a silent shelf station,
latency p95 above 2 s, more than 30% of red checks cleared by staff, and the cloud being offline.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Covers every demo scene, put-backs, shared picks, replayed and forged scans, forged tokens, payment
failure fallback, the offline gateway, the audit-log chain, the load-cell tracker, and that the phone's
JavaScript HMAC matches the server's.

## What is simulated in this prototype

* **Product recognition** is built (YOLO, see above), but ships with no trained model: you train it on
  your own products. Without `--model` the camera only confirms a hand was present, and product
  identity comes from which load-cell lane changed.
* **Package check on the phone** is a demo control; the on-device image model is not built yet. The
  server-side swap inference (scan vs. what the shelf saw) is real.
* **Aisle location** comes from buttons, standing in for BLE beacons.
* **Payments** never move real money: either the built-in sandbox, or Razorpay in test mode.
* One process plays both the store gateway and the cloud. Shelf events use HTTP, not MQTT.
