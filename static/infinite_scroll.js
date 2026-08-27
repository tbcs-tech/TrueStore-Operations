/* infinite_scroll.js — two pieces, usable independently:
   1. Floating scroll-to-top / scroll-to-bottom buttons - added automatically
      to every page that includes this script, no setup needed.
   2. InfiniteScroll.attach(tbodySelector) - progressively reveals a long
      table's rows in chunks as the user scrolls near the bottom, instead of
      rendering (and forcing the browser to lay out) hundreds of rows at
      once. Works on tables the server already rendered in full - no new
      backend/pagination endpoints needed. */

(function () {
  function initScrollJumpButtons() {
    if (document.getElementById('scrollJumpStack')) return;
    const stack = document.createElement('div');
    stack.className = 'scroll-jump-stack';
    stack.id = 'scrollJumpStack';
    stack.innerHTML =
      '<button class="scroll-jump-btn" id="scrollJumpTop" title="Scroll to top" type="button">\u2191</button>' +
      '<button class="scroll-jump-btn" id="scrollJumpBottom" title="Scroll to bottom" type="button">\u2193</button>';
    document.body.appendChild(stack);

    document.getElementById('scrollJumpTop').addEventListener('click', function () {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
    document.getElementById('scrollJumpBottom').addEventListener('click', function () {
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'smooth' });
    });

    function updateVisibility() {
      const scrollable = document.documentElement.scrollHeight > window.innerHeight + 200;
      stack.classList.toggle('visible', scrollable);
    }
    window.addEventListener('scroll', updateVisibility, { passive: true });
    window.addEventListener('resize', updateVisibility);
    updateVisibility();
    // Content can grow after load (infinite-scroll reveals more rows, or a
    // fetch() populates a table) - a light periodic re-check catches that
    // without needing every caller to remember to trigger it manually.
    setInterval(updateVisibility, 800);
  }

  function attachInfiniteScroll(tbodySelector, options) {
    const tbody = document.querySelector(tbodySelector);
    if (!tbody) return;
    const chunkSize = (options && options.chunkSize) || 40;
    const rows = Array.from(tbody.children).filter(function (el) { return el.tagName === 'TR'; });
    if (rows.length <= chunkSize) return; // short enough already, nothing to chunk

    rows.forEach(function (r, i) { if (i >= chunkSize) r.style.display = 'none'; });

    let revealed = chunkSize;
    function makeSentinel() {
      const sentinel = document.createElement('tr');
      sentinel.className = 'infinite-scroll-sentinel';
      const td = document.createElement('td');
      td.colSpan = 20;
      sentinel.appendChild(td);
      return sentinel;
    }

    let sentinel = makeSentinel();
    rows[chunkSize - 1].after(sentinel);

    const observer = new IntersectionObserver(function (entries) {
      if (!entries[0].isIntersecting) return;
      const next = Math.min(revealed + chunkSize, rows.length);
      for (let i = revealed; i < next; i++) { rows[i].style.display = ''; }
      revealed = next;
      sentinel.remove();
      if (revealed < rows.length) {
        sentinel = makeSentinel();
        rows[revealed - 1].after(sentinel);
        observer.observe(sentinel);
      } else {
        observer.disconnect();
      }
    }, { rootMargin: '400px' });

    observer.observe(sentinel);
  }

  document.addEventListener('DOMContentLoaded', initScrollJumpButtons);

  window.InfiniteScroll = { attach: attachInfiniteScroll, initButtons: initScrollJumpButtons };
})();
