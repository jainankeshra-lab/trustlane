import { useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { SvgUri } from 'react-native-svg';

import type { Cart, CartLine } from '@/lib/api';
import { useTrip } from '@/lib/trip';
import { colors, rupees } from '@/theme';
import { Button, Card, Chip, Muted } from './ui';

const STATUS: Record<CartLine['status'], string> = {
  verified: 'Verified ✓', scanned: 'Scanned', weighing: 'Weighing…', flagged: 'Check', removed: 'Removed', refunded: 'Refunded',
};

export function CartList({ lines, canChange = false }: { lines: CartLine[]; canChange?: boolean }) {
  const t = useTrip();
  if (!lines.length) return <Muted small>{t.tr('Nothing scanned yet.')}</Muted>;
  return (
    <View>
      {lines.map((ln) => {
        const live = !['removed', 'refunded'].includes(ln.status);
        const price = ln.status === 'weighing' ? '–' : rupees(ln.price);
        return (
          <View key={ln.line_id} style={styles.line}
            accessible={!canChange || !live}
            accessibilityLabel={`${ln.name}, ${price}, ${t.tr(STATUS[ln.status])}`}>
            <View style={{ flex: 1 }}>
              <Text style={styles.name}>{ln.name}</Text>
              {!!ln.note && <Muted small>{ln.note}</Muted>}
            </View>
            <View style={{ alignItems: 'flex-end', gap: 4 }}>
              <Text style={styles.price}>{price}</Text>
              <Chip label={ln.return_ts ? t.tr('Put it on the shelf') : t.tr(STATUS[ln.status])}
                kind={ln.status === 'weighing' || ln.return_ts ? 'info' : ln.status} />
              {canChange && live && !ln.return_ts && (
                <Button small kind="ghost" label={t.tr('Put back')} onPress={() => t.putBack(ln.line_id)}
                  accessibilityLabel={`${t.tr('Put back')}: ${ln.name}`} />
              )}
            </View>
          </View>
        );
      })}
    </View>
  );
}

/** Offers, coupon and loyalty points, plus the GST included in the total. */
export function Savings({ cart }: { cart: Cart }) {
  const t = useTrip();
  const [code, setCode] = useState(cart.coupon?.code ?? '');
  const pts = cart.points;
  return (
    <Card>
      {(cart.discounts ?? []).map((d) => (
        <View key={d.id} style={styles.spread}>
          <Text style={[styles.saving, { flex: 1 }]}>{d.label}</Text>
          <Text style={styles.saving}>-{rupees(d.amount)}</Text>
        </View>
      ))}
      {!!cart.items && <Muted small>{t.tr('Includes GST {amount}', { amount: rupees(cart.gst?.tax ?? 0) })}</Muted>}
      {!!cart.offers?.length && <Muted small>{t.tr('Offers today')}: {cart.offers.join(' · ')}</Muted>}
      <View style={styles.row}>
        <TextInput style={[styles.input, { flex: 1 }]} value={code} onChangeText={setCode} autoCapitalize="characters"
          autoCorrect={false} placeholder={t.tr('Coupon code')} accessibilityLabel={t.tr('Coupon code')} />
        <Button small label={cart.coupon?.applied ? t.tr('Applied ✓') : t.tr('Apply')} onPress={() => t.applyCoupon(code.trim())} />
      </View>
      {cart.coupon && !cart.coupon.applied && !!cart.coupon.reason && <Muted small>{cart.coupon.code}: {cart.coupon.reason}</Muted>}
      {pts && pts.balance >= pts.min_redeem && (
        <View style={styles.row}>
          <Text style={{ flex: 1, color: colors.ink }}>{pts.redeemed
            ? `−${rupees(pts.redeemed * pts.point_value_paise)}`
            : t.tr('You have {points} points', { points: pts.balance })}</Text>
          <Button small label={pts.redeemed ? t.tr("Don't use") : t.tr('Use points')}
            onPress={() => t.redeemPoints(pts.redeemed ? 0 : pts.balance)} />
        </View>
      )}
    </Card>
  );
}

export function NudgeList() {
  const t = useTrip();
  return (
    <View style={{ gap: 10 }}>
      {t.nudges.map((n) => (
        <View key={n.key} style={styles.nudge} accessibilityRole="alert">
          <Text style={{ color: colors.ink }}><Text style={{ fontWeight: '700' }}>{t.tr('Oops!')} </Text>{n.text}</Text>
          <View style={styles.row}>
            {!!n.sku && <Button small kind="primary" label={t.tr('Scan it now')} onPress={() => t.scanSku(n.sku!)} />}
            {!!n.pick_id && <Button small label={t.tr("I didn't take it")} onPress={() => t.notMe(n.pick_id!, n.key)} />}
            <Button small kind="ghost" label={t.tr('Dismiss')} onPress={() => t.dismissNudge(n.key)} />
          </View>
        </View>
      ))}
    </View>
  );
}

/** Product picker: the demo stand-in for the camera, and a fallback when a barcode won't scan. */
export function ProductGrid() {
  const t = useTrip();
  return (
    <View style={styles.grid}>
      {t.catalog.map((p) => (
        <Pressable key={p.sku} style={styles.product} onPress={() => t.scan(p.barcode)} accessibilityRole="button"
          accessibilityLabel={`${t.tr('Scan it now')}: ${p.name}, ${rupees(p.price)}`}>
          {p.image ? <SvgUri uri={t.server + p.image} width={40} height={40} accessibilityElementsHidden importantForAccessibility="no" /> : null}
          <Text style={styles.name} numberOfLines={2}>{p.name}</Text>
          <Muted small>{rupees(p.price)}{p.sold_by === 'weight' ? '/kg' : ''} · {t.tr('Aisle {aisle}', { aisle: p.aisle })}{p.age_restricted ? ' · ID' : ''}</Muted>
        </Pressable>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  line: { flexDirection: 'row', gap: 10, paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: colors.line },
  name: { color: colors.ink, fontWeight: '600' },
  price: { color: colors.ink },
  saving: { color: colors.green, fontWeight: '600' },
  spread: { flexDirection: 'row', justifyContent: 'space-between', gap: 10 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
  input: { borderWidth: 1, borderColor: colors.line, borderRadius: 10, padding: 12, backgroundColor: colors.card, color: colors.ink, fontSize: 16, minHeight: 48 },
  nudge: { backgroundColor: colors.amberSoft, borderColor: colors.amber, borderWidth: 1, borderRadius: 14, padding: 12, gap: 10 },
  grid: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  product: { width: '48%', minHeight: 48, backgroundColor: colors.card, borderColor: colors.line, borderWidth: 1, borderRadius: 12, padding: 10, gap: 4 },
});
