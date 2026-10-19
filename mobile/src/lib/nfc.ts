// Read the exit gate's NFC tag. Gates carry NTAG 424 DNA tags whose URL holds a signed, single-use
// read (picc_data + cmac); the server checks it, which proves the phone is physically at that gate.
import NfcManager, { Ndef, NfcTech } from 'react-native-nfc-manager';

export type GateTagRead = { picc_data: string; cmac: string };

export function parseGateUrl(url: string): GateTagRead | null {
  const query = url.includes('?') ? url.slice(url.indexOf('?') + 1) : '';
  const params: Record<string, string> = {};
  for (const part of query.split('&')) {
    const [k, v] = part.split('=');
    if (k) params[decodeURIComponent(k)] = decodeURIComponent(v ?? '');
  }
  return params.picc_data && params.cmac ? { picc_data: params.picc_data, cmac: params.cmac } : null;
}

export async function nfcSupported(): Promise<boolean> {
  try { return await NfcManager.isSupported(); } catch { return false; }
}

/** Wait for the shopper to touch the gate tag. iOS shows its own scanning sheet with `prompt`. */
export async function readGateTag(prompt: string): Promise<GateTagRead> {
  await NfcManager.start();
  try {
    await NfcManager.requestTechnology(NfcTech.Ndef, { alertMessage: prompt });
    const tag = await NfcManager.getTag();
    for (const record of tag?.ndefMessage ?? []) {
      if (Ndef.isType(record, Ndef.TNF_WELL_KNOWN, Ndef.RTD_URI)) {
        const read = parseGateUrl(Ndef.uri.decodePayload(Uint8Array.from(record.payload)));
        if (read) return read;
      }
    }
    throw new Error("That NFC tag isn't a TrustLane gate");
  } finally {
    NfcManager.cancelTechnologyRequest().catch(() => {});
  }
}
