import { router } from 'expo-router';
import { useMemo } from 'react';
import { StyleSheet, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { WebView, type WebViewMessageEvent } from 'react-native-webview';

import { Button, Muted, Title } from '@/components/ui';
import { useTrip } from '@/lib/trip';
import { colors } from '@/theme';

// Razorpay Checkout runs in the server's /checkout page inside a WebView. The page reports the
// result back; the app then confirms it with the server using its own trip signature.
export default function Pay() {
  const t = useTrip();
  const req = t.payRequest;
  const uri = useMemo(() => {
    if (!req) return null;
    const q = new URLSearchParams({ key: req.key, order_id: req.order_id, amount: String(req.amount),
      name: req.name, sid: req.session_id });
    return `${t.server}/checkout?${q.toString()}`;
  }, [req, t.server]);

  if (!uri) {
    return (
      <SafeAreaView style={styles.page}>
        <Title>No payment is waiting</Title>
        <Button label="Close" onPress={() => router.back()} />
      </SafeAreaView>
    );
  }

  const onMessage = async (e: WebViewMessageEvent) => {
    let msg: { type: string; reason?: string; razorpay_order_id?: string; razorpay_payment_id?: string; razorpay_signature?: string };
    try { msg = JSON.parse(e.nativeEvent.data); } catch { return; }
    if (msg.type === 'success' && msg.razorpay_order_id && msg.razorpay_payment_id && msg.razorpay_signature) {
      router.back();
      await t.confirmPayment({ razorpay_order_id: msg.razorpay_order_id, razorpay_payment_id: msg.razorpay_payment_id,
        razorpay_signature: msg.razorpay_signature });
    } else if (msg.type === 'dismissed' || msg.type === 'failed') {
      router.back();
    }
  };

  return (
    <SafeAreaView style={styles.page} edges={['top', 'bottom']}>
      <View style={{ flex: 1, borderRadius: 14, overflow: 'hidden' }}>
        <WebView source={{ uri }} onMessage={onMessage} javaScriptEnabled originWhitelist={['*']} />
      </View>
      <Muted small>Payments run in test mode. No money moves.</Muted>
      <Button label="Cancel" onPress={() => router.back()} />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, padding: 16, gap: 12, backgroundColor: colors.bg },
});
