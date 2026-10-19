import { router } from 'expo-router';
import { useCallback, useEffect, useState } from 'react';
import { ActivityIndicator, KeyboardAvoidingView, Linking, Platform, Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { SvgXml } from 'react-native-svg';

import { CartList, NudgeList, ProductGrid, Savings } from '@/components/shop';
import { Banner, Button, Card, Chip, Eyebrow, Muted, Title } from '@/components/ui';
import { Api } from '@/lib/api';
import { nfcSupported } from '@/lib/nfc';
import { storage } from '@/lib/storage';
import { useTrip } from '@/lib/trip';
import { colors, rupees } from '@/theme';

export default function Home() {
  const t = useTrip();
  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
        <ScrollView contentContainerStyle={styles.page} keyboardShouldPersistTaps="handled">
          {t.view === 'loading' && <ActivityIndicator style={{ marginTop: 80 }} color={colors.green} accessibilityLabel="Loading" />}
          {t.view === 'setup' && <Setup />}
          {t.view === 'start' && <Start />}
          {t.view === 'shop' && <Shop />}
          {t.view === 'exit' && <Exit />}
          {t.view === 'confirm' && <Confirm />}
          {t.view === 'staff' && <Staff />}
          {t.view === 'pay' && <PayPrompt />}
          {t.view === 'payfail' && <PayFailed />}
          {t.view === 'receipt' && <ReceiptView />}
        </ScrollView>
      </KeyboardAvoidingView>
      {t.toast && <View style={styles.toast} accessibilityLiveRegion="polite" accessibilityRole="alert"><Text style={styles.toastText}>{t.toast}</Text></View>}
    </SafeAreaView>
  );
}

function TopLinks() {
  const t = useTrip();
  return (
    <View style={styles.row}>
      <Pressable onPress={() => router.push('/account')} accessibilityRole="link" hitSlop={10} style={styles.linkTap}>
        <Text style={styles.link}>{t.tr('Account')}</Text>
      </Pressable>
      <Pressable onPress={() => router.push('/settings')} accessibilityRole="link" hitSlop={10} style={styles.linkTap}>
        <Text style={styles.link}>{t.tr('Settings')}</Text>
      </Pressable>
    </View>
  );
}

function Setup() {
  const t = useTrip();
  const [url, setUrl] = useState(t.server);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <View style={styles.stack}>
      <Eyebrow>TrustLane · Store connection</Eyebrow>
      <Title>Connect to the store</Title>
      <Muted>Enter the address shown when the store server starts (run it with -Lan on the same Wi-Fi).</Muted>
      <TextInput style={styles.input} value={url} onChangeText={setUrl} autoCapitalize="none" autoCorrect={false}
        keyboardType="url" placeholder="http://192.168.1.10:8000" accessibilityLabel="Store server address" />
      {problem && <Banner kind="warn">{problem}</Banner>}
      <Button label="Connect" kind="primary" busy={busy} onPress={async () => {
        setBusy(true);
        setProblem(await t.setServer(url));
        setBusy(false);
      }} />
    </View>
  );
}

