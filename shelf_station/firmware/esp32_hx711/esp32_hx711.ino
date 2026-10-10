// TrustLane shelf station firmware: ESP32 + one HX711 load-cell amplifier per lane, plus a
// Bluetooth LE scanner that hears shoppers' phones nearby (presence).
//
// Serial output for shelf_station/station.py, one line each:
//   W,<lane>,<grams>   ten times a second per lane
//   B,<id>,<rssi>      a phone advertising the TrustLane service with its rotating trip ID
//
// Libraries: "HX711" by Bogdan Necula (Library Manager). BLE comes with the ESP32 Arduino core
// (tested API: core 2.x, where getServiceData() returns std::string; on core 3.x it returns String,
// change the two marked lines).
// Calibrate: put a known weight on a lane, read the raw value, set CAL[lane] = raw / grams.
// Send 't' over serial to re-zero every lane with the shelf empty.
//
// The phone side (advertising the rotating ID as service data) needs the native app: browsers
// cannot advertise over Bluetooth. The ID is HMAC(trip key, "eid|" + floor(unix_time / 30))[:8 bytes].

#include "HX711.h"
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

const int LANES = 3;
const int DOUT_PINS[LANES] = {16, 18, 21};
const int SCK_PINS[LANES]  = {17, 19, 22};
float CAL[LANES] = {420.0, 420.0, 420.0};   // raw units per gram; set from your calibration

// 128-bit service UUID the TrustLane app advertises (any fixed UUID works; keep app and firmware in sync).
static BLEUUID TRUSTLANE_SERVICE("6e1f7a2c-4d3b-4c59-9a6e-7c1d2b3a4f50");

HX711 scales[LANES];
BLEScan* scanner;

// Bluetooth callbacks run on another task: hand sightings to loop() through a small ring buffer.
const int RING = 16;
char ringIds[RING][17];
int ringRssi[RING];
volatile int ringHead = 0, ringTail = 0;
portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

class Sightings : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice dev) override {
    if (!dev.haveServiceData() || !dev.getServiceDataUUID().equals(TRUSTLANE_SERVICE)) return;
    std::string data = dev.getServiceData();          // core 3.x: String data = dev.getServiceData();
    if (data.size() < 8) return;                       // core 3.x: data.length()
    portENTER_CRITICAL(&ringMux);
    int next = (ringHead + 1) % RING;
    if (next != ringTail) {
      for (int i = 0; i < 8; i++) sprintf(&ringIds[ringHead][i * 2], "%02x", (uint8_t)data[i]);
      ringRssi[ringHead] = dev.getRSSI();
      ringHead = next;
    }
    portEXIT_CRITICAL(&ringMux);
  }
};

void tareAll() {
  for (int i = 0; i < LANES; i++) scales[i].tare(20);
  Serial.println("# tared");
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < LANES; i++) {
    scales[i].begin(DOUT_PINS[i], SCK_PINS[i]);
    scales[i].set_scale(CAL[i]);
  }
  delay(500);
  tareAll();

  BLEDevice::init("");
  scanner = BLEDevice::getScan();
  scanner->setAdvertisedDeviceCallbacks(new Sightings(), true);  // true: report repeat advertisements
  scanner->setActiveScan(false);                                  // passive: listen only
  scanner->setInterval(160);
  scanner->setWindow(80);
  scanner->start(0, nullptr, false);                             // scan forever, non-blocking
}

void loop() {
  if (Serial.available() && Serial.read() == 't') tareAll();
  for (int i = 0; i < LANES; i++) {
    if (scales[i].wait_ready_timeout(100)) {
      Serial.print("W,");
      Serial.print(i);
      Serial.print(",");
      Serial.println(scales[i].get_units(2), 1);
    }
  }
  while (true) {  // drain Bluetooth sightings
    char id[17];
    int rssi;
    portENTER_CRITICAL(&ringMux);
    bool empty = ringTail == ringHead;
    if (!empty) {
      memcpy(id, ringIds[ringTail], 17);
      rssi = ringRssi[ringTail];
      ringTail = (ringTail + 1) % RING;
    }
    portEXIT_CRITICAL(&ringMux);
    if (empty) break;
    Serial.print("B,");
    Serial.print(id);
    Serial.print(",");
    Serial.println(rssi);
  }
  delay(100);
}
