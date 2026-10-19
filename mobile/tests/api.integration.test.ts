// The app's real API client (src/lib/api.ts) against the real Python server: signed requests,
// signed scans, the authenticated live socket, the exit QR and the gate. Run: npm run test:integration
import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, before, test } from 'node:test';

import { Api, ApiError, type Trip } from '../src/lib/api';

const ROOT = join(import.meta.dirname, '..', '..');
const PYTHON = process.env.TRUSTLANE_PYTHON ?? join(ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const PORT = 8013;
const api = new Api(`http://127.0.0.1:${PORT}`);
let server: ChildProcess;

before(async () => {
  assert.ok(existsSync(PYTHON), `Python venv not found at ${PYTHON} (set TRUSTLANE_PYTHON)`);
  server = spawn(PYTHON, ['-m', 'uvicorn', 'server.main:create_app', '--factory', '--app-dir', ROOT, '--host', '127.0.0.1',
    '--port', String(PORT)], { cwd: ROOT, env: { ...process.env, TRUSTLANE_DATA_DIR: mkdtempSync(join(tmpdir(), 'tl-')),
    TRUSTLANE_PAYMENTS: 'sandbox', TRUSTLANE_STAFF_PIN: '1', TRUSTLANE_PUSH: '0' }, stdio: 'ignore' });
  for (let i = 0; i < 60; i++) {
    try { await api.config(); return; } catch { await new Promise((r) => setTimeout(r, 250)); }
  }
  throw new Error('server did not start');
});

after(() => { server?.kill(); });

test('full trip with the app client: signed calls, live socket, QR exit, gate, receipt', async () => {
  const catalog = await api.catalog();
  const milk = catalog.find((p) => p.sku === 'milk')!;
  assert.ok(milk.image?.endsWith('milk.svg'));

  const started = await api.start(null);
  assert.ok(started.shopper?.shopper_secret, 'new shopper gets credentials');
  let trip: Trip = { sid: started.session_id, key: started.key, seq: 0, aisle: '3' };

  await api.trip(trip, '/location', { aisle: '3' });
  const messages: string[] = [];
  const ws = new WebSocket(api.wsUrl(trip));
  ws.onmessage = (e) => messages.push(JSON.parse(String(e.data)).kind);
  await new Promise<void>((resolve, reject) => { ws.onopen = () => resolve(); ws.onerror = () => reject(new Error('socket refused')); });

  trip = { ...trip, seq: 1 };
  const scanned = await api.sendScan(trip, api.signScan(trip, milk.barcode, null));
  assert.equal(scanned.status, 200);
  assert.ok('line' in scanned.data && scanned.data.cart.total === 6800);

  const { data: exit } = await api.trip<{ token: string; qr_svg: string }>(trip, '/checkout', {});
  assert.ok(exit.qr_svg.startsWith('<svg') && exit.token.includes('.'));
  assert.equal((await api.tap('G1', exit.token)).mode, 'online');

  const { data: cart } = await api.trip<{ state: string; receipt: { total: number } }>(trip, '');
  assert.equal(cart.state, 'exited');
  assert.equal(cart.receipt.total, 6800);
  await new Promise((r) => setTimeout(r, 300));
  assert.ok(messages.includes('cart') && messages.includes('receipt'), `socket got ${messages.join(',')}`);
  ws.close();

  // The same shopper credentials keep the same identity on the next trip.
  const again = await api.start(started.shopper!);
  assert.equal(again.shopper, undefined);
});

test('requests signed with the wrong key are refused', async () => {
  const a = await api.start(null);
  const b = await api.start(null);
  await assert.rejects(api.trip({ sid: a.session_id, key: b.key, seq: 0, aisle: null }, ''),
    (e: unknown) => e instanceof ApiError && e.status === 401);
});
