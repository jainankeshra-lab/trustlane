// Broadcasts the trip's rotating Bluetooth ID so shelf stations can detect the phone nearby.
// Native code: android/ (BluetoothLeAdvertiser) and ios/ (CBPeripheralManager).
// Not available in Expo Go: it needs a development build (eas build --profile development).
import { NativeModule, requireOptionalNativeModule } from 'expo';

export type AdvertisingState = { advertising: boolean; uuid?: string; error?: string | number };

type Events = {
  onAdvertisingState(event: AdvertisingState): void;
};

declare class TrustlaneBleModule extends NativeModule<Events> {
  isSupported(): boolean;
  startAdvertising(serviceUuid: string): void;
  stopAdvertising(): void;
}

// null when the native module isn't compiled in (Expo Go, web)
export default requireOptionalNativeModule<TrustlaneBleModule>('TrustlaneBle');
