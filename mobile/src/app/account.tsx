// Shopper account: sign in with a phone number (one-time code), invoice e-mail, receipts and points,
// and the shopper's rights under India's DPDP Act 2023 (a copy of their data; deleting the account).
import { useCallback, useEffect, useState } from 'react';
import { Alert, Linking, ScrollView, Share, StyleSheet, Text, TextInput, View } from 'react-native';

import { Button, Card, Muted } from '@/components/ui';
import { Api, type Account } from '@/lib/api';
import { storage } from '@/lib/storage';
import { useTrip } from '@/lib/trip';
import { colors, rupees } from '@/theme';

export default function AccountScreen() {
  const t = useTrip();
  const api = t.server ? new Api(t.server) : null;
  const [me, setMe] = useState<Account | null>(null);
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [codeSent, setCodeSent] = useState(false);
  const [email, setEmail] = useState('');
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const [grievance, setGrievance] = useState('');

  const load = useCallback(async () => {
    if (!api) return;
    const shopper = await storage.getShopper();
    if (!shopper) { setMe(null); return; }
    try {
      const account = await api.me(shopper);
      setMe(account);
      setEmail(account.email ?? '');
    } catch { setMe(null); }
  }, [t.server]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load();
    api?.storeInfo().then((s) => setGrievance(
      `Questions or complaints about your data: ${s.grievance_officer.name}, ${s.grievance_officer.email}. ${s.legal_name}.`)).catch(() => {});
  }, [load]); // eslint-disable-line react-hooks/exhaustive-deps

  const fail = (e: unknown) => setMsg({ text: e instanceof Error ? e.message : String(e), bad: true });

  const sendCode = async () => {
    try {
      const r = await api!.otpStart(phone);
      setCodeSent(true);
      setMsg({ text: `Code sent to ${r.sent_to}.` });
    } catch (e) { fail(e); }
  };

  const verify = async () => {
    try {
      const r = await api!.otpVerify(phone, code, await storage.getShopper());
      await storage.setShopper({ shopper_id: r.shopper_id, shopper_secret: r.shopper_secret });
      setMsg({ text: r.new ? 'Signed in. Your account is ready.' : 'Signed in. Welcome back!' });
      setCode('');
      load();
    } catch (e) { fail(e); }
  };

  const saveEmail = async () => {
    try {
      const shopper = await storage.getShopper();
      const r = await api!.saveProfile(shopper!, { email });
      setMe(r);
      setMsg({ text: r.email && r.email_receipts ? 'Saved. Invoices will be e-mailed to you.' : 'Saved. Your invoices are always under Receipts here.' });
    } catch (e) { fail(e); }
  };

  const exportData = async () => {
    const shopper = await storage.getShopper();
    if (!shopper) return setMsg({ text: 'Nothing stored yet: start a trip or sign in first.' });
    try {
      const data = await api!.exportData(shopper);
      await Share.share({ title: 'My TrustLane data', message: JSON.stringify(data, null, 2) });
    } catch (e) { fail(e); }
  };

  const deleteAccount = () => {
    Alert.alert('Delete your account?',
      'Your account, points and personal data are deleted. Invoices are kept 72 months as GST law requires, but no longer linked to you.',
      [{ text: 'Cancel', style: 'cancel' }, {
        text: 'Delete', style: 'destructive', onPress: async () => {
          const shopper = await storage.getShopper();
          if (!shopper) return;
          try {
            await api!.deleteAccount(shopper);
            await storage.clearShopper();
            setMe(null);
            setMsg({ text: 'Your account and personal data are deleted.' });
          } catch (e) { fail(e); }
        },
      }]);
  };

  const signedIn = !!me?.phone;
  return (
    <ScrollView contentContainerStyle={styles.page} keyboardShouldPersistTaps="handled">
      <Card>
        <Text style={styles.h2} accessibilityRole="header">{t.tr('Your account')}</Text>
        {signedIn ? (
          <>
            <Muted>{t.tr('Signed in · {phone}', { phone: me!.phone! })}</Muted>
            <Text style={styles.label}>{t.tr('E-mail for invoices')}</Text>
            <TextInput style={styles.input} value={email} onChangeText={setEmail} keyboardType="email-address"
              autoCapitalize="none" autoComplete="email" accessibilityLabel={t.tr('E-mail for invoices')} />
            <Button small label={t.tr('Save')} onPress={saveEmail} />
          </>
        ) : (
          <>
            <Muted small>{t.tr('Sign in with your mobile number to keep your receipts and points on any phone.')}</Muted>
            <Text style={styles.label}>{t.tr('Mobile number')}</Text>
            <TextInput style={styles.input} value={phone} onChangeText={setPhone} keyboardType="phone-pad"
              autoComplete="tel" placeholder="98765 43210" accessibilityLabel={t.tr('Mobile number')} />
            <Button small label={t.tr('Send code')} disabled={!phone.trim()} onPress={sendCode} />
            {codeSent && (
              <>
                <Text style={styles.label}>{t.tr('6-digit code')}</Text>
                <TextInput style={styles.input} value={code} onChangeText={setCode} keyboardType="number-pad"
                  autoComplete="sms-otp" textContentType="oneTimeCode" maxLength={6} accessibilityLabel={t.tr('6-digit code')} />
                <Button small kind="primary" label={t.tr('Sign in')} disabled={code.length !== 6} onPress={verify} />
              </>
            )}
          </>
        )}
        {msg && <Text style={[styles.msg, msg.bad && { color: colors.red }]} accessibilityLiveRegion="polite">{msg.text}</Text>}
      </Card>

      {me && (
        <Card>
          <Text style={styles.h2} accessibilityRole="header">{t.tr('Loyalty points')}: {me.points}</Text>
          <Text style={styles.label}>{t.tr('Receipts')}</Text>
          {me.receipts.length ? me.receipts.map((r) => (
            <View key={r.number} style={styles.spread} accessible accessibilityLabel={`${r.number}, ${rupees(r.total)}`}>
              <Text style={styles.ink}>{new Date(r.ts * 1000).toLocaleDateString()} · {r.number}</Text>
              <Text style={styles.ink}>{rupees(r.total)}</Text>
            </View>
          )) : <Muted small>{t.tr('No receipts yet.')}</Muted>}
        </Card>
      )}

      <Card>
        <Text style={styles.h2} accessibilityRole="header">{t.tr('Your data (DPDP Act 2023)')}</Text>
        <Button small label={t.tr('Download my data')} onPress={exportData} />
        <Button small label={t.tr('Delete my account and data')} onPress={deleteAccount} />
        {!!grievance && <Muted small>{grievance}</Muted>}
        <Text style={styles.link} accessibilityRole="link" onPress={() => Linking.openURL(t.server + '/api/store').catch(() => {})}>
          Store details
        </Text>
      </Card>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { padding: 16, gap: 14, backgroundColor: colors.bg },
  h2: { fontSize: 17, fontWeight: '700', color: colors.ink },
  label: { color: colors.ink, fontWeight: '600', marginTop: 4 },
  ink: { color: colors.ink },
  msg: { color: colors.ink, fontSize: 14 },
  link: { color: colors.blue, fontWeight: '600', minHeight: 44, textAlignVertical: 'center' },
  spread: { flexDirection: 'row', justifyContent: 'space-between', gap: 10 },
  input: { borderWidth: 1, borderColor: colors.line, borderRadius: 10, padding: 12, backgroundColor: colors.card, color: colors.ink, fontSize: 16, minHeight: 48 },
});
