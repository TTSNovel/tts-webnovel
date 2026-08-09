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

  async function init() {
    if (!document.querySelector('.book-card') && !document.getElementById('book-dl-btn')) return;
    injectStyles();
    const progress = await fetchProgress();
    renderLibraryBadges(progress);
    renderBookActions(progress);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
