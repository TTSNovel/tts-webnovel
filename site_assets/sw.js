// Minimal service worker — exists only so the site qualifies as an
// installable PWA (Android/Chrome's install prompt wants one; iOS Safari's
// "Add to Home Screen" doesn't strictly require it but it doesn't hurt).
// No offline caching: every page here needs a live login session and most
// content (TTS, newly imported books) is inherently network-dependent, so
// caching would mostly just serve stale pages.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
