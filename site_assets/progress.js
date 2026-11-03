/*
 * Reading-history UI for the Library (index.html) and book-detail pages —
 * NOT the chapter reader itself (see reader.js for the resume/highlight/
 * sync logic that runs there). Both index.html and books/<id>/index.html
 * are static files pre-rendered once at import time (see book_renderer.py)
 * and already sitting in the GCS bucket, so per-account reading position
 * can't be baked into them server-side, and neither can this file's own
 * styling (book_renderer.py's CSS is inlined into each page at generation
 * time too) — everything here is added/styled purely client-side instead
 * of depending on a bulk regeneration of already-deployed pages.
 *
 * Deliberately derives the book id from data already present on today's
 * deployed markup (.book-card's href, #book-dl-btn's data-book-id) rather
 * than a new data-id/#book-reading-actions hook, so this works immediately
 * against every book page that already exists — book_renderer.py's own
 * copies of those hooks are just for newly-generated pages going forward,
 * this script doesn't require them.
 */
(function () {
  'use strict';

  function pad4(n) {
    return String(n).padStart(4, '0');
  }

  // Same device identity as reader.js (localStorage "deviceId", "Firefox ·
  // Linux"-style name) — sent on every progress call so the history shows
  // which device did what.
  function newUuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  function deviceId() {
    try {
      let id = localStorage.getItem('deviceId');
      if (!id) {
        id = newUuid();
        localStorage.setItem('deviceId', id);
      }
      return id;
    } catch {
      return null;
    }
  }

  const UA_BROWSERS = [
    ['Edg/', 'Edge'],
    ['OPR/', 'Opera'],
    ['Firefox/', 'Firefox'],
    ['FxiOS/', 'Firefox'],
    ['CriOS/', 'Chrome'],
    ['Chrome/', 'Chrome'],
    ['Safari/', 'Safari'],
  ];
  const UA_OSES = [
    ['iPhone', 'iOS'],
    ['iPad', 'iPadOS'],
    ['Android', 'Android'],
    ['CrOS', 'ChromeOS'],
    ['Windows', 'Windows'],
    ['Mac OS X', 'macOS'],
    ['Linux', 'Linux'],
  ];

  function deviceName() {
    const ua = navigator.userAgent;
    const browser = UA_BROWSERS.find(([token]) => ua.includes(token))?.[1] || 'Trình duyệt';
    const os = UA_OSES.find(([token]) => ua.includes(token))?.[1];
    return os ? `${browser} · ${os}` : browser;
  }

  function progressHeaders(extra) {
    const headers = { 'X-Device-Name': encodeURIComponent(deviceName()), ...extra };
    const id = deviceId();
    if (id) headers['X-Device-Id'] = id;
    return headers;
  }

  async function fetchProgress() {
    try {
      const r = await fetch('/api/progress', { headers: progressHeaders() });
      if (!r.ok) return {};
      return await r.json();
    } catch {
      return {};
    }
  }

  // null on failure — the book page then just has no history section.
  async function fetchHistory(bookId) {
    try {
      const r = await fetch(`/api/progress/${bookId}/history?limit=100`, { headers: progressHeaders() });
      if (!r.ok) return null;
      return await r.json();
    } catch {
      return null;
    }
  }

  function injectStyles() {
    const style = document.createElement('style');
    style.textContent = `
      .progress-badge { display: block; margin-top: .1rem; font-size: .72rem; color: var(--accent); font-weight: 600; }
      .book-reading-actions { display: flex; gap: .5rem; margin-top: 1rem; flex-wrap: wrap; }
      .recent-grid { display: flex; gap: 1rem; overflow-x: auto; padding-bottom: .3rem; scroll-snap-type: x mandatory; }
      /* min-width:0 overrides the flex item's default automatic minimum
         size — without it, a card whose cover file happens to have a
         small natural resolution (some covers are ~120-160px wide source
         files, most are 300px+) gets floored wider than flex-basis by its
         own <img>'s intrinsic size, breaking the shelf's otherwise-uniform
         card width. Classic flexbox "min-width: auto" gotcha, confirmed
         live: without this, those cards render ~130-160px instead of 132px. */
      .recent-grid .book-card { flex: 0 0 132px; min-width: 0; scroll-snap-align: start; }
      .reading-history { margin: 0 0 1.25rem; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); }
      .reading-history summary { padding: .7rem 1rem; cursor: pointer; font-weight: 600; }
      .reading-history .msg { margin: 0 1rem .6rem; font-size: .85rem; color: var(--accent); }
      .reading-history ol { list-style: none; margin: 0; padding: 0; }
      .reading-history li { display: flex; gap: .75rem; align-items: center; padding: .55rem 1rem; border-top: 1px solid var(--border); }
      .reading-history li.inactive { opacity: .55; }
      .reading-history .what { flex: 1; min-width: 0; }
      .reading-history .meta { display: block; color: var(--muted); font-size: .8rem; }
      .reading-history .status { color: var(--muted); font-size: .8rem; font-style: italic; }
      .reading-history button { flex: none; border: 1px solid var(--border); background: var(--bg); color: var(--text);
        border-radius: 8px; padding: .3rem .6rem; font-size: .8rem; cursor: pointer; }
    `;
    document.head.appendChild(style);
  }

  function bookIdFromCard(card) {
    if (card.dataset.id) return card.dataset.id;
    const m = (card.getAttribute('href') || '').match(/books\/(\d+)\//);
    return m ? m[1] : null;
  }

  function renderLibraryBadges(progress) {
    document.querySelectorAll('.book-card').forEach((card) => {
      const id = bookIdFromCard(card);
      const p = id && progress[id];
      if (!p) return;
      const badge = document.createElement('span');
      badge.className = 'progress-badge';
      badge.textContent = `Đang đọc: Chương ${p.chapter + 1}`;
      card.appendChild(badge);
    });
  }

  // The book page's own state: its book id and the position (with rev) it
  // last saw — the base_rev of anything chosen on this page.
  const book = { id: null, position: null, container: null };

  function currentBookId() {
    const dlBtn = document.getElementById('book-dl-btn');
    const existingSlot = document.getElementById('book-reading-actions');
    return (existingSlot && existingSlot.dataset.bookId) || (dlBtn && dlBtn.dataset.bookId) || null;
  }

  function renderBookActions(progress) {
    const dlBtn = document.getElementById('book-dl-btn');
    const existingSlot = document.getElementById('book-reading-actions');
    const bookId = currentBookId();
    if (!bookId) return;
    const p = progress[bookId];

    // Re-rendered whenever the position is refreshed (tab visible again,
    // restore conflict), so start from an empty container each time.
    const container = existingSlot || book.container || document.createElement('div');
    container.className = 'book-reading-actions';
    container.replaceChildren();

    const start = document.createElement('a');
    start.className = 'dl-btn';
    start.href = `chapters/${pad4(0)}.html`;
    start.textContent = 'Đọc từ đầu';
    container.appendChild(start);

    if (p) {
      const cont = document.createElement('a');
      cont.className = 'dl-btn';
      cont.href = `chapters/${pad4(p.chapter)}.html`;
      // Going back to the current position is not a new choice — no event.
      cont.dataset.progressResume = '';
      cont.textContent = `Đọc tiếp (Chương ${p.chapter + 1})`;
      container.appendChild(cont);
    }

    if (!existingSlot && !book.container && dlBtn) dlBtn.closest('.book-dl').before(container);
    book.container = container;
  }

  // Clicking a chapter link here is a deliberate "select", but the reader
  // is a separate page load. The event (with its id fixed now, so a retry
  // is idempotent, and the rev this page saw) is handed over through
  // sessionStorage; reader.js posts it only if it lands on exactly this
  // chapter via a fresh navigation (see its takePendingSelect).
  function setupPendingSelect() {
    document.addEventListener('click', (e) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const link = e.target.closest('a[href]');
      if (!link || 'progressResume' in link.dataset) return;
      const m = new URL(link.href, location.href).pathname.match(/^\/books\/(\d+)\/chapters\/(\d+)\.html$/);
      if (!m || m[1] !== String(book.id)) return;
      try {
        sessionStorage.setItem(
          'progress.pendingSelect',
          JSON.stringify({
            id: newUuid(),
            book_id: parseInt(m[1], 10),
            chapter: parseInt(m[2], 10),
            base_rev: book.position ? book.position.rev : null,
            created_at: Date.now(),
          })
        );
      } catch {
        /* storage blocked — the chapter opens, just without an event */
      }
    });
  }

  const KIND_LABELS = {
    select: 'chọn chương',
    auto_next: 'tự sang chương',
    restore: 'khôi phục',
    legacy: 'bản cũ',
  };

  // "18:52 03/11" — built by hand rather than toLocaleString so every
  // browser shows the same compact form.
  function formatTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const two = (n) => String(n).padStart(2, '0');
    return `${two(d.getHours())}:${two(d.getMinutes())} ${two(d.getDate())}/${two(d.getMonth() + 1)}`;
  }

  // "Lịch sử đọc" on the book page: every event for this book, newest
  // first, including the ones that did not move the position (greyed) —
  // any of them can be made the position again with one click.
  function renderHistory(history, message) {
    let section = document.getElementById('reading-history');
    const list = document.querySelector('.chapter-list');
    if (!history || !history.events.length || !list) {
      if (section) section.remove();
      return;
    }
    if (!section) {
      section = document.createElement('details');
      section.id = 'reading-history';
      section.className = 'reading-history';
      list.before(section);
      if (location.hash === '#reading-history') {
        section.open = true;
        section.scrollIntoView();
      }
    }
    const summary = document.createElement('summary');
    summary.textContent = `Lịch sử đọc (${history.events.length})`;
    const ol = document.createElement('ol');
    history.events.forEach((ev) => {
      const li = document.createElement('li');
      const status = ev.conflict ? 'xung đột' : ev.applied ? '' : 'không áp dụng';
      if (status) li.className = 'inactive';
      const what = document.createElement('span');
      what.className = 'what';
      what.textContent = `Chương ${ev.chapter + 1}`;
      if (status) {
        const s = document.createElement('span');
        s.className = 'status';
        s.textContent = ` — ${status}`;
        what.appendChild(s);
      }
      const meta = document.createElement('span');
      meta.className = 'meta';
      meta.textContent = [ev.device_name || 'Không rõ thiết bị', formatTime(ev.server_ts), KIND_LABELS[ev.kind] || ev.kind]
        .filter(Boolean)
        .join(' · ');
      what.appendChild(meta);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = 'Đọc tiếp từ đây';
      btn.addEventListener('click', () => restoreFrom(ev.chapter, btn));
      li.append(what, btn);
      ol.appendChild(li);
    });
    const children = [summary];
    if (message) {
      const msg = document.createElement('p');
      msg.className = 'msg';
      msg.textContent = message;
      children.push(msg);
    }
    children.push(ol);
    section.replaceChildren(...children);
  }

  function openChapter(chapter) {
    window.location.href = `/books/${book.id}/chapters/${pad4(chapter)}.html`;
  }

  // "Đọc tiếp từ đây": a restore event against the rev this page saw. If
  // another device moved the position meanwhile (409), show who/where and
  // let a second click confirm against the new rev, instead of overwriting
  // what the user may not have seen.
  async function restoreFrom(chapter, btn) {
    if (book.position && book.position.chapter === chapter) {
      openChapter(chapter);
      return;
    }
    btn.disabled = true;
    const body = JSON.stringify({
      id: newUuid(),
      book_id: parseInt(book.id, 10),
      chapter,
      kind: 'restore',
      base_rev: book.position ? book.position.rev : 0,
      client_ts: new Date().toISOString(),
    });
    let r = null;
    try {
      r = await fetch('/api/progress/events', {
        method: 'POST',
        headers: progressHeaders({ 'Content-Type': 'application/json' }),
        body,
      });
    } catch {
      /* handled below */
    }
    if (r && r.status === 200) {
      openChapter(chapter);
      return;
    }
    btn.disabled = false;
    if (r && r.status === 409) {
      const result = await r.json();
      const pos = result.position;
      book.position = pos;
      await refreshBook(
        pos
          ? `${pos.device_name || 'Thiết bị khác'} vừa chuyển sang chương ${pos.chapter + 1} (${formatTime(pos.updated_at)}). Bấm lại để xác nhận.`
          : null
      );
      return;
    }
    await refreshBook('Không lưu được vị trí đọc. Thử lại sau.');
  }

  async function refreshBook(message) {
    const [progress, history] = await Promise.all([fetchProgress(), fetchHistory(book.id)]);
    const position = history ? history.position : progress[book.id] || null;
    book.position = position;
    renderBookActions({ ...progress, [book.id]: position || undefined });
    renderHistory(history, message);
  }

  // How many books the "Đọc gần đây" shelf shows — enough to fill a
  // horizontal scroll row without turning into an unbounded list of every
  // book ever opened.
  const RECENT_LIMIT = 10;

  // Home-page-only "continue reading" shelf, prepended above the category
  // sections — same idea as the iOS app's own recent-reads row. Built by
  // cloning each matching .book-card already rendered further down the
  // page (cover/title/author markup, plus renderLibraryBadges' "Đang đọc:
  // Chương N" badge, called before this) rather than fetching book
  // metadata separately — this page already has everything needed once a
  // book has an entry in /api/progress. The clone's link is rewritten to
  // jump straight into the last-read chapter, not the book's chapter list.
  function renderRecentSection(progress) {
    const wrap = document.getElementById('home-wrap');
    if (!wrap) return;

    const entries = Object.entries(progress)
      .filter(([, p]) => p && p.updated_at)
      .sort((a, b) => new Date(b[1].updated_at) - new Date(a[1].updated_at))
      .slice(0, RECENT_LIMIT);
    if (!entries.length) return;

    const cards = [];
    entries.forEach(([id, p]) => {
      const original = wrap.querySelector(`.book-card[data-id="${id}"]`);
      if (!original) return; // book no longer in the library
      const clone = original.cloneNode(true);
      clone.href = `books/${id}/chapters/${pad4(p.chapter)}.html`;
      cards.push(clone);
    });
    if (!cards.length) return;

    const section = document.createElement('section');
    section.className = 'category recent-section';
    section.innerHTML = '<h2>Đọc gần đây</h2><div class="book-grid recent-grid"></div>';
    section.querySelector('.recent-grid').append(...cards);
    wrap.prepend(section);
  }

  async function init() {
    if (!document.querySelector('.book-card') && !document.getElementById('book-dl-btn')) return;
    injectStyles();
    book.id = currentBookId();
    if (book.id) {
      // Book page: the history response carries the position too.
      setupPendingSelect();
      await refreshBook();
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refreshBook();
      });
      return;
    }
    const progress = await fetchProgress();
    renderLibraryBadges(progress);
    renderRecentSection(progress);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