function Start() {
  const t = useTrip();
  const [agreed, setAgreed] = useState(false);
  const [busy, setBusy] = useState(false);
  const razorpay = t.config?.payments.provider === 'razorpay';
  return (
    <View style={styles.stack}>
      <View style={styles.spread}>
        <Eyebrow>TrustLane · Scan &amp; Go</Eyebrow>
        <TopLinks />
      </View>
      <Title>{t.tr('Scan as you shop. Walk straight out.')}</Title>
      <Muted>{razorpay ? t.tr('At the exit gate you approve one UPI payment.') : t.tr("You're charged automatically when you leave.")}</Muted>
      <Card>
        <Text style={styles.bold} accessibilityRole="header">{t.tr('Before you start: how this store checks baskets')}</Text>
        <Muted small>{t.tr('Shelf cameras look at the shelf front and hands only. They never record faces, and video stays on the shelf device.')}</Muted>
        <Muted small>{t.tr("Shelves record which product was taken. Your phone shares which aisle you're in (from aisle beacons) and broadcasts a rotating Bluetooth ID that only the store can link to this trip.")}</Muted>
        <Muted small>{t.tr('Checks use what happened on this trip only, never who you are or how you look. You can dispute any check from the app.')}</Muted>
        <Muted small>{t.tr("Trip records are kept for 90 days for audit, under India's DPDP Act 2023.")}</Muted>
        <View style={styles.row}>
          <Switch value={agreed} onValueChange={setAgreed} accessibilityLabel={t.tr('I understand and agree')} trackColor={{ true: colors.green }} />
          <Text style={{ flex: 1, color: colors.ink }} onPress={() => setAgreed(!agreed)}>{t.tr('I understand and agree')}</Text>
        </View>
      </Card>
      <Button label={t.tr('Start trip')} kind="primary" disabled={!agreed} busy={busy} onPress={async () => {
        setBusy(true);
        try { await t.startTrip(); } finally { setBusy(false); }
      }} />
      <Muted small>Prototype: {razorpay ? 'Razorpay test mode' : 'sandbox payments'}, no money moves.</Muted>
    </View>
  );
}

/** UPI Reserve Pay (Cashfree sandbox): link once, approve in the UPI app, then every exit is debited
 *  automatically. Shown only when the store's payment flow is "mandate". */
function ReservePay() {
  const t = useTrip();
  const [status, setStatus] = useState('NONE');
  const [upi, setUpi] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const shopper = await storage.getShopper();
    if (!shopper || !t.server) return;
    try { setStatus((await new Api(t.server).mandateStatus(shopper)).status); } catch { /* best effort */ }
  }, [t.server]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 10000);
    return () => clearInterval(id);
  }, [refresh]);

  const link = async () => {
    const shopper = await storage.getShopper();
    if (!shopper) return;
    setBusy(true);
    try {
      const r = await new Api(t.server).linkMandate(shopper, upi.trim() || null);
      setStatus(r.status);
      setNote(upi.trim() ? 'Approve the request in your UPI app.' : 'Approve the block on the page that just opened.');
      if (r.approve_url) Linking.openURL(r.approve_url).catch(() => {});
    } catch (e) {
      setNote(`Couldn't link Reserve Pay: ${e instanceof Error ? e.message : String(e)}`);
    }
    setBusy(false);
  };

  const active = status === 'ACTIVE';
  return (
    <Card>
      <View style={styles.spread}><Text style={styles.h2} accessibilityRole="header">UPI Reserve Pay</Text>
        <Chip label={active ? 'Active' : status === 'NONE' ? 'Not linked' : status.toLowerCase()} kind={active ? 'green' : 'warn'} /></View>
      <Muted small>{active ? "Linked. You'll be charged automatically as you walk out."
        : status === 'PENDING' ? 'Waiting for you to approve the block in your UPI app.'
          : 'Block funds once (up to the limit, for 90 days). Each trip is debited at the gate: no tap, no PIN. Unused funds are released.'}</Muted>
      {!active && status !== 'PENDING' && (
        <>
          <TextInput style={styles.input} value={upi} onChangeText={setUpi} autoCapitalize="none" autoCorrect={false}
            placeholder="Your UPI ID (optional), e.g. success@upi" accessibilityLabel="UPI ID" />
          <Button small label="Link Reserve Pay" busy={busy} onPress={link} />
        </>
      )}
      {note && <Muted small>{note}</Muted>}
    </Card>
  );
}

