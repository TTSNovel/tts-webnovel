// Offline support is opt-in only — this worker never caches a response just
// because it was fetched. Two things populate Cache Storage, both explicit:
//  - download.js's "Tải xuống" button, into a "book-<id>" cache, per book.
//  - download.js's precacheAppShell(), into "app-shell-v1" — the fixed
//    handful of files (download.html, chapter-shell.html, reader.js,
//    download.js, manifest, icons) the PWA needs just to boot and show the
//    Download page with no network at all. Refreshed each time the
//    Download page loads online.
// This worker's only job is: try the network, and if that fails, look in
// whatever's already in Cache Storage for a match — it never writes to it.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

// Every "/books/<id>/chapters/<chapter>.html" URL is served by the same
// static chapter-shell.html file server-side (see server.py's
// chapter_shell route) — reuse that one cached copy for ANY chapter URL
// that isn't reachable live, instead of needing a cache entry per chapter
// URL for a file that's byte-identical across all ~3000 of them.
const CHAPTER_SHELL_RE = /^\/books\/\d+\/chapters\/[^/]+\.html$/;

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  const fallbackUrl = CHAPTER_SHELL_RE.test(url.pathname) ? '/chapter-shell.html' : req;
  event.respondWith(networkThenCacheFallback(req, fallbackUrl));
});

async function networkThenCacheFallback(request, fallback) {
  try {
    return await fetch(request);
  } catch (err) {
    // No cache name given: searches every open cache (book-<id> caches
    // and app-shell-v1 alike).
    const cached = await caches.match(fallback, { ignoreVary: true });
    if (cached) return cached;
    throw err;
  }
}
