// Trip state for the whole app: one shopping trip, its live connection, location reporting,
// Bluetooth, push, the offline scan queue, and the exit flow. Same protocol as web/phone.html.
import * as Haptics from 'expo-haptics';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { AppState } from 'react-native';

import { readGateTag } from './nfc';
import { Api, ApiError, NetworkError, type Cart, type PackageCheck, type PendingScan, type Product, type Receipt, type ServerConfig, type Trip } from './api';
import { broadcastTripId, requestBluetoothPermissions, scanShelves } from './bluetooth';
import { eidFor } from './crypto';
import { registerForPush } from './push';
import { deviceLang, translate, type Lang } from './i18n';
import { DEFAULT_PREFS, storage, type Prefs } from './storage';

export const GATE_ID = 'G1';
const LOCATION_EVERY_MS = 15_000;

export type View = 'loading' | 'setup' | 'start' | 'shop' | 'exit' | 'confirm' | 'staff' | 'pay' | 'payfail' | 'receipt';
export type Nudge = { key: string; text: string; sku?: string; pick_id?: string };
export type ConfirmItem = { pick_id: string; sku: string; name: string; price: number; shared: boolean };
export type PayRequest = { key: string; order_id: string; amount: number; currency: string; name: string; session_id: string };

type Status = {
  live: boolean;
  locationSentAt: number | null;
  beacon: { aisle: string; rssi: number } | null;
  beaconError: string | null;
  advertising: boolean;
  advertiseError: string | null;
  eid: string | null;
  push: string | null;
};

type TripContext = {
  view: View;
  server: string;
  config: ServerConfig | null;
  catalog: Product[];
  trip: Trip | null;
  cart: Cart | null;
  nudges: Nudge[];
  confirmItems: ConfirmItem[];
  staffText: string;
  payRequest: PayRequest | null;
  payFailText: string;
  receipt: Receipt | null;
  exit: { token: string; qrSvg: string } | null;
  exitStatus: string;
  queueLength: number;
  status: Status;
  prefs: Prefs;
  toast: string | null;
  setServer(url: string): Promise<string | null>;
  setPrefs(p: Prefs): void;
  startTrip(): Promise<void>;
  setAisle(aisle: string): void;
  scan(barcode: string, pkg?: PackageCheck | null): Promise<void>;
  scanSku(sku: string): Promise<void>;
  dismissNudge(key: string): void;
  goToExit(): Promise<void>;
  tapGate(): Promise<void>;
  tapNfc(): Promise<void>;
  resume(): Promise<void>;
  confirm(take: string[], deny: string[]): Promise<void>;
  confirmPayment(resp: { razorpay_order_id: string; razorpay_payment_id: string; razorpay_signature: string }): Promise<void>;
  payAgain(): Promise<void>;
  dispute(text: string): Promise<boolean>;
  newTrip(): Promise<void>;
  applyCoupon(code: string): Promise<void>;
  redeemPoints(points: number): Promise<void>;
  putBack(lineId: string): Promise<void>;
  notMe(pickId: string, nudgeKey: string): Promise<void>;
  pushTest(): Promise<string>;
  lang: Lang;
  tr(text: string, vars?: Record<string, string | number>): string;
};

export const Ctx = createContext<TripContext | null>(null);  // exported for screen tests