function Shop() {
  const t = useTrip();
  const s = t.status;
  const aisles = ['Entry', ...Array.from(new Set(t.catalog.map((p) => p.aisle))).sort((a, b) => Number(a) - Number(b))];
  const locAge = s.locationSentAt ? Math.round((Date.now() - s.locationSentAt) / 1000) : null;
  return (
    <View style={styles.stack}>
      <View style={styles.spread}>
        <View accessible accessibilityLabel={`${t.tr('Your cart')}: ${rupees(t.cart?.total ?? 0)}`}>
          <Muted small>{t.tr('Your cart')} · {t.trip?.sid}</Muted>
          <Text style={styles.total}>{rupees(t.cart?.total ?? 0)}</Text>
        </View>
        <View style={{ alignItems: 'flex-end', gap: 4 }}>
          <Chip label={s.live ? t.tr('Live') : t.tr('Reconnecting')} kind={s.live ? 'green' : 'warn'} />
          <TopLinks />
        </View>
      </View>
      {t.queueLength > 0 && <Banner kind="warn">{t.tr("{n} scan(s) saved on your phone while offline. They'll sync automatically.", { n: t.queueLength })}</Banner>}
      {t.cart?.needs_id && <Banner kind="warn">{t.tr('An item in your cart is age-restricted. Please show your ID to a team member before you leave.')}</Banner>}
      <NudgeList />
      {t.config?.payments.flow === 'mandate' && <ReservePay />}
      <Card>
        <View style={styles.spread}><Text style={styles.h2} accessibilityRole="header">{t.tr('Where are you?')}</Text>
          <Chip label={s.beacon ? t.tr('Beacon: aisle {aisle}', { aisle: s.beacon.aisle }) : t.tr('Choose your aisle')} kind={s.beacon ? 'green' : 'info'} /></View>
        <View style={styles.aisles}>
          {aisles.map((a) => {
            const label = a === 'Entry' ? t.tr('Entry') : t.tr('Aisle {aisle}', { aisle: a });
            return (
              <Pressable key={a} onPress={() => t.setAisle(a)} accessibilityRole="button" accessibilityLabel={label}
                accessibilityState={{ selected: t.trip?.aisle === a }} style={[styles.aisle, t.trip?.aisle === a && styles.aisleOn]}>
                <Text style={[styles.aisleText, t.trip?.aisle === a && { color: colors.white }]}>{label}</Text>
              </Pressable>
            );
          })}
        </View>
        <Muted small>{locAge === null ? 'Location not sent yet' : `Location sent ${locAge}s ago`}
          {s.beaconError ? ` · Beacons: ${s.beaconError}` : ''}</Muted>
        <Muted small>Bluetooth ID {s.eid ?? '…'} · {s.advertising ? 'broadcasting' : s.advertiseError ?? 'not broadcasting'}</Muted>
      </Card>
      <Button label={t.tr('Scan a barcode')} kind="primary" onPress={() => router.push('/scan')} />
      <ProductGrid />
      <Card>
        <Text style={styles.h2} accessibilityRole="header">{t.tr('Cart')}</Text>
        <CartList lines={t.cart?.lines ?? []} canChange={t.cart?.state === 'shopping'} />
      </Card>
      {t.cart && t.cart.items > 0 && <Savings cart={t.cart} />}
      <Button label={t.tr('Go to exit')} kind="primary" onPress={t.goToExit} />
    </View>
  );
}

function Exit() {
  const t = useTrip();
  const [nfc, setNfc] = useState(false);
  const [gateKey, setGateKey] = useState(false);
  useEffect(() => {
    nfcSupported().then(setNfc);
    if (t.server) new Api(t.server).gateStatus().then((g) => setGateKey(g.gate_key_required)).catch(() => {});
  }, [t.server]);
  return (
    <View style={styles.stack}>
      <Eyebrow>{t.tr('Exit')}</Eyebrow>
      <Title>{t.tr('Show this code at the gate scanner')}</Title>
      <View style={styles.qr} accessible accessibilityLabel="Exit QR code">
        {t.exit && <SvgXml xml={t.exit.qrSvg} width="100%" height={300} />}
      </View>
      <Card>
        <View style={styles.spread}><Muted>{t.tr('Total')}</Muted><Text style={styles.big}>{rupees(t.cart?.total ?? 0)}</Text></View>
        <Muted small>{t.tr('{n} item(s)', { n: t.cart?.items ?? 0 })}</Muted>
      </Card>
      {nfc && <Button label={t.tr('Tap your phone on the gate (NFC)')} kind="primary" onPress={t.tapNfc} />}
      {!gateKey && <Button label="Demo: tap at gate G1 without NFC" onPress={t.tapGate} />}
      <Button label={t.tr('Back to shopping')} onPress={t.resume} />
      {!!t.exitStatus && <Text style={styles.muted} accessibilityLiveRegion="polite">{t.exitStatus}</Text>}
    </View>
  );
}

