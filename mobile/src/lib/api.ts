// HTTP client for the TrustLane server. Every trip request is signed with the trip key
// (X-Trip-Auth); scans carry their own signature. Mirrors web/phone.html.
import { scanSignature, tripAuth } from './crypto';

export type Product = {
  sku: string; barcode: string; name: string; price: number; aisle: string; image: string | null;
  sold_by: 'unit' | 'weight'; age_restricted: boolean; monitored: boolean;
};
export type CartLine = {
  line_id: string; sku: string; name: string; price: number; note: string; grams: number | null;
  status: 'verified' | 'scanned' | 'weighing' | 'flagged' | 'removed' | 'refunded';
  return_ts?: number;
};
export type Discount = { id: string; label: string; amount: number };
export type Gst = { rates: { rate: number; taxable: number; tax: number; cgst: number; sgst: number }[];
  taxable: number; tax: number; outside_gst: number };
export type Receipt = {
  payment_id: string | null; provider: string; method: string; mode: string; vpa?: string | null;
  total: number; items: CartLine[]; refunds?: { amount: number }[];
  subtotal?: number; discounts?: Discount[]; gst?: Gst;
  invoice?: { number: string; url: string; total: number };
  points?: { earned: number; redeemed: number; balance: number };
};
export type Cart = {
  session_id: string; state: string; aisle: string | null; lines: CartLine[]; total: number; items: number;
  needs_id: boolean; receipt: Receipt | null;
  subtotal?: number; discounts?: Discount[]; gst?: Gst; offers?: string[];
  coupon?: { code: string; label: string; amount: number; applied: boolean; reason: string | null } | null;
  points?: { balance: number; redeemed: number; will_earn: number; point_value_paise: number; min_redeem: number };
};
export type Account = {
  shopper_id: string; phone: string | null; email: string | null; name: string | null; points: number;
  receipts: { number: string; kind: string; session_id: string; ts: number; total: number }[];
  email_receipts: boolean;
};
export type Trip = { sid: string; key: string; seq: number; aisle: string | null };
export type Shopper = { shopper_id: string; shopper_secret: string };
export type ServerConfig = { payments: { provider: string; flow: string; mode: string; key_id?: string }; match_window_s: number; location_fresh_s: number };
export type PendingScan = { seq: number; barcode: string; ts: number; sig: string; package_label: string | null; package_confidence: number | null };

