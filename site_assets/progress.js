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

  async function fetchProgress() {
    try {
      const r = await fetch('/api/progress');
      if (!r.ok) return {};
      return await r.json();
    } catch {
      return {};
    }
  }

  function injectStyles() {
    const style = document.createElement('style');
    style.textContent = `
      .progress-badge { display: block; margin-top: .1rem; font-size: .72rem; color: var(--accent); font-weight: 600; }
      .book-reading-actions { display: flex; gap: .5rem; margin-top: 1rem; flex-wrap: wrap; }
      .recent-grid { display: flex; gap: 1rem; overflow-x: auto; padding-bottom: .3rem; scroll-snap-type: x mandatory; }
      .recent-grid .book-card { flex: 0 0 132px; scroll-snap-align: start; }
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

  function renderBookActions(progress) {
    const dlBtn = document.getElementById('book-dl-btn');
    const existingSlot = document.getElementById('book-reading-actions');
    const bookId = (existingSlot && existingSlot.dataset.bookId) || (dlBtn && dlBtn.dataset.bookId);
    if (!bookId) return;
    const p = progress[bookId];

    const container = existingSlot || document.createElement('div');
    container.className = 'book-reading-actions';

    const start = document.createElement('a');
    start.className = 'dl-btn';
    start.href = `chapters/${pad4(0)}.html`;
    start.textContent = 'Đọc từ đầu';
    container.appendChild(start);

    if (p) {
      const cont = document.createElement('a');
      cont.className = 'dl-btn';
      cont.href = `chapters/${pad4(p.chapter)}.html`;
      cont.textContent = `Đọc tiếp (Chương ${p.chapter + 1})`;
      container.appendChild(cont);
    }

    if (!existingSlot && dlBtn) dlBtn.closest('.book-dl').before(container);
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
    const progress = await fetchProgress();
    renderLibraryBadges(progress);
    renderBookActions(progress);
    renderRecentSection(progress);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
