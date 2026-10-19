// The app's signatures must match the server's Python code byte for byte.
// vectors.json is generated from server/security.py. Run: npm test (Node 24 runs TypeScript directly).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { eidFor, eidToServiceUuid, scanSignature, tripAuth } from '../src/lib/crypto.ts';

const v = JSON.parse(readFileSync(new URL('./vectors.json', import.meta.url), 'utf8'));

test('scan signature matches the server', () => {
  const [sid, seq, barcode, ts] = v.scan.args;
  assert.equal(scanSignature(v.key, sid, seq, barcode, ts), v.scan.sig);
});

test('request signature matches the server', () => {
  const [method, path, ts] = v.request.args;
  assert.equal(tripAuth(v.key, method, path, ts), v.request.header);
});

test('rotating Bluetooth ID matches the server, and fits the service UUID', () => {
  const eid = eidFor(v.key, v.eid.now_ms);
  assert.equal(eid, v.eid.eid);
  const uuid = eidToServiceUuid(eid);
  assert.match(uuid, /^6e1f7a2c-4d3b-4c59-[0-9a-f]{4}-[0-9a-f]{12}$/);
  assert.equal(uuid.slice(19, 23) + uuid.slice(24), eid); // what the ESP32 firmware extracts
});
