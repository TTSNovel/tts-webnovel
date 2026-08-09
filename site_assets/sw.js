// Offline support is opt-in only — this worker never caches a response just
// because it was fetched. Two things populate Cache Storage, both explicit:
//  - download.js's "Tải xuống" button, into a "book-<id>" cache, per book.
//  - download.js's precacheAppShell(), into "app-shell-v1" — the fixed
//    handful of files (download.html, chapter-shell.html, reader.js,
//    download.js, manifest, icons) the PWA needs just to boot and show the
//    Download page with no network at all. Refreshed each time the
//    Download page loads online.
//
// SW_VERSION is stamped at deploy time (see build/deploy step) — logged so
// a stale cached copy is obvious at a glance instead of a guess.
const SW_VERSION = '__DEPLOY_VERSION__';
console.log(`sw.js: version ${SW_VERSION} evaluating`);

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) =>
  e.waitUntil(
    self.clients.claim().then(async () => {
      // console.log() in this file goes to the Service Worker's OWN
      // Inspector target, not the page's — postMessage puts the version
      // somewhere download.js can actually log it from the console
      // that's already open on the page.
      const clients = await self.clients.matchAll();
      clients.forEach((client) => client.postMessage({ type: 'sw-version', version: SW_VERSION }));
    })
  )
);
// A page that was already open (not just newly navigated) when this SW
// activates won't get the message above until ITS next activate — this
// covers that page by replying to an explicit request instead of only
// broadcasting once on our own activation.
self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'get-sw-version') {
    e.source.postMessage({ type: 'sw-version', version: SW_VERSION });
  }
});

// Every "/books/<id>/chapters/<chapter>.html" URL is served by the same
// static chapter-shell.html file server-side (see server.py's
// chapter_shell route) — reuse that one cached copy for ANY chapter URL
// that isn't reachable live, instead of needing a cache entry per chapter
// URL for a file that's byte-identical across all ~3000 of them.
const CHAPTER_SHELL_RE = /^\/books\/\d+\/chapters\/[^/]+\.html$/;
const BOOK_URL_RE = /^\/books\/(\d+)\//;
const APP_SHELL_CACHE = 'app-shell-v1'; // must match download.js's APP_SHELL_CACHE

// Which single cache a URL's fallback can possibly be in — book content
// (chapter data/meta.json/index.html/cover) only ever lands in that book's
// own "book-<id>" cache (see download.js's downloadBook()); everything
// else only ever lands in "app-shell-v1" (precacheAppShell()). Opening
// the one cache that can actually contain the match, instead of an
// unscoped caches.match() that has to open and scan every cache (a book
// with a few thousand chapters means a few thousand entries in that one
// cache alone), was step one of speeding this up — worth keeping even
// after the bigger fix below, since it's still less work per lookup.
function cacheNameFor(pathnameOrRequest) {
  const pathname = typeof pathnameOrRequest === 'string' ? pathnameOrRequest : new URL(pathnameOrRequest.url).pathname;
  const m = pathname.match(BOOK_URL_RE);
  return m ? `book-${m[1]}` : APP_SHELL_CACHE;
}

// The real fix: stop asking "is the network there?" separately for every
// single resource a page needs (each one racing its own timeout, and even
// with AbortController cancelling promptly, that's still N sequential-ish
// decisions). Ask ONCE per short window, remember the answer, and skip
// straight to cache for everything else while that answer says offline —
// no per-resource network attempt (and nothing to time out) at all.
//
// Concurrent callers while a check is already in flight share the SAME
// promise rather than each firing their own probe — a page's images,
// scripts, and the document itself all trigger fetch events within
// milliseconds of each other, and without this they'd each start a
// redundant probe before the first one had a chance to resolve.
const ONLINE_CHECK_TTL_MS = 5000;
const ONLINE_CHECK_TIMEOUT_MS = 500;
let onlineCache = null; // { value: bool, checkedAt: number }
let onlineCheckPromise = null;

function isOnline() {
  // navigator.onLine reflects the OS network interface (WiFi/cellular on
  // or off) — real device testing (Web Inspector's Timing tab) found that
  // when the interface is genuinely OFF, fetch()'s AbortController timeout
  // below did NOT bound the wait the way it should: the abort fires on
  // schedule, but WebKit/CFNetwork's "waiting for connectivity" handling
  // for a request with no interface at all can delay the actual rejection
  // by several seconds regardless — and since every fetch event (the page
  // navigation itself included) shares this one promise, EVERYTHING sat
  // queued behind that single slow-to-reject probe. navigator.onLine is
  // only ever used here as a short-circuit to "offline" — never to "online"
  // — so it can't reintroduce the false-positive problem the fetch probe
  // below still exists to catch (interface up, server actually unreachable).
  if (self.navigator && self.navigator.onLine === false) {
    onlineCache = { value: false, checkedAt: Date.now() };
    return Promise.resolve(false);
  }
  const now = Date.now();
  if (onlineCache && now - onlineCache.checkedAt < ONLINE_CHECK_TTL_MS) {
    return Promise.resolve(onlineCache.value);
  }
  if (!onlineCheckPromise) {
    onlineCheckPromise = (async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), ONLINE_CHECK_TIMEOUT_MS);
      let value;
      try {
        // HEAD, no-store: cheapest possible real round trip to this
        // origin — not asking for anything we'd need to read or cache,
        // just "did the server answer at all".
        await fetch('/manifest.json', { method: 'HEAD', cache: 'no-store', signal: controller.signal });
        value = true;
      } catch (e) {
        value = false;
      } finally {
        clearTimeout(timer);
      }
      onlineCache = { value, checkedAt: Date.now() };
      onlineCheckPromise = null;
      return value;
    })();
  }
  return onlineCheckPromise;
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  const fallbackUrl = CHAPTER_SHELL_RE.test(url.pathname) ? '/chapter-shell.html' : req;
  event.respondWith(handleFetch(req, fallbackUrl));
});

// Every fetch() this worker issues goes through here — no bare,
// unbounded fetch() anywhere else in the file. A previous version left
// exactly one such call in the "believed offline, not cached either" last
// resort below, reasoning it'd rarely be hit; live testing showed
// otherwise (a home page with more books than have been explicitly
// downloaded means MOST cover images take that exact path, so on a
// genuinely offline load every single one of them was a bare fetch()
// free to hang far longer than any timeout elsewhere in this file).
async function fetchWithTimeout(request, ms) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    return await fetch(request, { signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function handleFetch(request, fallback) {
  const online = await isOnline();
  if (!online) {
    // Believed offline from the recent check — go straight to cache, no
    // network attempt (and nothing to time out) for this resource at all.
    const cache = await caches.open(cacheNameFor(fallback));
    const cached = await cache.match(fallback, { ignoreVary: true });
    if (cached) return cached;
    // Not cached either — the online check could have been a false
    // negative (e.g. this exact origin request failing for an unrelated
    // reason), so still genuinely try the network as a last resort rather
    // than failing on our own say-so — but bounded, same as the "believed
    // online" path below, not a bare fetch().
    return fetchWithTimeout(request, 600);
  }

  // Believed online — network first, still with a short abort-backed
  // timeout in case THIS particular request stalls even though the
  // check above just succeeded (flaky connection, not a hard "offline").
  try {
    return await fetchWithTimeout(request, 3000);
  } catch (err) {
    const cache = await caches.open(cacheNameFor(fallback));
    const cached = await cache.match(fallback, { ignoreVary: true });
    if (cached) return cached;
    throw err;
  }
}
