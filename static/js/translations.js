/* Translations (Phase H) - admin dashboard module.
 *
 * A thin client for the translation API (/admin/translations). Two sections only: Overview and Translations. Edit a translation (its
 * status becomes Modified) -> Publish Changes -> the app can fetch it, with no new app build. Staging and production dashboards are the
 * same: each one reads and writes ONLY its own server's database, and nothing here talks to the other environment. The server is the
 * single source of truth for permissions, the environment check (an unidentified environment refuses every change) and the audit log;
 * the browser only shows what the API returns and hides controls the caller cannot use.
 *
 * Output safety: every HTML string is built with the h`...` tag, which escapes every interpolated value unless wrapped in raw()
 * (only used for markup produced by this file). innerHTML is assigned only through setHtml(). Server values are never put in
 * attributes unescaped, and never evaluated.
 */
(function () {
  'use strict';

  var API = '/admin/translations';
  var DENIED = 'You do not have permission to view Translations. Ask a Super Admin to grant the module to your role.';
  var TRACK_LABEL = { app: 'App text', legal: 'Legal text' };

  // ── output helpers ───────────────────────────────────────────────────────────────────────
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
  function fmt(iso) {
    if (!iso) return '—';
    var s = String(iso); if (!/(z|[+-]\d\d:?\d\d)$/i.test(s)) s += 'Z';
    var d = new Date(s); if (isNaN(d.getTime())) return '—';
    return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: true });
  }

  // ── state ────────────────────────────────────────────────────────────────────────────────
  var st = { env: null, langs: [], tab: 'overview', lang: '', track: 'app', filter: 'all', q: '', page: 1, strings: null, seq: 0 };

  function envName() { return st.env && st.env.environment ? st.env.environment.classification : 'unknown'; }
  // Every action that changes data needs a SAFELY IDENTIFIED environment. 'unknown' (not set / invalid / conflicting) disables them all;
  // the server refuses them too (503 environment_unknown).
  function envKnown() { var e = envName(); return e === 'staging' || e === 'development' || e === 'test' || e === 'production'; }
  var ENV_REASON = {
    environment_not_set: 'OFFRO_ENVIRONMENT is not set on this server.',
    environment_value_invalid: 'OFFRO_ENVIRONMENT has a value that is not recognised (use staging or production).',
    production_declared_on_staging_service: 'This is the Staging service but OFFRO_ENVIRONMENT says production.',
    platform_says_production: 'The hosting environment is named production but OFFRO_ENVIRONMENT says otherwise.',
    check_error: 'The environment check failed.'
  };
  function isSA() { return !!(st.env && st.env.caller_is_system_super_admin); }      // decided by the server, not by the role name in the browser
  function canPerm(action) {                                                         // UX only; the server re-checks every call
    return isSA() || (typeof window._rbacCanDo === 'function' && !!window._rbacCanDo('translations', action));
  }
  function canEdit() { return envKnown() && canPerm('edit'); }
  function langObj(code) { for (var i = 0; i < st.langs.length; i++) if (st.langs[i].code === code) return st.langs[i]; return null; }

  // ── API ──────────────────────────────────────────────────────────────────────────────────
  function ApiError(status, message, code) { this.status = status; this.message = message; this.code = code; }
  var CODES = {
    audit_unavailable: 'Nothing was changed because the audit log could not be written. Try again shortly.',
    nothing_to_publish: 'There are no changes to publish.',
    environment_unknown: 'This server does not know whether it is staging or production, so changes are refused. Ask the developer to set OFFRO_ENVIRONMENT.',
    language_exists: 'That language already exists. Nothing was changed.',
    language_disabled: 'This language is disabled, so nothing can be published for it.',
    version_conflict: 'Another publish happened at the same time. Nothing was changed. Try again.',
    rollback_in_progress: 'An older, unfinished rollback is blocking changes to this language and text type. Ask the developer to finish or clear it.'
  };
  function messageFor(data, status) {
    var d = data && data.detail, code = d && typeof d === 'object' ? d.code : '';
    if (code === 'invalid_values') {
      var bad = d.detail && typeof d.detail === 'object' ? Object.keys(d.detail) : [];
      return 'Some translations are not valid, so nothing was published' + (bad.length ? ' (' + bad.slice(0, 5).join(', ') + (bad.length > 5 ? ' and ' + (bad.length - 5) + ' more' : '') + ')' : '') + '. Fix them and publish again.';
    }
    if (code && CODES[code]) return CODES[code];
    if (code && d.detail && typeof d.detail === 'string') return d.detail;
    if (code) return 'Not allowed or not valid (' + code + ').';
    if (status === 403) return DENIED;
    if (status === 401) return 'Your session has expired. Please sign in again.';
    if (status >= 500) return 'The server could not complete the request. Please try again.';
    return 'The request could not be completed (' + status + ').';
  }
  async function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', credentials: 'include', headers: { 'Accept': 'application/json' } };
    if (opts.json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
    var res;
    try { res = await fetch(API + path, init); } catch (e) { throw new ApiError(0, 'Could not reach the server. Check your connection.'); }
    if (res.status === 401) { window.location.href = '/admin'; throw new ApiError(401, messageFor(null, 401)); }
    var data = null; try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) throw new ApiError(res.status, messageFor(data, res.status), data && data.detail && data.detail.code);
    return data;
  }
  function alertMsg(text, good) {
    var a = $('trn_alert'); if (!a) return;
    if (!text) { a.hidden = true; return; }
    a.className = 'trn-alert' + (good ? ' good' : ''); a.textContent = text; a.hidden = false;
  }

  // ── confirmation dialog ──────────────────────────────────────────────────────────────────
  // fields: [{id,label,type:'note'|'text'|'select',options?,required?}] ; resolves to {id:value} or null. run(values) may throw ApiError to keep it open.
  function dialog(o) {
    return new Promise(function (resolve) {
      var m = $('trn_modal'); $('trn_modal_title').textContent = o.title; $('trn_modal_text').textContent = o.text || '';
      var fh = '';
      (o.fields || []).forEach(function (f) {
        if (f.type === 'select') fh += h`<div class="mb-2"><label class="small fw-bold">${f.label}</label><select id="trn_f_${f.id}" class="form-select">${raw((f.options || []).map(function (op) { return h`<option value="${op[0]}">${op[1]}</option>`; }).join(''))}</select></div>`;
        else if (f.type === 'check') fh += h`<div class="mb-2"><label class="small fw-bold"><input type="checkbox" id="trn_f_${f.id}"> ${f.label}</label></div>`;
        else if (f.type === 'note') fh += h`<div class="mb-2"><label class="small fw-bold">${f.label}</label><textarea id="trn_f_${f.id}" rows="3" maxlength="300"></textarea></div>`;
        else fh += h`<div class="mb-2"><label class="small fw-bold">${f.label}</label><input type="text" id="trn_f_${f.id}" maxlength="80"></div>`;
      });
      setHtml($('trn_modal_fields'), (o.html || '') + fh);
      var box = m.querySelector('.trn-modal-box'); if (box) box.style.maxWidth = o.wide ? '880px' : '';
      var err = $('trn_modal_err'); err.hidden = true;
      $('trn_modal_ok').textContent = o.okLabel || 'Confirm';
      var ok = $('trn_modal_ok'), cancel = $('trn_modal_cancel');
      function close(v) { m.hidden = true; ok.onclick = null; cancel.onclick = null; if (box) box.style.maxWidth = ''; resolve(v); }
      cancel.onclick = function () { close(null); };
      ok.onclick = async function () {
        var vals = {};
        for (var i = 0; i < (o.fields || []).length; i++) {
          var f = o.fields[i]; vals[f.id] = f.type === 'check' ? ($('trn_f_' + f.id).checked ? '1' : '') : ($('trn_f_' + f.id).value || '').trim();
          if (f.required && !vals[f.id]) { err.textContent = f.type === 'check' ? 'Please tick the box to confirm.' : f.label + ' is required.'; err.hidden = false; return; }
        }
        ok.disabled = true;
        try { if (o.run) await o.run(vals); close(vals); }
        catch (e) { err.textContent = e.message || 'Failed.'; err.hidden = false; }
        ok.disabled = false;
      };
      m.hidden = false;
      var first = m.querySelector('textarea,input,select'); if (first) first.focus();
    });
  }

  // ── banner / tabs ────────────────────────────────────────────────────────────────────────
  function renderBanner() {
    var e = envName(), ks = st.env && st.env.kill_switch;
    var p = '<span class="trn-pill ' + (e === 'production' ? 'prod' : e === 'staging' ? 'stg' : 'warn') + '"><i class="fas fa-server"></i> ' + esc(e.toUpperCase()) + '</span>';
    p += ks && ks.enabled ? '<span class="trn-pill warn"><i class="fas fa-power-off"></i> Kill switch ON: apps use bundled text</span>'
                          : '<span class="trn-pill ok"><i class="fas fa-check"></i> Remote translations enabled</span>';
    if (e === 'unknown') {
      var why = ENV_REASON[(st.env && st.env.environment && st.env.environment.reason) || ''] || '';
      p += h`<span class="trn-pill warn">Environment not identified: changes are refused. ${why}</span>`;
    }
    setHtml($('trn_banner'), p);
  }
  function tabs() {
    return [['overview', 'Overview'], ['strings', 'Translations']];
  }
  function renderTabs() {
    setHtml($('trn_tabs'), tabs().map(function (t) {
      return h`<button type="button" class="trn-tab ${t[0] === st.tab ? 'active' : ''}" role="tab" data-act="tab" data-tab="${t[0]}">${t[1]}</button>`;
    }).join(''));
    ['overview', 'strings'].forEach(function (n) { var p = $('trn_pane_' + n); if (p) p.hidden = n !== st.tab; });
  }
  function selectors(withFilter) {
    var lo = st.langs.map(function (l) { return h`<option value="${l.code}" ${raw(l.code === st.lang ? 'selected' : '')}>${l.name_en} (${l.code})</option>`; }).join('');
    var to = ['app', 'legal'].map(function (t) { return h`<option value="${t}" ${raw(t === st.track ? 'selected' : '')}>${TRACK_LABEL[t]}</option>`; }).join('');
    return h`<select data-change="lang" aria-label="Language">${raw(lo)}</select><select data-change="track" aria-label="Track">${raw(to)}</select>`;
  }

  // ── overview ─────────────────────────────────────────────────────────────────────────────
  function statusPill(s, tr) {
    if (!s) return '—';
    if (s.complete && s.published_version) return '<span class="trn-pill ok">Complete</span>';
    if (s.published_version) return '<span class="trn-pill warn">Needs attention</span>';
    return '<span class="trn-pill off">Not published</span>';
  }
  function renderOverview() {
    var rows = '';
    st.langs.forEach(function (l) {
      ['app', 'legal'].forEach(function (tr) {
        var s = (l.status_indicators || {})[tr]; if (!s) return;
        var draft = s.draft_changes ? h`${s.draft_changes} modified` : '—';
        rows += h`<tr><td>${l.name_en} <span class="trn-key">${l.code}</span><div class="trn-hint">${l.name_native} · ${l.direction.toUpperCase()} · ${l.status}</div></td>` +
          h`<td>${TRACK_LABEL[tr]}</td><td>${s.published_version ? 'v' + s.published_version : '—'}</td><td>${raw(draft)}</td>` +
          h`<td>${s.missing} missing</td><td>${raw(statusPill(s))}</td><td>${fmt((l.last_updated || {})[tr])}</td>` +
          h`<td><button type="button" class="btn-s" data-act="open" data-lang="${l.code}" data-track="${tr}">Open</button></td></tr>`;
      });
    });
    var base = st.base || { code: 'en', name_en: 'English', name_native: 'English', direction: 'ltr' };
    var baseRow = h`<tr><td>${base.name_en} <span class="trn-key">${base.code}</span><div class="trn-hint">${base.name_native} · ${(base.direction || 'ltr').toUpperCase()} · built-in</div></td>` +
      h`<td colspan="7" class="trn-hint">Source language. Its text comes from the app and is not edited here.</td></tr>`;
    var add = '';
    if (isSA() && envKnown()) {
      var arHint = langObj('ar') ? '' : '<p class="trn-note">Arabic is not added yet. <button type="button" class="btn-s" data-act="fillar">Fill in Arabic</button> then press Add language.</p>';
      add = '<div class="trn-box"><h4>Add a language</h4>' + arHint + '<div class="trn-form">' +
        '<div><label>Code (e.g. ar)</label><input id="trn_nl_code" maxlength="10"></div><div><label>English name</label><input id="trn_nl_en" maxlength="60"></div>' +
        '<div><label>Native name</label><input id="trn_nl_native" maxlength="60"></div>' +
        '<div><label>Direction</label><select id="trn_nl_dir"><option value="ltr">Left to right</option><option value="rtl">Right to left</option></select></div>' +
        '<div><button type="button" class="btn-p" data-act="addlang">Add language</button></div></div></div>';
    }
    var none = st.langs.length ? '' : '<div class="trn-empty">No translation languages added yet.' +
      (!envKnown() ? ' Adding languages is disabled until the environment is identified.' : ' Add one below (for example Arabic).') + '</div>';
    var ks = (st.env && st.env.kill_switch) || {}, kill = '';
    if (isSA()) {
      kill = h`<div class="trn-box"><h4>Emergency kill switch (${envName()} only)</h4><p class="trn-note">${ks.enabled ? 'ON: apps ignore remote translations and use their bundled text.' : 'OFF: apps may use remote translations.'} ` +
        h`Use it only in an emergency. It affects only this environment's apps.</p>` +
        h`<button type="button" class="btn-${ks.enabled ? 'p' : 's'}" ${raw(envKnown() ? '' : 'disabled')} data-act="killswitch" data-on="${ks.enabled ? '0' : '1'}">${ks.enabled ? 'Turn kill switch OFF' : 'Turn kill switch ON'}</button></div>`;
    }
    setHtml($('trn_pane_overview'),
      '<div class="section-header"><div></div><button type="button" class="btn-p" data-act="refresh"><i class="fas fa-sync me-1"></i> Refresh</button></div>' +
      (envKnown() ? '' : '<div class="trn-alert">The environment is not identified, so every change is disabled until it is.</div>') +
      '<div class="table-wrap"><table class="trn-table"><thead><tr><th>Language</th><th>Track</th><th>Live version</th><th>Modified</th><th>Completion</th><th>Status</th><th>Last updated</th><th></th></tr></thead><tbody>' + baseRow + rows + '</tbody></table></div>' +
      none + kill + add);
  }

  // ── translations (strings) ───────────────────────────────────────────────────────────────
  async function loadStrings() {
    var my = ++st.seq, p = $('trn_pane_strings');
    if (!st.lang) { setHtml(p, '<div class="trn-empty">Select or add a language first.</div>'); return; }
    setHtml(p, '<div class="trn-empty"><i class="fas fa-spinner fa-spin"></i> Loading...</div>');
    try {
      var d = await api('/strings?lang=' + encodeURIComponent(st.lang) + '&track=' + st.track + '&filter=' + st.filter + '&q=' + encodeURIComponent(st.q) + '&page=' + st.page);
      if (my !== st.seq) return;
      st.strings = d; renderStrings();
    } catch (e) { if (my === st.seq) setHtml(p, h`<div class="trn-alert">${e.message}</div>`); }
  }
  function renderStrings() {
    var d = st.strings, lo = langObj(st.lang), dir = lo ? lo.direction : 'ltr', edit = canEdit();
    var filt = ['all', 'missing', 'modified', 'published'].map(function (f) { return h`<option value="${f}" ${raw(f === st.filter ? 'selected' : '')}>${f}</option>`; }).join('');
    var rows = d.rows.map(function (r) {
      var dis = (!edit || r.locked) ? 'disabled' : '';
      var hint = (r.placeholders && r.placeholders.length ? 'Keep: ' + r.placeholders.map(function (x) { return '{' + x + '}'; }).join(' ') + '. ' : '') + (r.max_length ? 'Max ' + r.max_length + ' chars.' : '');
      var extra = r.locked ? h`<div class="trn-hint">${r.lock_reason}</div>` : '';
      var btns = '';
      if (edit && !r.locked) btns += h`<button type="button" class="btn-p" data-act="save" data-key="${r.key}" hidden>Save</button>`;
      return h`<tr data-row="${r.key}"><td><div class="trn-key">${r.key}</div><div class="trn-hint">${r.screen_group}</div></td><td class="trn-en">${r.english}</td>` +
        h`<td><textarea class="trn-val" dir="${dir}" data-orig="${r.value || ''}" data-input="val" ${raw(dis)} aria-label="Translation of ${r.key}">${r.value || ''}</textarea>` +
        h`<div class="trn-hint">${hint}</div>${raw(extra)}<div class="trn-rowerr" hidden></div></td><td><span class="trn-pill ${r.status === 'published' ? 'ok' : r.status === 'modified' ? 'warn' : 'off'}" data-status>${r.status}</span></td><td>${raw(btns)}</td></tr>`;
    }).join('');
    var pages = Math.max(1, Math.ceil(d.total / d.page_size));
    var actions = '';
    if (isSA() && envKnown()) actions += '<button type="button" class="btn-p" data-act="publish">Publish Changes</button>';
    setHtml($('trn_pane_strings'),
      h`<div class="trn-toolbar">${raw(selectors())}<select data-change="filter" aria-label="Filter">${raw(filt)}</select>` +
      h`<input type="search" placeholder="Search key or English text" value="${st.q}" data-input="q" aria-label="Search"></div>` +
      (!envKnown() ? '<p class="trn-note">Changes are disabled until the environment is identified.</p>' : '') +
      (st.track === 'legal' ? '<p class="trn-note">Legal text can be edited and published here, but the customer app does not read legal text yet, so publishing it does not change what customers see.</p>' : '') +
      (d.rows.length ? '<div class="table-wrap"><table class="trn-table"><thead><tr><th>Key</th><th>English (source)</th><th>Translation</th><th>Status</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
        : '<div class="trn-empty">No translation keys match.' + (d.total === 0 && st.filter === 'all' && !st.q ? ' The key list is created from the app text during the localization phase.' : '') + '</div>') +
      h`<div class="trn-toolbar"><button type="button" class="btn-s" data-act="prev" ${raw(d.page <= 1 ? 'disabled' : '')}>Previous</button><span>Page ${d.page} of ${pages} · ${d.total} key${d.total === 1 ? '' : 's'}</span><button type="button" class="btn-s" data-act="next" ${raw(d.page >= pages ? 'disabled' : '')}>Next</button></div>` +
      '<div class="trn-actions">' + actions + '</div>');
  }
  async function saveRow(tr) {
    var key = tr.getAttribute('data-row'), ta = tr.querySelector('textarea'), err = tr.querySelector('.trn-rowerr'), btn = tr.querySelector('[data-act=save]');
    err.hidden = true; btn.disabled = true;
    try {
      var r = await api('/strings/' + encodeURIComponent(st.lang) + '/' + encodeURIComponent(key), { method: 'PUT', json: { value: ta.value } });
      ta.setAttribute('data-orig', ta.value); btn.hidden = true;
      var s = tr.querySelector('[data-status]'); s.textContent = 'modified'; s.className = 'trn-pill warn';
      if (r && r.audit_warning) alertMsg(r.audit_warning); else alertMsg('Saved. Status is now Modified: ' + key + '. Press Publish Changes to make it live.', true);
    } catch (e) { err.textContent = e.message; err.hidden = false; }
    btn.disabled = false;
  }

  // ── actions ──────────────────────────────────────────────────────────────────────────────
  var NOTE = { id: 'note', label: 'Note (optional)', type: 'note', required: false };
  async function reload() { await boot(true); }
  async function doAction(name, el) {
    alertMsg('');
    var lang = st.lang, track = st.track;
    if (name === 'publish') {
      var legalNote = track === 'legal' ? ' The customer app does not read legal text yet, so this does not change what customers see.' : ' Apps receive it the next time they check, unless the kill switch is ON.';
      return dialog({ title: 'Publish Changes', okLabel: 'Publish Changes', fields: [NOTE],
        text: 'Makes every modified ' + TRACK_LABEL[track] + ' translation for ' + lang + ' live in ' + envName() + ' only.' + legalNote,
        run: async function (f) { await api('/publish', { method: 'POST', json: { lang: lang, track: track, note: f.note } }); } }).then(function (r) { if (r) { alertMsg('Published.', true); reload(); } });
    }
    if (name === 'killswitch') {
      var on = el.getAttribute('data-on') === '1';
      return dialog({ title: on ? 'Turn kill switch ON' : 'Turn kill switch OFF', okLabel: 'Confirm', fields: [{ id: 'note', label: 'Reason (required)', type: 'note', required: true }],
        text: on ? 'Apps will ignore remote translations and use their bundled text (' + envName() + ' only).' : 'Apps may use remote translations again (' + envName() + ' only).',
        run: async function (f) { await api('/kill-switch', { method: 'POST', json: { enabled: on, reason: f.note } }); } }).then(function (r) { if (r) reload(); });
    }
    if (name === 'fillar') {
      $('trn_nl_code').value = 'ar'; $('trn_nl_en').value = 'Arabic'; $('trn_nl_native').value = 'العربية'; $('trn_nl_dir').value = 'rtl';
      return;
    }
    if (name === 'addlang') {
      var body = { code: $('trn_nl_code').value.trim(), name_en: $('trn_nl_en').value.trim(), name_native: $('trn_nl_native').value.trim(), direction: $('trn_nl_dir').value };
      if (!body.code || !body.name_en || !body.name_native) { alertMsg('Fill in the code and both names.'); return; }
      try { await api('/languages', { method: 'POST', json: body }); alertMsg('Language added as a draft.', true); st.lang = body.code; reload(); } catch (e) { alertMsg(e.message); }
    }
  }

  // ── events ───────────────────────────────────────────────────────────────────────────────
  var searchTimer = null;
  function wire() {
    var root = $('section-translations'); if (!root || root._wired) return; root._wired = true;
    root.addEventListener('click', function (ev) {
      var el = ev.target.closest('[data-act]'); if (!el || !root.contains(el)) return;
      var a = el.getAttribute('data-act');
      if (a === 'tab') { st.tab = el.getAttribute('data-tab'); renderTabs(); paneLoad(); }
      else if (a === 'open') { st.lang = el.getAttribute('data-lang'); st.track = el.getAttribute('data-track'); st.page = 1; st.tab = 'strings'; renderTabs(); paneLoad(); }
      else if (a === 'refresh') reload();
      else if (a === 'save') saveRow(el.closest('tr'));
      else if (a === 'prev') { st.page = Math.max(1, st.page - 1); loadStrings(); }
      else if (a === 'next') { st.page += 1; loadStrings(); }
      else doAction(a, el);
    });
    root.addEventListener('change', function (ev) {
      var a = ev.target.getAttribute('data-change'); if (!a) return;
      if (a === 'lang') st.lang = ev.target.value; else if (a === 'track') st.track = ev.target.value; else if (a === 'filter') st.filter = ev.target.value;
      st.page = 1; paneLoad();
    });
    root.addEventListener('input', function (ev) {
      var a = ev.target.getAttribute('data-input');
      if (a === 'val') { var tr = ev.target.closest('tr'), b = tr.querySelector('[data-act=save]'); if (b) b.hidden = ev.target.value === ev.target.getAttribute('data-orig'); }
      else if (a === 'q') { clearTimeout(searchTimer); var v = ev.target.value; searchTimer = setTimeout(function () { st.q = v; st.page = 1; loadStrings(); }, 350); }
    });
  }
  function paneLoad() {
    if (st.tab === 'overview') renderOverview(); else if (st.tab === 'strings') loadStrings();
  }

  async function boot(keepTab) {
    wire();
    $('trn_denied').hidden = true; $('trn_body').hidden = false;
    try {
      var r = await Promise.all([api('/environment'), api('/languages')]);
      st.env = r[0]; st.langs = r[1].languages; st.base = r[1].base_language || null;
    } catch (e) {
      if (e.status === 403) { $('trn_body').hidden = true; $('trn_denied').hidden = false; $('trn_denied_text').textContent = DENIED; return; }
      $('trn_body').hidden = false; alertMsg(e.message); return;
    }
    if (!langObj(st.lang)) st.lang = st.langs.length ? st.langs[0].code : '';
    renderBanner(); renderTabs(); paneLoad();
  }

  window.Translations = { open: function () { return boot(false); } };
})();
