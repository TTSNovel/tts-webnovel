/*
 * Piper TTS running entirely on-device (ONNX Runtime Web + the Piper
 * espeak-ng phonemizer, both compiled to WASM) — the "Piper (offline)"
 * voice option in reader.js's settings panel. Ported by hand from
 * diffusionstudio/vits-web (MIT) since that package assumes an npm
 * bundler and hardcodes huggingface.co as both the model host AND the
 * only URL prefix its OPFS cache will store — neither fits a plain-
 * <script>, no-build-step site that needs the model genuinely available
 * with zero network at all, not just "cached from a CDN".
 *
 * Everything this needs is self-hosted (same origin), unlike the online
 * voices which proxy through /api/tts:
 *  - /assets/vendor/piper/*  — onnxruntime-web (single-threaded wasm
 *    build, so no COOP/COEP cross-origin-isolation headers are required
 *    site-wide) + the Piper phonemizer wasm/data, loaded lazily as plain
 *    scripts only when this voice is actually used.
 *  - /models/piper/vi_VN-vais1000-medium.onnx(.json) — the exact same
 *    Piper voice tts-generate uses server-side (see
 *    tts-pipeline-infra/src/tts-generate/main.py's PRIMARY_LANG voice),
 *    mirrored into this site's own bucket so it's same-origin.
 * The ~93MB total (model + wasm + espeak-ng phoneme data) is fetched once
 * and cached in the Origin Private File System — explicitly, via the
 * download() button in reader.js's settings panel, never automatically.
 */
