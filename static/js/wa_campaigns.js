/* WhatsApp Campaigns (Phase 2G) - admin dashboard module: core (shell, tabs, helpers, overview, campaign list).
 *
 * The other screens live in wa_campaigns_audience.js, wa_campaigns_templates.js, wa_campaigns_editor.js and wa_campaigns_admin.js and
 * plug into this file through window.WaCampaigns.kit / register(). The server is the single source of truth for permissions, consent,
 * eligibility, the environment guard and the sending kill switch; the browser only displays what the existing
 * /admin/whatsapp-campaigns API returns and sends the admin's explicit requests. Nothing here sends a message on its own: opening or
 * saving a campaign never sends. Phone numbers are shown masked only; a number an admin types (test send, consent, manual import) is sent
 * once and cleared, never stored in the browser and never shown back.
 *
 * Output safety: every HTML string is built with the h`...` tag, which escapes every interpolated value unless it is wrapped
 * in raw() (only used for markup produced by this module's own helpers). innerHTML is assigned only through setHtml().
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
  // UX only: hides actions the role cannot use. The server enforces every permission on every request regardless.
  function can(action) {
    return typeof window._rbacCanDo === 'function' ? !!window._rbacCanDo('wa_campaigns', action) : true;
  }

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
  function ApiError(status, message, detail) { this.status = status; this.message = message; this.detail = detail; }

  function detailText(data, status) {
    var d = data && data.detail;
    if (typeof d === 'string' && d) return d;
    if (d && typeof d === 'object' && !Array.isArray(d) && typeof d.message === 'string') return d.message;
    if (status === 403) return DEFAULT_DENIED;
    if (status === 422) return 'Some of the values are not valid. Check the form and try again.';
    if (status === 401) return 'Your session has expired. Please sign in again.';
    if (status >= 500) return 'The server could not complete the request. Please try again.';
    return 'The request could not be completed (' + status + ').';
  }

  // One request function for the whole module. opts: {method, json, form}. Credentials are the dashboard's session cookie.
  async function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', credentials: 'include', headers: { 'Accept': 'application/json' } };
    if (opts.json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
    else if (opts.form) { init.body = opts.form; }                // multipart: the browser sets the boundary
    var res;
    try {
      res = await fetch(API + path, init);
    } catch (e) {
      throw new ApiError(0, 'Could not reach the server. Check your connection and try again.');
    }
    if (res.status === 401) {
      window.location.href = '/admin';
      throw new ApiError(401, detailText(null, 401));
    }
    var data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw new ApiError(res.status, detailText(data, res.status), data && data.detail);
    return data;
  }

  // ── state ────────────────────────────────────────────────────────────────────────────────
  var st = { status: '', page: 1, pages: 1, total: 0, listSeq: 0, overviewSeq: 0, counts: {}, tab: 'overview', cfg: null };
  var modules = {};                                               // name -> {pane, title, open, close?}
  var handlers = {};                                              // data-act / data-change / data-input / data-submit name -> fn(el, ev)
  var TABS = [['overview', 'Overview'], ['campaigns', 'Campaigns'], ['audience', 'Audience'], ['templates', 'Templates'],
              ['consent', 'Consent'], ['audit', 'Audit']];

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
    environment_unknown: 'Blocked: the environment is not identified (set WA_CAMPAIGN_ENVIRONMENT).',
    env_disabled: 'Blocked: the sending environment key is off.',
    switch_off: 'Blocked: the kill switch is off.',
    settings_unavailable: 'Blocked: settings unavailable.',
    gate_unavailable: 'Blocked: the gate could not be checked.'
  };

  function card(label, dot, value, sub, extraHtml) {                  // extraHtml: trusted markup from this file (buttons)
    return h`<div class="col-12 col-sm-6 col-lg-4"><div class="wac-card"><div class="wac-card-label">${label}</div>` +
           h`<div class="wac-card-value"><span class="wac-dot ${dot}"></span>${value}</div>` +
           (sub ? h`<div class="wac-card-sub">${sub}</div>` : '') + (extraHtml || '') + '</div></div>';
  }
  function errorCard(label, message) {
    return h`<div class="col-12 col-sm-6 col-lg-4"><div class="wac-card is-error"><div class="wac-card-label">${label}</div>` +
           h`<div class="wac-card-value"><span class="wac-dot bad"></span>Unavailable</div><div class="wac-card-sub">${message}</div></div></div>`;
  }

  var ENV_LABEL = { production: 'Production', staging: 'Staging', development: 'Development', test: 'Test', unknown: 'Not identified' };
  function environmentCard(env) {
    var c = env.classification || 'unknown', dot = c === 'production' ? 'bad' : (c === 'unknown' ? 'warn' : 'on'), sub;
    if (c === 'unknown') {
      sub = 'This server does not say what environment it is, so all sending is refused. Set WA_CAMPAIGN_ENVIRONMENT to staging (or production) in the hosting settings.';
    } else if (env.conflict) {
      sub = 'Conflict: the platform reports Production (' + (env.production_signals || []).join(', ') + ') while WA_CAMPAIGN_ENVIRONMENT says ' + env.explicit + '. Treated as Production.';
    } else if (env.source === 'explicit') {
      sub = 'Set by WA_CAMPAIGN_ENVIRONMENT.' + (env.overridden_signals && env.overridden_signals.length ? ' Ignored production-looking variables: ' + env.overridden_signals.join(', ') + '.' : '');
    } else {
      sub = 'Detected from: ' + ((env.production_signals || []).join(', ') || 'platform variables') + '. Set WA_CAMPAIGN_ENVIRONMENT to make it explicit.';
    }
    return card('Environment', dot, ENV_LABEL[c] || c, sub);
  }

  function switchControls(s) {
    if (s.switch_on === true) {
      return can('edit') ? '<div class="wac-card-actions"><button type="button" class="btn-d" data-act="sending-off">Turn sending OFF</button></div>' : '';
    }
    return can('approve') ? '<div class="wac-card-actions"><button type="button" class="btn-p" data-act="sending-on">Turn sending ON</button></div>' : '';
  }

  function renderCards(cfg, worker) {
    var html = '';
    if (cfg.error) {
      html += errorCard('Overall sending', cfg.error);
    } else {
      var c = cfg.data || {}, s = c.sending || {};
      html += card('Overall sending (key + kill switch)', s.allowed ? 'on' : 'off', s.allowed ? 'On' : 'Off',
                   (SENDING_REASON[s.reason] ? SENDING_REASON[s.reason] + ' ' : '') +
                   'Applies to the worker and to test sends.');
      html += card('Environment key', s.env_key_on ? 'on' : 'off', s.env_key_on ? 'Set' : 'Not set',
                   'WA_CAMPAIGN_SENDING_ENABLED on this server. It can only be changed in the hosting settings.');
      html += card('Kill switch', s.switch_on === true ? 'on' : (s.switch_on === false ? 'off' : 'warn'),
                   s.switch_on === true ? 'On' : (s.switch_on === false ? 'Off' : 'Unknown'),
                   'The runtime switch. Turning it off always works; turning it on needs approval and the environment key.', switchControls(s));
      var env = c.environment || {};
      html += environmentCard(env);
      html += card('Test numbers', 'off', num(c.test_numbers_configured),
                   (c.test_numbers_masked && c.test_numbers_masked.length ? 'Allow-listed: ' + c.test_numbers_masked.join(', ') + '. ' : '') +
                   'A test send may only reach these numbers.');
    }
    if (worker.error) {
      html += errorCard('Worker', worker.error);
    } else {
      var w = worker.data || {}, g = w.gate || {};
      html += card('Worker', g.ok ? 'on' : 'off', g.ok ? 'May run' : 'Stopped', WORKER_REASON[g.reason] || '');
      html += card('Production guard', w.production_blocked ? 'bad' : 'on', w.production_blocked ? 'Sending blocked here' : 'Not blocking',
                   (w.production_blocked ? 'Because of the environment above (Production, a conflict, or not identified), neither the bulk worker nor single-number test sends will send.'
                                         : 'The environment above is not Production, so this guard allows the worker and test sends; the key, kill switch, consent and allow-list checks still apply.'));
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
    return h`<tr><td><button type="button" class="wac-link wac-name" data-act="open-campaign" data-id="${c.id}">${c.name || 'Untitled'}</button></td><td>${raw(badge(c.status))}</td><td>${raw(tpl)}</td>` +
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

  // ── dialogs and messages ──────────────────────────────────────────────────────────────────
  var toastTimer = null;
  function toast(text, kind) {
    var el = $('wac_toast');
    if (!el) return;
    el.className = 'wac-toast show ' + (kind || 'ok');
    el.textContent = text;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = 'wac-toast'; el.textContent = ''; }, kind === 'error' ? 9000 : 5000);
  }

  var dlg = null;
  // Explicit confirmation. opts: {title, bodyHtml (trusted), confirmLabel, danger, checkbox (label text), type (exact text to type)}.
  // Resolves true only when the admin presses the confirm button with every requirement satisfied; Cancel / Escape / backdrop resolve false.
  function confirmDialog(opts) {
    return new Promise(function (resolve) {
      if (dlg) { dlg.finish(false); }
      var root = document.createElement('div');
      root.className = 'modal-overlay open wac-dialog';
      root.setAttribute('role', 'dialog');
      root.setAttribute('aria-modal', 'true');
      var needs = (opts.checkbox ? 1 : 0) + (opts.type ? 1 : 0);
      setHtml(root, h`<div class="modal-box"><div class="modal-header"><div class="modal-title">${opts.title}</div></div>` +
        h`<div class="modal-body">${raw(opts.bodyHtml || '')}` +
        (opts.checkbox ? h`<label class="wac-check"><input type="checkbox" id="wac_dlg_ck"> <span>${opts.checkbox}</span></label>` : '') +
        (opts.type ? h`<div class="wac-typebox"><label class="form-label" for="wac_dlg_type">Type <b>${opts.type}</b> to confirm</label>` +
                     h`<input type="text" id="wac_dlg_type" class="form-control" autocomplete="off" inputmode="numeric"></div>` : '') +
        h`</div><div class="modal-footer"><button type="button" class="btn-o" id="wac_dlg_no">Cancel</button>` +
        h`<button type="button" class="${opts.danger ? 'btn-d' : 'btn-p'}" id="wac_dlg_yes"${raw(needs ? ' disabled' : '')}>${opts.confirmLabel || 'Confirm'}</button></div></div>`);
      document.body.appendChild(root);
      var yes = root.querySelector('#wac_dlg_yes'), no = root.querySelector('#wac_dlg_no');
      var ck = root.querySelector('#wac_dlg_ck'), ty = root.querySelector('#wac_dlg_type');
      function ready() {
        yes.disabled = !((!ck || ck.checked) && (!ty || ty.value.trim() === String(opts.type)));
      }
      function finish(v) {
        document.removeEventListener('keydown', onKey, true);
        if (root.parentNode) root.parentNode.removeChild(root);
        dlg = null;
        resolve(v);
      }
      function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); finish(false); } }
      if (ck) ck.addEventListener('change', ready);
      if (ty) ty.addEventListener('input', ready);
      yes.addEventListener('click', function () { if (!yes.disabled) finish(true); });
      no.addEventListener('click', function () { finish(false); });
      root.addEventListener('mousedown', function (e) { if (e.target === root) finish(false); });
      document.addEventListener('keydown', onKey, true);
      dlg = { finish: finish };
      no.focus();                                                  // the safe choice has focus, never the confirm button
    });
  }

  // Runs `fn` while the button is disabled so a double click cannot submit twice; reports errors as a toast.
  async function busy(el, fn) {
    if (el) { if (el.getAttribute('data-busy') === '1') return undefined; el.setAttribute('data-busy', '1'); el.disabled = true; }
    try { return await fn(); }
    catch (e) { if (e && e.status === 403) toast('You do not have permission for this action.', 'error'); else toast((e && e.message) || 'Something went wrong.', 'error'); return undefined; }
    finally { if (el) { el.removeAttribute('data-busy'); el.disabled = false; } }
  }

  // ── sending switch (kill switch) ──────────────────────────────────────────────────────────
  async function setSending(el, enabled) {
    var ok = await confirmDialog(enabled ? {
      title: 'Turn WhatsApp sending ON?', confirmLabel: 'Turn sending ON', danger: true,
      checkbox: 'I understand this lets scheduled campaigns and test sends reach real phones in this environment.',
      bodyHtml: '<p>This only flips the runtime switch. The server still requires the environment key, a non-Production environment, consent, and every campaign still needs its own scheduling confirmation.</p>'
    } : {
      title: 'Turn WhatsApp sending OFF?', confirmLabel: 'Turn sending OFF',
      bodyHtml: '<p>All sending stops immediately: running campaigns pause and test sends are refused.</p>'
    });
    if (!ok) return;
    await busy(el, async function () {
      var r = await api('/sending', { method: 'POST', json: { enabled: enabled } });
      toast(enabled ? 'Sending switch is ON (' + (r.sending_allowed ? 'sending is allowed' : 'still blocked: ' + r.reason) + ').' : 'Sending switch is OFF.', 'ok');
      loadOverview();
    });
  }

  // ── tabs ────────────────────────────────────────────────────────────────────────────────
  function renderTabs() {
    setHtml($('wac_tabs'), TABS.map(function (t) {
      return h`<button type="button" role="tab" class="wac-tab ${st.tab === t[0] ? 'active' : ''}" aria-selected="${st.tab === t[0]}" data-act="tab" data-tab="${t[0]}">${t[1]}</button>`;
    }).join(''));
  }

  function showTab(name) {
    st.tab = name;
    renderTabs();
    TABS.forEach(function (t) { var el = $('wac_pane_' + t[0]); if (el) el.hidden = t[0] !== name; });
    if (name === 'overview') loadOverview();
    else if (name === 'campaigns') { if (modules.editor && modules.editor.showList) modules.editor.showList(); loadOverview(); loadList(); }
    else if (modules[name] && modules[name].open) modules[name].open();
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
    var nb = document.querySelector('#wac_pane_campaigns [data-act="new-campaign"]');
    if (nb) nb.hidden = !can('add');                               // UX only; the server enforces 'add'
    showTab(st.tab || 'overview');
  }

  function dispatch(attr, ev) {
    var el = ev.target.closest ? ev.target.closest('[' + attr + ']') : null;
    var root = $('section-wa_campaigns');
    if (!el || !root || !root.contains(el)) return;
    var name = el.getAttribute(attr), fn = handlers[name];
    if (fn) fn(el, ev);
  }

  handlers['tab'] = function (el) { showTab(el.getAttribute('data-tab')); };
  handlers['chip'] = function (el) { st.status = el.getAttribute('data-status') || ''; st.page = 1; renderChips(); loadList(); };
  handlers['retry-list'] = function () { loadList(); };
  handlers['sending-on'] = function (el) { setSending(el, true); };
  handlers['sending-off'] = function (el) { setSending(el, false); };
  handlers['open-campaign'] = function (el) { if (modules.editor) modules.editor.openCampaign(el.getAttribute('data-id')); };
  handlers['new-campaign'] = function (el) { if (modules.editor) modules.editor.newCampaign(el); };

  function register(name, mod) { modules[name] = mod; if (mod.handlers) Object.keys(mod.handlers).forEach(function (k) { handlers[k] = mod.handlers[k]; }); }

  function init() {
    var sec = $('section-wa_campaigns');
    if (!sec) return;
    sec.addEventListener('click', function (ev) { dispatch('data-act', ev); });
    sec.addEventListener('change', function (ev) { dispatch('data-change', ev); });
    sec.addEventListener('input', function (ev) { dispatch('data-input', ev); });
    sec.addEventListener('submit', function (ev) { if (ev.target.closest && ev.target.closest('[data-submit]')) ev.preventDefault(); dispatch('data-submit', ev); });
    $('wac_refresh').addEventListener('click', function () { loadOverview(); loadList(); });
    $('wac_prev').addEventListener('click', function () { if (st.page > 1) { st.page -= 1; loadList(); } });
    $('wac_next').addEventListener('click', function () { if (st.page < st.pages) { st.page += 1; loadList(); } });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  window.WaCampaigns = {
    open: open, _state: st, register: register, showTab: showTab,
    kit: { h: h, raw: raw, esc: esc, setHtml: setHtml, $: $, fmtDateTime: fmtDateTime, num: num, api: api, ApiError: ApiError, can: can,
           toast: toast, confirm: confirmDialog, busy: busy, badge: badge, STATUSES: STATUSES, handlers: handlers, loadList: loadList, loadOverview: loadOverview,
           showDenied: showDenied }
  };
})();
