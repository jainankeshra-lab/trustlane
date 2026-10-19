import { CameraView, useCameraPermissions, type BarcodeScanningResult } from 'expo-camera';
import { router } from 'expo-router';
import { useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Button, Muted, Title } from '@/components/ui';
import { Api } from '@/lib/api';
import { classifyPhoto, packageCheckAvailable } from '@/lib/packageCheck';
import { useTrip } from '@/lib/trip';
import { colors } from '@/theme';

export default function Scan() {
  const t = useTrip();
  const [permission, requestPermission] = useCameraPermissions();
  const handled = useRef(false);
  const camera = useRef<CameraView>(null);
  const api = useMemo(() => (t.server ? new Api(t.server) : null), [t.server]);
  const [checkPackage, setCheckPackage] = useState(false);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    if (api) packageCheckAvailable(api).then(setCheckPackage);  // also downloads the model once
  }, [api]);

  if (!permission) return <View style={styles.black} />;
  if (!permission.granted) {
    return (
      <SafeAreaView style={styles.ask}>
        <Title>Camera access</Title>
        <Muted>TrustLane uses the camera only to read product barcodes.</Muted>
        <Button label="Allow camera" kind="primary" onPress={requestPermission} />
        <Button label="Cancel" onPress={() => router.back()} />
      </SafeAreaView>
    );
  }

  const onScanned = async (result: BarcodeScanningResult) => {
    if (handled.current) return;  // one scan per opening, even though the camera keeps firing
    handled.current = true;
    let pkg = null;
    if (checkPackage && api && t.trip && camera.current) {
      // The camera already sees the package: one photo, checked against the barcode on this phone.
      setChecking(true);
      try {
        const photo = await camera.current.takePictureAsync({ quality: 0.6 });
        pkg = await classifyPhoto(api, t.trip, photo.uri);
      } catch (e) {
        console.warn('Package check skipped:', e);
      }
    }
    t.scan(result.data, pkg);
    router.back();
  };

  return (
    <View style={styles.black}>
      <CameraView
        ref={camera}
        style={StyleSheet.absoluteFill}
        facing="back"
        barcodeScannerSettings={{ barcodeTypes: ['ean13', 'ean8', 'upc_a', 'upc_e', 'code128'] }}
        onBarcodeScanned={onScanned}
      />
      <SafeAreaView style={styles.overlay} pointerEvents="box-none">
        <View style={styles.frame} />
        <Text style={styles.hint}>{checking ? 'Checking the package…' : "Point at the product's barcode"}</Text>
        {checking && <ActivityIndicator color={colors.white} />}
        {checkPackage && !checking && <Text style={styles.small}>Package check on: the photo stays on your phone</Text>}
        <Button label="Cancel" onPress={() => router.back()} />
      </SafeAreaView>
    </View>
  );
}

const styles = StyleSheet.create({
  black: { flex: 1, backgroundColor: '#000' },
  ask: { flex: 1, padding: 24, gap: 14, justifyContent: 'center', backgroundColor: colors.bg },
  overlay: { flex: 1, justifyContent: 'flex-end', alignItems: 'center', padding: 24, gap: 16 },
  frame: { position: 'absolute', top: '30%', width: '78%', height: 160, borderWidth: 3, borderColor: colors.white, borderRadius: 16 },
  hint: { color: colors.white, fontSize: 16, fontWeight: '600' },
  small: { color: colors.white, fontSize: 13 },
});
