// TrustLane exit gate firmware: ESP32 driving the gate hardware for gate_agent/agent.py.
//
//   * Floor scale in the exit lane: one platform load cell (or four summed in a junction box) into an HX711.
//   * Turnstile / door: a relay (or the turnstile controller's "free pass" dry contact) pulsed to open.
//   * Tower light: green / amber / red outputs (through a relay board or MOSFETs for 12/24 V lamps).
//
// Serial protocol, 115200 baud, one line each:
//   out  S,<grams>        floor-scale weight, five times a second
//   out  READY            after start-up
//   in   O,<ms>           pulse the gate relay open for <ms> milliseconds (capped at 10 s)
//   in   L,<G|A|R|->      tower light: green, amber, red, or off
//   in   T                tare the floor scale (lane empty)
//
// Fail-safe: the relay is normally open (gate locked) and closes only while a pulse runs; if the
// agent stops talking for 30 s the light goes amber so staff notice. Fire/egress rules still need a
// mechanical override on the turnstile itself.
//
// Libraries: "HX711" by Bogdan Necula. Board: ESP32 Dev Module (esp32 core 2.x or 3.x).

#include "HX711.h"

const int SCALE_DOUT = 16;
const int SCALE_SCK = 17;
float SCALE_CAL = 21.5;  // raw units per gram for your platform; calibrate with a known weight

const int RELAY_PIN = 26;
const bool RELAY_ACTIVE_HIGH = true;
const int LIGHT_GREEN = 25;
const int LIGHT_AMBER = 33;
const int LIGHT_RED = 32;
const unsigned long MAX_OPEN_MS = 10000;
const unsigned long AGENT_SILENT_MS = 30000;

HX711 scale;
unsigned long openUntil = 0;
unsigned long lastReport = 0;
unsigned long lastCommand = 0;
char line[32];
int lineLen = 0;

void relay(bool on) {
  digitalWrite(RELAY_PIN, (on == RELAY_ACTIVE_HIGH) ? HIGH : LOW);
}

void light(char c) {
  digitalWrite(LIGHT_GREEN, c == 'G' ? HIGH : LOW);
  digitalWrite(LIGHT_AMBER, c == 'A' ? HIGH : LOW);
  digitalWrite(LIGHT_RED, c == 'R' ? HIGH : LOW);
}

void handle(const char* cmd) {
  lastCommand = millis();
  if (cmd[0] == 'O' && cmd[1] == ',') {
    unsigned long ms = strtoul(cmd + 2, nullptr, 10);
    if (ms > MAX_OPEN_MS) ms = MAX_OPEN_MS;
    openUntil = millis() + ms;
    relay(true);
  } else if (cmd[0] == 'L' && cmd[1] == ',') {
    light(cmd[2]);
  } else if (cmd[0] == 'T') {
    scale.tare(10);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(LIGHT_GREEN, OUTPUT);
  pinMode(LIGHT_AMBER, OUTPUT);
  pinMode(LIGHT_RED, OUTPUT);
  relay(false);
  light('-');
  scale.begin(SCALE_DOUT, SCALE_SCK);
  scale.set_scale(SCALE_CAL);
  scale.tare(10);
  lastCommand = millis();
  Serial.println("READY");
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen) { line[lineLen] = 0; handle(line); lineLen = 0; }
    } else if (lineLen < (int)sizeof(line) - 1) {
      line[lineLen++] = c;
    }
  }
  if (openUntil && (long)(millis() - openUntil) >= 0) {
    openUntil = 0;
    relay(false);
  }
  if (millis() - lastCommand > AGENT_SILENT_MS) {
    light('A');  // no word from the gate agent: show staff something is wrong
    lastCommand = millis();
  }
  if (millis() - lastReport >= 200 && scale.is_ready()) {
    lastReport = millis();
    Serial.print("S,");
    Serial.println(scale.get_units(1), 0);
  }
}
