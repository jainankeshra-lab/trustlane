import { useState } from 'react';
import { ScrollView, StyleSheet, Switch, Text, TextInput, View } from 'react-native';

import { Banner, Button, Card, Muted } from '@/components/ui';
import { advertisingAvailable, bluetoothAvailable } from '@/lib/bluetooth';
import { useTrip } from '@/lib/trip';
import { colors } from '@/theme';

export default function Settings() {
  const t = useTrip();
  const [url, setUrl] = useState(t.server);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [pushResult, setPushResult] = useState<string | null>(null);
  const s = t.status;

  return (
    <ScrollView contentContainerStyle={styles.page}>
      <Card>
        <Text style={styles.h2}>Store server</Text>
        <TextInput style={styles.input} value={url} onChangeText={setUrl} autoCapitalize="none" autoCorrect={false}
          keyboardType="url" accessibilityLabel="Store server address" />
        {problem && <Banner kind="warn">{problem}</Banner>}
        {saved && !problem && <Muted small>Connected.</Muted>}
        <Button label="Save and connect" kind="primary" onPress={async () => {
          const p = await t.setServer(url);
          setProblem(p);
          setSaved(!p);
        }} />
      </Card>

      <Card>
        <Text style={styles.h2}>Bluetooth</Text>
        <Row label="Find my aisle from beacons" value={t.prefs.autoAisle}
          onChange={(v) => t.setPrefs({ ...t.prefs, autoAisle: v })} />
        <Row label="Let shelves recognise my phone (rotating ID, shelf tokens)" value={t.prefs.broadcastId}
          onChange={(v) => t.setPrefs({ ...t.prefs, broadcastId: v })} />
        <Muted small>Scanning: {bluetoothAvailable() ? 'available' : 'needs the development build'} ·
          Broadcasting: {advertisingAvailable() ? 'available' : 'needs the development build'}</Muted>
        {s.beaconError && <Muted small>Beacons: {s.beaconError}</Muted>}
        {s.advertiseError && <Muted small>Broadcast: {s.advertiseError}</Muted>}
        <Muted small>If you switch these off, the store can't see which aisle you're in, so shelf picks nearby may be checked
          with you at the exit.</Muted>
      </Card>

      <Card>
        <Text style={styles.h2} accessibilityRole="header">{t.tr('Language')}</Text>
        <View style={styles.choices} accessibilityRole="radiogroup">
          {(['auto', 'en', 'hi'] as const).map((l) => (
            <Button key={l} small kind={t.prefs.lang === l ? 'primary' : 'plain'}
              label={l === 'auto' ? 'Phone setting' : l === 'en' ? 'English' : 'हिन्दी'}
              onPress={() => t.setPrefs({ ...t.prefs, lang: l })} />
          ))}
        </View>
      </Card>

      <Card>
        <Text style={styles.h2} accessibilityRole="header">{t.tr('Status')}</Text>
        <Muted small>Live connection: {s.live ? 'on' : 'off'}</Muted>
        <Muted small>Push notifications: {s.push ?? 'not set up yet'}</Muted>
        <Muted small>Payments: {t.config ? `${t.config.payments.provider} (${t.config.payments.mode})` : 'unknown'}</Muted>
        <Button small label={t.tr('Send me a test notification')} disabled={!t.trip}
          onPress={async () => setPushResult(await t.pushTest())} />
        {pushResult && <Muted small>{pushResult}</Muted>}
      </Card>
    </ScrollView>
  );
}

function Row({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <View style={styles.row}>
      <Text style={{ flex: 1, color: colors.ink }}>{label}</Text>
      <Switch value={value} onValueChange={onChange} accessibilityLabel={label} trackColor={{ true: colors.green }} />
    </View>
  );
}

const styles = StyleSheet.create({
  page: { padding: 16, gap: 14, backgroundColor: colors.bg },
  h2: { fontSize: 17, fontWeight: '700', color: colors.ink },
  row: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  choices: { flexDirection: 'row', gap: 8, flexWrap: 'wrap' },
  input: { borderWidth: 1, borderColor: colors.line, borderRadius: 10, padding: 12, backgroundColor: colors.card, color: colors.ink, fontSize: 16 },
});
