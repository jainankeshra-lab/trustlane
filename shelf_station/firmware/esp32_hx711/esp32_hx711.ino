// TrustLane shelf station firmware: ESP32 + one HX711 load-cell amplifier per lane, plus a
// Bluetooth LE scanner that hears shoppers' phones nearby (presence).
//
// Serial output for shelf_station/station.py, one line each:
//   W,<lane>,<grams>   ten times a second per lane
//   B,<id>,<rssi>      a phone advertising the TrustLane service with its rotating trip ID
// Serial input:
//   t                  re-zero every lane (shelf empty)
//   A,<4 hex><8 hex>   shelf token to broadcast: shelf number + 32-bit token that rotates every 30 s,
//                      computed by station.py with this shelf's key. Phones (iOS too, in the background)
//                      hear it and report it, proving they were near this shelf.
//
// Libraries: "HX711" by Bogdan Necula (Library Manager). BLE comes with the ESP32 Arduino core
// (written for core 2.x, where BLEUUID::toString() returns std::string; on core 3.x it returns
// String: change the marked line).
// Calibrate: put a known weight on a lane, read the raw value, set CAL[lane] = raw / grams.
// Send 't' over serial to re-zero every lane with the shelf empty.
//
// The phone side is the TrustLane native app (mobile/): browsers cannot advertise over Bluetooth.
// The ID is HMAC(trip key, "eid|" + floor(unix_time / 30)) truncated to 8 bytes.

#include "HX711.h"
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLEAdvertising.h>

// This shelf also acts as the aisle beacon: phones running the TrustLane app pick the strongest
// "TL-AISLE-<n>" they hear as their aisle. Set it to this shelf's aisle.
const char* AISLE_BEACON_NAME = "TL-AISLE-7";

const int LANES = 3;
const int DOUT_PINS[LANES] = {16, 18, 21};
const int SCK_PINS[LANES]  = {17, 19, 22};
float CAL[LANES] = {420.0, 420.0, 420.0};   // raw units per gram; set from your calibration

HX711 scales[LANES];
BLEScan* scanner;

// Bluetooth callbacks run on another task: hand sightings to loop() through a small ring buffer.
const int RING = 16;
char ringIds[RING][17];
int ringRssi[RING];
volatile int ringHead = 0, ringTail = 0;
portMUX_TYPE ringMux = portMUX_INITIALIZER_UNLOCKED;

// The app advertises a 128-bit service UUID: TRUSTLANE_PREFIX + 16 hex digits of rotating trip ID,
// e.g. 6e1f7a2c-4d3b-4c59-fa74-2e105496d68d -> ID fa742e105496d68d. (iOS can't send service data,
// so the ID rides in the UUID itself.)
static const char* TRUSTLANE_PREFIX = "6e1f7a2c-4d3b-4c59-";
// Shelves advertise this fixed service UUID (iOS apps can only listen in the background for a known
// service) plus manufacturer data: company 0xFFFF (Bluetooth SIG test ID; use your registered ID in
// production), shelf number (2 bytes), token (4 bytes).
static const char* SHELF_SERVICE = "6e1f7a2d-4d3b-4c59-a5e1-f00000000001";
uint8_t shelfPayload[8] = {0xFF, 0xFF, 0, 0, 0, 0, 0, 0};
BLEAdvertising* beacon;
char cmd[32];
int cmdLen = 0;

class Sightings : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice dev) override {
    if (!dev.haveServiceUUID()) return;
    for (int u = 0; u < dev.getServiceUUIDCount(); u++) {
      std::string s = dev.getServiceUUID(u).toString();   // core 3.x: String s = ...toString(); use s.c_str()
      if (s.size() != 36 || strncasecmp(s.c_str(), TRUSTLANE_PREFIX, 19) != 0) continue;
      char id[17];
      memcpy(id, s.c_str() + 19, 4);       // "fa74"
      memcpy(id + 4, s.c_str() + 24, 12);  // "2e105496d68d"
      id[16] = 0;
      for (int i = 0; i < 16; i++) id[i] = tolower(id[i]);
      portENTER_CRITICAL(&ringMux);
      int next = (ringHead + 1) % RING;
      if (next != ringTail) {
        memcpy(ringIds[ringHead], id, 17);
        ringRssi[ringHead] = dev.getRSSI();
        ringHead = next;
      }
      portEXIT_CRITICAL(&ringMux);
    }
  }
};

void tareAll() {
  for (int i = 0; i < LANES; i++) scales[i].tare(20);
  Serial.println("# tared");
}

// Advertising: flags + shelf service UUID + manufacturer data; the aisle name goes in the scan response.
void advertise() {
  BLEAdvertisementData data;
  data.setFlags(0x06);  // general discoverable, BR/EDR not supported
  data.setCompleteServices(BLEUUID(SHELF_SERVICE));
  data.setManufacturerData(std::string((const char*)shelfPayload, 8));  // core 3.x: String((const char*)shelfPayload, 8)
  BLEAdvertisementData response;
  response.setName(AISLE_BEACON_NAME);
  beacon->stop();
  beacon->setAdvertisementData(data);
  beacon->setScanResponseData(response);
  beacon->start();
}

int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

void handleCommand(const char* line) {
  if (line[0] == 't' && line[1] == 0) { tareAll(); return; }
  if (line[0] == 'A' && line[1] == ',' && strlen(line) == 14) {
    for (int i = 0; i < 6; i++) {
      int hi = hexNibble(line[2 + 2 * i]), lo = hexNibble(line[3 + 2 * i]);
      if (hi < 0 || lo < 0) return;
      shelfPayload[2 + i] = (uint8_t)(hi * 16 + lo);
    }
    advertise();
  }
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < LANES; i++) {
    scales[i].begin(DOUT_PINS[i], SCK_PINS[i]);
    scales[i].set_scale(CAL[i]);
  }
  delay(500);
  tareAll();

  BLEDevice::init(AISLE_BEACON_NAME);

  // Aisle beacon + shelf token (the token stays zero until station.py sends the first one).
  beacon = BLEDevice::getAdvertising();
  beacon->setScanResponse(true);
  advertise();

  scanner = BLEDevice::getScan();
  scanner->setAdvertisedDeviceCallbacks(new Sightings(), true);  // true: report repeat advertisements
  scanner->setActiveScan(false);                                  // passive: listen only
  scanner->setInterval(160);
  scanner->setWindow(80);
  scanner->start(0, nullptr, false);                             // scan forever, non-blocking
}

void loop() {
  while (Serial.available()) {  // line commands from station.py
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdLen) { cmd[cmdLen] = 0; handleCommand(cmd); cmdLen = 0; }
    } else if (cmdLen < (int)sizeof(cmd) - 1) {
      cmd[cmdLen++] = c;
    }
  }
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
