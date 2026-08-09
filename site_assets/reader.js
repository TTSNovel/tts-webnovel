/*
 * Standalone read-aloud reader, adapted from kokoro-tts-addon/content.js.
 *
 * Dropped everything that only makes sense inside a browser extension
 * (chrome.storage, the panel iframe, background-script message relays,
 * batch-export mode, the CSP-bypass player iframe). Kept the part that
 * actually matters for a reading site: sentence-level segmentation,
 * look-ahead audio prefetch, gapless-ish playback, highlight-as-it-reads,
 * and the extension's auto-next-chapter / auto-stop-timer settings.
 *
 * App-shell pattern: this same script + its page (chapter-shell.html) is
 * served for every "/books/<id>/chapters/<n>.html" URL (see server.py's
 * chapter_shell route) — nothing here is chapter-specific up front. On
 * load, this file parses the visited URL itself, fetches the matching
 * content fragment from books/<id>/data/<n>.html and the book's
 * meta.json (title/author/chapter count), and fills in the page. Chapter
 * navigation (auto-next AND manual prev/next) reuses the exact same fetch,
 * swapping <article> in place — never a real page navigation, both because
 * that's what makes the shell pattern possible at all, and because iOS
 * Safari only allows audio.play() after a direct user gesture OR within a
 * document that has already played audio once ("unlocked"); a full page
 * reload would reset that unlock and silently break auto-advance on iOS.
 *
 * TTS calls go to same-origin `/api/tts` (see server.py) — never directly to
 * tts-generate — so no secret is ever present in this file or in the
 * browser. The site itself sits behind a login session; the browser already
 * carries that cookie on every same-origin request automatically.
 */
