// Push notifications: nudges reach the shopper even with the app in the background.
// Needs a development build with an EAS project id (and FCM / APNs credentials set up by EAS).
import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { Platform } from 'react-native';

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldPlaySound: true,
    shouldSetBadge: false,
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});

export async function registerForPush(): Promise<{ token?: string; error?: string }> {
  if (!Device.isDevice) return { error: 'Push needs a real phone' };
  if (Platform.OS === 'android') {
    await Notifications.setNotificationChannelAsync('default', {
      name: 'Shopping alerts',
      importance: Notifications.AndroidImportance.MAX,
      vibrationPattern: [0, 250, 250, 250],
      lightColor: '#17765A',
    });
  }
  const { status: existing } = await Notifications.getPermissionsAsync();
  let status = existing;
  if (existing !== 'granted') status = (await Notifications.requestPermissionsAsync()).status;
  if (status !== 'granted') return { error: 'Notifications are off for TrustLane' };
  const projectId = Constants?.expoConfig?.extra?.eas?.projectId || Constants?.easConfig?.projectId;
  if (!projectId) return { error: 'No EAS project id yet (run eas init)' };
  try {
    return { token: (await Notifications.getExpoPushTokenAsync({ projectId })).data };
  } catch (e) {
    return { error: e instanceof Error ? e.message : String(e) };
  }
}
