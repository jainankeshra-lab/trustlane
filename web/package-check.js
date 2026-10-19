// Package check on the phone: the store's product model (the shelf camera's classifier, exported to
// ONNX) runs in the browser with onnxruntime-web, so the photo never leaves the phone. If the browser
// can't run it, a small photo goes to the store server instead, which classifies it in memory.
const TLPackage = (() => {
  let info = null;
  let session = null;
  let loading = null;

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      if (window.ort) return resolve();
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = () => reject(new Error("couldn't load " + src));
      document.head.appendChild(s);
    });
  }

  async function available() {
    if (!info) {
      try { info = (await TL.api("/api/vision/package-model")).data; } catch { info = { available: false }; }
    }
    return !!info.available;
  }

  async function load() {
    if (session) return session;
    loading = loading || (async () => {
      await loadScript("/static/vendor/ort/ort.wasm.min.js");
      ort.env.wasm.wasmPaths = "/static/vendor/ort/";
      ort.env.wasm.numThreads = 1;  // threads need cross-origin isolation, which this page doesn't have
      session = await ort.InferenceSession.create(info.url, { executionProviders: ["wasm"] });
      return session;
    })();
    try { return await loading; } catch (e) { loading = null; throw e; }
  }

  // Same preprocessing as the server and Ultralytics: shorter side to `size`, centre crop, RGB 0..1, NCHW.
  function toTensor(source, size) {
    const w = source.videoWidth || source.naturalWidth || source.width;
    const h = source.videoHeight || source.naturalHeight || source.height;
    const side = Math.min(w, h);
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(source, (w - side) / 2, (h - side) / 2, side, side, 0, 0, size, size);
    const px = ctx.getImageData(0, 0, size, size).data;
    const plane = size * size;
    const data = new Float32Array(3 * plane);
    for (let i = 0; i < plane; i++) {
      data[i] = px[i * 4] / 255;
      data[plane + i] = px[i * 4 + 1] / 255;
      data[2 * plane + i] = px[i * 4 + 2] / 255;
    }
    return { tensor: new ort.Tensor("float32", data, [1, 3, size, size]), canvas };
  }

  function smallJpeg(source) {
    const w = source.videoWidth || source.naturalWidth || source.width;
    const h = source.videoHeight || source.naturalHeight || source.height;
    const scale = Math.min(1, 320 / Math.min(w, h));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    canvas.getContext("2d").drawImage(source, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.8).split(",")[1];
  }

  // source: <img>, <video> or <canvas>. serverCheck(base64Jpeg) is the fallback.
  async function classify(source, serverCheck) {
    if (!(await available())) throw new Error("the store has no package model yet");
    try {
      const s = await load();
      const { tensor } = toTensor(source, info.input.size);
      const out = await s.run({ [info.input.name]: tensor });
      const probs = out[s.outputNames[0]].data;
      let best = 0;
      for (let i = 1; i < probs.length; i++) if (probs[i] > probs[best]) best = i;
      return { label: info.names[best], confidence: Math.round(probs[best] * 1e4) / 1e4, where: "phone" };
    } catch (e) {
      if (!serverCheck) throw e;
      console.warn("On-phone package check unavailable, asking the store:", e);
      return { ...(await serverCheck(smallJpeg(source))), where: "server" };
    }
  }

  function imageFromFile(file) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(img.src); resolve(img); };
      img.onerror = () => reject(new Error("couldn't read that photo"));
      img.src = URL.createObjectURL(file);
    });
  }

  return { available, load, classify, imageFromFile, toTensor };
})();
