// Secrets (trip key, shopper secret) go in the OS keychain/keystore; everything else in AsyncStorage.
import AsyncStorage from '@react-native-async-storage/async-storage';
import Constants from 'expo-constants';
import * as SecureStore from 'expo-secure-store';

import type { PendingScan, Shopper, Trip } from './api';

const KEYS = { server: 'tl.server', queue: 'tl.queue', prefs: 'tl.prefs' };
const SECURE = { trip: 'tl.trip', shopper: 'tl.shopper' };

export type Prefs = { autoAisle: boolean; broadcastId: boolean; lang: 'auto' | 'en' | 'hi' };
export const DEFAULT_PREFS: Prefs = { autoAisle: true, broadcastId: true, lang: 'auto' };

async function getJson<T>(key: string, fallback: T): Promise<T> {
  try {
    const raw = await AsyncStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

async function getSecure<T>(key: string): Promise<T | null> {
  try {
    const raw = await SecureStore.getItemAsync(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

async function setSecure(key: string, value: unknown) {
  if (value === null) await SecureStore.deleteItemAsync(key);
  else await SecureStore.setItemAsync(key, JSON.stringify(value));
}

export const storage = {
  defaultServer: () => (Constants.expoConfig?.extra?.defaultServerUrl as string | undefined) ?? 'http://192.168.1.10:8000',
  getServer: async () => (await AsyncStorage.getItem(KEYS.server)) ?? storage.defaultServer(),
  setServer: (url: string) => AsyncStorage.setItem(KEYS.server, url.trim()),
  getQueue: () => getJson<PendingScan[]>(KEYS.queue, []),
  setQueue: (q: PendingScan[]) => AsyncStorage.setItem(KEYS.queue, JSON.stringify(q)),
  getPrefs: async () => ({ ...DEFAULT_PREFS, ...(await getJson<Prefs>(KEYS.prefs, DEFAULT_PREFS)) }),
  setPrefs: (p: Prefs) => AsyncStorage.setItem(KEYS.prefs, JSON.stringify(p)),
  getTrip: () => getSecure<Trip>(SECURE.trip),
  setTrip: (t: Trip | null) => setSecure(SECURE.trip, t),
  getShopper: () => getSecure<Shopper>(SECURE.shopper),
  setShopper: (s: Shopper) => setSecure(SECURE.shopper, s),
  clearShopper: () => setSecure(SECURE.shopper, null),
};
