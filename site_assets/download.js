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
    '/assets/icons/icon-192.png',
    '/assets/icons/icon-512.png',
    '/assets/icons/icon-180.png',
  ];

  // Best-effort, re-run every time the Download page loads while online —
  // cheap, and means the offline entry point stays fresh with no dedicated
  // UI or button of its own.
  async function precacheAppShell() {
    const cache = await caches.open(APP_SHELL_CACHE);
    await Promise.allSettled(
      APP_SHELL_FILES.map(async (url) => {
        const resp = await fetch(url);
        if (resp.ok) await cache.put(url, resp);
      })
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
  async function downloadBook(bookId, onProgress) {
    const cache = await caches.open('book-' + bookId);

    const metaResp = await fetch(`/books/${bookId}/meta.json`);
    if (!metaResp.ok) throw new Error('meta.json lỗi');
    const meta = await metaResp.clone().json();
    await cache.put(`/books/${bookId}/meta.json`, metaResp);

    const indexResp = await fetch(`/books/${bookId}/index.html`);
    if (indexResp.ok) await cache.put(`/books/${bookId}/index.html`, indexResp);

    const n = meta.n;
    const queue = Array.from({ length: n }, (_, i) => i);
    let done = 0;
    let failed = 0;

    async function worker() {
      while (queue.length) {
        const i = queue.shift();
        const url = `/books/${bookId}/data/${pad4(i)}.html`;
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

  function downloadedRowHtml(bookId, entry) {
    const authorHtml = entry.author ? `<span class="dl-author">${escapeHtml(entry.author)}</span>` : '';
    return `
      <div class="dl-row" data-book-id="${escapeHtml(bookId)}">
        <div class="dl-info">
          <a class="dl-title" href="/books/${encodeURIComponent(bookId)}/index.html">${escapeHtml(entry.title)}</a>
          ${authorHtml}
          <span class="dl-meta">${entry.n} chương · đã tải ${formatDate(entry.downloadedAt)}</span>
        </div>
        <button class="dl-btn dl-btn-danger" data-action="delete" data-book-id="${escapeHtml(bookId)}">Xoá</button>
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
      '<div class="dl-list">' + bookIds.map((bookId) => downloadedRowHtml(bookId, registry[bookId])).join('') + '</div>';
    wireDeleteButtons(root);
  }

  function wireDeleteButtons(root) {
    root.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-action="delete"]');
      if (!btn) return;
      btn.disabled = true;
      await deleteBook(btn.dataset.bookId);
      render();
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
  function initHomePage() {
    const wrap = document.getElementById('home-wrap');
    if (!wrap) return;
    fetch('/books.json', { cache: 'no-store' }).catch(() => {
      const registry = loadRegistry();
      const bookIds = Object.keys(registry);
      wrap.innerHTML = bookIds.length
        ? '<p class="dl-hint">Đang offline — chỉ hiện các truyện đã tải xuống.</p><div class="dl-list">' +
          bookIds.map((bookId) => downloadedRowHtml(bookId, registry[bookId])).join('') +
          '</div>'
        : '<p class="dl-hint">Đang offline — chưa có truyện nào được tải xuống.</p>';
    });
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
    precacheAppShell(); // best-effort on every page load; each file fetch fails silently if offline
    if (document.getElementById('download-app')) render();
    initBookPage();
    initHomePage();
    initPiperOfflinePage();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootstrap);
  else bootstrap();
})();
