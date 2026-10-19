// Bluetooth, both directions (observed location, hard to fake):
//  * listen to shelf beacons: the aisle name ("TL-AISLE-<n>") and each shelf's signed rotating token,
//    which the phone reports; this works on iPhones in the background too.
//  * broadcast the trip's rotating ID so shelf stations can hear this phone (Android in the
//    background; iOS only while the app is open).
import { PermissionsAndroid, Platform } from 'react-native';
import { BleManager } from 'react-native-ble-plx';

import TrustlaneBle from '../../modules/trustlane-ble';
import { eidFor, eidToServiceUuid } from './crypto';

const BEACON = /^TL-AISLE-(\w+)$/;
const BEACON_RSSI_MIN = -85;
const BEACON_FRESH_MS = 5000;

let manager: BleManager | null = null;
function ble(): BleManager | null {
  if (!manager) {
    try {
      manager = new BleManager();
    } catch {
      return null; // native module missing: Expo Go
    }
  }
  return manager;
}

export const bluetoothAvailable = () => ble() !== null;
export const advertisingAvailable = () => !!TrustlaneBle && safe(() => TrustlaneBle!.isSupported(), false);

function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn();
  } catch {
    return fallback;
  }
}

export async function requestBluetoothPermissions(): Promise<boolean> {
  if (Platform.OS !== 'android') return true; // iOS asks on first use, from the Info.plist message
  const api = typeof Platform.Version === 'number' ? Platform.Version : parseInt(String(Platform.Version), 10);
  const wanted = api >= 31
    ? [PermissionsAndroid.PERMISSIONS.BLUETOOTH_SCAN, PermissionsAndroid.PERMISSIONS.BLUETOOTH_CONNECT,
       PermissionsAndroid.PERMISSIONS.BLUETOOTH_ADVERTISE, PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION]
    : [PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION];
  const result = await PermissionsAndroid.requestMultiple(wanted);
  return wanted.every((p) => result[p] === PermissionsAndroid.RESULTS.GRANTED);
}

// Shelves advertise a fixed service (so iOS can listen in the background) and manufacturer data:
// company 0xFFFF, shelf number (2 bytes), a signed token that changes every 30 s (4 bytes).
export const SHELF_SERVICE = '6e1f7a2d-4d3b-4c59-a5e1-f00000000001';
export type HeardShelf = { shelf_no: number; token: string; rssi: number };

export function parseShelfData(b64: string | null | undefined): { shelf_no: number; token: string } | null {
  if (!b64) return null;
  let bytes: number[];
  try { bytes = Array.from(atob(b64), (c) => c.charCodeAt(0)); } catch { return null; }
  if (bytes.length !== 8 || bytes[0] !== 0xff || bytes[1] !== 0xff) return null;
  const token = bytes.slice(4).map((b) => b.toString(16).padStart(2, '0')).join('');
  return token === '00000000' ? null : { shelf_no: (bytes[2] << 8) | bytes[3], token };
}

type ShelfHandlers = {
  onAisle?: (aisle: string, rssi: number) => void;
  onShelves?: (heard: HeardShelf[]) => void;  // every 10 s, the shelf tokens heard since the last report
  onError: (msg: string) => void;
};

/** Listens to shelf beacons: the strongest aisle name (every 2 s) and the signed shelf tokens heard. */
export function scanShelves({ onAisle, onShelves, onError }: ShelfHandlers): () => void {
  const m = ble();
  if (!m) {
    onError('Bluetooth scanning needs the TrustLane development build (not Expo Go)');
    return () => {};
  }
  const aisles = new Map<string, { rssi: number; ts: number }>();
  const tokens = new Map<string, HeardShelf>();
  m.startDeviceScan([SHELF_SERVICE], { allowDuplicates: true }, (error, device) => {
    if (error) {
      onError(error.message);
      return;
    }
    if (!device || device.rssi == null) return;
    const name = device.localName ?? device.name;
    const match = name ? BEACON.exec(name) : null;
    if (match) aisles.set(match[1], { rssi: device.rssi, ts: Date.now() });
    const shelf = parseShelfData(device.manufacturerData);
    if (shelf) {
      const key = `${shelf.shelf_no}:${shelf.token}`;
      const prev = tokens.get(key);
      if (!prev || device.rssi > prev.rssi) tokens.set(key, { ...shelf, rssi: device.rssi });
    }
  }).catch((e: unknown) => onError(e instanceof Error ? e.message : String(e)));
  const aisleTimer = setInterval(() => {
    const now = Date.now();
    let best: { aisle: string; rssi: number } | null = null;
    for (const [aisle, h] of aisles) {
      if (now - h.ts > BEACON_FRESH_MS) aisles.delete(aisle);
      else if (!best || h.rssi > best.rssi) best = { aisle, rssi: h.rssi };
    }
    if (best && best.rssi >= BEACON_RSSI_MIN && onAisle) onAisle(best.aisle, best.rssi);
  }, 2000);
  const reportTimer = setInterval(() => {
    if (tokens.size && onShelves) onShelves([...tokens.values()].sort((a, b) => b.rssi - a.rssi).slice(0, 10));
    tokens.clear();
  }, 10_000);
  return () => {
    clearInterval(aisleTimer);
    clearInterval(reportTimer);
    m.stopDeviceScan().catch(() => {});
  };
}

/** Broadcasts the trip's rotating ID, re-keyed every 30 s. Returns a stop function. */
export function broadcastTripId(tripKey: string, onState: (s: { advertising: boolean; eid: string; error?: string }) => void): () => void {
  const native = TrustlaneBle;
  if (!native) {
    onState({ advertising: false, eid: eidFor(tripKey, Date.now()), error: 'Needs the TrustLane development build' });
    return () => {};
  }
  let eid = '';
  const sub = native.addListener('onAdvertisingState', (e) =>
    onState({ advertising: e.advertising, eid, error: e.error === undefined ? undefined : String(e.error) }));
  const rotate = () => {
    eid = eidFor(tripKey, Date.now());
    try {
      native.startAdvertising(eidToServiceUuid(eid));
    } catch (e) {
      onState({ advertising: false, eid, error: e instanceof Error ? e.message : String(e) });
    }
  };
  rotate();
  // Align rotation with the server's 30 s period, plus a small margin.
  const timer = setInterval(rotate, 30_000);
  return () => {
    clearInterval(timer);
    sub.remove();
    safe(() => native.stopAdvertising(), undefined);
  };
}
