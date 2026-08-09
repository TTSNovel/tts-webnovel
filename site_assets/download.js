/*
 * Two things live here:
 *  - The Download page (server.py's /download.html): a library of what's
 *    already been downloaded, for reading offline. It does NOT trigger
 *    downloads — only lists them (title/author/chapter count/date) with a
 *    "Đọc" link and a "Xoá" button.
 *  - initBookPage(): the actual "Tải xuống để đọc offline" button, which
 *    lives on each book's own page (book_renderer.py's _BOOK_PAGE) — that's
 *    the only place a download gets started.
 *
 * Storage split, mirrors sw.js:
 *  - Cache Storage ("book-<id>"): the actual chapter HTML fragments +
 *    meta.json + book index.html — the bytes reader.js/goToChapter()
 *    fetches, unchanged from the online path.
 *  - localStorage ("downloadedBooks"): a small JSON registry of which
 *    books are downloaded (title/author/n/timestamp), so the Download page
 *    can render its list with no network at all.
 */
(function () {
  'use strict';

  // Stamped at deploy time — logged so a stale cached copy of this file
  // is obvious at a glance instead of a guess.
  const DOWNLOAD_JS_VERSION = '__DEPLOY_VERSION__';

  // performance.now() is relative to navigation start — this line alone
  // tells you how long the document + this script took to reach the
  // browser before any of THIS file's JS ran. It does NOT cover the time
  // from tapping the home-screen icon to navigation actually starting
  // (OS process launch, WKWebView init) — that part never reaches JS at
  // all; measuring it needs Xcode Instruments' "App Launch" template (or
  // the OS-level Console.app log) on the real device, not this console.
  console.log(
    `download.js: version ${DOWNLOAD_JS_VERSION}, script evaluated at ${performance.now().toFixed(0)}ms since navigation start`
  );

  // console.log() inside sw.js itself goes to the Service Worker's OWN
  // Inspector target — not this page's console, even though both show
  // under the same "Web Inspector" window. sw.js posts its version back
  // via postMessage specifically so it shows up HERE instead, in the
  // console actually being watched.
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (e) => {
      if (e.data && e.data.type === 'sw-version') {
        console.log(`download.js: active Service Worker reports version ${e.data.version}`);
      }
    });
    // Covers the "SW already active before this postMessage listener was
    // attached" case (sw.js's own activate handler broadcasts once, but
    // only to clients that existed AT that moment) by explicitly asking.
    if (navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage({ type: 'get-sw-version' });
    }
    const controller = navigator.serviceWorker.controller;
    console.log(
      `download.js: navigator.serviceWorker.controller state = ${controller ? controller.state : 'null (no SW controlling this page)'}`
    );
    // What the SERVER has right now — compare against the "active Service
    // Worker reports version" line above. Different values means the
    // browser hasn't updated to the latest sw.js yet.
    fetch('/sw.js', { cache: 'no-store' })
      .then((r) => r.text())
      .then((text) => {
        const m = text.match(/SW_VERSION = '([^']+)'/);
        console.log(`download.js: /sw.js on server right now is version ${m ? m[1] : '(not found in fetched text)'}`);
      })
      .catch((e) => console.log('download.js: could not fetch /sw.js to check its version ::', e.message));
  }
  // Safari's Paint Timing support is inconsistent across versions — guarded
  // rather than assumed. When present, first-contentful-paint is the
  // closest thing to "when the user actually saw the dashboard".
  if (window.PerformancePaintTiming || performance.getEntriesByType) {
    try {
      const fcp = performance.getEntriesByType('paint').find((e) => e.name === 'first-contentful-paint');
      if (fcp) console.log(`download.js: first-contentful-paint at ${fcp.startTime.toFixed(0)}ms`);
    } catch (e) {
      /* Paint Timing not supported on this browser — skip */
    }
  }

  const REGISTRY_KEY = 'downloadedBooks';
  const CONCURRENCY = 6;

  // Fixed set of files the PWA itself needs to boot + render the Download
  // page with no network — deliberately NOT "whatever pages happen to get
  // visited". Book content only ever enters Cache Storage via
  // downloadBook(), triggered explicitly from a book's own page.
  const APP_SHELL_CACHE = 'app-shell-v1';
  const APP_SHELL_FILES = [
    '/',
    '/download.html',
    '/chapter-shell.html',
    '/manifest.json',
    '/assets/reader.js',
    '/assets/download.js',
    '/assets/piper-offline.js',
    '/assets/icons/icon-192.png',
    '/assets/icons/icon-512.png',
    '/assets/icons/icon-180.png',
  ];

  // Best-effort, re-run every time any page loads while online — cheap,
  // and means the offline entry point stays fresh with no dedicated UI or
  // button of its own.
  //
  // navigator.onLine is skipped as a signal elsewhere in this file
  // (initHomePage's own comment explains why) because it's unreliable for
  // deciding "is offline content available" — a false positive there
  // would wrongly skip showing content that's actually cached. Here the
  // stakes are the opposite: this is 9 fetches nobody is waiting on, and
  // skipping them by mistake just means the app-shell cache goes one page
  // load without refreshing, not a wrong answer shown to the user. So a
  // wrong "online" reading just means the calls run and fail as before
  // (no worse than not checking at all), while a wrong "offline" reading
  // usefully avoids saturating the browser's small per-origin connection
  // pool with 9 requests that would otherwise sit competing with the
  // actual page's own meta.json/chapter fetches for the network timeout.
  async function precacheAppShell() {
    if (navigator.onLine === false) return;
    const t0 = performance.now();
    const cache = await caches.open(APP_SHELL_CACHE);
    const results = await Promise.allSettled(
      APP_SHELL_FILES.map(async (url) => {
        const resp = await fetch(url);
        if (resp.ok) await cache.put(url, resp);
        else throw new Error(`${url} -> ${resp.status}`);
      })
    );
    const failed = results.filter((r) => r.status === 'rejected');
    console.log(
      `download.js: precacheAppShell() took ${(performance.now() - t0).toFixed(0)}ms for ${APP_SHELL_FILES.length} files` +
        (failed.length ? `, ${failed.length} failed: ${failed.map((r) => r.reason.message).join('; ')}` : '')
    );
  }

  function pad4(n) {
    return String(n).padStart(4, '0');
  }

  function loadRegistry() {
    try {
      return JSON.parse(localStorage.getItem(REGISTRY_KEY) || '{}');
    } catch (e) {
      return {};
    }
  }

  function saveRegistry(reg) {
    localStorage.setItem(REGISTRY_KEY, JSON.stringify(reg));
  }

  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function formatDate(ts) {
    return new Date(ts).toLocaleDateString('vi-VN');
  }

  // Fetches meta.json + the book's chapter-list page + every chapter
  // fragment, writing each straight into a cache dedicated to this book.
  // Registry is only updated on full success so a partial/failed download
  // never gets presented as "ready to read offline".
  //
  // Also doubles as the "update" path for an already-downloaded book (see
  // the Download page's "Cập nhật" button): meta.json/index.html/cover are
  // always re-fetched, and the chapter loop below skips whatever's already
  // cached — so re-running this on a book downloaded before some field
  // (e.g. cover) existed just backfills the gap, without re-fetching
  // hundreds/thousands of already-cached chapters over again.
  async function downloadBook(bookId, onProgress) {
    const cache = await caches.open('book-' + bookId);

    const metaResp = await fetch(`/books/${bookId}/meta.json`);
    if (!metaResp.ok) throw new Error('meta.json lỗi');
    const meta = await metaResp.clone().json();
    await cache.put(`/books/${bookId}/meta.json`, metaResp);

    const indexResp = await fetch(`/books/${bookId}/index.html`);
    if (indexResp.ok) await cache.put(`/books/${bookId}/index.html`, indexResp);

    // The book page's <img> references this by a plain relative filename
    // ("cover.jpg"), so it has to land in the cache under that exact same
    // "/books/<id>/<cover>" URL for the offline fallback to resolve it —
    // same reasoning as index.html/meta.json above. Not every book has
    // one (older imports predate cover extraction), so this is best-effort
    // and doesn't fail the whole download if missing.
    if (meta.cover) {
      const coverUrl = `/books/${bookId}/${meta.cover}`;
      try {
        const coverResp = await fetch(coverUrl);
        if (coverResp.ok) await cache.put(coverUrl, coverResp);
      } catch (e) {
        /* best-effort — a missing cover offline just falls back to no image */
      }
    }

    const n = meta.n;
    const queue = Array.from({ length: n }, (_, i) => i);
    let done = 0;
    let failed = 0;

    async function worker() {
      while (queue.length) {
        const i = queue.shift();
        const url = `/books/${bookId}/data/${pad4(i)}.html`;
        // Already cached (either a previous successful download, or this
        // exact call re-run as an "update") — nothing to do. This is what
        // makes calling downloadBook() again on an already-downloaded book
        // cheap: only meta.json/index.html/cover (always re-fetched above)
        // and any genuinely missing chapters do real network work, instead
        // of re-fetching all n chapters just to pick up a metadata change.
        if (await cache.match(url)) {
          done++;
          if (onProgress) onProgress(done, n);
          continue;
        }
        try {
          const resp = await fetch(url);
          if (resp.ok) await cache.put(url, resp);
          else failed++;
        } catch (e) {
          failed++;
        }
        done++;
        if (onProgress) onProgress(done, n);
      }
    }
    await Promise.all(Array.from({ length: Math.min(CONCURRENCY, n) }, worker));

    if (failed > 0) throw new Error(`${failed}/${n} chương tải lỗi`);

    const reg = loadRegistry();
    reg[bookId] = {
      title: meta.title,
      author: meta.author || null,
      category: meta.category,
      cover: meta.cover || null,
      n,
      downloadedAt: Date.now(),
    };
    saveRegistry(reg);
  }

  async function deleteBook(bookId) {
    await caches.delete('book-' + bookId);
    const reg = loadRegistry();
    delete reg[bookId];
    saveRegistry(reg);
  }

  // Same .book-card look as the home page's catalog grid (cover, title,
  // author, meta) — the only addition is the Cập nhật/Xoá button row right
  // under the cover, since that's the one thing the home page card doesn't
  // need. entry.cover was cached (see downloadBook()) at this exact same
  // "/books/<id>/<cover>" URL, so this <img> resolves from Cache Storage
  // offline too — no separate offline-fallback path needed for it.
  function downloadedRowHtml(bookId, entry) {
    const authorHtml = entry.author ? `<span class="author">${escapeHtml(entry.author)}</span>` : '';
    const bookHref = `/books/${encodeURIComponent(bookId)}/index.html`;
    const coverHtml = entry.cover
      ? `<img class="cover" src="/books/${encodeURIComponent(bookId)}/${encodeURIComponent(entry.cover)}" alt="" loading="lazy">`
      : '<div class="cover"></div>';
    return `
      <div class="book-card dl-card" data-book-id="${escapeHtml(bookId)}">
        <a href="${bookHref}" tabindex="-1">${coverHtml}</a>
        <div class="dl-card-actions">
          <button class="dl-btn" data-action="update" data-book-id="${escapeHtml(bookId)}" title="Tải lại phần còn thiếu (VD: ảnh bìa)">Cập nhật</button>
          <button class="dl-btn dl-btn-danger" data-action="delete" data-book-id="${escapeHtml(bookId)}">Xoá</button>
        </div>
        <a class="title" href="${bookHref}">${escapeHtml(entry.title)}</a>
        ${authorHtml}
        <span class="dl-meta">${entry.n} chương · đã tải ${formatDate(entry.downloadedAt)}</span>
        <div class="dl-progress" data-role="row-progress" hidden><div class="dl-progress-bar"></div></div>
      </div>`;
  }

  function render() {
    const root = document.getElementById('download-app');
    const registry = loadRegistry();
    const bookIds = Object.keys(registry);

    if (!bookIds.length) {
      root.innerHTML = '<p class="dl-hint">Chưa tải cuốn nào. Vào trang một cuốn sách và bấm "Tải xuống để đọc offline".</p>';
      return;
    }
    root.innerHTML =
      '<div class="dl-list-actions"><button class="dl-btn" data-action="update-all">Cập nhật tất cả</button></div>' +
      '<div class="book-grid">' + bookIds.map((bookId) => downloadedRowHtml(bookId, registry[bookId])).join('') + '</div>';
    wireRowActions(root);
  }

  // Runs downloadBook() again for one row's book — cheap (see downloadBook's
  // own comment: skips chapters already cached), used both by a single
  // row's "Cập nhật" button and by "Cập nhật tất cả" looping over every row.
  async function updateRow(root, bookId) {
    const row = root.querySelector(`.dl-card[data-book-id="${bookId}"]`);
    const updateBtn = row && row.querySelector('[data-action="update"]');
    const deleteBtn = row && row.querySelector('[data-action="delete"]');
    const progressEl = row && row.querySelector('[data-role="row-progress"]');
    const barEl = progressEl && progressEl.querySelector('.dl-progress-bar');
    if (updateBtn) updateBtn.disabled = true;
    if (deleteBtn) deleteBtn.disabled = true;
    if (progressEl) progressEl.hidden = false;
    try {
      await downloadBook(bookId, (done, total) => {
        if (barEl) barEl.style.width = `${Math.round((done / total) * 100)}%`;
      });
    } catch (err) {
      console.error('download.js: cập nhật thất bại ::', bookId, (err && err.message) || err);
    } finally {
      if (progressEl) progressEl.hidden = true;
      if (updateBtn) updateBtn.disabled = false;
      if (deleteBtn) deleteBtn.disabled = false;
    }
  }

  function wireRowActions(root) {
    root.addEventListener('click', async (e) => {
      const deleteBtn = e.target.closest('[data-action="delete"]');
      if (deleteBtn) {
        deleteBtn.disabled = true;
        await deleteBook(deleteBtn.dataset.bookId);
        render();
        return;
      }

      const updateBtn = e.target.closest('[data-action="update"]');
      if (updateBtn) {
        await updateRow(root, updateBtn.dataset.bookId);
        render(); // title/cover/n may have changed since this book was first downloaded
        return;
      }

      const updateAllBtn = e.target.closest('[data-action="update-all"]');
      if (updateAllBtn) {
        updateAllBtn.disabled = true;
        const bookIds = Object.keys(loadRegistry());
        // Sequential, not parallel — these can be large books (thousands of
        // chapters); running them all at once would multiply CONCURRENCY
        // across every book at the same time for no real benefit, since
        // the slow part (network) is already saturated by one book alone.
        for (const bookId of bookIds) await updateRow(root, bookId);
        render();
      }
    });
  }

  // The actual "Tải xuống để đọc offline" button, on a book's own page
  // (book_renderer.py's _BOOK_PAGE) — the only place a download starts.
  function initBookPage() {
    const btn = document.getElementById('book-dl-btn');
    if (!btn) return;
    const bookId = btn.dataset.bookId;
    const progressEl = document.getElementById('book-dl-progress');
    const barEl = progressEl && progressEl.querySelector('.dl-progress-bar');

    function reflect() {
      const downloaded = loadRegistry()[bookId];
      btn.disabled = false;
      if (progressEl) progressEl.hidden = true;
      if (downloaded) {
        btn.textContent = '✓ Xoá bản tải';
        btn.classList.add('dl-btn-danger');
        btn.onclick = async () => {
          btn.disabled = true;
          await deleteBook(bookId);
          reflect();
        };
      } else {
        btn.textContent = 'Tải xuống để đọc offline';
        btn.classList.remove('dl-btn-danger');
        btn.onclick = async () => {
          btn.disabled = true;
          if (progressEl) progressEl.hidden = false;
          try {
            await downloadBook(bookId, (done, total) => {
              btn.textContent = `Đang tải… ${done}/${total}`;
              if (barEl) barEl.style.width = `${Math.round((done / total) * 100)}%`;
            });
          } catch (err) {
            btn.disabled = false;
            btn.textContent = 'Lỗi — bấm để thử lại';
            return;
          }
          reflect();
        };
      }
    }
    reflect();
  }

  // Home page (server.py's "/", the PWA start_url): when there's genuinely
  // no network, swap the (possibly stale, cached-by-precacheAppShell)
  // catalog grid for the same downloaded-books list as the Download page
  // — otherwise a fully offline app opens straight into a dead end. Uses
  // a real fetch as the offline signal rather than navigator.onLine, which
  // is unreliable — books.json is never itself cached, so this fetch only
  // succeeds when the network is actually reachable.
  //
  // navigator.onLine === false is still checked FIRST, as a short-circuit
  // only (never treated as "definitely online"): real-device testing found
  // that fetch()'s own timeout doesn't reliably bound this when the network
  // interface is fully off (see sw.js's isOnline() for the same fix and the
  // measured reason why) — skipping straight to the offline branch here
  // avoids waiting on a fetch already known to be doomed.
  //
  // The grid's own <img class="cover"> tags are rendered with data-src,
  // not src (see book_renderer.py's cover_html()) — SPECIFICALLY so the
  // browser never fires all of them on its own the moment the HTML
  // parses. Measured live on a real device: 30+ book covers all queued
  // at once (most still "near" the viewport even with loading="lazy",
  // which only defers off-screen ones) with no network available took
  // ~4.5s of pure queueing before the page did anything else — every
  // other part of the load was fast. This function only resolves them to
  // real src once this SAME online check confirms the network is
  // actually there; offline, the whole grid is replaced below anyway
  // (with real covers for the much shorter downloaded-books list), so
  // there's nothing to resolve — the deferred covers are simply discarded
  // having never attempted a single network request.
  function initHomePage() {
    const wrap = document.getElementById('home-wrap');
    if (!wrap) return;
    const showOfflineGrid = () => {
      const registry = loadRegistry();
      const bookIds = Object.keys(registry);
      wrap.innerHTML = bookIds.length
        ? '<p class="dl-hint">Đang offline — chỉ hiện các truyện đã tải xuống.</p><div class="book-grid">' +
          bookIds.map((bookId) => downloadedRowHtml(bookId, registry[bookId])).join('') +
          '</div>'
        : '<p class="dl-hint">Đang offline — chưa có truyện nào được tải xuống.</p>';
    };
    if (navigator.onLine === false) {
      showOfflineGrid();
      return;
    }
    fetch('/books.json', { cache: 'no-store' })
      .then(() => {
        wrap.querySelectorAll('img.cover[data-src]').forEach((img) => {
          img.src = img.dataset.src;
          img.removeAttribute('data-src');
        });
      })
      .catch(showOfflineGrid);
  }

  // Big, easy-to-hit "Xoá model Piper offline" card on the Download page —
  // reader.js's settings panel also has a smaller one, but this is the
  // reliable place to reach for it specifically because it's not crammed
  // into a small popup.
  async function initPiperOfflinePage() {
    const statusEl = document.getElementById('piper-offline-status');
    const btn = document.getElementById('piper-offline-delete');
    if (!statusEl || !btn || !window.PiperOffline) return;

    async function refresh() {
      const downloaded = await window.PiperOffline.isDownloaded();
      statusEl.textContent = downloaded ? 'Model đã tải — dùng được offline.' : 'Chưa tải model Piper offline.';
      btn.hidden = !downloaded;
      btn.disabled = false;
    }

    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await window.PiperOffline.removeModel();
      } catch (e) {
        console.error('download.js: xoá model Piper offline thất bại ::', (e && e.message) || e);
        statusEl.textContent = `Xoá lỗi: ${(e && e.message) || e}`;
        btn.disabled = false;
        return;
      }
      await refresh();
    });

    await refresh();
  }

  function bootstrap() {
    console.log(
      `download.js: bootstrap() starting at ${performance.now().toFixed(0)}ms since navigation start ` +
        `(document.readyState was "${document.readyState}")`
    );
    precacheAppShell(); // best-effort on every page load; each file fetch fails silently if offline
    if (document.getElementById('download-app')) render();
    initBookPage();
    initHomePage();
    initPiperOfflinePage();
    console.log(`download.js: bootstrap() synchronous part done at ${performance.now().toFixed(0)}ms`);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootstrap);
  else bootstrap();
})();
