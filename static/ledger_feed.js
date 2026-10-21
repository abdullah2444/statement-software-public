/* Scrolling ledgers: the newest rows show first, and older rows load by themselves as
   you scroll down. The page says where new rows go with initLedgerFeed({ append }). */
(function () {
  'use strict';

  function initLedgerFeed(options) {
    const root = document.querySelector('[data-ledger-feed]');
    if (!root) return null;
    const countSelector = options.countSelector || '.display-row';
    const pick = (selector) => root.querySelector(selector);
    const shownNode = pick('[data-feed-shown]');
    const totalNode = pick('[data-feed-total]');
    const actions = pick('[data-feed-actions]');
    const moreButton = pick('[data-feed-more]');
    const endNote = pick('[data-feed-end]');
    const errorNote = pick('[data-feed-error]');
    const sentinel = pick('[data-feed-sentinel]');
    const topButton = document.querySelector('[data-feed-top]');
    const chunk = Number(root.dataset.chunk) || 30;
    const NEAR_BOTTOM = 900;

    let [beforeDate, beforeId] = (root.dataset.before || '|').split('|');
    let hasMore = root.dataset.hasMore === '1';
    let total = Number(root.dataset.total) || 0;
    let loading = null;
    let paused = false;  // after an error, wait for "Try again" instead of retrying on every scroll

    const shown = () => document.querySelectorAll(countSelector).length;

    function render() {
      const count = shown();
      shownNode.textContent = count;
      totalNode.textContent = Math.max(total, count);
      actions.hidden = !hasMore;
      endNote.hidden = hasMore || count === 0;
      root.classList.toggle('is-loading', !!loading);
      root.classList.toggle('is-complete', !hasMore);
      moreButton.disabled = !!loading;
      moreButton.textContent = loading ? 'Loading…' : 'Load older entries';
      // Forms that come back to this page (save, delete, undo) keep this many rows on screen.
      document.querySelectorAll('[data-return-shown]').forEach((input) => { input.value = count || ''; });
    }

    // Remember how many rows are open, so reload and Back show the same rows at the same place.
    function rememberShown() {
      try {
        const url = new URL(window.location.href);
        if (hasMore) url.searchParams.set('per_page', String(shown()));
        else url.searchParams.set('per_page', 'all');
        url.searchParams.delete('page');
        window.history.replaceState(window.history.state, '', url);
      } catch (err) { /* old browsers: nothing to remember */ }
    }

    // New markup minus any row that is already on screen.
    function fragment(html, container) {
      const template = document.createElement('template');
      template.innerHTML = html;
      Array.from(template.content.children).forEach((node) => {
        const id = node.dataset.entryId;
        if (id && container.querySelector(`:scope > [data-entry-id="${CSS.escape(id)}"]`)) node.remove();
      });
      return template.content;
    }

    function load(limit) {
      if (!hasMore) return Promise.resolve(false);
      if (loading) return loading;
      errorNote.hidden = true;
      const params = new URLSearchParams({ before_date: beforeDate, before_id: beforeId, limit: String(limit || chunk) });
      const url = root.dataset.url + (root.dataset.url.includes('?') ? '&' : '?') + params.toString();
      loading = fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' })
        .then((response) => {
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return response.json();
        })
        .then((data) => {
          if (!data.ok) throw new Error('Not ok');
          options.append(data, fragment);
          hasMore = !!data.has_more;
          total = data.total;
          if (data.before_id !== null && data.before_id !== undefined) {
            beforeDate = data.before_date;
            beforeId = data.before_id;
          }
          paused = false;
          return true;
        })
        .catch(() => {
          paused = true;
          errorNote.hidden = false;
          return false;
        })
        .finally(() => {
          loading = null;
          render();
          rememberShown();
          // A tall screen may still show the bottom: keep filling it.
          window.setTimeout(loadIfNear, 0);
        });
      render();
      return loading;
    }

    function nearBottom() {
      return sentinel.getBoundingClientRect().top < window.innerHeight + NEAR_BOTTOM;
    }

    function loadIfNear() {
      if (hasMore && !loading && !paused && nearBottom()) load();
    }

    // The observer catches the bottom coming near; the scroll and resize checks are a backup
    // for browsers where it is missing or late.
    if ('IntersectionObserver' in window) {
      new IntersectionObserver((items) => {
        if (items.some((item) => item.isIntersecting)) loadIfNear();
      }, { rootMargin: `0px 0px ${NEAR_BOTTOM}px 0px` }).observe(sentinel);
    }
    window.addEventListener('scroll', loadIfNear, { passive: true });
    window.addEventListener('resize', loadIfNear, { passive: true });

    moreButton.addEventListener('click', () => { paused = false; load(); });
    pick('[data-feed-all]').addEventListener('click', () => { paused = false; load('all'); });
    pick('[data-feed-retry]').addEventListener('click', () => { paused = false; load(); });

    if (topButton) {
      const target = document.querySelector(options.topTarget || '#table-top');
      const toggleTop = () => { topButton.hidden = window.scrollY < 1200; };
      window.addEventListener('scroll', toggleTop, { passive: true });
      topButton.addEventListener('click', () => {
        (target || document.body).scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      toggleTop();
    }

    render();

    return {
      load,
      render,
      get hasMore() { return hasMore; },
      // Rows added or removed on the page itself (add / delete without reloading).
      adjust(delta) { total = Math.max(total + delta, 0); render(); rememberShown(); },
      // A short message for a few seconds, e.g. when a saved row moved out of sight.
      notice(text) {
        let toast = document.querySelector('.ledger-toast');
        if (!toast) {
          toast = document.createElement('div');
          toast.className = 'ledger-toast';
          toast.setAttribute('role', 'status');
          document.body.appendChild(toast);
        }
        toast.textContent = text;
        toast.classList.add('is-visible');
        window.clearTimeout(toast._hideTimer);
        toast._hideTimer = window.setTimeout(() => toast.classList.remove('is-visible'), 4500);
      },
      // Load older rows until the element with this id is on the page (links to #entry-N).
      async reveal(elementId) {
        for (let round = 0; round < 100; round += 1) {
          const node = document.getElementById(elementId);
          if (node) return node;
          if (!hasMore || !(await load(200))) return null;
        }
        return null;
      },
    };
  }

  // Same icons as templates/_icons.html, for rows drawn by script.
  const ROW_ICONS = {
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="m19 6-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>',
    percent: '<path d="M19 5 5 19"/><circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
  };

  // Turns node into an icon button: tooltip text on hover, a small word under it on phones.
  function iconAction(node, name, tip, label) {
    node.classList.add('icon-action');
    node.dataset.tip = tip;
    node.setAttribute('aria-label', tip);
    node.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"'
      + ' stroke-linejoin="round" aria-hidden="true" focusable="false">' + ROW_ICONS[name] + '</svg>'
      + '<span class="icon-action-label"></span>';
    node.querySelector('.icon-action-label').textContent = label;
    return node;
  }

  window.initLedgerFeed = initLedgerFeed;
  window.iconAction = iconAction;
})();
