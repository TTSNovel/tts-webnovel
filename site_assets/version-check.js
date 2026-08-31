/*
 * Home page only (book_renderer.py's _INDEX_PAGE). index.html is fully
 * server-rendered with every book baked into the HTML at build/import
 * time — there's no client-side fetch of books.json to just re-run, so a
 * browser sitting on an already-loaded tab has no way to notice a newly
 * imported book short of reloading the document itself. This polls a tiny
 * version marker (bumped by write_index() on every book add) and reloads
 * automatically when it changes, instead of relying on the user to
 * happen to hard-refresh.
 */
(function () {
  'use strict';

  const POLL_INTERVAL_MS = 3 * 60 * 1000;
  const pageVersion = document.querySelector('meta[name="site-version"]')?.content || '';

  async function checkVersion() {
    try {
      // cache: 'no-store' — this is the one request that must never be
      // answered from the browser's own HTTP cache; a cached "no change"
      // response would defeat the entire point of polling.
      const res = await fetch('/version.json', { cache: 'no-store' });
      if (!res.ok) return;
      const { v } = await res.json();
      if (v && pageVersion && v !== pageVersion) location.reload();
    } catch (e) {
      // Offline or a blip — next poll (or the next tab-focus check) tries again.
    }
  }

  setInterval(checkVersion, POLL_INTERVAL_MS);
  // Also check the moment the user actually comes back to this tab —
  // catches a book added while the tab sat in the background far more
  // promptly than waiting out the rest of the poll interval.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') checkVersion();
  });
})();