function Confirm() {
  const t = useTrip();
  const [choices, setChoices] = useState<Record<string, 'take' | 'deny'>>({});
  const [busy, setBusy] = useState(false);
  return (
    <View style={styles.stack}>
      <Eyebrow>{t.tr('Quick check')}</Eyebrow>
      <Title>{t.tr('Did you take these?')}</Title>
      <Muted>{t.tr("The shelf recorded these items but we didn't see a scan. Add anything you have.")}</Muted>
      {t.confirmItems.map((it) => {
        const choice = choices[it.pick_id] ?? 'take';
        return (
          <Card key={it.pick_id}>
            <View style={styles.spread}><Text style={styles.bold}>{it.name}</Text><Text style={styles.ink}>{rupees(it.price)}</Text></View>
            <View style={styles.row} accessibilityRole="radiogroup">
              <Button small label={t.tr('I have it, add it')} kind={choice === 'take' ? 'primary' : 'plain'}
                accessibilityLabel={`${t.tr('I have it, add it')}: ${it.name}`}
                onPress={() => setChoices({ ...choices, [it.pick_id]: 'take' })} />
              <Button small label={t.tr("I didn't take it")} kind={choice === 'deny' ? 'primary' : 'plain'}
                accessibilityLabel={`${t.tr("I didn't take it")}: ${it.name}`}
                onPress={() => setChoices({ ...choices, [it.pick_id]: 'deny' })} />
            </View>
          </Card>
        );
      })}
      <Button label={t.tr('Confirm and pay')} kind="primary" busy={busy} onPress={async () => {
        setBusy(true);
        const take = t.confirmItems.filter((i) => (choices[i.pick_id] ?? 'take') === 'take').map((i) => i.pick_id);
        const deny = t.confirmItems.filter((i) => choices[i.pick_id] === 'deny').map((i) => i.pick_id);
        await t.confirm(take, deny);
        setBusy(false);
      }} />
    </View>
  );
}

function Staff() {
  const t = useTrip();
  return (
    <View style={styles.stack}>
      <Eyebrow>{t.tr('Almost done')}</Eyebrow>
      <Title>{t.staffText || t.tr('A team member will be with you in a moment.')}</Title>
      <Muted>They'll check a couple of things with you, then you're on your way.</Muted>
      <Dispute />
    </View>
  );
}

function PayPrompt() {
  const t = useTrip();
  return (
    <View style={styles.stack}>
      <Eyebrow>Gate check passed</Eyebrow>
      <Title>Approve your payment to open the gate</Title>
      <Card><View style={styles.spread}><Muted>{t.tr('Total')}</Muted><Text style={styles.big}>{rupees(t.payRequest?.amount ?? 0)}</Text></View></Card>
      <Button label="Pay with UPI" kind="primary" onPress={() => router.push('/pay')} />
      <Banner>Test mode: choose UPI and enter success@razorpay (or failure@razorpay). No money moves.</Banner>
    </View>
  );
}

function PayFailed() {
  const t = useTrip();
  return (
    <View style={styles.stack}>
      <Eyebrow>Payment</Eyebrow>
      <Title>{t.tr("Your payment didn't go through")}</Title>
      <Muted>{t.payFailText}</Muted>
      <Button label={t.tr('Try again')} kind="primary" onPress={t.payAgain} />
    </View>
  );
}

