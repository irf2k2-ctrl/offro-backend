/* Translations (Phase H) - admin dashboard module.
 *
 * A thin client for the Phase G API (/admin/translations). The server is the single source of truth for permissions, the environment
 * rules (no production edits, no direct production rollback, production receives text only from a signed staging package which is
 * imported as a DRAFT and published explicitly) and the audit log. The browser only shows what the API returns and hides controls the
 * caller cannot use. Nothing here talks to production from staging or the reverse: each environment's dashboard manages its own data.
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
  var st = { env: null, langs: [], tab: 'overview', lang: '', track: 'app', filter: 'all', q: '', page: 1, strings: null, versions: null, seq: 0 };

  function envName() { return st.env && st.env.environment ? st.env.environment.classification : 'unknown'; }
  function isProd() { return envName() === 'production'; }
  // Every action that changes data needs a SAFELY IDENTIFIED environment. 'unknown' (not set / invalid / conflicting) disables them all;
  // the server refuses them too (503 environment_unknown).
  function envKnown() { var e = envName(); return e === 'staging' || e === 'development' || e === 'test' || e === 'production'; }
  function canWriteNonProd() { return envKnown() && !isProd(); }
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
  function canEdit() { return canWriteNonProd() && canPerm('edit'); }
  function pendingFor(lang, track) { var p = st.env && st.env.pending_packages; return p ? p[track + ':' + lang] : null; }
  function langObj(code) { for (var i = 0; i < st.langs.length; i++) if (st.langs[i].code === code) return st.langs[i]; return null; }

  // ── API ──────────────────────────────────────────────────────────────────────────────────
  function ApiError(status, message, code) { this.status = status; this.message = message; this.code = code; }
  var CODES = {
    production_edit_blocked: 'Production cannot be edited or rolled back directly. Make the change in staging, approve it, export the package and import it here.',
    audit_unavailable: 'Nothing was changed because the audit log could not be written. Try again shortly.',
    version_not_approved: 'This version has not been approved for production yet.',
    already_approved: 'This version is already approved.',
    already_current: 'That version is already the current one.',
    nothing_to_publish: 'There are no changes to publish.',
    no_pending_package: 'There is no imported package waiting to be published.',
    package_signature_invalid: 'The package signature is not valid. Nothing was imported.',
    package_secret_not_configured: 'The package secret is missing or weak on this server. Ask the developer to configure it.',
    package_secrets_identical: 'The production secrets are misconfigured (identical). Ask the developer to fix them.',
    package_import_production_only: 'Packages are imported in production only.',
    package_export_staging_only: 'Packages are exported from staging only.',
    environment_unknown: 'This server does not know whether it is staging or production, so changes are refused. Ask the developer to set OFFRO_ENVIRONMENT.',
    language_exists: 'That language already exists. Nothing was changed.',
    draft_loss_confirmation_required: 'Unpublished drafts would be lost. Review the impact and confirm.',
    impact_changed: 'The drafts or versions changed after you opened this dialog. Nothing was changed. Close it and review the impact again.',
    rollback_in_progress: 'A rollback of this language and text type is unfinished. Resume or abort it in the Versions tab first.',
    rollback_incomplete: 'The new version is live, but restoring the working copy did not finish. Open the Versions tab and use Resume.',
    rollback_interrupted: 'The rollback was interrupted before the new version was written. Open the Versions tab to Resume or Abort it.',
    rollback_not_started: 'The rollback could not be started. Nothing was changed. Please try again.',
    rollback_not_found: 'This rollback was aborted or has already finished.',
    no_rollback_in_progress: 'There is no unfinished rollback for this language and text type.',
    abort_not_allowed_version_written: 'The new version is already live, so this rollback cannot be aborted. Use Resume to finish it.',
    language_disabled: 'This language is disabled, so nothing can be published for it.',
    version_integrity_failed: 'That version failed its integrity check, so it cannot be restored.',
    version_conflict: 'Another publish happened at the same time. Nothing was changed. Try again.',
    legal_review_required: 'Some legal texts in that version have no recorded review. Record the reviews first.',
    invalid_values: 'That version contains text that is no longer valid for the current key list, so it cannot be restored.'
  };
  function messageFor(data, status) {
    var d = data && data.detail, code = d && typeof d === 'object' ? d.code : '';
    if (code === 'rollback_owned_by_other') {
      var od = d.detail && typeof d.detail === 'object' ? d.detail : {};
      return 'This rollback is owned by ' + (od.owner || 'another Super Admin') + (od.lease_until ? ' until ' + fmt(od.lease_until) : '') + '. Another Super Admin can take over after that time.';
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
    if (e === 'production') p += '<span class="trn-pill off">Production: text arrives only from an approved staging package</span>';
    if (e === 'unknown') {
      var why = ENV_REASON[(st.env && st.env.environment && st.env.environment.reason) || ''] || '';
      p += h`<span class="trn-pill warn">Environment not identified: changes are refused. ${why}</span>`;
    }
    setHtml($('trn_banner'), p);
  }
  function tabs() {
    var t = [['overview', 'Overview'], ['strings', 'Translations'], ['versions', 'Versions'], ['audit', 'Audit']];
    if (isSA()) t.splice(3, 0, ['promotion', 'Promotion & safety']);
    return t;
  }
  function renderTabs() {
    setHtml($('trn_tabs'), tabs().map(function (t) {
      return h`<button type="button" class="trn-tab ${t[0] === st.tab ? 'active' : ''}" role="tab" data-act="tab" data-tab="${t[0]}">${t[1]}</button>`;
    }).join(''));
    ['overview', 'strings', 'versions', 'promotion', 'audit'].forEach(function (n) { var p = $('trn_pane_' + n); if (p) p.hidden = n !== st.tab; });
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
        var pend = pendingFor(l.code, tr);
        var draft = s.draft_changes ? h`v${(s.published_version || 0) + 1} (${s.draft_changes} change${s.draft_changes === 1 ? '' : 's'})` : '—';
        var review = [];
        if (s.waiting_review) review.push(s.waiting_review + ' awaiting review');
        if (pend) review.push('imported package v' + pend.source_version + ' awaiting publish');
        rows += h`<tr><td>${l.name_en} <span class="trn-key">${l.code}</span><div class="trn-hint">${l.name_native} · ${l.direction.toUpperCase()} · ${l.status}</div></td>` +
          h`<td>${TRACK_LABEL[tr]}</td><td>${s.published_version ? 'v' + s.published_version : '—'}</td><td>${raw(draft)}</td>` +
          h`<td>${s.missing} missing</td><td>${raw(statusPill(s))}</td><td>${review.join('; ') || '—'}</td><td>${fmt((l.last_updated || {})[tr])}</td>` +
          h`<td><button type="button" class="btn-s" data-act="open" data-lang="${l.code}" data-track="${tr}">Open</button></td></tr>`;
      });
    });
    var base = st.base || { code: 'en', name_en: 'English', name_native: 'English', direction: 'ltr' };
    var baseRow = h`<tr><td>${base.name_en} <span class="trn-key">${base.code}</span><div class="trn-hint">${base.name_native} · ${(base.direction || 'ltr').toUpperCase()} · built-in</div></td>` +
      h`<td colspan="8" class="trn-hint">Source language. Its text comes from the app and is not edited here.</td></tr>`;
    var add = '';
    if (isSA() && canWriteNonProd()) {
      var arHint = langObj('ar') ? '' : '<p class="trn-note">Arabic is not added yet. <button type="button" class="btn-s" data-act="fillar">Fill in Arabic</button> then press Add language.</p>';
      add = '<div class="trn-box"><h4>Add a language</h4>' + arHint + '<div class="trn-form">' +
        '<div><label>Code (e.g. ar)</label><input id="trn_nl_code" maxlength="10"></div><div><label>English name</label><input id="trn_nl_en" maxlength="60"></div>' +
        '<div><label>Native name</label><input id="trn_nl_native" maxlength="60"></div>' +
        '<div><label>Direction</label><select id="trn_nl_dir"><option value="ltr">Left to right</option><option value="rtl">Right to left</option></select></div>' +
        '<div><button type="button" class="btn-p" data-act="addlang">Add language</button></div></div></div>';
    }
    var none = st.langs.length ? '' : '<div class="trn-empty">No translation languages added yet.' +
      (isProd() ? ' They appear here after a signed staging package is imported.' : !envKnown() ? ' Adding languages is disabled until the environment is identified.' : ' Add one below (for example Arabic).') + '</div>';
    setHtml($('trn_pane_overview'),
      '<div class="section-header"><div></div><button type="button" class="btn-p" data-act="refresh"><i class="fas fa-sync me-1"></i> Refresh</button></div>' +
      (envKnown() ? '' : '<div class="trn-alert">The environment is not identified, so every change is disabled until it is.</div>') +
      '<div class="table-wrap"><table class="trn-table"><thead><tr><th>Language</th><th>Track</th><th>Published</th><th>Draft</th><th>Completion</th><th>Status</th><th>Review / approval</th><th>Last updated</th><th></th></tr></thead><tbody>' + baseRow + rows + '</tbody></table></div>' +
      none + add);
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
      if (edit && !r.locked) btns += h`<button type="button" class="btn-p" data-act="save" data-key="${r.key}" hidden>Save draft</button>`;
      if (st.track === 'legal' && isSA() && canWriteNonProd() && r.value) btns += h` <button type="button" class="btn-s" data-act="legalreview" data-key="${r.key}">Record legal review</button>`;
      return h`<tr data-row="${r.key}"><td><div class="trn-key">${r.key}</div><div class="trn-hint">${r.screen_group}</div></td><td class="trn-en">${r.english}</td>` +
        h`<td><textarea class="trn-val" dir="${dir}" data-orig="${r.value || ''}" data-input="val" ${raw(dis)} aria-label="Translation of ${r.key}">${r.value || ''}</textarea>` +
        h`<div class="trn-hint">${hint}</div>${raw(extra)}<div class="trn-rowerr" hidden></div></td><td><span class="trn-pill ${r.status === 'published' ? 'ok' : r.status === 'modified' ? 'warn' : 'off'}" data-status>${r.status}</span></td><td>${raw(btns)}</td></tr>`;
    }).join('');
    var pages = Math.max(1, Math.ceil(d.total / d.page_size));
    var pend = pendingFor(st.lang, st.track), actions = '';
    if (edit) actions += '<button type="button" class="btn-s" data-act="submit">Mark drafts ready for review</button>';
    if (isSA() && (canWriteNonProd() || (isProd() && pend))) actions += h`<button type="button" class="btn-p" data-act="publish">${isProd() ? 'Publish imported package' : 'Publish draft'}</button>`;
    setHtml($('trn_pane_strings'),
      h`<div class="trn-toolbar">${raw(selectors())}<select data-change="filter" aria-label="Filter">${raw(filt)}</select>` +
      h`<input type="search" placeholder="Search key or English text" value="${st.q}" data-input="q" aria-label="Search"></div>` +
      (!envKnown() ? '<p class="trn-note">Changes are disabled until the environment is identified.</p>' : '') +
      (isProd() ? '<p class="trn-note">Production is read-only here. To change text: edit and publish in staging, approve it, export the package and import it on this server.</p>' : '') +
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
      if (r && r.audit_warning) alertMsg(r.audit_warning); else alertMsg('Saved as draft: ' + key, true);
    } catch (e) { err.textContent = e.message; err.hidden = false; }
    btn.disabled = false;
  }

  // ── versions ─────────────────────────────────────────────────────────────────────────────
  async function loadVersions() {
    var p = $('trn_pane_versions');
    if (!st.lang) { setHtml(p, '<div class="trn-empty">Select or add a language first.</div>'); return; }
    setHtml(p, '<div class="trn-empty"><i class="fas fa-spinner fa-spin"></i> Loading...</div>');
    try {
      st.versions = await api('/versions?lang=' + encodeURIComponent(st.lang) + '&track=' + st.track);
      st.rollback = null;
      if (isSA() && canWriteNonProd()) {
        try { st.rollback = await api('/rollback-status?lang=' + encodeURIComponent(st.lang) + '&track=' + st.track); } catch (e) { st.rollback = null; }
      }
      renderVersions();
    }
    catch (e) { setHtml(p, h`<div class="trn-alert">${e.message}</div>`); }
  }
  // The unfinished-rollback banner: who owns it, when the lease ends, and exactly what can still be done.
  function rollbackBanner() {
    var rb = st.rollback;
    if (!rb || !rb.active) return '';
    var owner = (rb.owner && rb.owner.name) || 'unknown', started = (rb.started_by && rb.started_by.name) || 'unknown';
    var t = h`<div class="trn-alert" data-rollback-banner><strong>Incomplete rollback</strong>: restoring v${rb.target_version} as new version v${rb.new_version}. ` +
      h`Started by ${started} (${fmt(rb.started_at)}). Current owner: ${owner}. Lease ends ${fmt(rb.lease_until)}${rb.lease_expired ? ' (expired)' : ''}. ` +
      'Publishing, saving drafts and marking drafts ready are paused for this language and text type until it is finished.';
    if (rb.version_written) {
      t += h`<p class="trn-note" data-rollback-resume-only>The new version v${rb.new_version} is already live and cannot be undone here. Resume is the only recovery option: it finishes restoring the working copy. To change the wording afterwards, roll back to another version or publish a new one.</p>`;
    } else {
      t += '<p class="trn-note">The new version has not been written yet, so you can either Resume (finish the rollback) or Abort (cancel it; nothing has changed).</p>';
    }
    if (rb.can_resume) {
      t += '<button type="button" class="btn-p" data-act="rbresume">' + (rb.is_owner ? 'Resume' : 'Take over and resume') + '</button> ';
      if (rb.can_abort && !rb.version_written) t += '<button type="button" class="btn-s" data-act="rbabort">Abort rollback</button>';
    } else {
      t += h`<p class="trn-note" data-rollback-owned>Owned by ${owner} until ${fmt(rb.lease_until)}. Another Super Admin can take over after that time.</p>`;
    }
    return t + '</div>';
  }
  function renderVersions() {
    var vs = st.versions.versions, latest = vs.length ? vs[0].version : 0, pend = pendingFor(st.lang, st.track);
    var rows = vs.map(function (v) {
      var src = v.promoted_from ? 'Promoted from staging v' + v.promoted_from.version : v.restored_from ? 'Rollback to v' + v.restored_from : 'Published';
      var b = '';
      if (isSA() && canWriteNonProd()) {
        if (!v.approved) b += h`<button type="button" class="btn-s" data-act="approve" data-v="${v.version}">Approve for production</button> `;
        if (v.version !== latest && !(st.rollback && st.rollback.active)) b += h`<button type="button" class="btn-s" data-act="rollback" data-v="${v.version}">Roll back to this</button> `;
        if (v.approved && envName() === 'staging') b += h`<button type="button" class="btn-p" data-act="export" data-v="${v.version}">Export package</button>`;
      }
      return h`<tr><td>v${v.version}${raw(v.version === latest ? ' <span class="trn-pill ok">current</span>' : '')}</td><td>${src}</td><td>${v.count} strings</td>` +
        h`<td>${raw(v.approved ? '<span class="trn-pill ok">Approved</span>' : '<span class="trn-pill off">Not approved</span>')}</td><td>${(v.published_by && v.published_by.name) || '—'}</td><td>${fmt(v.published_at)}</td><td>${v.note || ''}</td><td>${raw(b)}</td></tr>`;
    }).join('');
    setHtml($('trn_pane_versions'),
      h`<div class="trn-toolbar">${raw(selectors())}</div>` + rollbackBanner() +
      (pend ? h`<p class="trn-note">An imported package (staging v${pend.source_version}) is waiting. Publish it from the Translations tab.</p>` : '') +
      (isProd() ? '<p class="trn-note">Production history is read-only. Rollback is done in staging and promoted again as a new approved package.</p>' : '') +
      (vs.length ? '<div class="table-wrap"><table class="trn-table"><thead><tr><th>Version</th><th>Source</th><th>Size</th><th>Approval</th><th>By</th><th>When</th><th>Note</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
        : '<div class="trn-empty">Nothing has been published for this language and track yet.</div>'));
  }

  // ── promotion & safety (Super Admin) ─────────────────────────────────────────────────────
  function renderPromotion() {
    if (!isSA()) return;
    var e = st.env, cfg = e.package_config || {}, ks = e.kill_switch || {};
    var secrets = h`<ul class="trn-note"><li>Own secret: ${cfg.own_secret || '—'} (fingerprint ${cfg.own_fingerprint || '—'})</li>` +
      (envName() === 'production' ? h`<li>Verify secret: ${cfg.verify_secret || '—'} (fingerprint ${cfg.verify_fingerprint || '—'})</li><li>Secrets distinct: ${cfg.secrets_distinct ? 'yes' : 'NO'}</li><li>Ready to import: ${cfg.ready_to_import ? 'yes' : 'NO'}</li>`
        : h`<li>Ready to export: ${cfg.ready_to_export ? 'yes' : 'NO'}</li>`) + '</ul>';
    var imp = envName() === 'production'
      ? '<div class="trn-box"><h4>Import a package from staging</h4><p class="trn-note">The file is verified (signature, approval, key and value rules) and stored as a draft only. Nothing goes live until you publish it.</p><input type="file" id="trn_pkg_file" accept="application/json,.json"> <button type="button" class="btn-p" data-act="import">Verify &amp; import as draft</button></div>'
      : '<div class="trn-box"><h4>Promote to production</h4><p class="trn-note">Approve a published version, then export its signed package from the Versions tab and import it in the production dashboard.</p></div>';
    var pend = Object.keys(e.pending_packages || {}).map(function (k) { var p = e.pending_packages[k]; return h`<li>${p.track}:${p.lang}, staging v${p.source_version}, imported ${fmt(p.imported_at)}</li>`; }).join('');
    setHtml($('trn_pane_promotion'),
      h`<div class="trn-box"><h4>Package secrets</h4>${raw(secrets)}<p class="trn-hint">Only fingerprints are shown, never the secrets.</p></div>` + imp +
      (pend ? h`<div class="trn-box"><h4>Waiting to be published</h4><ul>${raw(pend)}</ul></div>` : '') +
      h`<div class="trn-box"><h4>Kill switch (${envName()} only)</h4><p class="trn-note">${ks.enabled ? 'ON: apps ignore remote translations and use their bundled text.' : 'OFF: apps may use remote translations.'} This switch is independent of the other environment.</p>` +
      h`<button type="button" class="btn-${ks.enabled ? 'p' : 's'}" ${raw(envKnown() ? '' : 'disabled')} data-act="killswitch" data-on="${ks.enabled ? '0' : '1'}">${ks.enabled ? 'Turn kill switch OFF' : 'Turn kill switch ON'}</button></div>`);
  }

  // ── audit ────────────────────────────────────────────────────────────────────────────────
  async function loadAudit() {
    var p = $('trn_pane_audit'); setHtml(p, '<div class="trn-empty"><i class="fas fa-spinner fa-spin"></i> Loading...</div>');
    try {
      var d = await api('/audit?limit=100');
      var rows = d.entries.map(function (a) {
        return h`<tr><td>${fmt(a.at)}</td><td>${(a.admin && a.admin.name) || '—'}</td><td>${a.action}</td><td>${a.outcome}</td><td>${a.lang || ''} ${a.track || ''} ${a.key || ''}</td><td>${a.version || ''}</td><td>${a.environment || ''}</td></tr>`;
      }).join('');
      setHtml(p, d.entries.length ? '<div class="table-wrap"><table class="trn-table"><thead><tr><th>When</th><th>Admin</th><th>Action</th><th>Outcome</th><th>Target</th><th>Version</th><th>Env</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<div class="trn-empty">No audit entries yet.</div>');
    } catch (e) { setHtml(p, h`<div class="trn-alert">${e.message}</div>`); }
  }

  // ── rollback: preview of what would be lost, confirmation, recovery ─────────────────────
  var ROLL_SECTIONS = [['overwritten', 'Drafts overwritten'], ['deleted', 'Drafts deleted'], ['working_removed', 'Published values removed from the working copy'],
                       ['live_changed', 'Live text changes'], ['status_only', 'Status only (no text lost)']];
  function rollRows(sec, rows) {
    var head = { overwritten: ['Key', 'Your draft', 'Will be restored to'], deleted: ['Key', 'Your draft'], working_removed: ['Key', 'Published text'],
                 live_changed: ['Key', 'Change', 'Live now', 'After rollback'], status_only: ['Key', 'Reason'] }[sec] || ['Key'];
    var body = rows.map(function (r) {
      var c = { overwritten: [r.key, r.draft, r.restored], deleted: [r.key, r.draft], working_removed: [r.key, r.live],
                live_changed: [r.key, r.change, r.live, r.restored], status_only: [r.key, r.reason] }[sec] || [r.key];
      return '<tr>' + c.map(function (x, i) { return h`<td dir="${i === 0 ? 'ltr' : 'auto'}">${x === null || x === undefined ? '—' : x}</td>`; }).join('') + '</tr>';
    }).join('');
    return '<div class="table-wrap"><table class="trn-table"><thead><tr>' + head.map(function (x) { return h`<th>${x}</th>`; }).join('') + '</tr></thead><tbody>' + body + '</tbody></table></div>';
  }
  async function rollSection(sec, page) {
    var r = st.roll, box = $('trn_roll_rows'), err = $('trn_modal_err'); if (!r || !box) return;
    err.hidden = true; setHtml(box, '<div class="trn-empty"><i class="fas fa-spinner fa-spin"></i> Loading...</div>');
    try {
      var d = await api('/rollback-preview?lang=' + encodeURIComponent(r.lang) + '&track=' + r.track + '&version=' + encodeURIComponent(r.v) + '&section=' + encodeURIComponent(sec) + '&page=' + page + '&page_size=50');
      var pages = Math.max(1, Math.ceil((d.total || 0) / (d.page_size || 50)));
      var nav = h`<div class="trn-toolbar"><button type="button" class="btn-s" data-act="rollsec" data-sec="${sec}" data-page="${page - 1}" ${raw(page <= 1 ? 'disabled' : '')}>Previous</button>` +
        h`<span>Page ${page} of ${pages} · ${d.total} key${d.total === 1 ? '' : 's'}</span><button type="button" class="btn-s" data-act="rollsec" data-sec="${sec}" data-page="${page + 1}" ${raw(page >= pages ? 'disabled' : '')}>Next</button></div>`;
      setHtml(box, (d.rows && d.rows.length ? rollRows(sec, d.rows) : '<div class="trn-empty">None.</div>') + nav);
    } catch (e) { setHtml(box, ''); err.textContent = e.message; err.hidden = false; }
  }
  async function rollCsv() {
    var r = st.roll, err = $('trn_modal_err'); if (!r) return; err.hidden = true;
    try {
      var res = await fetch(API + '/rollback-preview?lang=' + encodeURIComponent(r.lang) + '&track=' + r.track + '&version=' + encodeURIComponent(r.v) + '&format=csv', { credentials: 'include' });
      if (res.status === 401) { window.location.href = '/admin'; return; }
      if (!res.ok) { var data = null; try { data = await res.json(); } catch (e) { data = null; } throw new ApiError(res.status, messageFor(data, res.status)); }
      var blob = await res.blob(), a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = 'rollback-impact-' + r.track + '-' + r.lang + '-v' + r.v + '.csv';
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    } catch (e) { err.textContent = e.message || 'Could not download the file.'; err.hidden = false; }
  }
  async function rollbackFlow(lang, track, v) {
    var imp;
    try { imp = await api('/rollback-preview?lang=' + encodeURIComponent(lang) + '&track=' + track + '&version=' + encodeURIComponent(v)); } catch (e) { alertMsg(e.message); return; }
    var c = imp.counts || {}, tot = imp.totals || {}, keys = imp.keys || {};
    if (imp.already_current) {
      alertMsg('v' + v + ' has the same text as the current version, so there is nothing to roll back. Your ' + ((c.overwritten || 0) + (c.deleted || 0)) + ' unpublished draft(s) are untouched.');
      return;
    }
    var bl = imp.blockers || {}, bk = bl.keys ? Object.keys(bl.keys) : [];
    if ((bl.global && bl.global.length) || bk.length) {
      var why = (bl.global || []).map(function (x) { return CODES[x] || x; }).concat(bk.slice(0, 10).map(function (k) { return k + ' (' + bl.keys[k] + ')'; }));
      alertMsg('v' + v + ' cannot be restored: ' + why.join('; ') + (bk.length > 10 ? ' and ' + (bk.length - 10) + ' more' : '') + '. Nothing was changed.');
      return;
    }
    st.roll = { lang: lang, track: track, v: v };
    var secBtns = ROLL_SECTIONS.map(function (s) {
      var n = tot[s[0]] !== undefined ? tot[s[0]] : 0;
      return n ? h`<button type="button" class="btn-s" data-act="rollsec" data-sec="${s[0]}" data-page="1">${s[1]} (${n})</button> ` : '';
    }).join('');
    var firstRows = '';
    ['overwritten', 'deleted'].forEach(function (sec) {
      if (keys[sec] && keys[sec].length) firstRows += h`<h5>${sec === 'overwritten' ? 'Drafts that will be overwritten' : 'Drafts that will be deleted'}${tot[sec] > keys[sec].length ? ' (first ' + keys[sec].length + ' of ' + tot[sec] + ')' : ''}</h5>` + rollRows(sec, keys[sec]);
    });
    var lost = (c.overwritten || 0) + (c.deleted || 0);
    var html = h`<p><strong>Live text:</strong> ${c.live_restored || 0} restored, ${c.live_removed || 0} removed (an app shows its built-in text for removed keys).</p>` +
      h`<p><strong>Unpublished drafts:</strong> ${c.overwritten || 0} overwritten, ${c.deleted || 0} deleted${c.ready_for_review ? ' (' + c.ready_for_review + ' marked ready for review)' : ''}. ` +
      h`<strong>Published values removed from the working copy:</strong> ${c.working_removed || 0}. Status-only changes: ${c.status_only || 0}.</p>` +
      (lost ? '<p class="trn-note"><strong>These draft changes cannot be recovered after the rollback.</strong> Download the full list below if you want to keep them.</p>' : '') +
      firstRows +
      '<div class="trn-toolbar">' + secBtns + '<button type="button" class="btn-s" data-act="rollcsv">Download full impact (CSV)</button></div>' +
      '<div id="trn_roll_rows"></div>';
    var fields = [NOTE];
    if (imp.requires_confirmation) fields.push({ id: 'ack', type: 'check', required: true, label: 'I understand that ' + lost + ' unpublished draft change' + (lost === 1 ? '' : 's') + ' will be lost.' });
    var result = null;
    return dialog({ title: 'Roll back to v' + v, okLabel: 'Roll back', fields: fields, html: html, wide: true,
      text: 'Publishes the text of v' + v + ' as a NEW version and makes the working copy match it. Earlier versions are never deleted. To reach production it must be approved, exported and imported again.',
      run: async function (f) {
        var body = { lang: lang, track: track, version: +v, note: f.note };
        if (imp.requires_confirmation) body.confirm_impact_hash = imp.impact_hash;
        result = await api('/rollback', { method: 'POST', json: body });
      } }).then(function (r) { st.roll = null; if (r) { alertMsg('Rolled back: new version v' + (result && result.version) + ' created.', true); reload(); } });
  }
  async function rollbackRecover(lang, track, action) {
    var rb = st.rollback || {}, takeover = !!rb.can_resume && !rb.is_owner;
    var txt = action === 'abort' ? 'Cancels this unfinished rollback. The new version has not been written, so nothing has changed.'
      : 'Finishes the unfinished rollback: the working copy is restored to match v' + rb.target_version + ' (new version v' + rb.new_version + (rb.version_written ? ', already live' : '') + ').' +
        (takeover ? ' You are taking over from ' + ((rb.owner && rb.owner.name) || 'the previous owner') + ' because the lease expired.' : '');
    return dialog({ title: action === 'abort' ? 'Abort rollback' : (takeover ? 'Take over and resume rollback' : 'Resume rollback'), okLabel: action === 'abort' ? 'Abort' : 'Resume', text: txt,
      run: async function () { await api('/rollback/resume', { method: 'POST', json: { lang: lang, track: track, action: action } }); } })
      .then(function (r) { if (r) { alertMsg(action === 'abort' ? 'Rollback aborted.' : 'Rollback finished.', true); reload(); } });
  }

  // ── actions ──────────────────────────────────────────────────────────────────────────────
  var NOTE = { id: 'note', label: 'Note (required)', type: 'note', required: true };
  async function reload() { await boot(true); }
  async function doAction(name, el) {
    alertMsg('');
    var lang = st.lang, track = st.track, v = el && el.getAttribute('data-v');
    if (name === 'publish') {
      return dialog({ title: isProd() ? 'Publish imported package' : 'Publish draft', okLabel: 'Publish', fields: [NOTE],
        text: (isProd() ? 'This makes the imported, signed package live for ' : 'This creates a new published version for ') + lang + ' (' + TRACK_LABEL[track] + '). Apps will receive it unless the kill switch is ON.',
        run: async function (f) { await api('/publish', { method: 'POST', json: { lang: lang, track: track, note: f.note } }); } }).then(function (r) { if (r) { alertMsg('Published.', true); reload(); } });
    }
    if (name === 'rollback') return rollbackFlow(lang, track, v);
    if (name === 'rollsec') return rollSection(el.getAttribute('data-sec'), +el.getAttribute('data-page') || 1);
    if (name === 'rollcsv') return rollCsv();
    if (name === 'rbresume' || name === 'rbabort') return rollbackRecover(lang, track, name === 'rbabort' ? 'abort' : 'resume');
    if (name === 'approve') return dialog({ title: 'Approve v' + v + ' for production', okLabel: 'Approve', fields: [NOTE],
      text: 'Confirm this version has been tested. Approval lets it be exported as a signed package. It does not publish anything in production.',
      run: async function (f) { await api('/approve-for-production', { method: 'POST', json: { lang: lang, track: track, version: +v, note: f.note } }); } }).then(function (r) { if (r) { alertMsg('Approved.', true); reload(); } });
    if (name === 'export') {
      try {
        var pkg = await api('/package?lang=' + encodeURIComponent(lang) + '&track=' + track + '&version=' + encodeURIComponent(v));
        var blob = new Blob([JSON.stringify(pkg, null, 2)], { type: 'application/json' }), a = document.createElement('a');
        a.href = URL.createObjectURL(blob); a.download = 'offro-translations-' + track + '-' + lang + '-v' + v + '.json';
        document.body.appendChild(a); a.click(); a.remove(); setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
        alertMsg('Package downloaded. Import it in the production dashboard.', true);
      } catch (e) { alertMsg(e.message); }
      return;
    }
    if (name === 'import') {
      var file = $('trn_pkg_file') && $('trn_pkg_file').files[0];
      if (!file) { alertMsg('Choose the package file first.'); return; }
      if (file.size > 2 * 1024 * 1024) { alertMsg('That file is too large to be a translation package.'); return; }
      var obj; try { obj = JSON.parse(await file.text()); } catch (e) { alertMsg('That file is not valid JSON.'); return; }
      return dialog({ title: 'Import package as draft', okLabel: 'Import', text: 'The package is verified first. It becomes a draft only; publishing is a separate step.',
        run: async function () { await api('/package/import', { method: 'POST', json: obj }); } }).then(function (r) { if (r) { alertMsg('Imported as a draft. Review it, then publish.', true); st.tab = 'strings'; reload(); } });
    }
    if (name === 'killswitch') {
      var on = el.getAttribute('data-on') === '1';
      return dialog({ title: on ? 'Turn kill switch ON' : 'Turn kill switch OFF', okLabel: 'Confirm', fields: [{ id: 'note', label: 'Reason (required)', type: 'note', required: true }],
        text: on ? 'Apps will ignore remote translations and use their bundled text (' + envName() + ' only).' : 'Apps may use remote translations again (' + envName() + ' only).',
        run: async function (f) { await api('/kill-switch', { method: 'POST', json: { enabled: on, reason: f.note } }); } }).then(function (r) { if (r) reload(); });
    }
    if (name === 'submit') {
      try { var s = await api('/submit', { method: 'POST', json: { lang: lang, track: track } }); alertMsg(s.submitted + ' draft(s) marked ready for review.', true); loadStrings(); } catch (e) { alertMsg(e.message); }
      return;
    }
    if (name === 'legalreview') {
      var key = el.getAttribute('data-key');
      return dialog({ title: 'Record legal review', okLabel: 'Record', fields: [NOTE], text: 'Confirms the current text of ' + key + ' was reviewed. Editing it again invalidates the review.',
        run: async function (f) { await api('/legal/review', { method: 'POST', json: { lang: lang, key: key, note: f.note } }); } }).then(function (r) { if (r) alertMsg('Review recorded.', true); });
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
    if (st.tab === 'overview') renderOverview(); else if (st.tab === 'strings') loadStrings(); else if (st.tab === 'versions') loadVersions();
    else if (st.tab === 'promotion') renderPromotion(); else if (st.tab === 'audit') loadAudit();
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
    if (!isSA() && st.tab === 'promotion') st.tab = 'overview';
    renderBanner(); renderTabs(); paneLoad();
  }

  window.Translations = { open: function () { return boot(false); } };
})();
