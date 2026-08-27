/* card_picker.js — shared "browse as cards" modal for products/customers/vendors.
   Injects its own overlay markup once, then openCardPicker() can be called
   any number of times with different datasets/renderers. */

(function () {
  const PAGE_SIZE = 24;
  let state = { items: [], filtered: [], page: 0, getSearchText: null, renderCard: null, onSelect: null, getCategory: null, activeCategory: '' };

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function ensureDom() {
    if (document.getElementById('cpOverlay')) return;
    const overlay = document.createElement('div');
    overlay.className = 'cp-overlay';
    overlay.id = 'cpOverlay';
    overlay.innerHTML =
      '<div class="cp-box">' +
      '  <div class="cp-header">' +
      '    <div class="cp-title" id="cpTitle">Browse</div>' +
      '    <button class="cp-close" type="button" onclick="CardPicker.close()">\u2715</button>' +
      '  </div>' +
      '  <div class="cp-search-row" style="display:flex;gap:8px;margin-bottom:12px;">' +
      '    <input type="text" class="cp-search" id="cpSearch" placeholder="Search..." style="margin-bottom:0;flex:1;">' +
      '    <select class="cp-search" id="cpCategory" style="margin-bottom:0;flex:0 0 160px;display:none;"></select>' +
      '  </div>' +
      '  <div class="cp-grid" id="cpGrid"></div>' +
      '  <div class="cp-footer">' +
      '    <span id="cpCount"></span>' +
      '    <span>' +
      '      <button class="cp-page-btn" id="cpPrev" type="button" onclick="CardPicker.prevPage()">\u2190 Prev</button>' +
      '      <button class="cp-page-btn" id="cpNext" type="button" onclick="CardPicker.nextPage()">Next \u2192</button>' +
      '      <button class="cp-page-btn" id="cpDone" type="button" onclick="CardPicker.close()" style="display:none;">Done</button>' +
      '    </span>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(overlay);

    overlay.addEventListener('click', function (e) {
      if (e.target === overlay && !state.multiSelect) close();
    });
    document.getElementById('cpSearch').addEventListener('input', function () {
      applyFilters(this.value, state.activeCategory);
    });
    document.getElementById('cpCategory').addEventListener('change', function () {
      state.activeCategory = this.value;
      applyFilters(document.getElementById('cpSearch').value, this.value);
    });
  }

  function applyFilters(query, category) {
    const q = (query || '').trim().toLowerCase();
    state.filtered = state.items.filter(function (item) {
      const matchesText = !q || state.getSearchText(item).toLowerCase().includes(q);
      const matchesCategory = !category ||
        (state.getCategory && (state.getCategory(item) || '').toString().toLowerCase() === category.toLowerCase());
      return matchesText && matchesCategory;
    });
    state.page = 0;
    render();
  }

  function render() {
    const grid = document.getElementById('cpGrid');
    const start = state.page * PAGE_SIZE;
    const pageItems = state.filtered.slice(start, start + PAGE_SIZE);

    if (pageItems.length === 0) {
      grid.innerHTML = '<div class="cp-empty">No matches.</div>';
    } else {
      grid.innerHTML = pageItems
        .map(function (item, i) {
          const card = state.renderCard(item);
          const lines = (card.lines || [])
            .map(function (l) {
              const cls = l.warn ? 'cp-val cp-warn' : 'cp-val';
              return '<div class="cp-card-line"><span>' + escapeHtml(l.label) + '</span><span class="' + cls + '">' + escapeHtml(l.value) + '</span></div>';
            })
            .join('');
          return (
            '<div class="cp-card" data-idx="' + (start + i) + '">' +
            '<div class="cp-card-title">' + escapeHtml(card.title) + '</div>' +
            (card.subtitle ? '<div class="cp-card-subtitle">' + escapeHtml(card.subtitle) + '</div>' : '') +
            lines +
            '</div>'
          );
        })
        .join('');
      Array.from(grid.querySelectorAll('.cp-card')).forEach(function (el) {
        el.addEventListener('click', function () {
          const idx = parseInt(el.getAttribute('data-idx'), 10);
          const item = state.filtered[idx];
          if (state.multiSelect) {
            state.onSelect(item);
            state.addedCount++;
            el.classList.add('cp-card-added');
            setTimeout(function () { el.classList.remove('cp-card-added'); }, 350);
            updateDoneLabel();
          } else {
            close();
            state.onSelect(item);
          }
        });
      });
    }

    const totalPages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
    document.getElementById('cpCount').textContent =
      state.filtered.length + ' result(s) \u00b7 page ' + (state.page + 1) + ' of ' + totalPages;
    document.getElementById('cpPrev').disabled = state.page <= 0;
    document.getElementById('cpNext').disabled = state.page >= totalPages - 1;
  }

  function updateDoneLabel() {
    const btn = document.getElementById('cpDone');
    if (btn) btn.textContent = state.addedCount > 0 ? 'Done (' + state.addedCount + ' added)' : 'Done';
  }

  function open(options) {
    ensureDom();
    state.items = options.items || [];
    state.getSearchText = options.getSearchText;
    state.getCategory = options.getCategory || null;
    state.renderCard = options.renderCard;
    state.onSelect = options.onSelect;
    state.filtered = state.items;
    state.page = 0;
    state.activeCategory = '';
    state.multiSelect = !!options.multiSelect;
    state.addedCount = 0;

    document.getElementById('cpTitle').textContent = options.title || 'Browse';
    document.getElementById('cpSearch').value = '';
    document.getElementById('cpSearch').placeholder = options.searchPlaceholder || 'Search...';

    const doneBtn = document.getElementById('cpDone');
    doneBtn.style.display = state.multiSelect ? '' : 'none';
    doneBtn.textContent = 'Done';

    const categorySelect = document.getElementById('cpCategory');
    if (options.categories && options.categories.length) {
      categorySelect.style.display = '';
      categorySelect.innerHTML = '<option value="">' + escapeHtml(options.categoryLabel || 'All categories') + '</option>' +
        options.categories.map(function (c) {
          return '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + '</option>';
        }).join('');
    } else {
      categorySelect.style.display = 'none';
      categorySelect.innerHTML = '';
    }

    document.getElementById('cpOverlay').classList.add('open');
    render();
    setTimeout(function () {
      document.getElementById('cpSearch').focus();
    }, 0);
  }

  function close() {
    const overlay = document.getElementById('cpOverlay');
    if (overlay) overlay.classList.remove('open');
  }

  function nextPage() {
    const totalPages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
    if (state.page < totalPages - 1) {
      state.page++;
      render();
    }
  }

  function prevPage() {
    if (state.page > 0) {
      state.page--;
      render();
    }
  }

  window.CardPicker = { open: open, close: close, nextPage: nextPage, prevPage: prevPage };

  // Shared helper: turns {field_key: value} (from the custom-fields APIs) into
  // card line objects, skipping empty values, so every page's renderCard can
  // just spread this in rather than duplicating the formatting logic.
  window.customFieldLines = function (cf) {
    if (!cf) return [];
    return Object.keys(cf)
      .filter(function (k) { return cf[k] !== '' && cf[k] != null; })
      .map(function (k) {
        var label = k.replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
        return { label: label, value: String(cf[k]) };
      });
  };
})();