(function () {
  'use strict';

  // Stamped at deploy time — logged so a stale cached copy of this file
  // is obvious at a glance instead of a guess (see sw.js's own version log
  // for the same reasoning).
  const READER_JS_VERSION = '__DEPLOY_VERSION__';

  // performance.now() is relative to navigation start, so this alone
  // already answers "how long did the document/script loading itself
  // take before reader.js even started running" — no separate timer
  // needed just for that part.
  console.log(
    `reader.js: version ${READER_JS_VERSION}, script evaluated at ${performance.now().toFixed(0)}ms since navigation start`
  );

  const MAX_TTS_CHARS = 150;
  const PRELOAD_AHEAD = 6;

  let sentences = [];
  let index = 0;
  let active = false;
  let paused = false;
  const preloadCache = {};
  const preloadReady = new Set();
  // Single Audio element reused for the whole session (never recreated or
  // discarded) rather than `new Audio()` per sentence — once it has played
  // due to a user gesture, iOS keeps allowing .play() on THIS element
  // without a fresh gesture, for as long as the document stays alive.
  let audioEl = null;
  // Cached object URL for a tiny silent WAV clip, built once — see
  // unlockAudioElement() below.
  let silentClipUrl = null;
  let autoStopTimer = null;
  // Background prefetch of the next chapter's content + first-sentence
  // audio, kicked off as soon as the current chapter starts (see
  // prefetchNextChapterIfNeeded). Without this, auto-advance has to fetch
  // both the new chapter's HTML and its first TTS clip live at the chapter
  // boundary — two real network round trips with no audio playing, which
  // breaks the "next .play() follows closely after the previous `ended`"
  // continuity iOS Safari requires to keep allowing gesture-less autoplay.
  let nextChapterPrefetch = null;

  // Chapter-page identity, resolved once on load from the URL + meta.json
  // (see initChapterPage) and kept current by goToChapter() on every jump.
  let bookId = null;
  let bookTitle = '';
  let totalChapters = 0;
  let currentChapterNum = 0;

  // Set while the "Piper (offline)" model is auto-downloading (see
  // refreshPiperOfflineBox) so fetchAudioUrl can wait for it instead of
  // failing outright if playback starts before the download finishes.
  let piperOfflineDownloadPromise = null;

  function pad4(n) {
    return String(n).padStart(4, '0');
  }

  // Reading-history sync — GET once per page load (in parallel with the
  // chapter fetch, see bootstrap()), POST at milestones (pause, chapter
  // navigation, tab hidden, tab close) rather than every sentence. Same
  // backend (/api/progress) and cadence as the iOS app, since progress is
  // account-wide, not device-specific.
  async function fetchProgress() {
    try {
      const r = await fetch('/api/progress');
      if (!r.ok) return {};
      return await r.json();
    } catch {
      return {};
    }
  }

  // `beacon: true` for the tab-hidden/page-unload cases — a plain fetch()
  // started that late in a page's life isn't guaranteed to finish (or even
  // start) before the page is actually torn down; sendBeacon is the
  // browser-native "fire this even if the page is closing" primitive.
  function postProgress(chapter, sentence, { beacon = false } = {}) {
    if (!bookId) return;
    const body = JSON.stringify({ book_id: parseInt(bookId, 10), chapter, sentence });
    if (beacon && navigator.sendBeacon) {
      navigator.sendBeacon('/api/progress', new Blob([body], { type: 'application/json' }));
      return;
    }
    fetch('/api/progress', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body }).catch(() => {});
  }

  // Runs once per page load, after both the chapter content and the saved
  // progress are available. If this exact chapter matches what's saved,
  // pre-splits into sentences and highlights/scrolls to the saved sentence
  // — start()'s existing "if (!sentences.length) sentences = wrap..." guard
  // then leaves `index` alone instead of resetting it to 0, so pressing ▶
  // continues from here (same trick ReaderPlaybackController.prepareResume
  // uses on iOS). Otherwise this is a fresh chapter visit (direct link,
  // manual browsing) — record it as the new baseline immediately, same as
  // a manual chapter jump would.
  function applyResumeOrBaseline(progress) {
    const saved = progress && progress[bookId];
    if (saved && saved.chapter === currentChapterNum) {
      sentences = wrapContentSentences(document.querySelector('article'));
      if (sentences.length) {
        index = Math.min(Math.max(saved.sentence, 0), sentences.length - 1);
        highlightSentence(index);
        updateProgress();
      }
    } else {
      postProgress(currentChapterNum, 0);
    }
  }

  function splitSentences(text) {
    const raw = text.match(/[^.!?]+[.!?]*[\s]*/g) || [text];
    const parts = raw.map((s) => s.trim()).filter((s) => s.length > 2);
    const result = [];
    for (const s of parts) result.push(...chunkLongSentence(s));
    return result;
  }

  function chunkLongSentence(sentence) {
    if (sentence.length <= MAX_TTS_CHARS) return [sentence];
    const chunks = [];
    const parts = sentence.split(/,\s*/);
    let current = '';
    for (const part of parts) {
      const next = current ? current + ', ' + part : part;
      if (next.length > MAX_TTS_CHARS && current) {
        chunks.push(current);
        current = part;
      } else {
        current = next;
      }
    }
    if (current) chunks.push(current);
    return chunks;
  }

  function escapeHtml(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function cleanTextForTTS(text) {
    return text
      .replace(/[·‧・•]/g, '')
      .replace(/[​-‍﻿]/g, '')
      .replace(/~/g, '')
      .replace(/\.{2,}/g, '.')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function wrapContentSentences(container) {
    const result = [];
    container.querySelectorAll('p, h1, h2, h3, h4, li').forEach((node) => {
      if (node.dataset.readerSkip !== undefined) return;
      const text = node.innerText.trim();
      if (!text || text.length < 3) return;
      const parts = splitSentences(text);
      let html = '';
      parts.forEach((sent) => {
        const idx = result.length;
        result.push(sent);
        html += `<span data-r-s="${idx}">${escapeHtml(sent)} </span>`;
      });
      node.innerHTML = html;
    });
    return result;
  }

  // Same sentence-splitting logic as wrapContentSentences, but for a
  // detached fragment (prefetched chapter HTML not yet in the document) —
  // uses textContent instead of innerText since innerText needs the node
  // to actually be laid out/rendered, which a detached container never is.
  function firstSentenceOf(html) {
    const tmp = document.createElement('div');
    tmp.innerHTML = html;
    const nodes = tmp.querySelectorAll('p, h1, h2, h3, h4, li');
    for (const node of nodes) {
      if (node.dataset.readerSkip !== undefined) continue;
      const text = node.textContent.trim();
      if (!text || text.length < 3) continue;
      const parts = splitSentences(text);
      if (parts.length) return parts[0];
    }
    return null;
  }

  // Kicks off (once per chapter) a background fetch of the next chapter's
  // HTML fragment plus its first sentence's TTS audio, well ahead of when
  // auto-advance will actually need them — see the nextChapterPrefetch
  // comment above for why this matters on iOS. Best-effort: on failure the
  // chapter-boundary code just falls back to a live fetch, same as before.
  function prefetchNextChapterIfNeeded() {
    if (localStorage.getItem('reader.autoNext') !== '1') return;
    const nextNum = currentChapterNum + 1;
    if (nextNum >= totalChapters) return;
    if (nextChapterPrefetch && nextChapterPrefetch.num === nextNum) return;

    const entry = { num: nextNum, html: null, audioPromise: null };
    nextChapterPrefetch = entry;
    fetch(`/books/${bookId}/data/${pad4(nextNum)}.html`)
      .then((r) => {
        if (!r.ok) throw new Error(`fetch chapter failed: ${r.status}`);
        return r.text();
      })
      .then((html) => {
        entry.html = html;
        const firstSentence = firstSentenceOf(html);
        if (firstSentence) entry.audioPromise = fetchAudioUrl(firstSentence);
      })
      .catch(() => {
        /* best-effort prefetch; boundary code falls back to a live fetch */
      });
  }

  function highlightSentence(i) {
    document.querySelectorAll('[data-r-s].reading').forEach((el) => el.classList.remove('reading'));
    const span = document.querySelector(`[data-r-s="${i}"]`);
    if (span) span.scrollIntoView({ behavior: 'smooth', block: 'center' }), span.classList.add('reading');
  }

  // Backgrounded/locked-screen tabs get their JS execution throttled hard
  // by iOS — a pending fetch() or a message to the Piper-offline Worker can
  // sit unanswered far longer than it would in the foreground, sometimes
  // indefinitely. Without a bound, readLoop's `await preloadCache[i]`
  // (see fetchAudioUrl below) just hangs forever: no error, no stop(), no
  // way for the ▶ button to recognize anything needs retrying. Capping
  // this turns a silent, unrecoverable hang into a normal, retryable
  // failure (readLoop's existing catch → stop() → showReaderError()).
  const TTS_TIMEOUT_MS = 20000;

  function withTimeout(promise, ms) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`TTS timeout sau ${Math.round(ms / 1000)}s`)), ms);
      promise.then(
        (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        (e) => {
          clearTimeout(timer);
          reject(e);
        }
      );
    });
  }

  async function fetchAudioUrl(text) {
    const speed = parseFloat(localStorage.getItem('reader.speed') || '1.0');
    const model = localStorage.getItem('reader.model') || '';
    return withTimeout(fetchAudioUrlUnbounded(text, model, speed), TTS_TIMEOUT_MS);
  }

  async function fetchAudioUrlUnbounded(text, model, speed) {
    if (model === 'piper_offline') {
      await ensurePiperOfflineReady();
      const blob = await window.PiperOffline.synthesize(cleanTextForTTS(text), speed);
      return URL.createObjectURL(blob);
    }

    const response = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: cleanTextForTTS(text), speed, model: model || undefined }),
    });
    if (!response.ok) throw new Error(`TTS error: ${response.status}`);
    return URL.createObjectURL(await response.blob());
  }

  function preloadSentence(i) {
    if (i >= sentences.length || preloadCache[i]) return;
    preloadCache[i] = fetchAudioUrl(sentences[i])
      .then((url) => {
        preloadReady.add(i);
        updateProgress();
        return url;
      })
      .catch((err) => {
        preloadReady.add(i);
        updateProgress();
        throw err;
      });
  }

  function dropPreloadedAudio() {
    Object.entries(preloadCache).forEach(([k, p]) => {
      p.then((url) => URL.revokeObjectURL(url)).catch(() => {});
      delete preloadCache[k];
    });
    preloadReady.clear();
  }

  function ensureAudioElement() {
    if (!audioEl) {
      audioEl = new Audio();
      audioEl.preload = 'auto';
    }
    return audioEl;
  }

  // Builds (once) a ~0.1s silent WAV as a local blob URL — used purely to
  // "unlock" audioEl below, never actually heard.
  function buildSilentClipUrl() {
    const sampleRate = 8000;
    const numSamples = 800;
    const buffer = new ArrayBuffer(44 + numSamples * 2);
    const view = new DataView(buffer);
    view.setUint32(0, 0x46464952, true); // "RIFF"
    view.setUint32(4, 36 + numSamples * 2, true);
    view.setUint32(8, 0x45564157, true); // "WAVE"
    view.setUint32(12, 0x20746d66, true); // "fmt "
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    view.setUint32(36, 0x61746164, true); // "data"
    view.setUint32(40, numSamples * 2, true);
    // remaining bytes already zero-initialized == silence
    return URL.createObjectURL(new Blob([buffer], { type: 'audio/wav' }));
  }

  // iOS Safari only exempts LATER .play() calls that happen outside a user
  // gesture (e.g. after `await`-ing a TTS fetch) if this exact <audio>
  // element has genuinely started real playback at least once *during* a
  // gesture. Must be called synchronously from inside the tap handler,
  // before any await — playing a real (if silent) clip right here "primes"
  // audioEl so the real first-sentence .play() a moment later (once the
  // TTS fetch resolves) is no longer blocked, without needing a 2nd tap.
  function unlockAudioElement() {
    const el = ensureAudioElement();
    if (!silentClipUrl) silentClipUrl = buildSilentClipUrl();
    el.src = silentClipUrl;
    const p = el.play();
    if (p && p.catch) p.catch(() => {});
  }

  function playAudioUrl(url) {
    return new Promise((resolve, reject) => {
      ensureAudioElement();
      audioEl.src = url;
      audioEl.onended = resolve;
      audioEl.onerror = reject;
      if (!paused) {
        audioEl.play().catch((err) => {
          if (err.name === 'NotAllowedError') {
            // Browser autoplay policy blocked playback (no direct user
            // gesture yet in this document). Pause and wait instead of
            // rejecting: silently skipping ahead would blow through the
            // rest of the chapter with no audio at all. The promise stays
            // pending until the user taps ▶ (resume() re-calls .play()
            // on this same Audio, which then fires onended normally).
            paused = true;
            updatePlayBtn();
            showReaderError('Đã chặn tự phát — nhấn ▶ để tiếp tục', false);
          } else {
            reject(err);
          }
        });
      }
    });
  }

  let els = null;
  function updateProgress() {
    if (!els) return;
    els.progress.textContent = `${Math.min(index + 1, sentences.length)} / ${sentences.length}`;
    // preloadReady accumulates as each preloadSentence() call settles
    // (success or failure) — this is simply "how many sentences' audio
    // has been fetched so far", the same idea as the extension's own
    // preload counter this was ported from.
    els.preload.textContent = `${preloadReady.size} / ${sentences.length}`;
  }

  function updatePlayBtn() {
    if (!els) return;
    els.playBtn.textContent = active && !paused ? '⏸' : '▶';
  }

  // The reader-bar itself stays deliberately silent (no status text) —
  // this is the only place playback errors surface now. autoOpen defaults
  // to true (an error nobody sees isn't much better than none at all) —
  // pass false for the one case that isn't really a failure (autoplay
  // blocked on the very first tap, already visible via the ▶ button
  // itself reverting), where popping the panel open would just be noise.
  function showReaderError(message, autoOpen) {
    if (!els || !els.errorBox) return;
    els.errorBox.textContent = message;
    els.errorBox.hidden = false;
    if (autoOpen !== false) els.panel.hidden = false;
  }

  function scheduleAutoStop() {
    if (autoStopTimer) {
      clearTimeout(autoStopTimer);
      autoStopTimer = null;
    }
    const minutes = parseFloat(localStorage.getItem('reader.autoStopMinutes') || '30');
    if (!minutes || minutes <= 0) return;
    autoStopTimer = setTimeout(autoStopFire, minutes * 60000);
  }

  function clearAutoStopState() {
    if (autoStopTimer) {
      clearTimeout(autoStopTimer);
      autoStopTimer = null;
    }
  }

  function autoStopFire() {
    clearAutoStopState();
    stop();
  }

  // Reflects currentChapterNum/totalChapters onto the prev/next nav links —
  // called after every chapter load/jump since the shell ships with both
  // disabled by default (safe until we know where we actually are).
  // querySelectorAll, not querySelector — prev/next each appear twice now
  // (topbar + reader-bar), and both copies need to stay in sync.
  function updateChapterNav() {
    const hasPrev = currentChapterNum > 0;
    const hasNext = currentChapterNum < totalChapters - 1;
    document.querySelectorAll('[data-chapter-nav="prev"]').forEach((link) => {
      link.classList.toggle('disabled', !hasPrev);
      link.href = hasPrev ? `${pad4(currentChapterNum - 1)}.html` : '#';
    });
    document.querySelectorAll('[data-chapter-nav="next"]').forEach((link) => {
      link.classList.toggle('disabled', !hasNext);
      link.href = hasNext ? `${pad4(currentChapterNum + 1)}.html` : '#';
    });
  }

  // Fetches a chapter's content fragment and injects it into <article> in
  // place — no navigation, so the audio-unlock state (and this whole
  // script's in-memory state) survives across chapters. Throws on failure;
  // callers decide how to react.
  async function goToChapter(num, prefetchedHtml) {
    let html = prefetchedHtml;
    if (html == null) {
      const resp = await fetch(`/books/${bookId}/data/${pad4(num)}.html`);
      if (!resp.ok) throw new Error(`fetch chapter failed: ${resp.status}`);
      html = await resp.text();
    }
    document.querySelector('article').innerHTML = html;

    currentChapterNum = num;
    updateChapterNav();
    const h1 = document.querySelector('article h1');
    document.title = `${h1 ? h1.textContent.trim() : ''} — ${bookTitle}`;
    history.replaceState(null, '', `/books/${bookId}/chapters/${pad4(num)}.html`);
    window.scrollTo(0, 0);
  }

  // Resolves bookId/bookTitle/totalChapters/currentChapterNum from the
  // visited URL + the book's meta.json, then loads that chapter's content
  // — this is what turns the generic shell into "this specific chapter"
  // on first load. Returns false if the URL doesn't look like a chapter
  // page at all (shouldn't happen since this script only ships on those
  // pages, but fail safe rather than throw).
  async function initChapterPage() {
    const m = window.location.pathname.match(/\/books\/(\d+)\/chapters\/(\d+)\.html$/);
    if (!m) return false;
    bookId = m[1];
    const num = parseInt(m[2], 10);

    // meta.json and this chapter's own content don't depend on each other
    // (goToChapter only ever needed the chapter number from the URL) — run
    // them in parallel rather than one after another. Offline, each fetch
    // attempt can take up to the service worker's network timeout before
    // falling back to cache (see sw.js's NETWORK_TIMEOUT_MS), so doing
    // these sequentially doubled that worst-case wait on every single
    // chapter-page load for no reason.
    const tFetch = performance.now();
    const [meta, chapterHtml] = await Promise.all([
      fetch(`/books/${bookId}/meta.json`).then((r) => {
        if (!r.ok) throw new Error(`fetch book meta failed: ${r.status}`);
        return r.json();
      }),
      fetch(`/books/${bookId}/data/${pad4(num)}.html`).then((r) => {
        if (!r.ok) throw new Error(`fetch chapter failed: ${r.status}`);
        return r.text();
      }),
    ]);
    console.log(`reader.js: meta.json + chapter fetch (parallel) took ${(performance.now() - tFetch).toFixed(0)}ms`);
    bookTitle = meta.title;
    totalChapters = meta.n;
    const bookTitleEl = document.querySelector('.book-title');
    if (bookTitleEl) bookTitleEl.textContent = bookTitle;

    const tRender = performance.now();
    await goToChapter(num, chapterHtml);
    console.log(`reader.js: goToChapter() render took ${(performance.now() - tRender).toFixed(0)}ms`);
    return true;
  }

  async function readLoop() {
    while (active) {
      while (active && index < sentences.length) {
        while (paused && active) await new Promise((r) => setTimeout(r, 100));
        if (!active) break;

        const i = index;
        if (i === 0) prefetchNextChapterIfNeeded();
        if (!preloadCache[i]) preloadSentence(i);

        let url;
        try {
          url = await preloadCache[i];
        } catch (e) {
          console.error(
            'reader.js: TTS fetch failed for sentence',
            i,
            '::',
            (e && e.name) || typeof e,
            (e && e.message) || String(e),
            '::stack::',
            (e && e.stack) || 'n/a'
          );
          // Stop outright instead of skipping ahead — this used to be
          // `index++; continue`, which on a persistent failure (the whole
          // point of PRELOAD_AHEAD preloading several sentences at once)
          // blew through the rest of the chapter in seconds with nothing
          // ever actually played. index is left where it is so pressing
          // ▶ again retries this exact sentence instead of skipping it.
          showReaderError(`Lỗi đọc câu ${i + 1}: ${(e && e.message) || e}`);
          clearAutoStopState();
          stop();
          return;
        }
        if (!active) break;

        highlightSentence(i);
        updateProgress();
        for (let k = 1; k <= PRELOAD_AHEAD; k++) preloadSentence(i + k);

        try {
          await playAudioUrl(url);
        } catch (e) {
          /* skip to next sentence on playback error */
        }
        URL.revokeObjectURL(url);
        delete preloadCache[i];
        index++;
      }
      if (!active) break;

      // Chapter finished — auto-advance in place if enabled, otherwise end
      // the session here.
      const autoNext = localStorage.getItem('reader.autoNext') === '1';
      const hasNext = currentChapterNum < totalChapters - 1;
      if (!(autoNext && hasNext)) break;

      const nextNum = currentChapterNum + 1;
      const prefetched = nextChapterPrefetch && nextChapterPrefetch.num === nextNum ? nextChapterPrefetch : null;
      nextChapterPrefetch = null;

      try {
        await goToChapter(nextNum, prefetched ? prefetched.html ?? undefined : undefined);
      } catch (e) {
        console.error('reader.js: auto-advance failed to load next chapter ::', (e && e.message) || e);
        showReaderError(`Lỗi tải chương tiếp theo: ${(e && e.message) || e}`);
        break;
      }
      dropPreloadedAudio(); // cached audio was keyed by the old chapter's sentence indices
      sentences = wrapContentSentences(document.querySelector('article'));
      index = 0;
      postProgress(nextNum, 0);
      if (!sentences.length) break;
      // Reuse the prefetched first-sentence audio (if it's ready) instead of
      // starting a fresh fetch here — keeps the upcoming .play() close to
      // the previous sentence's `ended` event, same reason as the prefetch
      // itself (see nextChapterPrefetch comment near the top of the file).
      if (prefetched && prefetched.audioPromise) {
        preloadCache[0] = prefetched.audioPromise
          .then((url) => {
            preloadReady.add(0);
            updateProgress();
            return url;
          })
          .catch((err) => {
            preloadReady.add(0);
            updateProgress();
            throw err;
          });
      }
      updateMediaSessionMetadata();
    }

    clearAutoStopState();
    stop();
  }

  function start() {
    if (!sentences.length) {
      sentences = wrapContentSentences(document.querySelector('article'));
    }
    if (!sentences.length) return;
    active = true;
    paused = false;
    updatePlayBtn();
    scheduleAutoStop();
    updateMediaSessionMetadata();
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
    readLoop();
  }

  function pause() {
    paused = true;
    if (audioEl) audioEl.pause();
    updatePlayBtn();
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
    postProgress(currentChapterNum, index);
  }

  function resume() {
    paused = false;
    if (audioEl) audioEl.play().catch(() => {});
    updatePlayBtn();
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
  }

  function stop() {
    active = false;
    paused = false;
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'none';
    // audioEl itself is kept alive (not nulled) across stop/start within the
    // same page — that's what preserves iOS's "this element already played
    // due to a user gesture" autoplay allowance for the rest of the session.
    if (audioEl) audioEl.pause();
    dropPreloadedAudio();
    updatePlayBtn();
    postProgress(currentChapterNum, index);
  }

  // Escape / manual play-button-triggered stop ends the listening session
  // outright, clearing the sleep timer rather than letting it keep running
  // with nothing playing.
  function manualStop() {
    clearAutoStopState();
    stop();
  }

  // Manual "‹ Chương trước" / "Chương tiếp theo ›" clicks (and the lock-
  // screen prev/next-track buttons, see setupMediaSession) go through the
  // same in-place fetch as auto-advance — not a real navigation. Mainly for
  // iOS playback continuity if the user jumps chapters mid-session, but
  // it's a nicer instant transition either way.
  async function jumpToChapterNum(num) {
    if (num < 0 || num >= totalChapters) return;
    const wasActive = active;
    if (wasActive) stop();
    nextChapterPrefetch = null; // was for the old currentChapterNum + 1, now stale

    try {
      await goToChapter(num);
    } catch (err) {
      return; // leave the old content in place; user can retry
    }
    sentences = [];
    index = 0;
    postProgress(num, 0);
    if (wasActive) start();
  }

  // Delegated on `document` since the nav links' disabled state/handlers
  // need to keep working across every goToChapter() content swap.
  function setupChapterNavInterception() {
    document.addEventListener('click', (e) => {
      const link = e.target.closest('[data-chapter-nav]');
      if (!link || link.classList.contains('disabled')) return;
      e.preventDefault();
      jumpToChapterNum(currentChapterNum + (link.dataset.chapterNav === 'next' ? 1 : -1));
    });
  }

  // Lock-screen / control-center "now playing" info + hardware/software
  // media keys (works while the screen is locked or another app is in
  // foreground) — same mechanism native podcast/music apps use.
  function updateMediaSessionMetadata() {
    if (!('mediaSession' in navigator)) return;
    const chapterTitle = document.querySelector('article h1')?.textContent.trim() || document.title;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: chapterTitle,
      artist: bookTitle || 'Thư viện truyện',
      album: 'Thư viện truyện',
      artwork: [{ src: '/assets/icons/icon-512.png', sizes: '512x512', type: 'image/png' }],
    });
  }

  function setupMediaSession() {
    if (!('mediaSession' in navigator)) return;
    navigator.mediaSession.setActionHandler('play', () => {
      if (!active) start();
      else if (paused) resume();
    });
    navigator.mediaSession.setActionHandler('pause', () => {
      if (active && !paused) pause();
    });
    navigator.mediaSession.setActionHandler('nexttrack', () => jumpToChapterNum(currentChapterNum + 1));
    navigator.mediaSession.setActionHandler('previoustrack', () => jumpToChapterNum(currentChapterNum - 1));
  }

  // Shows/hides the model box in the settings panel depending on whether
  // "Piper (offline)" is the selected voice, and just reflects state —
  // selecting the voice does NOT itself download anything. The actual
  // download only starts from ensurePiperOfflineReady(), called when ▶ is
  // pressed and audio is actually about to be synthesized; the button
  // here only ever offers "Xoá" once a model is present.
  async function refreshPiperOfflineBox() {
    if (!els) return;
    const isOffline = els.model.value === 'piper_offline';
    els.piperOfflineBox.hidden = !isOffline;
    if (!isOffline || !window.PiperOffline) return;

    els.piperOfflineProgress.hidden = true;
    const downloaded = await window.PiperOffline.isDownloaded();
    if (downloaded) {
      els.piperOfflineStatus.textContent = 'Đã tải — dùng được offline';
      els.piperOfflineBtn.hidden = false;
      els.piperOfflineBtn.disabled = false;
      els.piperOfflineBtn.textContent = 'Xoá model';
      // Load the model now (page load, or whenever this voice becomes
      // selected) instead of waiting for the first ▶ press to pay for it —
      // ensureModelLoaded() inside the worker is a no-op once already
      // loaded, so calling this repeatedly (this function re-runs on every
      // settings-panel refresh) costs nothing after the first real call.
      window.PiperOffline.warmUp().catch((e) => {
        console.warn('reader.js: Piper offline warm-up failed ::', (e && e.message) || e);
      });
    } else {
      els.piperOfflineStatus.textContent = 'Chưa tải — sẽ tự tải khi bấm ▶ (~93MB)';
      els.piperOfflineBtn.hidden = true;
    }
  }

  // Kicks off the model download the first time it's actually needed
  // (first ▶ press with this voice selected) and lets every subsequent
  // fetchAudioUrl() call piggyback on the same in-flight download instead
  // of starting a second one.
  // Progress only ever shows inside the settings panel's piperOfflineBox —
  // deliberately never mirrored into the bottom bar's small status text.
  // That text used to get a frequently-changing "Đang tải model… NN%"
  // string during download, and since the bar's width used to be
  // content-driven (see .reader-bar CSS), that made the whole centered
  // bar visibly shift left-right on every percentage update.
  function ensurePiperOfflineReady() {
    if (!window.PiperOffline) return Promise.reject(new Error('Piper offline chưa sẵn sàng'));
    if (!piperOfflineDownloadPromise) {
      piperOfflineDownloadPromise = window.PiperOffline.isDownloaded().then((downloaded) => {
        if (downloaded) return;
        if (els) {
          els.piperOfflineBtn.hidden = true;
          els.piperOfflineProgress.hidden = false;
          els.piperOfflineStatus.textContent = 'Đang tải model (~93MB)… 0%';
        }
        return window.PiperOffline.download((loaded, total) => {
          const pct = total ? Math.round((loaded / total) * 100) : 0;
          if (els) {
            els.piperOfflineStatus.textContent = `Đang tải model… ${pct}%`;
            els.piperOfflineProgress.querySelector('.dl-progress-bar').style.width = `${pct}%`;
          }
        });
      });
      piperOfflineDownloadPromise
        .then(() => {
          // Deliberately NOT reset to null here — once resolved, every
          // future ensurePiperOfflineReady() call (one per sentence, for
          // the rest of the reading session) just reuses this same
          // already-resolved promise for free. Resetting it used to force
          // a fresh isDownloaded() re-check per sentence, and something
          // about that path caused the 63MB model to be re-fetched over
          // and over (seen live as dozens of repeated/canceled requests).
          refreshPiperOfflineBox();
        })
        .catch((err) => {
          piperOfflineDownloadPromise = null; // let the next ▶ press retry
          if (els) {
            els.piperOfflineStatus.textContent = 'Tải lỗi — bấm ▶ để thử lại';
            els.piperOfflineProgress.hidden = true;
          }
          throw err;
        });
    }
    return piperOfflineDownloadPromise;
  }

  function buildControlBar() {
    const bar = document.createElement('div');
    bar.className = 'reader-bar';
    bar.innerHTML = `
      <button class="reader-btn" data-role="play">▶</button>
      <span class="reader-progress">
        <span data-role="progress">0 / 0</span>
        <span class="reader-preload" data-role="preload" title="Số câu đã tải trước">0 / 0</span>
      </span>
      <button class="reader-btn reader-btn-ghost" data-role="settingsToggle" title="Cài đặt đọc">⚙</button>
    `;

    const panel = document.createElement('div');
    panel.className = 'reader-settings';
    panel.hidden = true;
    panel.innerHTML = `
      <div class="msg err" data-role="errorBox" hidden></div>
      <label>Giọng đọc
        <select data-role="model">
          <option value="piper_vi">Piper VN</option>
          <option value="google_tts">Google Cloud TTS</option>
          <option value="vieneu">VieNeu-TTS</option>
          <option value="piper_offline">Piper (offline)</option>
        </select>
      </label>
      <div class="reader-piper-offline" data-role="piperOfflineBox" hidden>
        <span data-role="piperOfflineStatus"></span>
        <button type="button" class="dl-btn" data-role="piperOfflineBtn"></button>
        <div class="dl-progress" data-role="piperOfflineProgress" hidden><div class="dl-progress-bar"></div></div>
      </div>
      <label>Tốc độ
        <select data-role="speed">
          <option value="0.85">0.85x</option>
          <option value="1">1x</option>
          <option value="1.15">1.15x</option>
          <option value="1.3">1.3x</option>
          <option value="1.5">1.5x</option>
        </select>
      </label>
      <label class="reader-settings-checkbox">
        <input type="checkbox" data-role="autoNext">
        Tự động sang chương tiếp khi đọc xong
      </label>
      <label>Tự dừng sau (phút, 0 = không bao giờ)
        <input type="number" data-role="autoStopMinutes" min="0" max="600" step="5">
      </label>
    `;

    document.body.appendChild(bar);
    document.body.appendChild(panel);

    els = {
      playBtn: bar.querySelector('[data-role="play"]'),
      progress: bar.querySelector('[data-role="progress"]'),
      preload: bar.querySelector('[data-role="preload"]'),
      settingsToggle: bar.querySelector('[data-role="settingsToggle"]'),
      errorBox: panel.querySelector('[data-role="errorBox"]'),
      panel,
      model: panel.querySelector('[data-role="model"]'),
      speed: panel.querySelector('[data-role="speed"]'),
      autoNext: panel.querySelector('[data-role="autoNext"]'),
      autoStopMinutes: panel.querySelector('[data-role="autoStopMinutes"]'),
      piperOfflineBox: panel.querySelector('[data-role="piperOfflineBox"]'),
      piperOfflineStatus: panel.querySelector('[data-role="piperOfflineStatus"]'),
      piperOfflineBtn: panel.querySelector('[data-role="piperOfflineBtn"]'),
      piperOfflineProgress: panel.querySelector('[data-role="piperOfflineProgress"]'),
    };

    els.model.value = localStorage.getItem('reader.model') || 'piper_vi';
    els.model.addEventListener('change', () => {
      localStorage.setItem('reader.model', els.model.value);
      // Already-preloaded/cached audio was generated with the old model —
      // drop it so the next sentence picks up the newly selected voice
      // instead of finishing the current chapter in a mixed voice.
      dropPreloadedAudio();
      refreshPiperOfflineBox();
    });
    refreshPiperOfflineBox();

    els.speed.value = localStorage.getItem('reader.speed') || '1';
    els.speed.addEventListener('change', () => {
      localStorage.setItem('reader.speed', els.speed.value);
      dropPreloadedAudio();
    });

    els.autoNext.checked = localStorage.getItem('reader.autoNext') === '1';
    els.autoNext.addEventListener('change', () => {
      localStorage.setItem('reader.autoNext', els.autoNext.checked ? '1' : '0');
      if (els.autoNext.checked && active) prefetchNextChapterIfNeeded();
    });

    els.autoStopMinutes.value = localStorage.getItem('reader.autoStopMinutes') || '30';
    els.autoStopMinutes.addEventListener('change', () => {
      localStorage.setItem('reader.autoStopMinutes', els.autoStopMinutes.value);
      if (active) scheduleAutoStop(); // apply the new value to the running session
    });

    // Only the ⚙ icon toggles the panel — no "click outside closes it"
    // listener. That listener used to intercept taps on piperOfflineBtn
    // (right under the voice <select>, inside this same panel) before
    // its own click handler ran, since document-level click handlers see
    // the event as it bubbles up past the panel.
    els.settingsToggle.addEventListener('click', () => {
      panel.hidden = !panel.hidden;
    });

    els.playBtn.addEventListener('click', () => {
      if (!active) {
        unlockAudioElement(); // must happen synchronously in this gesture — see unlockAudioElement()
        start();
      } else if (!paused) pause();
      else resume();
    });

    // Only ever offers "Xoá model" — downloading happens automatically as
    // soon as this voice is selected (see refreshPiperOfflineBox).
    els.piperOfflineBtn.addEventListener('click', async (e) => {
      // Stopped from bubbling to the document-level "click outside closes
      // the panel" listener (see below) — on iOS Safari, taps on this
      // button (right under the voice <select>, in a position:fixed
      // panel) were closing the whole panel and never reaching this
      // handler at all, consistent with that listener winning the race.
      e.stopPropagation();
      console.warn('reader.js: piperOfflineBtn clicked, PiperOffline defined?', !!window.PiperOffline);
      if (!window.PiperOffline) return;
      els.piperOfflineBtn.disabled = true;
      try {
        await window.PiperOffline.removeModel();
        console.warn('reader.js: removeModel() resolved without throwing');
      } catch (e) {
        console.error('reader.js: xoá model Piper offline thất bại ::', (e && e.name) || typeof e, (e && e.message) || e);
        els.piperOfflineStatus.textContent = `Xoá lỗi: ${(e && e.message) || e}`;
        els.piperOfflineBtn.disabled = false;
        return;
      }
      refreshPiperOfflineBox();
      console.warn('reader.js: after refreshPiperOfflineBox, isDownloaded now:', await window.PiperOffline.isDownloaded());
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && active) manualStop();
    });

    setupChapterNavInterception();
    setupMediaSession();
  }

  async function bootstrap() {
    const tBootstrap = performance.now();
    console.log(
      `reader.js: bootstrap() starting at ${tBootstrap.toFixed(0)}ms since navigation start ` +
        `(document.readyState was "${document.readyState}")`
    );
    // Independent of chapter content (keyed by book id, not by what's in
    // <article>) — fetched in parallel rather than after, same reasoning as
    // meta.json + chapter content inside initChapterPage().
    const progressPromise = fetchProgress();
    try {
      await initChapterPage();
    } catch (e) {
      console.error('reader.js: initChapterPage() failed ::', (e && e.message) || e);
      document.querySelector('article').innerHTML = '<p>Lỗi tải nội dung chương. Thử tải lại trang.</p>';
    }
    buildControlBar();
    if (bookId) applyResumeOrBaseline(await progressPromise);
    console.log(
      `reader.js: bootstrap() done — total ${(performance.now() - tBootstrap).toFixed(0)}ms, ` +
        `${performance.now().toFixed(0)}ms since navigation start (chapter content + controls ready)`
    );
  }

  // Backgrounding the tab (switching apps, locking the screen) or closing it
  // outright are the two "session might just end here" moments a reading
  // page has that a native app's onDisappear/scenePhase covers for free —
  // sync whatever the current position is so it isn't lost.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && bookId) postProgress(currentChapterNum, index, { beacon: true });
  });
  window.addEventListener('pagehide', () => {
    if (bookId) postProgress(currentChapterNum, index, { beacon: true });
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
  } else {
    bootstrap();
  }
})();
