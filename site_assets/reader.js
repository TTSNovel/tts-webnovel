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
 * served for every "/books/<slug>/chapters/<n>.html" URL (see server.py's
 * chapter_shell route) — nothing here is chapter-specific up front. On
 * load, this file parses the visited URL itself, fetches the matching
 * content fragment from books/<slug>/data/<n>.html and the book's
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
  let bookSlug = null;
  let bookTitle = '';
  let totalChapters = 0;
  let currentChapterNum = 0;

  function pad4(n) {
    return String(n).padStart(4, '0');
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
    fetch(`/books/${bookSlug}/data/${pad4(nextNum)}.html`)
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

  async function fetchAudioUrl(text) {
    const speed = parseFloat(localStorage.getItem('reader.speed') || '1.0');
    const model = localStorage.getItem('reader.model') || '';
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

  function playAudioUrl(url) {
    return new Promise((resolve, reject) => {
      if (!audioEl) {
        audioEl = new Audio();
        audioEl.preload = 'auto';
      }
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
            if (els) els.status.textContent = 'Đã chặn tự phát — nhấn ▶ để tiếp tục';
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
  }

  function updatePlayBtn() {
    if (!els) return;
    els.playBtn.textContent = active && !paused ? '⏸' : '▶';
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
    if (els) els.status.textContent = 'Đã tự dừng';
    clearAutoStopState();
    stop();
  }

  // Reflects currentChapterNum/totalChapters onto the prev/next nav links —
  // called after every chapter load/jump since the shell ships with both
  // disabled by default (safe until we know where we actually are).
  function updateChapterNav() {
    const prevLink = document.querySelector('[data-chapter-nav="prev"]');
    const nextLink = document.querySelector('[data-chapter-nav="next"]');
    const hasPrev = currentChapterNum > 0;
    const hasNext = currentChapterNum < totalChapters - 1;
    if (prevLink) {
      prevLink.classList.toggle('disabled', !hasPrev);
      prevLink.href = hasPrev ? `${pad4(currentChapterNum - 1)}.html` : '#';
    }
    if (nextLink) {
      nextLink.classList.toggle('disabled', !hasNext);
      nextLink.href = hasNext ? `${pad4(currentChapterNum + 1)}.html` : '#';
    }
  }

  // Fetches a chapter's content fragment and injects it into <article> in
  // place — no navigation, so the audio-unlock state (and this whole
  // script's in-memory state) survives across chapters. Throws on failure;
  // callers decide how to react.
  async function goToChapter(num, prefetchedHtml) {
    let html = prefetchedHtml;
    if (html == null) {
      const resp = await fetch(`/books/${bookSlug}/data/${pad4(num)}.html`);
      if (!resp.ok) throw new Error(`fetch chapter failed: ${resp.status}`);
      html = await resp.text();
    }
    document.querySelector('article').innerHTML = html;

    currentChapterNum = num;
    updateChapterNav();
    const h1 = document.querySelector('article h1');
    document.title = `${h1 ? h1.textContent.trim() : ''} — ${bookTitle}`;
    history.replaceState(null, '', `/books/${bookSlug}/chapters/${pad4(num)}.html`);
    window.scrollTo(0, 0);
  }

  // Resolves bookSlug/bookTitle/totalChapters/currentChapterNum from the
  // visited URL + the book's meta.json, then loads that chapter's content
  // — this is what turns the generic shell into "this specific chapter"
  // on first load. Returns false if the URL doesn't look like a chapter
  // page at all (shouldn't happen since this script only ships on those
  // pages, but fail safe rather than throw).
  async function initChapterPage() {
    const m = window.location.pathname.match(/\/books\/([^/]+)\/chapters\/(\d+)\.html$/);
    if (!m) return false;
    bookSlug = m[1];
    const num = parseInt(m[2], 10);

    const metaResp = await fetch(`/books/${bookSlug}/meta.json`);
    if (!metaResp.ok) throw new Error(`fetch book meta failed: ${metaResp.status}`);
    const meta = await metaResp.json();
    bookTitle = meta.title;
    totalChapters = meta.n;
    const bookTitleEl = document.querySelector('.topbar .book-title');
    if (bookTitleEl) bookTitleEl.textContent = bookTitle;

    await goToChapter(num);
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
          index++;
          continue;
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

      els.status.textContent = 'Đang chuyển chương…';
      try {
        await goToChapter(nextNum, prefetched ? prefetched.html ?? undefined : undefined);
      } catch (e) {
        els.status.textContent = 'Lỗi tải chương tiếp theo';
        break;
      }
      dropPreloadedAudio(); // cached audio was keyed by the old chapter's sentence indices
      sentences = wrapContentSentences(document.querySelector('article'));
      index = 0;
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
      els.status.textContent = '';
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
    if (els) els.status.textContent = '';
    nextChapterPrefetch = null; // was for the old currentChapterNum + 1, now stale

    try {
      await goToChapter(num);
    } catch (err) {
      return; // leave the old content in place; user can retry
    }
    sentences = [];
    index = 0;
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

  function buildControlBar() {
    const bar = document.createElement('div');
    bar.className = 'reader-bar';
    bar.innerHTML = `
      <button class="reader-btn" data-role="play">▶</button>
      <span class="reader-progress" data-role="progress">0 / 0</span>
      <span class="reader-status" data-role="status"></span>
      <button class="reader-btn reader-btn-ghost" data-role="settingsToggle" title="Cài đặt đọc">⚙</button>
    `;

    const panel = document.createElement('div');
    panel.className = 'reader-settings';
    panel.hidden = true;
    panel.innerHTML = `
      <label>Giọng đọc
        <select data-role="model">
          <option value="piper_vi">Piper VN</option>
          <option value="google_tts">Google Cloud TTS</option>
          <option value="vieneu">VieNeu-TTS</option>
        </select>
      </label>
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
      status: bar.querySelector('[data-role="status"]'),
      settingsToggle: bar.querySelector('[data-role="settingsToggle"]'),
      panel,
      model: panel.querySelector('[data-role="model"]'),
      speed: panel.querySelector('[data-role="speed"]'),
      autoNext: panel.querySelector('[data-role="autoNext"]'),
      autoStopMinutes: panel.querySelector('[data-role="autoStopMinutes"]'),
    };

    els.model.value = localStorage.getItem('reader.model') || 'piper_vi';
    els.model.addEventListener('change', () => {
      localStorage.setItem('reader.model', els.model.value);
      // Already-preloaded/cached audio was generated with the old model —
      // drop it so the next sentence picks up the newly selected voice
      // instead of finishing the current chapter in a mixed voice.
      dropPreloadedAudio();
    });

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

    els.settingsToggle.addEventListener('click', () => {
      panel.hidden = !panel.hidden;
    });
    document.addEventListener('click', (e) => {
      if (panel.hidden) return;
      if (!panel.contains(e.target) && e.target !== els.settingsToggle) panel.hidden = true;
    });

    els.playBtn.addEventListener('click', () => {
      if (!active) start();
      else if (!paused) pause();
      else resume();
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && active) manualStop();
    });

    setupChapterNavInterception();
    setupMediaSession();
  }

  async function bootstrap() {
    try {
      await initChapterPage();
    } catch (e) {
      document.querySelector('article').innerHTML = '<p>Lỗi tải nội dung chương. Thử tải lại trang.</p>';
    }
    buildControlBar();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
  } else {
    bootstrap();
  }
})();
