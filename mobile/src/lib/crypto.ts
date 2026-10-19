// Trip signatures and the rotating Bluetooth ID. Must match server/security.py exactly
// (tests/crypto.test.ts checks this against vectors produced by the Python server code).
import { hmac } from '@noble/hashes/hmac.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, hexToBytes, utf8ToBytes } from '@noble/hashes/utils.js';

export const EID_PERIOD_S = 30;
// 128-bit Bluetooth service UUID = this prefix + the 16-hex-digit trip ID (firmware matches the prefix).
export const EID_UUID_PREFIX = '6e1f7a2c-4d3b-4c59-';

export function hmacHex(keyHex: string, message: string): string {
  return bytesToHex(hmac(sha256, hexToBytes(keyHex), utf8ToBytes(message)));
}

/** X-Trip-Auth header value: "<ts_ms>.<hmac(key, METHOD|path|ts_ms)>" */
export function tripAuth(keyHex: string, method: string, path: string, tsMs: number): string {
  return `${tsMs}.${hmacHex(keyHex, `${method.toUpperCase()}|${path}|${tsMs}`)}`;
}

export function scanSignature(keyHex: string, sessionId: string, seq: number, barcode: string, tsMs: number): string {
  return hmacHex(keyHex, `${sessionId}|${seq}|${barcode}|${tsMs}`);
}

/** Rotating Bluetooth ID: only the server can map it to a trip, and it changes every 30 s. */
export function eidFor(keyHex: string, nowMs: number): string {
  return hmacHex(keyHex, `eid|${Math.floor(nowMs / 1000 / EID_PERIOD_S)}`).slice(0, 16);
}

export function eidToServiceUuid(eid: string): string {
  return `${EID_UUID_PREFIX}${eid.slice(0, 4)}-${eid.slice(4, 16)}`;
}