function ReceiptView() {
  const t = useTrip();
  const r = t.receipt;
  if (!r) return null;
  const refunded = (r.refunds ?? []).reduce((sum, x) => sum + x.amount, 0);
  const saved = (r.discounts ?? []).reduce((sum, d) => sum + d.amount, 0);
  return (
    <View style={styles.stack}>
      <Eyebrow>{t.tr('Paid · gate open')}</Eyebrow>
      <Title>{t.tr('Thanks for shopping!')}</Title>
      <Card>
        {r.items.map((ln) => (
          <View key={ln.line_id} style={styles.spread}><Text style={[styles.ink, { flex: 1 }]}>{ln.name}</Text><Text style={styles.ink}>{rupees(ln.price)}</Text></View>
        ))}
        {(r.discounts ?? []).map((d) => (
          <View key={d.id} style={styles.spread}><Text style={[styles.saving, { flex: 1 }]}>{d.label}</Text><Text style={styles.saving}>-{rupees(d.amount)}</Text></View>
        ))}
        <View style={styles.spread}><Muted>{t.tr('Total charged')}</Muted><Text style={styles.big}>{rupees(r.total)}</Text></View>
        {r.gst && <Muted small>{saved ? `${t.tr('You saved {amount}', { amount: rupees(saved) })} · ` : ''}{t.tr('Includes GST {amount}', { amount: rupees(r.gst.tax) })}</Muted>}
        <Muted small>{[r.payment_id, r.provider, r.method, r.vpa, `${r.mode} mode`].filter(Boolean).join(' · ')}
          {refunded ? ` · refunded ${rupees(refunded)}` : ''}</Muted>
        {r.points && <Muted small>{t.tr('+{earned} points · balance {balance}', { earned: r.points.earned, balance: r.points.balance })}</Muted>}
      </Card>
      {r.invoice && <Button label={t.tr('View GST invoice {number}', { number: r.invoice.number })}
        onPress={() => Linking.openURL(t.server + r.invoice!.url).catch(() => {})} />}
      <Dispute />
      <Button label={t.tr('Start a new trip')} onPress={t.newTrip} />
    </View>
  );
}

function Dispute() {
  const t = useTrip();
  const [text, setText] = useState('');
  const [sent, setSent] = useState(false);
  if (sent) return <Muted small>Sent. A team member will review it.</Muted>;
  return (
    <Card>
      <Text style={styles.bold} accessibilityRole="header">{t.tr('Something not right?')}</Text>
      <TextInput style={[styles.input, { minHeight: 64 }]} value={text} onChangeText={setText} multiline
        placeholder={t.tr('Tell us what happened')} accessibilityLabel={t.tr('Tell us what happened')} />
      <Button small label={t.tr('Send to the store team')} disabled={!text.trim()} onPress={async () => setSent(await t.dispute(text))} />
    </Card>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  page: { padding: 16, paddingBottom: 48 },
  stack: { gap: 14 },
  spread: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 10 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 10, flexWrap: 'wrap' },
  total: { fontSize: 24, fontWeight: '700', color: colors.ink },
  big: { fontSize: 20, fontWeight: '700', color: colors.ink },
  h2: { fontSize: 17, fontWeight: '700', color: colors.ink },
  bold: { fontWeight: '700', color: colors.ink },
  ink: { color: colors.ink },
  muted: { color: colors.muted, fontSize: 13 },
  saving: { color: colors.green, fontWeight: '600' },
  link: { color: colors.blue, fontWeight: '600' },
  linkTap: { minHeight: 44, justifyContent: 'center' },
  input: { borderWidth: 1, borderColor: colors.line, borderRadius: 10, padding: 12, backgroundColor: colors.card, color: colors.ink, fontSize: 16, minHeight: 48 },
  aisles: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  aisle: { minHeight: 44, justifyContent: 'center', paddingVertical: 10, paddingHorizontal: 12, borderRadius: 10, borderWidth: 1, borderColor: colors.line, backgroundColor: colors.card },
  aisleOn: { backgroundColor: colors.green, borderColor: colors.green },
  aisleText: { color: colors.ink, fontWeight: '600' },
  qr: { backgroundColor: colors.white, borderRadius: 14, padding: 12, alignItems: 'center' },
  toast: { position: 'absolute', left: 16, right: 16, bottom: 24, backgroundColor: colors.ink, borderRadius: 12, padding: 12 },
  toastText: { color: colors.white },
});