(function () {
  'use strict';

  const VENDOR_BASE = '/assets/vendor/piper';
  // Served from a small dedicated public GCS bucket, NOT proxied through
  // this site's own Cloud Run service — Cloud Run's front end hard-caps
  // buffered (non-chunked) response bodies at ~32MB ("Response size was
  // too large" in Cloud Run's own logs) and this 63MB model blew past
  // that with a bare 500. The bucket holds nothing but this public,
  // MIT-licensed voice model (same one tts-generate uses server-side), so
  // serving it unauthenticated costs nothing in privacy — unlike the
  // book content, which stays behind login via novel-web's own bucket.
  const MODEL_URL = 'https://storage.googleapis.com/tts-pipeline-yl-piper-offline/vi_VN-vais1000-medium.onnx';
  const MODEL_CONFIG_URL = 'https://storage.googleapis.com/tts-pipeline-yl-piper-offline/vi_VN-vais1000-medium.onnx.json';
  const OPFS_DIR = 'piper-offline';
  const MODEL_FILE = 'model.onnx';
  const CONFIG_FILE = 'model.onnx.json';

  async function opfsDir() {
    const root = await navigator.storage.getDirectory();
    return root.getDirectoryHandle(OPFS_DIR, { create: true });
  }

  async function opfsRead(name) {
    try {
      const dir = await opfsDir();
      const handle = await dir.getFileHandle(name);
      return await handle.getFile();
    } catch (e) {
      // NotFoundError (file genuinely never downloaded) is expected and
      // silent on purpose. Anything else was being swallowed here too,
      // which is exactly why "Model Piper offline chưa được tải" kept
      // showing up as the reported error even when the real cause was
      // something else entirely (e.g. failing to actually read a 63MB
      // file back) — log everything but NotFoundError.
      if (!e || e.name !== 'NotFoundError') {
        console.error(`PiperOffline: opfsRead(${name}) failed ::`, (e && e.name) || typeof e, (e && e.message) || e);
      }
      return undefined;
    }
  }

  // Fallback path for browsers where FileSystemFileHandle.createWritable()
  // doesn't exist at all — confirmed live on a real iPhone: "TypeError:
  // handle.createWritable is not a function". createSyncAccessHandle() is
  // the alternative, but it's synchronous-only, which the spec only
  // allows inside a Worker — hence going through one here, thrown away
  // right after (this only runs for the 2 writes an install ever does,
  // so there's no reuse benefit worth the complexity of keeping it alive).
  const SYNC_WRITER_WORKER_SRC = `
    self.onmessage = async (e) => {
      const { name, buffer, dir } = e.data;
      try {
        const root = await navigator.storage.getDirectory();
        const dirHandle = await root.getDirectoryHandle(dir, { create: true });
        const fileHandle = await dirHandle.getFileHandle(name, { create: true });
        const accessHandle = await fileHandle.createSyncAccessHandle();
        accessHandle.truncate(0);
        accessHandle.write(new Uint8Array(buffer), { at: 0 });
        accessHandle.flush();
        accessHandle.close();
        self.postMessage({ ok: true });
      } catch (err) {
        // JavaScriptCore's Error.stack (Safari) doesn't include the
        // message text the way V8's does — String(err.stack) alone was
        // reporting things like "@blob:...:27:53" with no actual message,
        // completely hiding what went wrong. message + stack, explicitly.
        const msg = (err && err.message) || String(err);
        const stack = (err && err.stack) || 'n/a';
        self.postMessage({ ok: false, error: msg + ' :: stack :: ' + stack });
      }
    };
  `;

  async function opfsWriteViaSyncHandleWorker(name, blob) {
    const buffer = await blob.arrayBuffer();
    const worker = new Worker(URL.createObjectURL(new Blob([SYNC_WRITER_WORKER_SRC], { type: 'application/javascript' })));
    try {
      return await new Promise((resolve, reject) => {
        worker.onmessage = (e) => {
          if (e.data.ok) resolve();
          else reject(new Error(e.data.error));
        };
        worker.onerror = (err) => reject(err);
        worker.postMessage({ name, buffer, dir: OPFS_DIR }, [buffer]);
      });
    } finally {
      worker.terminate();
    }
  }

  async function opfsWrite(name, blob) {
    const dir = await opfsDir();
    const handle = await dir.getFileHandle(name, { create: true });
    if (typeof handle.createWritable === 'function') {
      const writable = await handle.createWritable();
      await writable.write(blob);
      await writable.close();
      return;
    }
    console.warn('PiperOffline: createWritable() unavailable on this browser — using createSyncAccessHandle() fallback');
    await opfsWriteViaSyncHandleWorker(name, blob);
  }

  async function opfsRemoveAll() {
    console.warn('PiperOffline: opfsRemoveAll starting');
    try {
      const root = await navigator.storage.getDirectory();
      await root.removeEntry(OPFS_DIR, { recursive: true });
      console.warn('PiperOffline: removeEntry() returned with no error');
    } catch (e) {
      if (e && e.name === 'NotFoundError') {
        console.warn('PiperOffline: opfsRemoveAll — NotFoundError, already gone');
        return; // nothing to remove, fine
      }
      console.error('PiperOffline: opfsRemoveAll failed ::', (e && e.name) || typeof e, (e && e.message) || e);
      throw e;
    }
    // removeEntry() resolving without throwing has NOT reliably meant the
    // directory is actually gone in testing (same "reports success but
    // isn't durable" pattern seen with writes) — verify for real instead
    // of trusting the resolved promise.
    const stillThere = await opfsRead(MODEL_FILE);
    console.warn('PiperOffline: post-delete verification, model file still present? ::', !!stillThere);
    if (stillThere) {
      throw new Error('removeEntry() không báo lỗi nhưng file model vẫn còn trên OPFS (đã xác minh lại).');
    }
  }

  async function fetchBlobWithProgress(url, onProgress) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`fetch ${url} failed: ${res.status}`);
    const reader = res.body && res.body.getReader();
    const total = +(res.headers.get('Content-Length') || 0);
    if (!reader) return res.blob();
    let loaded = 0;
    const chunks = [];
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      loaded += value.length;
      if (onProgress) onProgress(loaded, total || loaded);
    }
    return new Blob(chunks);
  }

  async function isDownloaded() {
    return !!(await opfsRead(MODEL_FILE));
  }

  // Single-flight, enforced HERE rather than trusted to callers — reader.js
  // preloads up to PRELOAD_AHEAD (6) sentences at once, and each one calls
  // download() independently if the model isn't ready yet. If reader.js's
  // own dedup promise ever resets after a failure (it does, to allow a
  // retry), those 6+ near-simultaneous sentences would otherwise each
  // kick off their OWN full fetch+write of the same 63MB model at once —
  // confirmed via a stress test (6 concurrent download() calls from a
  // clean slate) to cause exactly that without this guard. This makes
  // "only one real download ever in flight" true no matter how many
  // places call download() or how they handle failure.
  let downloadPromise = null;
  async function download(onProgress) {
    if (await isDownloaded()) return;
    if (!downloadPromise) {
      downloadPromise = (async () => {
        try {
          const [modelBlob, configBlob] = await Promise.all([
            fetchBlobWithProgress(MODEL_URL, onProgress),
            fetch(MODEL_CONFIG_URL).then((r) => {
              if (!r.ok) throw new Error(`fetch model config failed: ${r.status}`);
              return r.blob();
            }),
          ]);
          await opfsWrite(MODEL_FILE, modelBlob);
          await opfsWrite(CONFIG_FILE, configBlob);
        } finally {
          downloadPromise = null;
        }
      })();
    }
    return downloadPromise;
  }

  async function removeModel() {
    await opfsRemoveAll();
    recycleInferenceWorker();
  }

  // Everything WASM (onnxruntime-web + the phonemizer) and the actual
  // inference now runs inside a dedicated Worker, not the main thread.
  // Two problems traced back to running it on the main thread:
  //  - The page became unresponsive to taps/clicks during every
  //    synthesize() call — single-threaded WASM inference blocks the JS
  //    event loop for its whole duration, and there's nothing async about
  //    it from the browser's perspective.
  //  - "Out of memory" after only a handful of sentences. WASM linear
  //    memory only ever grows, never shrinks, for the life of a module
  //    instance — the ONLY way to actually reclaim it in a browser is to
  //    destroy the whole execution context holding it. Worker.terminate()
  //    does exactly that (guaranteed full reclaim); nothing short of it
  //    (session.release(), dropping references, etc.) reliably does on
  //    the WASM side. So the worker is deliberately terminated and
  //    respawned every RECYCLE_AFTER_CALLS synthesize() calls — the
  //    number is a conservative guess (OOM previously hit by sentence 5
  //    running everything in one long-lived context), not a measured
  //    threshold; tune it up if real-device testing shows it's overly
  //    cautious. Model reload after a recycle is a local OPFS read, not a
  //    network fetch, so the cost is real but bounded.
  const RECYCLE_AFTER_CALLS = 5;

  // Absolute URLs throughout — inside a Worker created from a Blob URL
  // (blob:https://.../<uuid>), relative paths like "/assets/vendor/..."
  // fail to resolve at all ("Failed to parse URL from ..."), unlike on
  // the main thread where they resolve fine against the page's own URL.
  const VENDOR_BASE_ABS = `${self.location.origin}${VENDOR_BASE}`;

  const INFERENCE_WORKER_SRC = `
    importScripts(${JSON.stringify(`${VENDOR_BASE_ABS}/ort.wasm.min.js`)}, ${JSON.stringify(`${VENDOR_BASE_ABS}/piper_phonemize.js`)});
    ort.env.wasm.wasmPaths = ${JSON.stringify(`${VENDOR_BASE_ABS}/`)};
    ort.env.wasm.numThreads = 1;

    const OPFS_DIR = ${JSON.stringify(OPFS_DIR)};
    const MODEL_FILE = ${JSON.stringify(MODEL_FILE)};
    const CONFIG_FILE = ${JSON.stringify(CONFIG_FILE)};
    const VENDOR_BASE = ${JSON.stringify(VENDOR_BASE_ABS)};

    async function opfsRead(name) {
      try {
        const root = await navigator.storage.getDirectory();
        const dir = await root.getDirectoryHandle(OPFS_DIR);
        const handle = await dir.getFileHandle(name);
        return await handle.getFile();
      } catch (e) {
        return undefined;
      }
    }

    let session = null;
    let modelConfig = null;
    async function ensureModelLoaded() {
      if (session && modelConfig) return;
      const [modelFile, configFile] = await Promise.all([opfsRead(MODEL_FILE), opfsRead(CONFIG_FILE)]);
      if (!modelFile || !configFile) throw new Error('Model Piper offline chưa được tải.');
      modelConfig = JSON.parse(await configFile.text());
      session = await ort.InferenceSession.create(await modelFile.arrayBuffer());
    }

    function phonemize(text, espeakVoice) {
      return new Promise((resolve, reject) => {
        createPiperPhonemize({
          print: (data) => resolve(JSON.parse(data).phoneme_ids),
          printErr: (message) => reject(new Error(String(message))),
          locateFile: (url) => {
            if (url.endsWith('.wasm')) return VENDOR_BASE + '/piper_phonemize.wasm';
            if (url.endsWith('.data')) return VENDOR_BASE + '/piper_phonemize.data';
            return url;
          },
        }).then((phonemizer) => {
          phonemizer.callMain(['-l', espeakVoice, '--input', JSON.stringify([{ text }]), '--espeak_data', '/espeak-ng-data']);
        }).catch(reject);
      });
    }

    function pcm2wav(buffer, numChannels, sampleRate) {
      const bufferLength = buffer.length;
      const headerLength = 44;
      const view = new DataView(new ArrayBuffer(bufferLength * numChannels * 2 + headerLength));
      view.setUint32(0, 0x46464952, true);
      view.setUint32(4, view.buffer.byteLength - 8, true);
      view.setUint32(8, 0x45564157, true);
      view.setUint32(12, 0x20746d66, true);
      view.setUint32(16, 0x10, true);
      view.setUint16(20, 0x0001, true);
      view.setUint16(22, numChannels, true);
      view.setUint32(24, sampleRate, true);
      view.setUint32(28, numChannels * 2 * sampleRate, true);
      view.setUint16(32, numChannels * 2, true);
      view.setUint16(34, 16, true);
      view.setUint32(36, 0x61746164, true);
      view.setUint32(40, 2 * bufferLength, true);
      let p = headerLength;
      for (let i = 0; i < bufferLength; i++) {
        const v = buffer[i];
        if (v >= 1) view.setInt16(p, 0x7fff, true);
        else if (v <= -1) view.setInt16(p, -0x8000, true);
        else view.setInt16(p, (v * 0x8000) | 0, true);
        p += 2;
      }
      return view.buffer;
    }

    self.onmessage = async (e) => {
      const { id, text, speed } = e.data;
      try {
        await ensureModelLoaded();
        const phonemeIds = await phonemize(text.trim(), modelConfig.espeak.voice);
        const lengthScale = speed && speed !== 1.0 ? 1.0 / speed : modelConfig.inference.length_scale;
        const feeds = {
          input: new ort.Tensor('int64', phonemeIds, [1, phonemeIds.length]),
          input_lengths: new ort.Tensor('int64', [phonemeIds.length]),
          scales: new ort.Tensor('float32', [modelConfig.inference.noise_scale, lengthScale, modelConfig.inference.noise_w]),
        };
        if (Object.keys(modelConfig.speaker_id_map || {}).length) {
          feeds.sid = new ort.Tensor('int64', [0]);
        }
        const { output } = await session.run(feeds);
        const wavBuffer = pcm2wav(output.data, 1, modelConfig.audio.sample_rate);
        self.postMessage({ id, ok: true, buffer: wavBuffer }, [wavBuffer]);
      } catch (err) {
        const msg = (err && err.message) || String(err);
        const stack = (err && err.stack) || 'n/a';
        self.postMessage({ id, ok: false, error: msg + ' :: stack :: ' + stack });
      }
    };
  `;

  let inferenceWorker = null;
  let workerCallCount = 0;
  let msgId = 0;
  const pending = new Map();

  function recycleInferenceWorker() {
    if (inferenceWorker) inferenceWorker.terminate();
    inferenceWorker = null;
    workerCallCount = 0;
    for (const [, p] of pending) p.reject(new Error('Inference worker recycled mid-request'));
    pending.clear();
  }

  function getInferenceWorker() {
    if (!inferenceWorker) {
      inferenceWorker = new Worker(
        URL.createObjectURL(new Blob([INFERENCE_WORKER_SRC], { type: 'application/javascript' }))
      );
      inferenceWorker.onmessage = (e) => {
        const { id, ok, buffer, error } = e.data;
        const p = pending.get(id);
        if (!p) return;
        pending.delete(id);
        if (ok) p.resolve(new Blob([buffer], { type: 'audio/x-wav' }));
        else p.reject(new Error(error));
      };
      inferenceWorker.onerror = (err) => {
        console.error('PiperOffline: inference worker crashed ::', err.message || err);
        for (const [, p] of pending) p.reject(err);
        pending.clear();
        inferenceWorker = null;
        workerCallCount = 0;
      };
    }
    return inferenceWorker;
  }

  // speed: same convention as tts-generate's synthesize() server-side
  // (main.py) — length_scale = 1/speed — so switching between online and
  // offline voices doesn't change how the "Tốc độ" setting behaves.
  async function synthesize(text, speed) {
    const worker = getInferenceWorker();
    const id = ++msgId;
    const result = await new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      worker.postMessage({ id, text, speed });
    });
    // Recycle only checked AFTER a response comes back, and only acted on
    // once nothing else is in flight — reader.js's PRELOAD_AHEAD fires
    // several synthesize() calls back to back, and recycling mid-batch
    // used to kill whichever of THOSE were still pending too (seen live:
    // "Inference worker recycled mid-request" on sentence 2, which had
    // done nothing wrong itself — it just happened to still be in flight
    // when sentence 6 or so decided the count meant it was time to
    // recycle).
    workerCallCount++;
    if (workerCallCount >= RECYCLE_AFTER_CALLS && pending.size === 0) {
      recycleInferenceWorker();
    }
    return result;
  }

  window.PiperOffline = { isDownloaded, download, removeModel, synthesize };
})();
