// Package check on the phone. The store's product model (the shelf camera's classifier, exported to
// ONNX) is downloaded once and run with onnxruntime-react-native, so the photo never leaves the phone.
// If the model can't run here, a small photo goes to the store server, which classifies it in memory.
import { Directory, File, Paths } from 'expo-file-system';
import { ImageManipulator, SaveFormat } from 'expo-image-manipulator';
import * as jpeg from 'jpeg-js';

import type { Api, PackageCheck, PackageModelInfo, Trip } from './api';

export type PackageResult = PackageCheck & { where: 'phone' | 'server' };

type Loaded = { base: string; info: PackageModelInfo; session: any | null };
let loaded: Loaded | null = null;
let loading: Promise<Loaded> | null = null;

async function load(api: Api): Promise<Loaded> {
  if (loaded && loaded.base === api.base) return loaded;
  loading = loading ?? (async () => {
    const info = await api.packageModel();
    let session = null;
    if (info.available) {
      try {
        const dir = new Directory(Paths.cache, 'models');
        dir.create({ idempotent: true, intermediates: true });
        // Named by content hash: a retrained model is a new file, an unchanged one is never re-downloaded.
        const file = new File(dir, `package-${info.sha256.slice(0, 16)}.onnx`);
        if (!file.exists) await File.downloadFileAsync(api.base + info.url, file, { idempotent: true });
        const { InferenceSession } = require('onnxruntime-react-native');
        session = await InferenceSession.create(file.uri);
      } catch (e) {
        console.warn('On-phone package model unavailable, the store server will check instead:', e);
      }
    }
    loaded = { base: api.base, info, session };
    return loaded;
  })();
  try { return await loading; } finally { loading = null; }
}

export async function packageCheckAvailable(api: Api): Promise<boolean> {
  try { return (await load(api)).info.available; } catch { return false; }
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Same preprocessing as the server and Ultralytics: centre square, resize, RGB 0..1, NCHW. */
async function squareJpeg(uri: string, size: number): Promise<string> {
  const original = await ImageManipulator.manipulate(uri).renderAsync();
  const side = Math.min(original.width, original.height);
  const ref = await ImageManipulator.manipulate(uri)
    .crop({ originX: (original.width - side) / 2, originY: (original.height - side) / 2, width: side, height: side })
    .resize({ width: size, height: size })
    .renderAsync();
  const saved = await ref.saveAsync({ format: SaveFormat.JPEG, compress: 0.95, base64: true });
  return saved.base64!;
}

export function toTensorData(rgba: Uint8Array, size: number): Float32Array {
  const plane = size * size;
  const data = new Float32Array(3 * plane);
  for (let i = 0; i < plane; i++) {
    data[i] = rgba[i * 4] / 255;
    data[plane + i] = rgba[i * 4 + 1] / 255;
    data[2 * plane + i] = rgba[i * 4 + 2] / 255;
  }
  return data;
}

export function best(probs: ArrayLike<number>, names: string[]): PackageCheck {
  let top = 0;
  for (let i = 1; i < probs.length; i++) if (probs[i] > probs[top]) top = i;
  return { label: names[top], confidence: Math.round(probs[top] * 1e4) / 1e4 };
}

/** Classify a photo of the item in the shopper's hand. Null when the store has no model. */
export async function classifyPhoto(api: Api, trip: Trip, photoUri: string): Promise<PackageResult | null> {
  const { info, session } = await load(api);
  if (!info.available) return null;
  const size = info.input.size;
  if (session) {
    try {
      const pixels = jpeg.decode(base64ToBytes(await squareJpeg(photoUri, size)), { useTArray: true, formatAsRGBA: true });
      const { Tensor } = require('onnxruntime-react-native');
      const out = await session.run({ [info.input.name]: new Tensor('float32', toTensorData(pixels.data, size), [1, 3, size, size]) });
      return { ...best(out[session.outputNames[0]].data as Float32Array, info.names), where: 'phone' };
    } catch (e) {
      console.warn('On-phone package check failed, asking the store:', e);
    }
  }
  const small = await squareJpeg(photoUri, 320);
  const { data } = await api.trip<PackageCheck>(trip, '/package-check', { image_b64: small });
  return { label: data.label, confidence: data.confidence, where: 'server' };
}