export function useTrip(): TripContext {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useTrip must be used inside <TripProvider>');
  return ctx;
}

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function TripProvider({ children }: { children: ReactNode }) {
  const [view, setView] = useState<View>('loading');
  const [server, setServerState] = useState('');
  const [config, setConfig] = useState<ServerConfig | null>(null);
  const [catalog, setCatalog] = useState<Product[]>([]);
  const [trip, setTrip] = useState<Trip | null>(null);
  const [cart, setCart] = useState<Cart | null>(null);
  const [nudges, setNudges] = useState<Nudge[]>([]);
  const [confirmItems, setConfirmItems] = useState<ConfirmItem[]>([]);
  const [staffText, setStaffText] = useState('');
  const [payRequest, setPayRequest] = useState<PayRequest | null>(null);
  const [payFailText, setPayFailText] = useState('');
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [exit, setExit] = useState<{ token: string; qrSvg: string } | null>(null);
  const [exitStatus, setExitStatus] = useState('');
  const [queue, setQueue] = useState<PendingScan[]>([]);
  const [prefs, setPrefsState] = useState<Prefs>(DEFAULT_PREFS);
  const [toast, setToast] = useState<string | null>(null);
  const [status, setStatus] = useState<Status>({
    live: false, locationSentAt: null, beacon: null, beaconError: null, advertising: false,
    advertiseError: null, eid: null, push: null,
  });

  const api = useMemo(() => (server ? new Api(server) : null), [server]);
  const tripRef = useRef<Trip | null>(null);
  const queueRef = useRef<PendingScan[]>([]);
  tripRef.current = trip;
  queueRef.current = queue;

  const say = useCallback((text: string) => {
    setToast(text);
    setTimeout(() => setToast((t) => (t === text ? null : t)), 3500);
  }, []);
  const patchStatus = useCallback((p: Partial<Status>) => setStatus((s) => ({ ...s, ...p })), []);

  const saveTrip = useCallback(async (t: Trip | null) => {
    setTrip(t);
    tripRef.current = t;
    await storage.setTrip(t);
  }, []);

  const saveQueue = useCallback(async (q: PendingScan[]) => {
    setQueue(q);
    queueRef.current = q;
    await storage.setQueue(q);
  }, []);

  // ---------------------------------------------------------------- loading and server setup
  const connectServer = useCallback(async (url: string): Promise<string | null> => {
    const candidate = new Api(url);
    try {
      const [cfg, products] = await Promise.all([candidate.config(), candidate.catalog()]);
      setConfig(cfg);
      setCatalog(products);
      setServerState(candidate.base);
      return null;
    } catch (e) {
      return e instanceof NetworkError ? `Can't reach ${url}. Is the server running with -Lan on the same Wi-Fi?` : errorText(e);
    }
  }, []);

  const applyCart = useCallback((c: Cart) => {
    setCart(c);
    if (c.state === 'exited' && c.receipt) {
      setReceipt(c.receipt);
      setView('receipt');
    }
  }, []);

  useEffect(() => {
    (async () => {
      const [url, savedTrip, savedQueue, savedPrefs] = await Promise.all([
        storage.getServer(), storage.getTrip(), storage.getQueue(), storage.getPrefs()]);
      setPrefsState(savedPrefs);
      setQueue(savedQueue);
      const problem = await connectServer(url);
      if (problem) {
        setServerState(url);
        setView('setup');
        return;
      }
      if (!savedTrip) {
        setView('start');
        return;
      }
      setTrip(savedTrip);
      try {
        const { data } = await new Api(url).trip<Cart>(savedTrip, '');
        applyCart(data);
        if (data.state === 'exited') return;
        if (data.state === 'staff_check') setView('staff');
        else if (data.state === 'paying') { setView('shop'); new Api(url).trip(savedTrip, '/pay', {}).catch(() => {}); }
        else if (data.state === 'at_exit') { await new Api(url).trip(savedTrip, '/resume', {}); setView('shop'); }
        else setView('shop');
      } catch (e) {
        if (e instanceof ApiError && (e.status === 404 || e.status === 401)) {
          await storage.setTrip(null);
          setTrip(null);
          setView('start');
        } else setView('shop');
      }
    })();
  }, [connectServer, applyCart]);

  const setServer = useCallback(async (url: string) => {
    const problem = await connectServer(url);
    if (!problem) {
      await storage.setServer(url);
      setView(tripRef.current ? 'shop' : 'start');
    }
    return problem;
  }, [connectServer]);

  const setPrefs = useCallback((p: Prefs) => {
    setPrefsState(p);
    storage.setPrefs(p);
  }, []);

  // ---------------------------------------------------------------- live updates
  const handleMessage = useCallback((kind: string, d: any) => {
    switch (kind) {
      case 'cart': applyCart(d); break;
      case 'nudge':
        setNudges((n) => [...n.filter((x) => x.key !== (d.pick_id || d.sku)), { key: d.pick_id || d.sku || String(Date.now()), ...d }]);
        Haptics.notificationAsync(Haptics.NotificationFeedbackType.Warning).catch(() => {});
        break;
      case 'resolved':
        setNudges((n) => n.filter((x) => x.pick_id !== d.pick_id && !(x.pick_id === undefined && x.sku === d.sku)));
        break;
      case 'info': say(d.text); break;
      case 'confirm': setConfirmItems(d.items); setView('confirm'); break;
      case 'staff': setStaffText(d.text); setView('staff'); break;
      case 'pay': setPayRequest(d); setView('pay'); break;
      case 'payment_failed': setPayFailText(d.text); setView('payfail'); break;
      case 'receipt': setReceipt(d); setView('receipt'); break;
    }
  }, [applyCart, say]);

  const flushQueue = useCallback(async (): Promise<boolean> => {
    const t = tripRef.current;
    if (!api || !t) return true;
    let q = [...queueRef.current];
    while (q.length) {
      try {
        await api.sendScan(t, q[0], true);
      } catch (e) {
        if (e instanceof NetworkError) {
          await saveQueue(q);
          return false;
        }
        say(`A saved scan was refused: ${errorText(e)}`);
      }
      q = q.slice(1);
      await saveQueue(q);
    }
    return true;
  }, [api, saveQueue, say]);

  useEffect(() => {
    if (!api || !trip) return;
    let socket: WebSocket | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const open = () => {
      socket = new WebSocket(api.wsUrl(trip));
      socket.onopen = () => { patchStatus({ live: true }); flushQueue(); };
      socket.onmessage = (e) => {
        try {
          const msg = JSON.parse(String(e.data));
          handleMessage(msg.kind, msg.data);
        } catch { /* ignore malformed frames */ }
      };
      socket.onclose = () => {
        patchStatus({ live: false });
        if (!closed) retry = setTimeout(open, 2000);
      };
    };
    open();
    const appState = AppState.addEventListener('change', (s) => {
      if (s === 'active' && socket?.readyState !== WebSocket.OPEN) { socket?.close(); }
    });
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      appState.remove();
      socket?.close();
    };
  }, [api, trip?.sid, handleMessage, flushQueue, patchStatus]); // eslint-disable-line react-hooks/exhaustive-deps

  // Location: re-report every 15 s, so the store never mistakes this phone for a silent one.
  const sendLocation = useCallback(async () => {
    const t = tripRef.current;
    if (!api || !t?.aisle) return;
    try {
      await api.trip(t, '/location', { aisle: t.aisle });
      patchStatus({ locationSentAt: Date.now() });
    } catch { /* retried on the next tick */ }
  }, [api, patchStatus]);

  useEffect(() => {
    if (!trip) return;
    const timers = [setInterval(sendLocation, LOCATION_EVERY_MS), setInterval(flushQueue, 5000),
      setInterval(() => patchStatus({ eid: eidFor(trip.key, Date.now()) }), 1000)];
    return () => timers.forEach(clearInterval);
  }, [trip?.sid, sendLocation, flushQueue, patchStatus]); // eslint-disable-line react-hooks/exhaustive-deps

  const setAisle = useCallback((aisle: string) => {
    const t = tripRef.current;
    if (!t || t.aisle === aisle) return;
    saveTrip({ ...t, aisle }).then(sendLocation);
  }, [saveTrip, sendLocation]);

  // Bluetooth: aisle beacons and the broadcast trip ID.
  useEffect(() => {
    if (!trip || (!prefs.autoAisle && !prefs.broadcastId)) return;
    let stops: (() => void)[] = [];
    let cancelled = false;
    requestBluetoothPermissions().then((granted) => {
      if (cancelled) return;
      if (!granted) {
        patchStatus({ beaconError: 'Bluetooth permission denied', advertiseError: 'Bluetooth permission denied' });
        return;
      }
      stops.push(scanShelves({
        onAisle: prefs.autoAisle ? (aisle, rssi) => {
          patchStatus({ beacon: { aisle, rssi }, beaconError: null });
          setAisle(aisle);
        } : undefined,
        // Signed shelf tokens prove where the phone is, even an iPhone in a pocket.
        onShelves: prefs.broadcastId ? (heard) => {
          const t = tripRef.current;
          if (api && t) api.trip(t, '/heard', { beacons: heard }).catch(() => {});
        } : undefined,
        onError: (msg) => patchStatus({ beaconError: msg }),
      }));
      if (prefs.broadcastId) {
        stops.push(broadcastTripId(trip.key, (s) => patchStatus({ advertising: s.advertising, eid: s.eid, advertiseError: s.error ?? null })));
      }
    });
    return () => {
      cancelled = true;
      stops.forEach((s) => s());
      stops = [];
    };
  }, [trip?.sid, api, prefs.autoAisle, prefs.broadcastId, setAisle, patchStatus]); // eslint-disable-line react-hooks/exhaustive-deps

  // Push: register once per trip.
  useEffect(() => {
    if (!api || !trip) return;
    registerForPush().then(async (r) => {
      if (r.token) {
        try {
          await api.trip(trip, '/push-token', { token: r.token });
          patchStatus({ push: 'on' });
        } catch (e) { patchStatus({ push: errorText(e) }); }
      } else patchStatus({ push: r.error ?? 'off' });
    });
  }, [api, trip?.sid, patchStatus]); // eslint-disable-line react-hooks/exhaustive-deps

  // Fallback refresh in case a live message was missed.
  useEffect(() => {
    if (!api || !trip || view === 'receipt') return;
    const timer = setInterval(() => {
      api.trip<Cart>(trip, '').then((r) => applyCart(r.data)).catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, [api, trip?.sid, view, applyCart]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------- actions
  const startTrip = useCallback(async () => {
    if (!api) return;
    const shopper = await storage.getShopper();
    const data = await api.start(shopper);
    if (data.shopper) await storage.setShopper(data.shopper);
    await saveQueue([]);
    await saveTrip({ sid: data.session_id, key: data.key, seq: 0, aisle: 'Entry' });
    setNudges([]);
    setReceipt(null);
    setExit(null);
    setView('shop');
    sendLocation();
  }, [api, saveQueue, saveTrip, sendLocation]);

  const scan = useCallback(async (barcode: string, pkg: PackageCheck | null = null) => {
    const t = tripRef.current;
    if (!api || !t) return;
    const next = { ...t, seq: t.seq + 1 };
    await saveTrip(next);
    const signed = api.signScan(next, barcode, pkg);
    if (queueRef.current.length) {  // keep scans in order behind the ones already queued
      await saveQueue([...queueRef.current, signed]);
      flushQueue();
      return;
    }
    try {
      const { status: code, data } = await api.sendScan(next, signed);
      if (code === 202) say('Saved. The store is offline, so it will sync shortly.');
      else if ('line' in data) {
        applyCart(data.cart);
        const ln = data.line;
        say(ln.status === 'flagged' ? `${ln.name} scanned, please check the message above`
          : ln.status === 'weighing' ? `${ln.name}: the produce shelf weighs it` : `${ln.name} added`);
      }
    } catch (e) {
      if (e instanceof NetworkError) {
        await saveQueue([...queueRef.current, signed]);
        say('No connection. Scan saved on your phone.');
      } else say(errorText(e));
    }
  }, [api, saveTrip, saveQueue, flushQueue, applyCart, say]);

  const scanSku = useCallback(async (sku: string) => {
    const p = catalog.find((x) => x.sku === sku);
    if (p) await scan(p.barcode);
  }, [catalog, scan]);

  const goToExit = useCallback(async () => {
    const t = tripRef.current;
    if (!api || !t) return;
    if (queueRef.current.length && !(await flushQueue())) {
      say('Some scans are still saved offline. Move to where there is signal and try again.');
      return;
    }
    try {
      const { data } = await api.trip<{ token: string; qr_svg: string }>(t, '/checkout', {});
      setExit({ token: data.token, qrSvg: data.qr_svg });
      setExitStatus('');
      setView('exit');
    } catch (e) { say(errorText(e)); }
  }, [api, flushQueue, say]);

  const tapGate = useCallback(async () => {
    if (!api || !exit) return;
    setExitStatus('Checking…');
    try {
      const r = await api.tap(GATE_ID, exit.token);
      if (r.mode === 'offline') setExitStatus('Store is offline: the gate checked your signed code locally.');
    } catch (e) { setExitStatus(errorText(e)); }
  }, [api, exit]);

  const tapNfc = useCallback(async () => {
    const t = tripRef.current;
    if (!api || !t) return;
    try {
      setExitStatus("Hold the top of your phone to the gate's NFC tag…");
      const read = await readGateTag('Hold your phone to the gate');
      // A fresh exit token for this tap, so it can't have expired while the shopper walked over.
      const { data } = await api.trip<{ token: string; qr_svg: string }>(t, '/checkout', {});
      setExit({ token: data.token, qrSvg: data.qr_svg });
      setExitStatus('Checking at the gate…');
      const r = await api.nfcTap(data.token, read);
      setExitStatus(r.mode === 'offline' ? `Gate ${r.gate}: store is offline, the gate checked your signed code.` : `Tapped gate ${r.gate}.`);
    } catch (e) { setExitStatus(errorText(e)); }
  }, [api]);

  const resume = useCallback(async () => {
    const t = tripRef.current;
    if (!api || !t) return;
    await api.trip(t, '/resume', {}).catch(() => {});
    setView('shop');
  }, [api]);

  const confirm = useCallback(async (take: string[], deny: string[]) => {
    const t = tripRef.current;
    if (!api || !t) return;
    try { await api.trip(t, '/confirm', { take, deny }); } catch (e) { say(errorText(e)); }
  }, [api, say]);

  const confirmPayment = useCallback(async (resp: { razorpay_order_id: string; razorpay_payment_id: string; razorpay_signature: string }) => {
    const t = tripRef.current;
    if (!api || !t) return;
    try { await api.trip(t, '/payment/razorpay', resp); } catch (e) { setPayFailText(errorText(e)); setView('payfail'); }
  }, [api]);

  const payAgain = useCallback(async () => {
    const t = tripRef.current;
    if (!api || !t) return;
    try { await api.trip(t, '/pay', {}); } catch (e) { say(errorText(e)); }
  }, [api, say]);

  const dispute = useCallback(async (text: string) => {
    const t = tripRef.current;
    if (!api || !t) return false;
    try {
      await api.trip(t, '/disputes', { text });
      return true;
    } catch (e) {
      say(errorText(e));
      return false;
    }
  }, [api, say]);

  const newTrip = useCallback(async () => {
    await saveTrip(null);
    await saveQueue([]);
    setCart(null);
    setNudges([]);
    setReceipt(null);
    setExit(null);
    setView('start');
  }, [saveTrip, saveQueue]);

  const dismissNudge = useCallback((key: string) => setNudges((n) => n.filter((x) => x.key !== key)), []);

  const cartCall = useCallback(async (sub: string, body: unknown) => {
    const t = tripRef.current;
    if (!api || !t) return;
    try { applyCart((await api.trip<Cart>(t, sub, body)).data); } catch (e) { say(errorText(e)); }
  }, [api, applyCart, say]);

  const applyCoupon = useCallback((code: string) => cartCall('/coupon', { code }), [cartCall]);
  const redeemPoints = useCallback((points: number) => cartCall('/points', { points }), [cartCall]);
  const putBack = useCallback((lineId: string) => cartCall(`/lines/${encodeURIComponent(lineId)}/put-back`, {}), [cartCall]);

  const notMe = useCallback(async (pickId: string, nudgeKey: string) => {
    const t = tripRef.current;
    if (!api || !t) return;
    setNudges((n) => n.filter((x) => x.key !== nudgeKey));
    try { await api.trip(t, `/picks/${encodeURIComponent(pickId)}/not-me`, {}); } catch (e) { say(errorText(e)); }
  }, [api, say]);

  const pushTest = useCallback(async () => {
    const t = tripRef.current;
    if (!api || !t) return 'Start a trip first';
    try {
      const { data } = await api.trip<{ tickets: { status: string; message?: string }[] }>(t, '/push-test', {});
      const ticket = data.tickets[0];
      return ticket?.status === 'ok' ? 'Sent: it should arrive in a few seconds.' : `Expo refused it: ${ticket?.message ?? 'unknown error'}`;
    } catch (e) { return errorText(e); }
  }, [api]);

  const lang: Lang = prefs.lang === 'auto' ? deviceLang() : prefs.lang;
  const tr = useCallback((text: string, vars?: Record<string, string | number>) => translate(lang, text, vars), [lang]);

  const value: TripContext = {
    view, server, config, catalog, trip, cart, nudges, confirmItems, staffText, payRequest, payFailText, receipt,
    exit, exitStatus, queueLength: queue.length, status, prefs, toast,
    setServer, setPrefs, startTrip, setAisle, scan, scanSku, dismissNudge, goToExit, tapGate, tapNfc, resume, confirm,
    confirmPayment, payAgain, dispute, newTrip, applyCoupon, redeemPoints, putBack, notMe, pushTest, lang, tr,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
