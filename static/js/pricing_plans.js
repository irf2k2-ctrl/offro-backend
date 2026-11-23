/* Pricing & Plans - admin dashboard module.
 *
 * A thin client for /admin/pricing-config. The server is the single source of truth for permissions, validation and what is
 * configured; the browser only shows what the API returns and hides controls the caller cannot use. It never calls checkout or any
 * payment API, and it never converts a price between currencies.
 *
 * Output safety: every HTML string is built with the h`...` tag, which escapes every interpolated value unless wrapped in raw()
 * (only used for markup produced by this file). innerHTML is assigned only through setHtml(). Server values are never evaluated.
 */
(function () {
  'use strict';
  var API = '/admin/pricing-config';
  var TABS = [['store', 'Store Plans'], ['infl', 'Influencer Plans'], ['banner', 'Banners & Ads'], ['product', 'Product Settings'],
              ['tax', 'GST & VAT'], ['countries', 'Countries & Currencies'], ['reward', 'Reward Redemption Points']];
  var SECTION_OF = { store: 'store_plans', infl: 'influencer', banner: 'banner', product: 'product', reward: 'reward' };

  function esc(v) {
    if (v === null || v === undefined || v === false) return '';
    return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function Raw(s) { this.s = s; }
  function raw(s) { return new Raw(s); }
  function h(strings) {
    var out = strings[0];
    for (var i = 1; i < strings.length; i++) { var v = arguments[i]; out += (v instanceof Raw ? v.s : esc(v)) + strings[i]; }
    return out;
  }
  function setHtml(el, html) { if (el) el.innerHTML = html; }
  function $(id) { return document.getElementById(id); }

  var st = { tab: 'store', country: 'IN', countries: [], canAdd: false, prices: null, edit: null, msg: null, history: null, seq: 0, loaded: false, loading: false, loadErr: null, busy: false, rseq: 0 };

  // ── permissions (display only; the server enforces them) ───────────────────────────────────────────────
  function canEdit() {
    var me = window._rbacMe;
    if (!me) return true;
    if (me.role === 'Super Admin') return true;
    var p = me.permissions || {};
    return !!(p['Settings'] && p['Settings'].edit);
  }

  // ── helpers ───────────────────────────────────────────────────────────────────────────────────────────
  function num(v) {
    if (v === null || v === undefined || v === '') return '-';
    var n = Number(v);
    if (!isFinite(n)) return '-';
    return n.toFixed(3).replace(/0$/, '');
  }
  function pct(v) { var n = Number(v); return isFinite(n) ? String(n) : '-'; }
  function cur() {
    for (var i = 0; i < st.countries.length; i++) if (st.countries[i].code === st.country) return st.countries[i];
    return null;
  }
  function money(c, v) { return (v === null || v === undefined) ? '-' : (c ? c.currency_symbol + ' ' : '') + num(v); }
  function taxText(tax) {
    if (!tax) return '-';
    if (!tax.configured) return 'Not configured';
    if (tax.tax_free) return 'Tax-free';
    return tax.label + ' ' + pct(tax.percent) + '%';
  }
  function pill(active, configured) {
    if (!configured) return '<span class="pill w">Not configured</span>';
    return active ? '<span class="pill a">Active</span>' : '<span class="pill i">Inactive</span>';
  }
  function flash(kind, text) { st.msg = { kind: kind, text: text }; renderMsg(); }
  function renderMsg() {
    var m = st.msg;
    setHtml($('pp_msg'), m ? h`<div class="pp-alert ${m.kind}" role="${m.kind === 'err' ? 'alert' : 'status'}">${m.text}</div>` : '');
  }
  function errText(body, fallback) {
    var d = body && body.detail;
    if (d && typeof d === 'object') return d.message || d.detail || fallback;
    if (typeof d === 'string') return d;
    return fallback;
  }

  // Friendly wording for failures. Validation (422) and conflict (409) messages come from the server unchanged because they say what
  // to fix; transport, session and server faults get plain wording instead of raw status codes.
  function friendly(status, body) {
    var m = errText(body, '');
    if (status === 401) return 'Your session has expired. Please sign in again.';
    if (status === 403) return m || 'You do not have permission to do that.';
    if (status === 404) return m || 'That item was not found. Refresh the page and try again.';
    if (status === 429) return 'Too many requests. Please wait a moment and try again.';
    if (status >= 500) return 'Something went wrong on our side. Please try again in a moment.';
    return m || 'The request could not be completed (' + status + ').';
  }
  async function api(path, opts) {
    opts = opts || {};
    var init = { credentials: 'include', method: opts.method || 'GET' };
    if (opts.body !== undefined) { init.headers = { 'Content-Type': 'application/json' }; init.body = JSON.stringify(opts.body); }
    var r;
    try { r = await fetch(API + path, init); }
    catch (e) { var ne = new Error('Could not reach the server. Check your internet connection and try again.'); ne.status = 0; throw ne; }
    var body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    if (!r.ok) { var err = new Error(friendly(r.status, body)); err.status = r.status; throw err; }
    return body;
  }

  // ── loading ──────────────────────────────────────────────────────────────────────────────────────────
  async function loadCountries() {
    var d = await api('/countries');
    st.countries = d.countries || [];
    st.canAdd = !!d.can_add;
    if (!st.countries.some(function (c) { return c.code === st.country; })) st.country = st.countries.length ? st.countries[0].code : '';
  }
  async function loadPrices() {
    st.prices = null;
    if (!st.country) return;
    var seq = ++st.seq;
    var d = await api('/countries/' + encodeURIComponent(st.country) + '/prices');
    if (seq === st.seq) st.prices = d;
  }
  // Full (re)load of the active tab. The newest call wins: an older response that arrives late is dropped.
  async function refresh() {
    var seq = ++st.rseq;
    st.loading = true; st.loadErr = null; show(true); render();
    try {
      await loadCountries();
      if (['store', 'infl', 'banner', 'product'].indexOf(st.tab) >= 0) await loadPrices();
      if (seq !== st.rseq) return;
      st.loading = false; render();
    } catch (e) {
      if (seq !== st.rseq) return;
      st.loading = false;
      if (e.status === 403) show(false); else { st.loadErr = e.message; render(); }
    }
  }
  // After a successful save: refresh the lists. If only this refresh fails the save still stands, and the message says so.
  async function afterSave(withPrices) {
    try { await loadCountries(); if (withPrices) await loadPrices(); st.loadErr = null; }
    catch (e) { st.loadErr = 'Your change was saved, but the list could not be refreshed. ' + e.message; }
    render();
  }
  function loaderHtml() { return '<div class="pp-loading" role="status"><span class="pp-spin" aria-hidden="true"></span>Loading pricing data...</div>'; }
  function errorHtml() {
    return h`<div class="pp-card"><div class="pp-alert err" role="alert">${st.loadErr}</div><div class="pp-toolbar"><button type="button" class="pp-btn pri" data-act="retry">Try again</button></div></div>`;
  }
  // While a save/toggle is in flight every button is disabled and the clicked one reads "Saving...", so a second click cannot send it twice.
  function setBusy(on, btn) {
    st.busy = on;
    var root = $('section-pricing_plans');
    if (root) {
      var bs = root.querySelectorAll('button');
      for (var i = 0; i < bs.length; i++) bs[i].disabled = on;
    }
    if (!btn) return;
    if (on) { btn.setAttribute('data-label', btn.textContent); btn.textContent = 'Saving...'; }
    else if (btn.getAttribute('data-label') !== null) { btn.textContent = btn.getAttribute('data-label'); btn.removeAttribute('data-label'); }
  }
  async function guarded(btn, fn) {
    if (st.busy) return;
    setBusy(true, btn);
    try { await fn(); } finally { setBusy(false, btn); }
  }
  function show(ok) { var d = $('pp_denied'), b = $('pp_body'); if (d) d.hidden = ok; if (b) b.hidden = !ok; }

  // ── render ───────────────────────────────────────────────────────────────────────────────────────────
  function renderTabs() {
    setHtml($('pp_tabs'), TABS.map(function (t) {
      return h`<button type="button" class="pp-tab ${t[0] === st.tab ? 'active' : ''}" role="tab" aria-selected="${t[0] === st.tab ? 'true' : 'false'}" data-tab="${t[0]}">${t[1]}</button>`;
    }).join(''));
  }
  function render() {
    renderTabs(); renderMsg();
    var el = $('pp_pane');
    if (!el) return;
    if (st.loading) return setHtml(el, loaderHtml());
    if (st.loadErr) return setHtml(el, errorHtml());
    if (st.tab === 'tax') return setHtml(el, taxView());
    if (st.tab === 'countries') return setHtml(el, countriesView());
    if (st.tab === 'reward') return setHtml(el, rewardView());
    setHtml(el, priceView());
  }

  function countrySelect() {
    return h`<label for="pp_country" style="display:inline;margin-right:6px">Country</label><select id="pp_country">${raw(st.countries.map(function (c) {
      return h`<option value="${c.code}" ${c.code === st.country ? raw('selected') : ''}>${c.name}${c.status === 'inactive' ? ' (inactive)' : ''}</option>`;
    }).join(''))}</select>`;
  }

  function priceHead(title, help) {
    var c = cur(), p = st.prices;
    return h`<div class="pp-head"><div><div class="pp-title">${title}</div><div class="pp-help">${help}</div></div>
      <div class="pp-toolbar">${raw(countrySelect())}<button type="button" class="pp-btn" data-act="history">View change history</button></div></div>
      <div class="pp-meta" style="margin-bottom:10px">Currency: <b>${c ? c.currency_code + ' (' + c.currency_symbol + ')' : '-'}</b> &nbsp;|&nbsp; Tax: <b>${p ? taxText(p.tax) : '-'}</b></div>`;
  }
  function taxBanner() {
    var p = st.prices;
    if (!p || p.tax.configured) return '';
    return h`<div class="pp-alert err" role="alert">${p.tax.error} Open the GST &amp; VAT tab to set it. Totals stay blank until then.</div>`;
  }

  function priceView() {
    var p = st.prices, c = cur();
    var titles = { store: ['Store plans', 'Subscription price a merchant pays OffrO for a store, by duration.'],
                   infl: ['Influencer plans', 'Subscription fee an influencer pays OffrO.'],
                   banner: ['Banners & ads', 'Price per day for a banner.'],
                   product: ['Product settings', "OffrO's product day price and free product window. Merchant product prices are not managed here."] };
    var t = titles[st.tab];
    var html = h`<div class="pp-card">${raw(priceHead(t[0], t[1]))}`;
    if (!c) return html + '<div class="pp-help">No country is available.</div></div>';
    if (!p) return html + loaderHtml() + '</div>';
    html += taxBanner();
    html += editPanel();
    html += '<div class="pp-tablewrap"><table class="pp-table">' + priceTable() + '</table></div>';
    html += h`<div class="pp-foot">${p.is_india ? 'India values are the live ones and are read and saved through the existing pricing fields.' : 'Prices for this country are independent. No exchange rate is applied.'}</div></div>`;
    return html;
  }

  function actionsCell(key, row, opts) {
    opts = opts || {};
    if (!canEdit()) return '<span class="pp-help">View only</span>';
    var toggle = '';
    if (opts.toggle !== false) {
      toggle = row.configured
        ? h`<button type="button" class="pp-btn" data-act="toggle" data-key="${key}" data-id="${opts.id || ''}" data-to="${row.active ? '0' : '1'}">${row.active ? 'Deactivate' : 'Activate'}</button>` : '';
    } else if (opts.note) {
      toggle = h`<span class="pp-help">${opts.note}</span>`;
    }
    return h`<div class="pp-actions"><button type="button" class="pp-btn" data-act="edit" data-key="${key}" data-id="${opts.id || ''}">${row.configured ? 'Edit' : 'Set up'}</button>${raw(toggle)}</div>`;
  }

  function priceTable() {
    var p = st.prices, c = cur(), isIn = p.is_india, rows = '';
    var head = function (a, b) { return h`<thead><tr><th>${a}</th><th>${b}</th><th>Tax</th><th>Total payable</th><th>Status</th><th>Actions</th></tr></thead>`; };
    if (st.tab === 'store') {
      rows = head('Plan', 'Price') + '<tbody>' + p.store_plans.map(function (r) {
        return h`<tr><td><b>${r.label}</b></td><td>${r.configured ? money(c, r.price) : 'Not configured'}</td><td>${r.configured ? taxText(p.tax) : '-'}</td><td>${money(c, r.total)}</td><td>${raw(pill(r.active, r.configured))}</td><td>${raw(actionsCell('store', r, { id: r.id, toggle: !isIn, note: 'Live' }))}</td></tr>`;
      }).join('') + '</tbody>';
    } else if (st.tab === 'infl') {
      var r = p.influencer;
      rows = head('Plan', 'Fee') + h`<tbody><tr><td><b>Influencer subscription</b></td><td>${r.configured ? money(c, r.price) : 'Not configured'}</td><td>${r.configured ? taxText(r.tax && r.tax.label ? { configured: true, label: r.tax.label, percent: r.tax.percent, tax_free: r.tax.tax_free } : p.tax) : '-'}</td><td>${money(c, r.total)}</td><td>${raw(pill(r.active, r.configured))}</td><td>${raw(actionsCell('infl', r, { toggle: true }))}</td></tr></tbody>`;
    } else if (st.tab === 'banner') {
      var b = p.banner;
      rows = head('Item', 'Price per day') + h`<tbody><tr><td><b>Banner price per day</b></td><td>${b.configured ? money(c, b.price) : 'Not configured'}</td><td>${b.configured ? taxText(p.tax) : '-'}</td><td>${money(c, b.total)}</td><td>${raw(pill(b.active, b.configured))}</td><td>${raw(actionsCell('banner', b, { toggle: !isIn, note: 'Live' }))}</td></tr></tbody>`;
    } else {
      var q = p.product;
      rows = h`<thead><tr><th>Setting</th><th>Value</th><th>Tax</th><th>Total</th><th>Status</th><th>Actions</th></tr></thead><tbody>
        <tr><td><b>Product day price</b></td><td>${q.configured ? money(c, q.price) : 'Not configured'}</td><td>${q.configured ? taxText(p.tax) : '-'}</td><td>${money(c, q.total)}</td>
            <td rowspan="3">${raw(pill(q.active, q.configured))}</td><td rowspan="3">${raw(actionsCell('product', q, { toggle: !isIn, note: 'Live' }))}</td></tr>
        <tr><td><b>Free product window</b></td><td>${q.configured ? q.free_product_limit + ' products' : 'Not configured'}</td><td>Not applicable</td><td>-</td></tr>
        <tr><td><b>Maximum validity</b></td><td>${q.configured ? q.product_max_validity_days + ' days' : 'Not configured'}</td><td>Not applicable</td><td>-</td></tr></tbody>`;
    }
    return rows;
  }

  // ── edit panel (price tabs) ──────────────────────────────────────────────────────────────────────────
  function field(id, label, val, type, extra) {
    return h`<div><label for="${id}">${label}</label><input id="${id}" type="${type || 'text'}" value="${val === null || val === undefined ? '' : val}" ${raw(extra || '')} autocomplete="off"></div>`;
  }
  function editPanel() {
    var e = st.edit, p = st.prices, c = cur();
    if (!e || e.kind !== 'price' || !p) return '';
    var sym = c ? c.currency_code : '';
    var f = '', title = '';
    if (e.key === 'store') {
      var r = p.store_plans.filter(function (x) { return x.id === e.id; })[0] || {};
      title = 'Edit ' + (r.label || 'plan') + ' price';
      f = field('pp_f_price', 'Price (' + sym + ')', e.vals ? e.vals.price : (r.price === null ? '' : r.price), 'number', 'min="0" step="any"');
    } else if (e.key === 'infl') {
      title = 'Edit influencer subscription fee';
      f = field('pp_f_price', 'Fee (' + sym + ')', e.vals ? e.vals.price : (p.influencer.price === null ? '' : p.influencer.price), 'number', 'min="0" step="any"');
    } else if (e.key === 'banner') {
      title = 'Edit banner price per day';
      f = field('pp_f_price', 'Price per day (' + sym + ')', e.vals ? e.vals.price : (p.banner.price === null ? '' : p.banner.price), 'number', 'min="0" step="any"');
    } else if (e.key === 'product') {
      var q = p.product, v = e.vals || {};
      title = 'Edit product settings';
      f = field('pp_f_price', 'Product day price (' + sym + ')', v.price !== undefined ? v.price : (q.price === null ? '' : q.price), 'number', 'min="0" step="any"') +
          field('pp_f_free', 'Free product window (products)', v.free !== undefined ? v.free : (q.free_product_limit === null ? '' : q.free_product_limit), 'number', 'min="0" step="1"') +
          field('pp_f_valid', 'Maximum validity (days)', v.valid !== undefined ? v.valid : (q.product_max_validity_days === null ? '' : q.product_max_validity_days), 'number', 'min="1" step="1"');
    }
    return h`<div class="pp-edit"><h4>${title}</h4><div class="pp-form">${raw(f)}</div>
      ${raw(e.err ? h`<div class="pp-ferr" role="alert">${e.err}</div>` : '')}
      <div class="pp-toolbar" style="margin-top:12px"><button type="button" class="pp-btn pri" data-act="save">Save</button><button type="button" class="pp-btn" data-act="cancel">Cancel</button></div></div>`;
  }

  // ── GST & VAT tab ────────────────────────────────────────────────────────────────────────────────────
  function taxView() {
    var e = st.edit, html = '<div class="pp-card"><div class="pp-head"><div><div class="pp-title">GST &amp; VAT</div><div class="pp-help">India keeps its single GST percentage. Every other country has its own tax label and rate, or is explicitly tax-free.</div></div><div class="pp-toolbar"><button type="button" class="pp-btn" data-act="history">View change history</button></div></div>';
    if (e && e.kind === 'tax') html += taxForm(e);
    html += '<div class="pp-tablewrap"><table class="pp-table"><thead><tr><th>Country</th><th>Tax label</th><th>Rate</th><th>Status</th><th>Actions</th></tr></thead><tbody>';
    html += st.countries.map(function (c) {
      var label = c.tax_free ? (c.tax_label || 'No tax') : (c.tax_label || '-');
      var rate = c.tax_free ? '0% (tax-free)' : (c.tax_percent === null || c.tax_percent === undefined ? 'Not set' : pct(c.tax_percent) + '%');
      return h`<tr><td><b>${c.name}</b> <span class="pp-help">${c.code}</span></td><td>${label}</td><td>${rate}</td><td>${raw(c.tax_configured ? '<span class="pill a">Configured</span>' : '<span class="pill w">Not configured</span>')}</td>
        <td>${raw(canEdit() ? h`<div class="pp-actions"><button type="button" class="pp-btn" data-act="edit-tax" data-code="${c.code}">Edit</button></div>` : '<span class="pp-help">View only</span>')}</td></tr>`;
    }).join('') + '</tbody></table></div>';
    var missing = st.countries.filter(function (c) { return !c.tax_configured; });
    if (missing.length) html += h`<div class="pp-alert err" role="alert" style="margin-top:12px">Tax is not configured for: ${missing.map(function (c) { return c.name; }).join(', ')}. No other country's rate is applied to them.</div>`;
    return html + '</div>';
  }
  function taxForm(e) {
    var c = st.countries.filter(function (x) { return x.code === e.code; })[0];
    if (!c) return '';
    var v = e.vals || {};
    var isIn = c.code === 'IN';
    var label = v.label !== undefined ? v.label : c.tax_label, pct = v.pct !== undefined ? v.pct : (c.tax_percent === null || c.tax_percent === undefined ? '' : c.tax_percent);
    var free = v.free !== undefined ? v.free : c.tax_free;
    var body = isIn
      ? field('pp_t_pct', 'GST percentage', pct, 'number', 'min="0" max="100" step="any"') + '<div class="pp-help full">India uses one GST percentage. It is the same value the existing Pricing &amp; GST screen edits.</div>'
      : field('pp_t_label', 'Tax label (GST, VAT, Sales Tax ...)', label, 'text', 'maxlength="30"') + field('pp_t_pct', 'Tax rate (%)', pct, 'number', 'min="0" max="100" step="any"') +
        h`<div><label for="pp_t_free"><input id="pp_t_free" type="checkbox" ${free ? raw('checked') : ''}> This country has no tax</label></div>`;
    return h`<div class="pp-edit"><h4>Tax for ${c.name}</h4><div class="pp-form">${raw(body)}</div>
      ${raw(e.err ? h`<div class="pp-ferr" role="alert">${e.err}</div>` : '')}
      <div class="pp-toolbar" style="margin-top:12px"><button type="button" class="pp-btn pri" data-act="save-tax">Save</button><button type="button" class="pp-btn" data-act="cancel">Cancel</button></div></div>`;
  }

  // ── Countries & Currencies tab ───────────────────────────────────────────────────────────────────────
  function countriesView() {
    var e = st.edit;
    var html = '<div class="pp-card"><div class="pp-head"><div><div class="pp-title">Countries &amp; currencies</div><div class="pp-help">Add a country here to price it. Each country has its own currency and its own price list; no conversion between them.</div></div><div class="pp-toolbar">';
    if (canEdit() && st.canAdd) html += '<button type="button" class="pp-btn pri" data-act="add-country">Add Country</button>';
    html += '<button type="button" class="pp-btn" data-act="history">View change history</button></div></div>';
    if (e && e.kind === 'country') html += countryForm(e);
    html += '<div class="pp-tablewrap"><table class="pp-table"><thead><tr><th>Country</th><th>Code</th><th>Currency</th><th>Symbol</th><th>Tax</th><th>Status</th><th>Actions</th></tr></thead><tbody>';
    html += st.countries.map(function (c) {
      var tax = c.tax_free ? 'Tax-free' : (c.tax_configured ? c.tax_label + ' ' + pct(c.tax_percent) + '%' : 'Not configured');
      var acts = '<span class="pp-help">View only</span>';
      if (canEdit()) {
        acts = c.built_in
          ? h`<div class="pp-actions"><span class="pp-help">Built-in default</span></div>`
          : h`<div class="pp-actions"><button type="button" class="pp-btn" data-act="edit-country" data-code="${c.code}">Edit</button><button type="button" class="pp-btn" data-act="country-status" data-code="${c.code}" data-to="${c.status === 'active' ? 'inactive' : 'active'}">${c.status === 'active' ? 'Deactivate' : 'Activate'}</button></div>`;
      }
      return h`<tr><td><b>${c.name}</b></td><td>${c.code}</td><td>${c.currency_name} (${c.currency_code})</td><td>${c.currency_symbol}</td><td>${tax}</td><td>${raw(pill(c.status === 'active', true))}</td><td>${raw(acts)}</td></tr>`;
    }).join('') + '</tbody></table></div></div>';
    return html;
  }
  function countryForm(e) {
    var v = e.vals || {}, adding = e.mode === 'add';
    var g = function (k) { return v[k] !== undefined ? v[k] : ''; };
    var body = field('pp_c_name', 'Country name', g('name'), 'text', 'maxlength="60"') +
      field('pp_c_code', 'Country code (ISO, 2 letters)', g('code'), 'text', 'maxlength="2" ' + (adding ? '' : 'disabled')) +
      field('pp_c_cname', 'Currency name', g('currency_name'), 'text', 'maxlength="40"') +
      field('pp_c_ccode', 'Currency code (3 letters, e.g. INR, SAR, JOD)', g('currency_code'), 'text', 'maxlength="3"') +
      field('pp_c_sym', 'Currency symbol', g('currency_symbol'), 'text', 'maxlength="8"') +
      field('pp_c_tlabel', 'Tax type / label (GST, VAT, Sales Tax ...)', g('tax_label'), 'text', 'maxlength="30"') +
      field('pp_c_trate', 'Tax percentage / rate', g('tax_percent'), 'number', 'min="0" max="100" step="any"') +
      h`<div><label for="pp_c_free"><input id="pp_c_free" type="checkbox" ${v.tax_free ? raw('checked') : ''}> This country has no tax</label></div>` +
      h`<div><label for="pp_c_status">Status</label><select id="pp_c_status"><option value="active" ${g('status') !== 'inactive' ? raw('selected') : ''}>Active</option><option value="inactive" ${g('status') === 'inactive' ? raw('selected') : ''}>Inactive</option></select></div>`;
    return h`<div class="pp-edit"><h4>${adding ? 'Add country' : 'Edit ' + (v.name || 'country')}</h4><div class="pp-form">${raw(body)}</div>
      <div class="pp-help" style="margin-top:8px">Tax is optional here. Until a rate (or "no tax") is set the country shows a configuration error and no tax is applied from any other country.</div>
      ${raw(e.err ? h`<div class="pp-ferr" role="alert">${e.err}</div>` : '')}
      <div class="pp-toolbar" style="margin-top:12px"><button type="button" class="pp-btn pri" data-act="save-country">${adding ? 'Add country' : 'Save'}</button><button type="button" class="pp-btn" data-act="cancel">Cancel</button></div></div>`;
  }

  // ── Reward Redemption Points tab ─────────────────────────────────────────────────────────────────────
  function rewardView() {
    var e = st.edit;
    var html = '<div class="pp-card"><div class="pp-head"><div><div class="pp-title">Reward redemption points</div><div class="pp-help">How many points a country needs to redeem, and what one point is worth in that country\'s currency. India\'s settings are never applied to another country.</div></div><div class="pp-toolbar"><button type="button" class="pp-btn" data-act="history">View change history</button></div></div>';
    if (e && e.kind === 'reward') html += rewardForm(e);
    html += '<div class="pp-tablewrap"><table class="pp-table"><thead><tr><th>Country</th><th>Value of 1 point</th><th>Minimum points to redeem</th><th>Status</th><th>Actions</th></tr></thead><tbody>';
    html += st.countries.map(function (c) {
      var r = c.reward || {};
      var cfg = !!r.configured;
      return h`<tr><td><b>${c.name}</b> <span class="pp-help">${c.code}</span></td><td>${cfg ? c.currency_symbol + ' ' + num(r.conversion_rate) : 'Not configured'}</td><td>${cfg ? r.min_withdraw_points : 'Not configured'}</td>
        <td>${raw(cfg ? pill(r.active, true) : '<span class="pill w">Not configured</span>')}</td>
        <td>${raw(canEdit() ? h`<div class="pp-actions"><button type="button" class="pp-btn" data-act="edit-reward" data-code="${c.code}">${cfg ? 'Edit' : 'Set up'}</button>${raw(cfg && c.code !== 'IN' ? h`<button type="button" class="pp-btn" data-act="reward-toggle" data-code="${c.code}" data-to="${r.active ? '0' : '1'}">${r.active ? 'Deactivate' : 'Activate'}</button>` : '')}</div>` : '<span class="pp-help">View only</span>')}</td></tr>`;
    }).join('') + '</tbody></table></div>';
    var miss = st.countries.filter(function (c) { return !(c.reward && c.reward.configured); });
    if (miss.length) html += h`<div class="pp-alert err" role="alert" style="margin-top:12px">Reward conversion is not configured for: ${miss.map(function (c) { return c.name; }).join(', ')}. Redemption settings from another country are not used for them.</div>`;
    return html + '</div>';
  }
  function rewardForm(e) {
    var c = st.countries.filter(function (x) { return x.code === e.code; })[0];
    if (!c) return '';
    var r = c.reward || {}, v = e.vals || {};
    var rate = v.rate !== undefined ? v.rate : (r.configured ? r.conversion_rate : '');
    var min = v.min !== undefined ? v.min : (r.configured ? r.min_withdraw_points : '');
    var ex = (rate !== '' && isFinite(Number(rate))) ? '100 points = ' + c.currency_symbol + ' ' + num(100 * Number(rate)) : '';
    return h`<div class="pp-edit"><h4>Reward redemption: ${c.name}</h4><div class="pp-form">
      ${raw(field('pp_r_rate', 'Value of 1 point (' + c.currency_code + ')', rate, 'number', 'min="0" step="any"'))}
      ${raw(field('pp_r_min', 'Minimum points to redeem', min, 'number', 'min="1" step="1"'))}</div>
      <div class="pp-help" style="margin-top:8px">${ex}</div>
      ${raw(e.err ? h`<div class="pp-ferr" role="alert">${e.err}</div>` : '')}
      <div class="pp-toolbar" style="margin-top:12px"><button type="button" class="pp-btn pri" data-act="save-reward">Save</button><button type="button" class="pp-btn" data-act="cancel">Cancel</button></div></div>`;
  }

  // ── history ─────────────────────────────────────────────────────────────────────────────────────────
  async function openHistory() {
    setHtml($('pp_msg'), loaderHtml());
    try {
      var q = (st.tab === 'countries' || st.tab === 'tax' || st.tab === 'reward') ? '' : '?country=' + encodeURIComponent(st.country);
      var d = await api('/history' + q);
      var rows = (d.history || []).map(function (x) {
        var o = x.old || {}, n = x.new || {};
        var keys = Object.keys(o).concat(Object.keys(n).filter(function (k) { return !(k in o); }));
        var lines = keys.map(function (k) { return h`<div>${k}: ${o[k] === undefined || o[k] === null ? '(none)' : String(o[k])} → ${n[k] === undefined || n[k] === null ? '(none)' : String(n[k])}</div>`; }).join('');
        return h`<tr><td>${x.at.replace('T', ' ').slice(0, 16)}</td><td>${x.country_code}</td><td>${x.section}</td><td>${x.action}</td><td>${x.by}</td><td style="font-size:12px;word-break:break-word">${raw(lines)}</td></tr>`;
      }).join('');
      var host = $('pp_msg');
      setHtml(host, h`<div class="pp-card"><div class="pp-head"><div class="pp-title">Change history</div><button type="button" class="pp-btn" data-act="close-history">Close</button></div>
        <div class="pp-tablewrap"><table class="pp-table"><thead><tr><th>When (UTC)</th><th>Country</th><th>Section</th><th>Action</th><th>By</th><th>Change</th></tr></thead><tbody>${raw(rows || '<tr><td colspan="6">No changes recorded yet.</td></tr>')}</tbody></table></div></div>`);
    } catch (e) { flash('err', e.message); }
  }

  // ── actions ──────────────────────────────────────────────────────────────────────────────────────────
  function val(id) { var el = $(id); return el ? el.value : ''; }
  async function put(path, body, okText) {
    var d = await api(path, { method: 'PUT', body: body });
    flash('ok', okText || 'Saved.');
    return d;
  }
  async function onSave() {
    var e = st.edit, section = SECTION_OF[st.tab], body;
    if (!e) return;
    if (e.key === 'store') body = { plans: [{ id: e.id, price: val('pp_f_price') }] };
    else if (e.key === 'infl') body = { fee: val('pp_f_price') };
    else if (e.key === 'banner') body = { price_per_day: val('pp_f_price') };
    else body = { price_per_day: val('pp_f_price'), free_product_limit: val('pp_f_free'), product_max_validity_days: val('pp_f_valid') };
    try {
      await put('/countries/' + encodeURIComponent(st.country) + '/prices/' + section, body);
      st.edit = null; await afterSave(true);
    } catch (er) { e.err = er.message; e.vals = { price: val('pp_f_price'), free: val('pp_f_free'), valid: val('pp_f_valid') }; render(); }
  }
  async function onToggle(key, id, to) {
    var on = to === '1', section = SECTION_OF[st.tab], body;
    if (key === 'store') body = { plans: [{ id: id, active: on }] };
    else if (key === 'infl') body = { enabled: on };
    else body = { active: on };
    try {
      await put('/countries/' + encodeURIComponent(st.country) + '/prices/' + section, body, on ? 'Activated.' : 'Deactivated.');
      await afterSave(true);
    } catch (er) { flash('err', er.message); }
  }
  async function onSaveTax() {
    var e = st.edit, c = st.countries.filter(function (x) { return x.code === e.code; })[0], body;
    var isIn = e.code === 'IN';
    body = isIn ? { tax_percent: val('pp_t_pct') } : { tax_label: val('pp_t_label'), tax_percent: val('pp_t_pct'), tax_free: !!($('pp_t_free') && $('pp_t_free').checked) };
    if (!isIn) { var cc = c || {}; body.name = cc.name; body.currency_name = cc.currency_name; body.currency_code = cc.currency_code; body.currency_symbol = cc.currency_symbol; }
    try {
      await put('/countries/' + encodeURIComponent(e.code), body, 'Tax saved.');
      st.edit = null; await afterSave(false);
    } catch (er) { e.err = er.message; e.vals = { label: val('pp_t_label'), pct: val('pp_t_pct'), free: !!($('pp_t_free') && $('pp_t_free').checked) }; render(); }
  }
  function countryBody() {
    return { name: val('pp_c_name'), code: val('pp_c_code'), currency_name: val('pp_c_cname'), currency_code: val('pp_c_ccode'), currency_symbol: val('pp_c_sym'),
             tax_label: val('pp_c_tlabel'), tax_percent: val('pp_c_trate'), tax_free: !!($('pp_c_free') && $('pp_c_free').checked), status: val('pp_c_status') };
  }
  async function onSaveCountry() {
    var e = st.edit, b = countryBody();
    try {
      if (e.mode === 'add') { await api('/countries', { method: 'POST', body: b }); flash('ok', 'Country added.'); }
      else { await api('/countries/' + encodeURIComponent(e.code), { method: 'PUT', body: b }); flash('ok', 'Country saved.'); }
      st.edit = null; await afterSave(false);
    } catch (er) { e.err = er.message; e.vals = Object.assign({}, b, { code: e.mode === 'add' ? b.code : e.code }); render(); }
  }
  async function onSaveReward() {
    var e = st.edit;
    try {
      await put('/countries/' + encodeURIComponent(e.code) + '/prices/reward', { conversion_rate: val('pp_r_rate'), min_withdraw_points: val('pp_r_min') }, 'Reward settings saved.');
      st.edit = null; await afterSave(false);
    } catch (er) { e.err = er.message; e.vals = { rate: val('pp_r_rate'), min: val('pp_r_min') }; render(); }
  }

  function onClick(ev) {
    var t = ev.target.closest ? ev.target.closest('[data-act],[data-tab]') : null;
    if (!t) return;
    var tab = t.getAttribute('data-tab');
    if (tab) { if (st.busy) return; st.tab = tab; st.edit = null; st.msg = null; refresh(); return; }
    var act = t.getAttribute('data-act'), code = t.getAttribute('data-code'), key = t.getAttribute('data-key'), id = t.getAttribute('data-id');
    st.msg = act === 'history' ? st.msg : null;
    if (act === 'edit') { st.edit = { kind: 'price', key: key, id: id }; render(); }
    else if (act === 'cancel') { st.edit = null; render(); }
    else if (act === 'save') guarded(t, onSave);
    else if (act === 'toggle') guarded(t, function () { return onToggle(key, id, t.getAttribute('data-to')); });
    else if (act === 'edit-tax') { st.edit = { kind: 'tax', code: code }; render(); }
    else if (act === 'save-tax') guarded(t, onSaveTax);
    else if (act === 'add-country') { st.edit = { kind: 'country', mode: 'add', vals: { status: 'active' } }; render(); }
    else if (act === 'edit-country') {
      var c = st.countries.filter(function (x) { return x.code === code; })[0];
      st.edit = { kind: 'country', mode: 'edit', code: code, vals: c ? { name: c.name, code: c.code, currency_name: c.currency_name, currency_code: c.currency_code, currency_symbol: c.currency_symbol, tax_label: c.tax_label, tax_percent: c.tax_percent === null ? '' : c.tax_percent, tax_free: c.tax_free, status: c.status } : {} };
      render();
    }
    else if (act === 'save-country') guarded(t, onSaveCountry);
    else if (act === 'country-status') {
      guarded(t, async function () {
        try { await api('/countries/' + encodeURIComponent(code) + '/status', { method: 'POST', body: { status: t.getAttribute('data-to') } }); flash('ok', 'Country status updated.'); await afterSave(false); }
        catch (er) { flash('err', er.message); }
      });
    }
    else if (act === 'edit-reward') { st.edit = { kind: 'reward', code: code }; render(); }
    else if (act === 'save-reward') guarded(t, onSaveReward);
    else if (act === 'reward-toggle') {
      guarded(t, async function () {
        try { await put('/countries/' + encodeURIComponent(code) + '/prices/reward', { active: t.getAttribute('data-to') === '1' }, 'Reward status updated.'); await afterSave(false); }
        catch (er) { flash('err', er.message); }
      });
    }
    else if (act === 'retry') refresh();
    else if (act === 'history') openHistory();
    else if (act === 'close-history') { st.msg = null; renderMsg(); }
  }
  function onChange(ev) {
    if (ev.target && ev.target.id === 'pp_country') {
      if (st.busy) return;
      st.country = ev.target.value; st.edit = null; st.msg = null; st.loadErr = null;
      var cs = st.country, pr = loadPrices();     // loadPrices() clears st.prices synchronously, so the table shows its loading state
      render();
      pr.then(function () { if (cs === st.country) render(); })
        .catch(function (er) { if (cs !== st.country) return; st.loadErr = er.message; render(); });
    }
  }

  var wired = false;
  window.PricingPlans = {
    open: function () {
      var root = $('section-pricing_plans');
      if (root && !wired) { root.addEventListener('click', onClick); root.addEventListener('change', onChange); wired = true; }
      st.edit = null; st.msg = null;
      refresh();
    },
    _state: st
  };
})();
