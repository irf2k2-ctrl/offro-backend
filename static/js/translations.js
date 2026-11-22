/* Translations (Phase H) - admin dashboard module.
 *
 * A thin client for the translation API (/admin/translations). Two sections only: Overview and Translations. Edit a translation (its
 * status becomes Modified) -> Publish Changes -> the app can fetch it, with no new app build. Staging and production dashboards are the
 * same: each one reads and writes ONLY its own server's database, and nothing here talks to the other environment. The server is the
 * single source of truth for permissions, the environment check (an unidentified environment refuses every change) and the audit log;
 * the browser only shows what the API returns and hides controls the caller cannot use.
 *
 * Country mapping: a language (and English, the built-in base language) can be offered in several countries, and a country can have
 * several languages. The country choices always come from the server (the one country master); this file has no country list.
 * An empty selection means "no restriction" (offered in every country).
 *
 * Output safety: every HTML string is built with the h`...` tag, which escapes every interpolated value unless wrapped in raw()
 * (only used for markup produced by this file). innerHTML is assigned only through setHtml(). Server values are never put in
 * attributes unescaped, and never evaluated.
 */
(function () {
  'use strict';

  var API = '/admin/translations';
  var DENIED = 'You do not have permission to view Translations. Ask a Super Admin to grant the module to your role.';
  var TRACK_LABEL = { app: 'App text', legal: 'Legal text', documents: 'Legal documents' };
  // 'documents' is its own text type (whole legal / information documents, edited side by side with the English) with its own versions.
  function apiTrack(t) { return t; }

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
  var st = { env: null, langs: [], countries: [], tab: 'overview', lang: '', track: 'app', filter: 'all', q: '', page: 1, strings: null, seq: 0, docs: [], doc: '', docData: null };

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
  function countryObj(iso) { for (var i = 0; i < st.countries.length; i++) if (st.countries[i].iso === iso) return st.countries[i]; return null; }
  function countryLabel(c) { return (c.flag ? c.flag + ' ' : '') + c.name; }
  // Mapped countries as small chips, from the server's list (an unknown code is skipped). Nothing selected = every country.
  function countryChips(codes) {
    var out = (codes || []).map(function (iso) { var c = countryObj(iso); return c ? h`<span class="trn-chip" title="${c.iso}">${countryLabel(c)}</span>` : ''; }).join('');
    return out || '<span class="trn-hint">All countries</span>';
  }
  // Checkbox group for choosing countries (the options are the server's supported countries).
  function countryBoxes(id, selected) {
    selected = selected || [];
    if (!st.countries.length) return '<div class="trn-hint">No supported countries are available.</div>';
    return h`<div class="trn-countries" id="${id}">` + st.countries.map(function (c) {
      return h`<label class="trn-country"><input type="checkbox" value="${c.iso}" ${raw(selected.indexOf(c.iso) >= 0 ? 'checked' : '')}> ${countryLabel(c)}</label>`;
    }).join('') + '</div>';
  }
  function checkedIn(id) {
    var box = $(id), out = [];
    if (box) Array.prototype.forEach.call(box.querySelectorAll('input[type=checkbox]'), function (i) { if (i.checked) out.push(i.value); });
    return out;
  }

  // ── API ──────────────────────────────────────────────────────────────────────────────────
  function ApiError(status, message, code) { this.status = status; this.message = message; this.code = code; }
  var CODES = {
    audit_unavailable: 'Nothing was changed because the audit log could not be written. Try again shortly.',
    nothing_to_publish: 'There are no changes to publish.',
    environment_unknown: 'This server does not know whether it is staging or production, so changes are refused. Ask the developer to set OFFRO_ENVIRONMENT.',
    language_exists: 'That language already exists. Nothing was changed.',
    unsupported_country: 'One of the selected countries is not supported. Nothing was changed.',
    invalid_countries: 'The country selection is not valid. Nothing was changed.',
    language_disabled: 'This language is disabled, so nothing can be published for it.',
    not_xlsx: 'Please choose an Excel (.xlsx) file that was downloaded with Download Draft.',
    empty_file: 'The file is empty.',
    file_too_large: 'The file is too large.',
    invalid_file: 'The file could not be read. Download a fresh draft and try again.',
    missing_sheet: 'This is not a Download Draft file (the Translations or Info sheet is missing).',
    not_a_draft_file: 'This is not a Download Draft file. Download a draft, edit it, then import it.',
    macros_not_allowed: 'Files with macros are not accepted.',
    too_many_rows: 'The file has too many rows.',
    environment_mismatch: 'This file was downloaded from a different environment (staging or production) than this server. Nothing was changed. Download a fresh draft here.',
    language_mismatch: 'This file belongs to a different language than the one selected. Nothing was changed.',
    track_mismatch: 'This file belongs to a different text type (App text / Legal text) than the one selected. Nothing was changed.',
    version_conflict: 'Another publish happened at the same time. Nothing was changed. Try again.',
    unknown_document: 'This document does not exist.',
    document_exists: 'A document with that name already exists, so nothing was changed. Existing documents are never replaced.',
    invalid_slug: 'The document id must be 2-48 characters: lowercase letters, digits and underscores, starting with a letter.',
    invalid_title: 'Enter a title of up to 80 characters.',
    empty_content: 'The English text cannot be empty.',
    not_editable_here: 'The English text of this document is managed where it was created (Policies, Terms or About). It cannot be changed here.',
    too_long: 'This text is too long for this document.',
    empty_value: 'The translation cannot be empty.',
    html_not_allowed: 'HTML tags and entities are not allowed. Use plain text (headings with #, lists with - or 1.).',
    url_email_or_phone_not_in_english: 'The translation contains a link, e-mail address or phone number that is not in the English text. Use only the ones from the English text.',
    key_deprecated: 'This document no longer has English text, so it cannot be edited.'
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
    if (opts.form) init.body = opts.form;                                  // multipart: the browser sets the boundary itself
    var res;
    try { res = await fetch(API + path, init); } catch (e) { throw new ApiError(0, 'Could not reach the server. Check your connection.'); }
    if (res.status === 401) { window.location.href = '/admin'; throw new ApiError(401, messageFor(null, 401)); }
    if (opts.blob && res.ok) return { blob: await res.blob(), name: ((res.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/) || [])[1] || 'translations.xlsx' };
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
        else if (f.type === 'multicheck') fh += h`<div class="mb-2"><label class="small fw-bold">${f.label}</label>${raw(countryBoxes('trn_f_' + f.id, f.selected))}<div class="trn-hint">${f.hint || ''}</div></div>`;
        else if (f.type === 'long') fh += h`<div class="mb-2"><label class="small fw-bold">${f.label}</label><textarea id="trn_f_${f.id}" rows="10" class="trn-long">${f.value || ''}</textarea></div>`;
        else if (f.type === 'check') fh += h`<div class="mb-2"><label class="small fw-bold"><input type="checkbox" id="trn_f_${f.id}"> ${f.label}</label></div>`;
        else if (f.type === 'note') fh += h`<div class="mb-2"><label class="small fw-bold">${f.label}</label><textarea id="trn_f_${f.id}" rows="3" maxlength="300"></textarea></div>`;
        else fh += h`<div class="mb-2"><label class="small fw-bold">${f.label}</label><input type="text" id="trn_f_${f.id}" maxlength="80" value="${f.value || ''}"></div>`;
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
          var f = o.fields[i];
          vals[f.id] = f.type === 'multicheck' ? checkedIn('trn_f_' + f.id) : f.type === 'check' ? ($('trn_f_' + f.id).checked ? '1' : '') : ($('trn_f_' + f.id).value || '').trim();
          if (f.required && (f.type === 'multicheck' ? !vals[f.id].length : !vals[f.id])) { err.textContent = f.type === 'check' ? 'Please tick the box to confirm.' : f.label + ' is required.'; err.hidden = false; return; }
        }
        ok.disabled = true;
        try { var kept = o.run ? await o.run(vals) : null; if (kept === 'keep') { ok.disabled = false; return; } close(vals); }
        catch (e) { err.textContent = e.message || 'Failed.'; err.hidden = false; }
        ok.disabled = false;
      };
      m.hidden = false;
      if (o.onOpen) o.onOpen();
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
    var to = ['app', 'legal', 'documents'].map(function (t) { return h`<option value="${t}" ${raw(t === st.track ? 'selected' : '')}>${TRACK_LABEL[t]}</option>`; }).join('');
    return h`<select data-change="lang" aria-label="Language">${raw(lo)}</select><select data-change="track" aria-label="Track">${raw(to)}</select>`;
  }

  // ── overview ─────────────────────────────────────────────────────────────────────────────
  function statusPill(s, tr) {
    if (!s) return '—';
    if (s.complete && s.published_version) return '<span class="trn-pill ok">Complete</span>';
    if (s.published_version) return '<span class="trn-pill warn">Needs attention</span>';
    return '<span class="trn-pill off">Not published</span>';
  }
  function countriesCell(l) {
    var edit = isSA() && envKnown() ? h` <button type="button" class="btn-s trn-edit-countries" data-act="editcountries" data-lang="${l.code}" aria-label="Edit countries for ${l.name_en}">Edit</button>` : '';
    return '<div class="trn-countries-cell">' + countryChips(l.countries) + edit + '</div>';
  }
  function renderOverview() {
    var rows = '';
    st.langs.forEach(function (l) {
      ['app', 'legal'].forEach(function (tr) {
        var s = (l.status_indicators || {})[tr]; if (!s) return;
        var draft = s.draft_changes ? h`${s.draft_changes} modified` : '—';
        rows += h`<tr><td>${l.name_en} <span class="trn-key">${l.code}</span><div class="trn-hint">${l.name_native} · ${l.direction.toUpperCase()} · ${l.status}</div></td>` +
          h`<td>${raw(countriesCell(l))}</td><td>${TRACK_LABEL[tr]}</td><td>${raw(draft)}</td>` +
          h`<td>${s.missing} missing</td><td>${raw(statusPill(s))}</td><td>${fmt((l.last_updated || {})[tr])}</td>` +
          h`<td><button type="button" class="btn-s" data-act="open" data-lang="${l.code}" data-track="${tr}">Open</button></td></tr>`;
      });
    });
    var base = st.base || { code: 'en', name_en: 'English', name_native: 'English', direction: 'ltr' };
    var baseRow = h`<tr><td>${base.name_en} <span class="trn-key">${base.code}</span><div class="trn-hint">${base.name_native} · ${(base.direction || 'ltr').toUpperCase()} · built-in</div></td>` +
      h`<td>${raw(countriesCell(base))}</td><td colspan="6" class="trn-hint">Source language. Its text comes from the app and is not edited here.</td></tr>`;
    var add = '';
    if (isSA() && envKnown()) {
      var arHint = langObj('ar') ? '' : '<p class="trn-note">Arabic is not added yet. <button type="button" class="btn-s" data-act="fillar">Fill in Arabic</button> then press Add language.</p>';
      add = '<div class="trn-box"><h4>Add a language</h4>' + arHint + '<div class="trn-form">' +
        '<div><label>Code (e.g. ar)</label><input id="trn_nl_code" maxlength="10"></div><div><label>English name</label><input id="trn_nl_en" maxlength="60"></div>' +
        '<div><label>Native name</label><input id="trn_nl_native" maxlength="60"></div>' +
        '<div><label>Direction</label><select id="trn_nl_dir"><option value="ltr">Left to right</option><option value="rtl">Right to left</option></select></div>' +
        '<div><button type="button" class="btn-p" data-act="addlang">Add language</button></div></div>' +
        '<div class="trn-nl-countries"><label class="small fw-bold">Countries (select one or more)</label>' + countryBoxes('trn_nl_countries', []) +
        '<div class="trn-hint">Only supported countries are listed. Select none to offer the language in every country.</div></div></div>';
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
      '<div class="table-wrap"><table class="trn-table"><thead><tr><th>Language</th><th>Countries</th><th>Track</th><th>Modified</th><th>Completion</th><th>Status</th><th>Last updated</th><th></th></tr></thead><tbody>' + baseRow + rows + '</tbody></table></div>' +
      none + kill + add);
  }

  // ── translations (strings) ───────────────────────────────────────────────────────────────
  async function loadStrings() {
    var my = ++st.seq, p = $('trn_pane_strings');
    if (!st.lang) { setHtml(p, '<div class="trn-empty">Select or add a language first.</div>'); return; }
    if (st.track === 'documents') return loadDocuments(my);
    setHtml(p, '<div class="trn-empty"><i class="fas fa-spinner fa-spin"></i> Loading...</div>');
    try {
      var d = await api('/strings?lang=' + encodeURIComponent(st.lang) + '&track=' + apiTrack(st.track) + '&filter=' + st.filter + '&q=' + encodeURIComponent(st.q) + '&page=' + st.page);
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
    // The one Publish Changes button lives in the top toolbar, next to the search box (same action, same rules as before).
    var publish = isSA() && envKnown() ? '<button type="button" class="btn-p trn-publish" data-act="publish">Publish Changes</button>' : '';
    // Download Draft (view permission) and Import (edit permission) for the selected language + text type. Import never publishes.
    var xfer = '<button type="button" class="btn-s trn-xfer" data-act="downloaddraft">Download Draft</button>' +
      (edit ? '<button type="button" class="btn-s trn-xfer" data-act="importdraft">Import</button>' : '');
    setHtml($('trn_pane_strings'),
      h`<div class="trn-toolbar">${raw(selectors())}<select data-change="filter" aria-label="Filter">${raw(filt)}</select>` +
      h`<input type="search" placeholder="Search key or English text" value="${st.q}" data-input="q" aria-label="Search">${raw(publish)}${raw(xfer)}</div>` +
      (!envKnown() ? '<p class="trn-note">Changes are disabled until the environment is identified.</p>' : '') +
      (st.track === 'legal' ? '<p class="trn-note">Legal text can be edited and published here, but the customer app does not read legal text yet, so publishing it does not change what customers see. Whole documents (Privacy Policy, Terms, About Us ...) are translated in the <strong>Legal documents</strong> view, and the customer app does show those.</p>' : '') +
      (d.rows.length ? '<div class="table-wrap"><table class="trn-table"><thead><tr><th>Key</th><th>English (source)</th><th>Translation</th><th>Status</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
        : '<div class="trn-empty">No translation keys match.' + (d.total === 0 && st.filter === 'all' && !st.q ? ' The key list is created from the app text during the localization phase.' : '') + '</div>') +
      h`<div class="trn-toolbar"><button type="button" class="btn-s" data-act="prev" ${raw(d.page <= 1 ? 'disabled' : '')}>Previous</button><span>Page ${d.page} of ${pages} · ${d.total} key${d.total === 1 ? '' : 's'}</span><button type="button" class="btn-s" data-act="next" ${raw(d.page >= pages ? 'disabled' : '')}>Next</button></div>`);
  }

  // ── legal & information documents (side-by-side editor) ──────────────────────────────────
  // Left: the English source, READ ONLY (a readonly element with no save action; the server has no route that changes English here).
  // Right: the selected language, editable. Saving uses the same endpoint, permission, validation and Modified/Published rule as every
  // other translation (PUT /strings/{lang}/doc_<slug>); publishing is the same Publish Changes of the legal text type.
  async function loadDocuments(my) {
    var p = $('trn_pane_strings');
    try {
      var d = await api('/documents?lang=' + encodeURIComponent(st.lang));
      if (my !== st.seq) return;
      st.docs = d.documents || [];
      var avail = st.docs.filter(function (x) { return x.has_english && !x.too_long; });
      if (!avail.some(function (x) { return x.slug === st.doc; })) st.doc = avail.length ? avail[0].slug : '';
      st.docData = st.doc ? await api('/documents/' + encodeURIComponent(st.doc) + '?lang=' + encodeURIComponent(st.lang)) : null;
      if (my !== st.seq) return;
      renderDocuments();
    } catch (e) { if (my === st.seq) setHtml(p, h`<div class="trn-alert">${e.message}</div>`); }
  }
  function statusLabel(s) { return s === 'published' ? 'Published' : s === 'modified' ? 'Modified' : 'Not translated'; }
  function renderDocuments() {
    var d = st.docData, lo = langObj(st.lang), dir = lo ? lo.direction : 'ltr', edit = canEdit();
    var avail = st.docs.filter(function (x) { return x.has_english && !x.too_long; });
    var opts = avail.map(function (x) { return h`<option value="${x.slug}" ${raw(x.slug === st.doc ? 'selected' : '')}>${x.title} · ${statusLabel(x.status)}</option>`; }).join('');
    var publish = isSA() && envKnown() ? '<button type="button" class="btn-p trn-publish" data-act="publish">Publish Changes</button>' : '';
    var xfer = '<button type="button" class="btn-s trn-xfer" data-act="downloaddraft">Download Draft</button>' + (edit ? '<button type="button" class="btn-s trn-xfer" data-act="importdraft">Import</button>' : '');
    var add = isSA() && envKnown() ? '<button type="button" class="btn-s" data-act="adddoc">Add document</button>' : '';
    var pending = st.docs.filter(function (x) { return !x.has_english; }).map(function (x) { return x.title; });
    var tooLong = st.docs.filter(function (x) { return x.too_long; }).map(function (x) { return x.title; });
    var body;
    if (!d) {
      body = '<div class="trn-empty">No document has English text yet.' + (isSA() ? ' Use <strong>Add document</strong> to add one.' : '') + '</div>';
    } else {
      var langName = lo ? lo.name_en + ' (' + lo.code + ')' : st.lang;
      var own = isSA() && envKnown() && (st.docs.filter(function (x) { return x.slug === d.slug; })[0] || {}).editable_english_here;
      var dis = edit ? '' : 'disabled';
      body = h`<div class="trn-doc-grid">` +
        h`<section class="trn-doc-panel trn-doc-panel-en" aria-label="English source">` +
          h`<header><span class="trn-doc-lang">English (en)</span><span class="trn-pill off">Read-only source</span>${raw(own ? '<button type="button" class="btn-s trn-doc-editen" data-act="editenglish">Edit English</button>' : '')}</header>` +
          h`<textarea class="trn-doc-en" dir="ltr" readonly aria-readonly="true" tabindex="0" aria-label="English text of ${d.title} (read only)">${d.english}</textarea>` +
          h`<footer class="trn-hint">${d.english.length} characters · the English text cannot be changed here</footer></section>` +
        h`<section class="trn-doc-panel" aria-label="${langName} translation">` +
          h`<header><span class="trn-doc-lang" data-doc-lang>${langName}</span><span class="trn-pill ${d.status === 'published' ? 'ok' : d.status === 'modified' ? 'warn' : 'off'}" data-doc-status>${statusLabel(d.status)}</span></header>` +
          h`<textarea class="trn-doc-val" dir="${dir}" lang="${st.lang}" data-orig="${d.value || ''}" data-input="docval" ${raw(dis)} aria-label="${langName} translation of ${d.title}" placeholder="Translate the English text on the left. Keep the headings (#), lists (- or 1.) and links exactly as they are.">${d.value || ''}</textarea>` +
          h`<footer><span class="trn-hint" data-doc-count>${(d.value || '').length} / ${d.max_length} characters</span>` +
          h`${raw(edit ? '<button type="button" class="btn-p" data-act="savedoc" hidden>Save</button>' : '')}</footer><div class="trn-rowerr" data-doc-err hidden></div></section></div>` +
        (d.english_changed ? '<p class="trn-note trn-doc-stale">The English text was changed after this translation was saved. Review the translation against the current English.</p>' : '') +
        '<p class="trn-hint">Saving makes the translation Modified. It reaches customers only after Publish Changes. Customers whose language has no published translation see the English text.</p>';
    }
    setHtml($('trn_pane_strings'),
      h`<div class="trn-toolbar">${raw(selectors())}<select data-change="doc" class="trn-doc-select" aria-label="Document" ${raw(avail.length ? '' : 'disabled')}>${raw(opts)}</select>${raw(add)}${raw(publish)}${raw(xfer)}</div>` +
      (!envKnown() ? '<p class="trn-note">Changes are disabled until the environment is identified.</p>' : '') +
      (pending.length ? h`<p class="trn-hint">No English text yet (not listed above): ${pending.join(', ')}.</p>` : '') +
      (tooLong.length ? h`<p class="trn-note">Too long to translate here: ${tooLong.join(', ')}.</p>` : '') +
      body);
  }
  async function saveDoc() {
    var ta = document.querySelector('#trn_pane_strings .trn-doc-val'), btn = document.querySelector('#trn_pane_strings [data-act=savedoc]'), err = document.querySelector('#trn_pane_strings [data-doc-err]');
    if (!ta || !btn || !st.docData) return;
    err.hidden = true; btn.disabled = true;
    try {
      var r = await api('/strings/' + encodeURIComponent(st.lang) + '/' + encodeURIComponent(st.docData.key), { method: 'PUT', json: { value: ta.value } });
      ta.setAttribute('data-orig', ta.value); btn.hidden = true;
      var back = r && r.status === 'published';
      var s = document.querySelector('#trn_pane_strings [data-doc-status]'); s.textContent = back ? 'Published' : 'Modified'; s.className = 'trn-pill ' + (back ? 'ok' : 'warn');
      st.docData.value = ta.value; st.docData.status = back ? 'published' : 'modified';
      st.docs.forEach(function (x) { if (x.slug === st.doc) x.status = st.docData.status; });
      var sel = document.querySelector('#trn_pane_strings select[data-change=doc]');
      if (sel) Array.prototype.forEach.call(sel.options, function (o) { if (o.value === st.doc) o.textContent = st.docData.title + ' · ' + statusLabel(st.docData.status); });
      if (r && r.audit_warning) alertMsg(r.audit_warning);
      else alertMsg(back ? 'Saved. This text is the same as the published text, so its status is Published.' : 'Saved. Status is now Modified. Press Publish Changes to make it live.', true);
    } catch (e) { err.textContent = e.message; err.hidden = false; }
    btn.disabled = false;
  }
  function docDirty() {
    var ta = document.querySelector('#trn_pane_strings .trn-doc-val');
    return !!(ta && ta.value !== ta.getAttribute('data-orig'));
  }
  function addDocument() {
    var known = [['faq', 'FAQ'], ['contact_us', 'Contact Us'], ['cancellation_policy', 'Cancellation Policy'], ['disclaimer', 'Disclaimer'], ['how_offro_works', 'How Offro Works'], ['merchant_policy', 'Merchant Policy']]
      .filter(function (k) { return st.docs.some(function (x) { return x.slug === k[0] && !x.has_english; }); });
    var hint = known.length ? '<p class="trn-hint">Types waiting for English text: ' + known.map(function (k) { return esc(k[1] + ' (' + k[0] + ')'); }).join(', ') + '. Use the same id to fill one.</p>' : '';
    return dialog({ title: 'Add document', okLabel: 'Add document', wide: true,
      text: 'Adds the English text of a document that does not exist yet. An existing document is never replaced. Translate it afterwards in this editor.',
      html: hint,
      fields: [{ id: 'slug', label: 'Document id (lowercase letters, digits, underscores)', type: 'text', required: true },
               { id: 'title', label: 'Title', type: 'text', required: true },
               { id: 'audience', label: 'Shown to', type: 'select', options: [['customer', 'Customers'], ['merchant', 'Merchants']] },
               { id: 'content', label: 'English text', type: 'long', required: true }],
      run: async function (f) { var r = await api('/documents', { method: 'POST', json: { slug: f.slug.toLowerCase(), title: f.title, audience: f.audience, content: f.content } }); st.doc = r.slug; } })
      .then(function (r) { if (r) { alertMsg('Document added. It can now be translated.', true); loadStrings(); } });
  }
  function editEnglish() {
    var d = st.docData; if (!d) return;
    return dialog({ title: 'Edit English text: ' + d.title, okLabel: 'Save English text', wide: true,
      text: 'This changes the English source for every language. Existing translations stay as they are and are flagged for review.',
      fields: [{ id: 'title', label: 'Title', type: 'text', required: true, value: d.title }, { id: 'content', label: 'English text', type: 'long', required: true, value: d.english }],
      run: async function (f) { await api('/documents/' + encodeURIComponent(d.slug) + '/english', { method: 'PUT', json: { content: f.content, title: f.title } }); } })
      .then(function (r) { if (r) { alertMsg('English text saved.', true); loadStrings(); } });
  }
  async function saveRow(tr) {
    var key = tr.getAttribute('data-row'), ta = tr.querySelector('textarea'), err = tr.querySelector('.trn-rowerr'), btn = tr.querySelector('[data-act=save]');
    err.hidden = true; btn.disabled = true;
    try {
      var r = await api('/strings/' + encodeURIComponent(st.lang) + '/' + encodeURIComponent(key), { method: 'PUT', json: { value: ta.value } });
      ta.setAttribute('data-orig', ta.value); btn.hidden = true;
      var back = r && r.status === 'published';                           // the text equals the published baseline again
      var s = tr.querySelector('[data-status]'); s.textContent = back ? 'published' : 'modified'; s.className = 'trn-pill ' + (back ? 'ok' : 'warn');
      if (r && r.audit_warning) alertMsg(r.audit_warning);
      else alertMsg(back ? 'Saved. This text is the same as the published text, so its status is Published: ' + key + '.'
                         : 'Saved. Status is now Modified: ' + key + '. Press Publish Changes to make it live.', true);
    } catch (e) { err.textContent = e.message; err.hidden = false; }
    btn.disabled = false;
  }

  // ── actions ──────────────────────────────────────────────────────────────────────────────
  var NOTE = { id: 'note', label: 'Note (optional)', type: 'note', required: false };
  // Import: step 1 checks the file and shows the summary (nothing is written); step 2 stores the changes as Modified (never publishes).
  function importSummary(r, applied) {
    var valid = r.valid !== undefined ? r.valid : r.updated + r.unchanged;
    var rows = [['Total records', r.total], ['Valid records', valid], ['Updated records', r.updated], ['Unchanged records', r.unchanged], ['Invalid records', r.invalid], ['Missing records', r.missing]];
    var out = '<table class="trn-table trn-import-summary"><tbody>' + rows.map(function (x) { return h`<tr><th>${x[0]}</th><td data-sum="${x[0]}">${x[1]}</td></tr>`; }).join('') + '</tbody></table>';
    if (r.invalid_rows && r.invalid_rows.length) out += '<div class="trn-hint">Invalid (first ' + Math.min(r.invalid_rows.length, 20) + '): ' + r.invalid_rows.slice(0, 20).map(function (x) { return esc('row ' + x.row + ' ' + (x.key || '(no key)') + ': ' + x.code); }).join('; ') + '</div>';
    if (r.missing_keys && r.missing_keys.length) out += '<div class="trn-hint">Not in the file (first ' + Math.min(r.missing_keys.length, 10) + '): ' + r.missing_keys.slice(0, 10).map(esc).join(', ') + '</div>';
    if (r.baseline_changed) out += '<div class="trn-hint">Note: the published text changed since this file was downloaded. Changes were compared with the CURRENT published text.</div>';
    if (applied) out += '<div class="trn-hint">Imported changes are Modified. Nothing is live until you press Publish Changes.</div>';
    else out += '<div class="trn-hint trn-partial">This is a check only: nothing has been changed yet. <strong>Apply import</strong> will store the ' + r.updated + ' updated record' + (r.updated === 1 ? '' : 's') +
      ' as Modified and <strong>skip the ' + r.invalid + ' invalid record' + (r.invalid === 1 ? '' : 's') + '</strong> (invalid and duplicate rows are never written). Unchanged and missing records are left as they are.</div>';
    return out;
  }
  function importDraft(lang, track) {
    var previewed = false, lo = langObj(lang);
    function send(apply) {
      var inp = $('trn_imp_file'), file = inp && inp.files && inp.files[0];
      if (!file) throw new ApiError(0, 'Choose the edited .xlsx file first.');
      if (!/\.xlsx$/i.test(file.name)) throw new ApiError(0, messageFor({ detail: { code: 'not_xlsx' } }, 400));
      var fd = new FormData(); fd.append('file', file); fd.append('lang', lang); fd.append('track', track); fd.append('apply', apply ? '1' : '');
      return api('/import', { method: 'POST', form: fd });
    }
    return dialog({ title: 'Import translations (' + (lo ? lo.name_en : lang) + ', ' + TRACK_LABEL[track] + ')', okLabel: 'Check file', wide: true,
      text: 'Upload the edited draft. Rows are matched by key; English is never changed. Changed texts become Modified; nothing is published.',
      html: '<div class="mb-2"><input type="file" id="trn_imp_file" accept=".xlsx" aria-label="Edited draft file"></div><div id="trn_imp_out"></div>',
      onOpen: function () { $('trn_imp_file').onchange = function () { previewed = false; $('trn_modal_ok').textContent = 'Check file'; setHtml($('trn_imp_out'), ''); }; },
      run: async function () {
        if (!previewed) {
          var r = await send(false); setHtml($('trn_imp_out'), importSummary(r, false));
          if (!r.updated) { $('trn_modal_ok').textContent = 'Check file'; return 'keep'; }
          previewed = true; $('trn_modal_ok').textContent = 'Apply import (' + r.updated + ')'; return 'keep';
        }
        var res = await send(true);
        st.importResult = res;
      } }).then(function (v) {
        if (v && st.importResult) {
          var r = st.importResult; st.importResult = null;
          alertMsg('Import applied: ' + r.updated + ' updated, ' + r.unchanged + ' unchanged, ' + r.invalid + ' invalid, ' + r.missing + ' missing. Updated texts are Modified; press Publish Changes to make them live.', true);
          reload();
        }
      });
  }
  async function reload() { await boot(true); }
  async function doAction(name, el) {
    alertMsg('');
    var lang = st.lang, track = apiTrack(st.track), isDocs = st.track === 'documents';
    if (name === 'publish') {
      var legalNote = isDocs ? ' Customers see the published translation of a document the next time they open it; where a language has none they see English.' : track === 'legal' ? ' The customer app does not read legal text yet, so this does not change what customers see.' : ' Apps receive it the next time they check, unless the kill switch is ON.';
      return dialog({ title: 'Publish Changes', okLabel: 'Publish Changes', fields: [NOTE],
        text: 'Makes every modified ' + (isDocs ? 'legal document' : TRACK_LABEL[track]) + ' translation for ' + lang + ' live in ' + envName() + ' only.' + legalNote,
        run: async function (f) { await api('/publish', { method: 'POST', json: { lang: lang, track: track, note: f.note } }); } }).then(function (r) { if (r) { alertMsg('Published.', true); reload(); } });
    }
    if (name === 'killswitch') {
      var on = el.getAttribute('data-on') === '1';
      return dialog({ title: on ? 'Turn kill switch ON' : 'Turn kill switch OFF', okLabel: 'Confirm', fields: [{ id: 'note', label: 'Reason (required)', type: 'note', required: true }],
        text: on ? 'Apps will ignore remote translations and use their bundled text (' + envName() + ' only).' : 'Apps may use remote translations again (' + envName() + ' only).',
        run: async function (f) { await api('/kill-switch', { method: 'POST', json: { enabled: on, reason: f.note } }); } }).then(function (r) { if (r) reload(); });
    }
    if (name === 'downloaddraft') {
      try {
        var f = await api('/draft?lang=' + encodeURIComponent(lang) + '&track=' + encodeURIComponent(track), { blob: true });
        var a = document.createElement('a'); a.href = URL.createObjectURL(f.blob); a.download = f.name; document.body.appendChild(a); a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
        alertMsg('Draft downloaded. Edit only the translation column, then use Import.', true);
      } catch (e) { alertMsg(e.message); }
      return;
    }
    if (name === 'importdraft') return importDraft(lang, track);
    if (name === 'adddoc') return addDocument();
    if (name === 'editenglish') return editEnglish();
    if (name === 'editcountries') {
      var code = el.getAttribute('data-lang'), lo = code === 'en' ? st.base : langObj(code);
      if (!lo) return;
      return dialog({ title: 'Countries for ' + lo.name_en, okLabel: 'Save countries',
        text: 'Choose every country where this language is offered. Applies to ' + envName() + ' only and takes effect immediately.',
        fields: [{ id: 'countries', label: 'Countries', type: 'multicheck', selected: lo.countries || [], hint: 'Select none to offer the language in every country.' }],
        run: async function (f) { await api('/languages/' + encodeURIComponent(code) + '/countries', { method: 'PUT', json: { countries: f.countries } }); } })
        .then(function (r) { if (r) { alertMsg('Countries saved for ' + lo.name_en + '.', true); reload(); } });
    }
    if (name === 'fillar') {
      $('trn_nl_code').value = 'ar'; $('trn_nl_en').value = 'Arabic'; $('trn_nl_native').value = 'العربية'; $('trn_nl_dir').value = 'rtl';
      return;
    }
    if (name === 'addlang') {
      var body = { code: $('trn_nl_code').value.trim(), name_en: $('trn_nl_en').value.trim(), name_native: $('trn_nl_native').value.trim(), direction: $('trn_nl_dir').value, countries: checkedIn('trn_nl_countries') };
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
      else if (a === 'savedoc') saveDoc();
      else if (a === 'prev') { st.page = Math.max(1, st.page - 1); loadStrings(); }
      else if (a === 'next') { st.page += 1; loadStrings(); }
      else doAction(a, el);
    });
    root.addEventListener('change', function (ev) {
      var a = ev.target.getAttribute('data-change'); if (!a) return;
      if (docDirty() && (a === 'lang' || a === 'track' || a === 'doc') && !window.confirm('You have an unsaved translation. Discard it?')) { renderDocuments(); return; }
      if (a === 'lang') st.lang = ev.target.value; else if (a === 'track') st.track = ev.target.value; else if (a === 'filter') st.filter = ev.target.value; else if (a === 'doc') st.doc = ev.target.value;
      st.page = 1; paneLoad();
    });
    root.addEventListener('input', function (ev) {
      var a = ev.target.getAttribute('data-input');
      if (a === 'val') { var tr = ev.target.closest('tr'), b = tr.querySelector('[data-act=save]'); if (b) b.hidden = ev.target.value === ev.target.getAttribute('data-orig'); }
      else if (a === 'docval') { var b2 = document.querySelector('#trn_pane_strings [data-act=savedoc]'); if (b2) b2.hidden = ev.target.value === ev.target.getAttribute('data-orig'); var c2 = document.querySelector('#trn_pane_strings [data-doc-count]'); if (c2 && st.docData) c2.textContent = ev.target.value.length + ' / ' + st.docData.max_length + ' characters'; }
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
      st.env = r[0]; st.langs = r[1].languages; st.base = r[1].base_language || null; st.countries = r[1].supported_countries || [];
    } catch (e) {
      if (e.status === 403) { $('trn_body').hidden = true; $('trn_denied').hidden = false; $('trn_denied_text').textContent = DENIED; return; }
      $('trn_body').hidden = false; alertMsg(e.message); return;
    }
    if (!langObj(st.lang)) st.lang = st.langs.length ? st.langs[0].code : '';
    renderBanner(); renderTabs(); paneLoad();
  }

  window.Translations = { open: function () { return boot(false); } };
})();