export type PackageCheck = { label: string; confidence: number };
export type PackageModelInfo = {
  available: boolean; names: string[]; input: { name: string; size: number }; sha256: string; url: string;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export class NetworkError extends Error {}

export class Api {
  constructor(public base: string) {
    this.base = base.replace(/\/+$/, '');
  }

  get wsBase() {
    return this.base.replace(/^http/, 'ws');
  }

  async request<T>(path: string, init: { method?: string; body?: unknown; headers?: Record<string, string> } = {}): Promise<{ status: number; data: T }> {
    let res: Response;
    try {
      res = await fetch(this.base + path, {
        method: init.method ?? (init.body === undefined ? 'GET' : 'POST'),
        headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
        body: init.body === undefined ? undefined : JSON.stringify(init.body),
      });
    } catch (e) {
      throw new NetworkError(e instanceof Error ? e.message : String(e));
    }
    const text = await res.text();
    let data: any = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!res.ok) throw new ApiError(res.status, (data && data.detail) || res.statusText || `HTTP ${res.status}`);
    return { status: res.status, data: data as T };
  }

  trip<T>(trip: Trip, sub: string, body?: unknown, method?: string) {
    const path = `/api/sessions/${trip.sid}${sub}`;
    const m = method ?? (body === undefined ? 'GET' : 'POST');
    return this.request<T>(path, { method: m, body, headers: { 'X-Trip-Auth': tripAuth(trip.key, m, path, Date.now()) } });
  }

  config() { return this.request<ServerConfig>('/api/config').then((r) => r.data); }
  catalog() { return this.request<Product[]>('/api/catalog').then((r) => r.data); }

  start(shopper: Shopper | null) {
    return this.request<{ session_id: string; key: string; shopper?: Shopper }>('/api/sessions', {
      body: { consent: true, ...(shopper ?? {}) },
    }).then((r) => r.data);
  }

  /** UPI Reserve Pay: block funds once; later exits are debited with no tap. */
  linkMandate(shopper: Shopper, upiId: string | null) {
    return this.request<{ subscription_id: string; status: string; approve_url: string | null }>('/api/shoppers/mandate', {
      body: { ...shopper, upi_id: upiId },
    }).then((r) => r.data);
  }

  mandateStatus(shopper: Shopper) {
    return this.request<{ status: string; subscription_id?: string; expires?: number }>('/api/shoppers/mandate/status', {
      body: shopper,
    }).then((r) => r.data);
  }

  signScan(trip: Trip, barcode: string, pkg: PackageCheck | null): PendingScan {
    const ts = Date.now();
    return {
      seq: trip.seq, barcode, ts, sig: scanSignature(trip.key, trip.sid, trip.seq, barcode, ts),
      package_label: pkg?.label ?? null, package_confidence: pkg?.confidence ?? null,
    };
  }

  /** The store's package-check model (ONNX), for running on the phone. */
  packageModel() { return this.request<PackageModelInfo>('/api/vision/package-model').then((r) => r.data); }

  sendScan(trip: Trip, scan: PendingScan, offline = false) {
    return this.request<{ line: CartLine; cart: Cart } | { buffered: true }>(`/api/sessions/${trip.sid}/scans`, {
      body: { ...scan, offline },
    });
  }

  wsUrl(trip: Trip) {
    const channel = `phone:${trip.sid}`;
    const auth = tripAuth(trip.key, 'WS', `/ws/${channel}`, Date.now());
    return `${this.wsBase}/ws/${encodeURIComponent(channel)}?auth=${encodeURIComponent(auth)}`;
  }

  tap(gate: string, token: string) {
    return this.request<{ mode: string }>(`/api/gate/${gate}/tap`, { body: { token } }).then((r) => r.data);
  }

  /** Exit by touching the gate's NFC tag: the tag's signed read says which gate, the token which trip. */
  nfcTap(token: string, read: { picc_data: string; cmac: string }) {
    return this.request<{ mode: string; gate: string }>('/api/gate/nfc-tap', { body: { token, ...read } }).then((r) => r.data);
  }

  // ---- shopper account (phone sign-in), DPDP access and erasure
  otpStart(phone: string) {
    return this.request<{ sent_to: string; expires_in: number }>('/api/shoppers/otp/start', { body: { phone } }).then((r) => r.data);
  }

  otpVerify(phone: string, code: string, current: Shopper | null) {
    return this.request<Shopper & { new: boolean }>('/api/shoppers/otp/verify', {
      body: { phone, code, ...(current ?? {}) },
    }).then((r) => r.data);
  }

  me(shopper: Shopper) { return this.request<Account>('/api/shoppers/me', { body: shopper }).then((r) => r.data); }

  saveProfile(shopper: Shopper, fields: { email?: string; name?: string }) {
    return this.request<Account>('/api/shoppers/me/profile', { body: { ...shopper, ...fields } }).then((r) => r.data);
  }

  exportData(shopper: Shopper) { return this.request<unknown>('/api/shoppers/me/export', { body: shopper }).then((r) => r.data); }

  deleteAccount(shopper: Shopper) {
    return this.request<{ erased: boolean }>('/api/shoppers/me/delete', { body: { ...shopper, confirm: true } }).then((r) => r.data);
  }

  storeInfo() {
    return this.request<{ legal_name: string; grievance_officer: { name: string; email: string } }>('/api/store').then((r) => r.data);
  }

  gateStatus() {
    return this.request<{ cloud_online: boolean; gate_key_required: boolean; nfc: boolean }>('/api/gate/status').then((r) => r.data);
  }
}
