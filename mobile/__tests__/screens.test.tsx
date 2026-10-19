// Screen tests: render the real components with a fake trip state and check what the shopper sees
// and which actions their taps call. Also checks every translated string has a Hindi version.
import { fireEvent, render, screen } from '@testing-library/react-native';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join } from 'path';

import { CartList, NudgeList, Savings } from '@/components/shop';
import type { Cart } from '@/lib/api';
import { missing, translate, type Lang } from '@/lib/i18n';
import { Ctx } from '@/lib/trip';

function fakeTrip(lang: Lang = 'en', extra: Record<string, unknown> = {}) {
  return {
    server: 'http://store', catalog: [], nudges: [], lang,
    tr: (text: string, vars?: Record<string, string | number>) => translate(lang, text, vars),
    putBack: jest.fn(), notMe: jest.fn(), scanSku: jest.fn(), dismissNudge: jest.fn(),
    applyCoupon: jest.fn(), redeemPoints: jest.fn(), scan: jest.fn(),
    ...extra,
  } as any;
}

async function withTrip(ui: React.ReactElement, trip = fakeTrip()) {
  await render(<Ctx.Provider value={trip}>{ui}</Ctx.Provider>);
  return trip;
}

const LINES: Cart['lines'] = [
  { line_id: 'L1', sku: 'razor', name: '3-blade razor', price: 29900, note: '', grams: null, status: 'verified' },
  { line_id: 'L2', sku: 'gum', name: 'Spearmint gum', price: 3000, note: '', grams: null, status: 'scanned', return_ts: 5 },
  { line_id: 'L3', sku: 'milk', name: 'Toned milk 1 L', price: 6800, note: 'Put back', grams: null, status: 'removed' },
];

test('cart lines can be put back while shopping, with a clear label per item', async () => {
  const trip = await withTrip(<CartList lines={LINES} canChange />);
  const buttons = screen.getAllByRole('button', { name: /Put back/ });
  expect(buttons).toHaveLength(1);  // gum is already on its way back; milk is removed
  await fireEvent.press(screen.getByLabelText('Put back: 3-blade razor'));
  expect(trip.putBack).toHaveBeenCalledWith('L1');
  expect(screen.getByText('Put it on the shelf')).toBeTruthy();
});

test('no put-back buttons once the trip is at the exit', async () => {
  await withTrip(<CartList lines={LINES} />);
  expect(screen.queryAllByRole('button', { name: /Put back/ })).toHaveLength(0);
});

test('a nudge offers scan, "I didn\'t take it" and dismiss', async () => {
  const trip = await withTrip(<NudgeList />, fakeTrip('en', {
    nudges: [{ key: 'P1', text: 'Did you pick up the razor?', sku: 'razor', pick_id: 'P1' }],
  }));
  await fireEvent.press(screen.getByRole('button', { name: "I didn't take it" }));
  expect(trip.notMe).toHaveBeenCalledWith('P1', 'P1');
  await fireEvent.press(screen.getByRole('button', { name: 'Scan it now' }));
  expect(trip.scanSku).toHaveBeenCalledWith('razor');
});

test('offers, coupon and points: what the shopper sees and what taps do', async () => {
  const cart = {
    session_id: 'T1', state: 'shopping', aisle: '7', lines: LINES, total: 30000, items: 2, needs_id: false, receipt: null,
    discounts: [{ id: 'choc-10', label: '10% off chocolate', amount: 1900 }],
    gst: { rates: [], taxable: 0, tax: 4600, outside_gst: 0 }, offers: ['Gum: buy 2, get 1 free'],
    coupon: { code: 'BIG', label: 'BIG', amount: 0, applied: false, reason: 'Spend at least Rs 1000' },
    points: { balance: 120, redeemed: 0, will_earn: 3, point_value_paise: 100, min_redeem: 50 },
  } as Cart;
  const trip = await withTrip(<Savings cart={cart} />);
  expect(screen.getByText('10% off chocolate')).toBeTruthy();
  expect(screen.getByText('Includes GST ₹46.00')).toBeTruthy();
  expect(screen.getByText('BIG: Spend at least Rs 1000')).toBeTruthy();
  await fireEvent.changeText(screen.getByLabelText('Coupon code'), 'welcome50 ');
  await fireEvent.press(screen.getByRole('button', { name: 'Apply' }));
  expect(trip.applyCoupon).toHaveBeenCalledWith('welcome50');
  await fireEvent.press(screen.getByRole('button', { name: 'Use points' }));
  expect(trip.redeemPoints).toHaveBeenCalledWith(120);
});

test('Hindi: the same screen in Hindi', async () => {
  await withTrip(<CartList lines={LINES} canChange />, fakeTrip('hi'));
  expect(screen.getByText('सत्यापित ✓')).toBeTruthy();
  expect(screen.getByLabelText('वापस रखें: 3-blade razor')).toBeTruthy();
});

test('every string the app translates has a Hindi version', async () => {
  const files: string[] = [];
  const walk = (dir: string) => readdirSync(dir).forEach((f) => {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) walk(p); else if (p.endsWith('.tsx')) files.push(p);
  });
  walk(join(__dirname, '..', 'src'));
  const keys = new Set<string>();
  const re = /\btr\(\s*(['"])((?:\\.|(?!\1).)*)\1/g;
  for (const f of files) {
    for (const m of readFileSync(f, 'utf8').matchAll(re)) keys.add(m[2].replace(/\\'/g, "'").replace(/\\"/g, '"'));
  }
  expect(keys.size).toBeGreaterThan(40);
  expect(missing('hi', [...keys])).toEqual([]);
});
