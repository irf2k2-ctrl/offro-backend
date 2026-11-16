/* WhatsApp Campaigns (Phase 2G) - admin dashboard module.
 *
 * Step 1: shell, overview and campaign list. READ-ONLY: this file only issues GET requests in this step.
 * The server is the single source of truth for permissions, consent, eligibility and the sending kill switch; this file only
 * displays what the existing /admin/whatsapp-campaigns API returns. Phone numbers are never requested or shown here.
 *
 * Output safety: every HTML string is built with the h`...` tag, which escapes every interpolated value unless it is wrapped
 * in raw() (only used for markup produced by this file's own helpers). innerHTML is assigned only through setHtml().
 */
(function () {
  'use strict';

  var API = '/admin/whatsapp-campaigns';
  var STATUSES = ['draft', 'ready', 'scheduled', 'running', 'paused', 'completed', 'cancelled'];
  var PAGE_SIZE = 25;
  var DEFAULT_DENIED = 'You do not have permission to view WhatsApp Campaigns. Ask a Super Admin to grant the module to your role.';

  // ── output helpers ───────────────────────────────────────────────────────────────────────
  function esc(v) {
    if (v === null || v === undefined || v === false) return '';
    return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function Raw(s) { this.s = s; }
  function raw(s) { return new Raw(s); }
  function h(strings) {
    var out = strings[0];
    for (var i = 1; i < strings.length; i++) {
      var v = arguments[i];
      out += (v instanceof Raw ? v.s : esc(v)) + strings[i];
    }
    return out;
  }
  function setHtml(el, html) { if (el) el.innerHTML = html; }
  function $(id) { return document.getElementById(id); }

  function fmtDateTime(iso) {
    if (!iso) return '—';
    var s = String(iso);
    if (!/(z|[+-]\d\d:?\d\d)$/i.test(s)) s += 'Z';                // the API returns UTC; treat a bare timestamp as UTC
    var d = new Date(s);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric',
                                       hour: '2-digit', minute: '2-digit', hour12: true });
  }
  function num(v) { return (typeof v === 'number' && isFinite(v)) ? String(v) : '—'; }

  // ── API ──────────────────────────────────────────────────────────────────────────────────
  function ApiError(status, message) { this.status = status; this.message = message; }

  function detailText(data, status) {
    var d = data && data.detail;
    if (typeof d === 'string' && d) return d;
    if (d && typeof d === 'object' && !Array.isArray(d) && typeof d.message === 'string') return d.message;
    if (status === 403) return DEFAULT_DENIED;
    if (status === 401) return 'Your session has expired. Please sign in again.';
    if (status >= 500) return 'The server could not complete the request. Please try again.';
    return 'The request could not be completed (' + status + ').';
  }

  async function api(path) {
    var res;
    try {
      res = await fetch(API + path, { credentials: 'include', headers: { 'Accept': 'application/json' } });
    } catch (e) {
      throw new ApiError(0, 'Could not reach the server. Check your connection and try again.');
    }
    if (res.status === 401) {
      window.location.href = '/admin';
      throw new ApiError(401, detailText(null, 401));
    }
    var data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw new ApiError(res.status, detailText(data, res.status));
    return data;
  }

  // ── state ────────────────────────────────────────────────────────────────────────────────
  var st = { status: '', page: 1, pages: 1, total: 0, listSeq: 0, overviewSeq: 0, counts: {} };

  function showDenied(text) {
    $('wac_body').hidden = true;
    $('wac_denied').hidden = false;
    $('wac_denied_text').textContent = text || DEFAULT_DENIED;
  }
  function hideDenied() {
    $('wac_denied').hidden = true;
    $('wac_body').hidden = false;
  }

  // ── status overview ──────────────────────────────────────────────────────────────────────
  var SENDING_REASON = {
    ok: 'Sending is enabled: the environment key and the kill switch are both on.',
    env_disabled: 'The environment key is not set on this server, so no message can be sent.',
    switch_off: 'The kill switch is off, so no message can be sent.',
    settings_unavailable: 'The settings could not be read; sending is treated as OFF.'
  };
  var WORKER_REASON = {
    ok: 'The worker may run.',
    worker_disabled: 'The worker is not enabled on this server.',
    production_blocked: 'Blocked: this server is a Production environment.',
    env_disabled: 'Blocked: the sending environment key is off.',
    switch_off: 'Blocked: the kill switch is off.',
    settings_unavailable: 'Blocked: settings unavailable.',
    gate_unavailable: 'Blocked: the gate could not be checked.'
  };

  function card(label, dot, value, sub) {
    return h`<div class="col-12 col-sm-6 col-lg-4"><div class="wac-card"><div class="wac-card-label">${label}</div>` +
           h`<div class="wac-card-value"><span class="wac-dot ${dot}"></span>${value}</div>` +
           (sub ? h`<div class="wac-card-sub">${sub}</div>` : '') + '</div></div>';
  }
  function errorCard(label, message) {
    return h`<div class="col-12 col-sm-6 col-lg-4"><div class="wac-card is-error"><div class="wac-card-label">${label}</div>` +
           h`<div class="wac-card-value"><span class="wac-dot bad"></span>Unavailable</div><div class="wac-card-sub">${message}</div></div></div>`;
  }

  function renderCards(cfg, worker) {
    var html = '';
    if (cfg.error) {
      html += errorCard('Overall sending', cfg.error);
    } else {
      var c = cfg.data || {}, s = c.sending || {};
      html += card('Overall sending (key + kill switch)', s.allowed ? 'on' : 'off', s.allowed ? 'On' : 'Off',
                   (SENDING_REASON[s.reason] ? SENDING_REASON[s.reason] + ' ' : '') +
                   'Applies to the worker and to test sends. It is not specific to Production.');
      html += card('Environment key', s.env_key_on ? 'on' : 'off', s.env_key_on ? 'Set' : 'Not set',
                   'WA_CAMPAIGN_SENDING_ENABLED on this server. It can only be changed in the hosting settings.');
      html += card('Kill switch', s.switch_on === true ? 'on' : (s.switch_on === false ? 'off' : 'warn'),
                   s.switch_on === true ? 'On' : (s.switch_on === false ? 'Off' : 'Unknown'),
                   'The runtime switch. Turning it off always works; turning it on needs approval and the environment key.');
      html += card('Test numbers', 'off', num(c.test_numbers_configured), 'Allow-listed numbers a test send may reach.');
    }
    if (worker.error) {
      html += errorCard('Worker', worker.error);
    } else {
      var w = worker.data || {}, g = w.gate || {};
      html += card('Worker', g.ok ? 'on' : 'off', g.ok ? 'May run' : 'Stopped', WORKER_REASON[g.reason] || '');
      html += card('Worker Production guard', w.production_blocked ? 'bad' : 'off', w.production_blocked ? 'Worker blocked' : 'Worker not blocked',
                   (w.production_blocked ? 'This server identifies as Production, so the bulk worker will not send.'
                                         : 'This server does not identify as Production, so this guard is not blocking the bulk worker.') +
                   ' This guard covers the worker only. Single-number test sends have no Production guard of their own; they depend on the key, the kill switch and the test-number allow-list.');
      html += card('Bulk live mode', w.live_key_on ? 'warn' : 'off', w.live_key_on ? 'Key on' : 'Key off',
                   'WA_CAMPAIGN_BULK_LIVE_ENABLED. A campaign still needs its own live confirmation.');
      html += card('Campaigns running', 'off', num(w.running_campaigns) + ' running / ' + num(w.paused_campaigns) + ' paused',
                   w.last_tick ? 'Last worker check: ' + fmtDateTime(w.last_tick.at) + ' IST' : 'The worker has not checked yet.');
    }
    setHtml($('wac_cards'), html);
  }

  async function settle(promise) {
    try { return { data: await promise }; } catch (e) { return { error: e.message, status: e.status }; }
  }

  function renderChips() {
    var html = h`<button type="button" class="wac-chip ${st.status === '' ? 'active' : ''}" data-act="chip" data-status="">All</button>`;
    STATUSES.forEach(function (s) {
      var n = st.counts[s];
      html += h`<button type="button" class="wac-chip ${st.status === s ? 'active' : ''}" data-act="chip" data-status="${s}">` +
              h`${s.charAt(0).toUpperCase() + s.slice(1)}${raw(typeof n === 'number' ? h`<span class="n">${n}</span>` : '')}</button>`;
    });
    setHtml($('wac_chips'), html);
  }

  async function loadOverview() {
    var seq = ++st.overviewSeq;
    setHtml($('wac_cards'), '<div class="col-12"><div class="wac-loading"><i class="fas fa-spinner fa-spin"></i> Loading status...</div></div>');
    var tasks = [settle(api('/config')), settle(api('/worker/status'))];
    STATUSES.forEach(function (s) { tasks.push(settle(api('/campaigns?status=' + s + '&page=1&page_size=1'))); });
    var res = await Promise.all(tasks);
    if (seq !== st.overviewSeq) return;
    if (res[0].status === 403 && res[1].status === 403) { showDenied(res[0].error); return; }
    renderCards(res[0], res[1]);
    for (var i = 0; i < STATUSES.length; i++) {
      var r = res[2 + i];
      st.counts[STATUSES[i]] = r.data && typeof r.data.total === 'number' ? r.data.total : undefined;
    }
    renderChips();
  }

  // ── campaign list ────────────────────────────────────────────────────────────────────────
  function badge(status) {
    var known = STATUSES.indexOf(status) !== -1;
    return h`<span class="wac-badge ${known ? status : 'unknown'}">${status || 'unknown'}</span>`;
  }

  function rowHtml(c) {
    var t = c.template;
    var tpl = t && t.name ? h`<code>${t.name}</code> <span class="wac-muted">${t.language || ''}</span>` : '<span class="wac-muted">Not chosen</span>';
    return h`<tr><td><span class="wac-name">${c.name || 'Untitled'}</span></td><td>${raw(badge(c.status))}</td><td>${raw(tpl)}</td>` +
           h`<td style="text-align:right">${num(c.eligible)}</td><td>${fmtDateTime(c.send_at)}</td><td>${c.created_by || '—'}</td>` +
           h`<td>${fmtDateTime(c.updated_at)}</td></tr>`;
  }

  function listState(text, isError, retry) {
    return h`<tr><td colspan="7" class="wac-cell-state ${isError ? 'is-error' : ''}">${text}` +
           (retry ? '<button type="button" class="btn-o" data-act="retry-list">Retry</button>' : '') + '</td></tr>';
  }

  function updatePager() {
    $('wac_prev').disabled = st.page <= 1;
    $('wac_next').disabled = st.page >= st.pages;
    $('wac_count').textContent = st.total ? ('Page ' + st.page + ' of ' + st.pages + ' · ' + st.total + ' campaign' + (st.total === 1 ? '' : 's')) : '';
  }

  async function loadList() {
    var seq = ++st.listSeq;
    $('wac_list_msg').textContent = '';
    $('wac_list_msg').className = 'wac-msg';
    setHtml($('wac_list'), '<tr><td colspan="7" class="wac-cell-state"><i class="fas fa-spinner fa-spin"></i> Loading campaigns...</td></tr>');
    var q = '/campaigns?page=' + st.page + '&page_size=' + PAGE_SIZE + (st.status ? '&status=' + encodeURIComponent(st.status) : '');
    var data;
    try {
      data = await api(q);
    } catch (e) {
      if (seq !== st.listSeq) return;
      if (e.status === 403) { showDenied(e.message); return; }
      st.total = 0; st.pages = 1; updatePager();
      setHtml($('wac_list'), listState(e.message, true, true));
      return;
    }
    if (seq !== st.listSeq) return;
    st.page = data.page || 1; st.pages = data.pages || 1; st.total = data.total || 0;
    var items = data.items || [];
    if (!items.length) {
      setHtml($('wac_list'), listState(st.status ? 'No ' + st.status + ' campaigns.' : 'No campaigns yet.', false, false));
    } else {
      setHtml($('wac_list'), items.map(rowHtml).join(''));
    }
    updatePager();
  }

  // ── wiring ───────────────────────────────────────────────────────────────────────────────
  function canViewHere() {
    if (typeof window._rbacCanView === 'function' && window._rbacMe && window._rbacMe.role !== 'Super Admin') {
      return !!window._rbacCanView('wa_campaigns');
    }
    return true;                                                   // the server still enforces the permission on every call
  }

  function open() {
    if (!canViewHere()) { showDenied(DEFAULT_DENIED); return; }
    hideDenied();
    renderChips();
    loadOverview();
    loadList();
  }

  function onClick(ev) {
    var el = ev.target.closest ? ev.target.closest('[data-act]') : null;
    if (!el || !$('section-wa_campaigns').contains(el)) return;
    var act = el.getAttribute('data-act');
    if (act === 'chip') { st.status = el.getAttribute('data-status') || ''; st.page = 1; renderChips(); loadList(); }
    else if (act === 'retry-list') { loadList(); }
  }

  function init() {
    var sec = $('section-wa_campaigns');
    if (!sec) return;
    sec.addEventListener('click', onClick);
    $('wac_refresh').addEventListener('click', function () { loadOverview(); loadList(); });
    $('wac_prev').addEventListener('click', function () { if (st.page > 1) { st.page -= 1; loadList(); } });
    $('wac_next').addEventListener('click', function () { if (st.page < st.pages) { st.page += 1; loadList(); } });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  window.WaCampaigns = { open: open, _state: st };
})();
