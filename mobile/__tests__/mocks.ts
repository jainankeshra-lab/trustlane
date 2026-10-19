// Native modules that need a device: stand-ins so screens can render under Jest.
jest.mock('react-native-ble-plx', () => ({ BleManager: jest.fn(() => ({ startDeviceScan: jest.fn(), stopDeviceScan: jest.fn() })) }));
jest.mock('react-native-nfc-manager', () => ({
  __esModule: true,
  default: { isSupported: jest.fn(async () => false), start: jest.fn(), requestTechnology: jest.fn(), getTag: jest.fn(), cancelTechnologyRequest: jest.fn(async () => {}) },
  Ndef: { isType: jest.fn(), uri: { decodePayload: jest.fn() }, TNF_WELL_KNOWN: 1, RTD_URI: 'U' },
  NfcTech: { Ndef: 'Ndef' },
}));
jest.mock('onnxruntime-react-native', () => ({}), { virtual: true });
jest.mock('react-native-svg', () => ({ SvgUri: () => null, SvgXml: () => null }));
jest.mock('@/lib/push', () => ({ registerForPush: jest.fn(async () => ({ token: null, error: 'test' })) }));
jest.mock('@react-native-async-storage/async-storage', () => require('@react-native-async-storage/async-storage/jest/async-storage-mock'));
