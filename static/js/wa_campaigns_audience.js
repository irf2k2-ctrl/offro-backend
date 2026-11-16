/* WhatsApp Campaigns (Phase 2G) - Audience screen and the reusable audience picker used by the campaign editor.
 * Depends on wa_campaigns.js (window.WaCampaigns.kit). Uses only the existing /audience/* API; eligibility, consent and de-duplication are
 * decided by the server. Numbers are shown masked; a number typed into the manual-import box is sent once and the box is cleared. */
(function () {
  'use strict';
  var W = window.WaCampaigns;
  if (!W) return;
  var K = W.kit, h = K.h, raw = K.raw, setHtml = K.setHtml, $ = K.$, api = K.api, num = K.num;

  var MAX_CSV_BYTES = 2 * 1024 * 1024;                           // same limit the server enforces
  var REASON = { eligible: 'Eligible', opted_out: 'Opted out (STOP)', unknown_consent: 'No recorded consent', consent_lookup_failed: 'Consent could not be checked',
                 duplicate: 'Duplicate', invalid_number: 'Invalid number', merchant_inactive: 'Merchant inactive', merchant_no_active_store: 'No active store',
                 merchant_not_found: 'Merchant not found', merchant_out_of_scope: 'Outside your city scope', invalid_characters: 'Invalid characters',
                 too_short: 'Too short', too_long: 'Too long', empty_phone: 'Empty number', invalid_format: 'Invalid format',
                 invalid_indian_mobile: 'Not an Indian mobile', international_needs_plus: 'Needs +country code', scientific_notation: 'Spreadsheet-damaged number' };
  function reason(code) { return REASON[code] || String(code || '').replace(/_/g, ' '); }
  function eligPill(e) {
    if (!e) return '<span class="wac-pill">—</span>';
    var ok = e.eligible === true;
    return h`<span class="wac-pill ${ok ? 'ok' : 'bad'}" title="${e.consent_source || ''}">${ok ? 'Eligible' : reason(e.reason)}</span>`;
  }
  function msg(el, text, kind) { if (el) { el.textContent = text || ''; el.className = 'wac-msg' + (kind === 'error' ? ' is-error' : ''); } }

  // ═════════════════ reusable picker ═════════════════
  var pickers = {};                                              // root element id -> state

  function newPicker(rootId, initial) {
    var a = initial || {}, m = a.merchants || null;
    var flt = (m && m.filter) || {};
    return { id: rootId, filters: null, f: { q: flt.q || '', city: flt.city || '', area: flt.area || '', state: flt.state || '' },
             useFilter: !!(m && m.filter), ids: new Set((m && m.ids) || []), imports: new Set(a.imports || []), importList: null,
             page: 1, pages: 1, total: 0, merchants: null, mErr: '', seq: 0, pv: null, pvStatus: 'all', pvPage: 1, pvSeq: 0, readOnly: false };
  }
  function pk(el) { var root = el.closest('[data-aud-root]'); return root ? pickers[root.id] : null; }

  function audienceValue(p) {
    var ids = Array.from(p.ids), useF = p.useFilter;
    var m = (ids.length || useF) ? { ids: ids, filter: useF ? { q: p.f.q, city: p.f.city, area: p.f.area, state: p.f.state } : null } : null;
    return { merchants: m, imports: Array.from(p.imports) };
  }

  function opts(list, cur, label) {
    return h`<option value="">${label}</option>` + (list || []).map(function (v) { return h`<option value="${v}"${raw(v === cur ? ' selected' : '')}>${v}</option>`; }).join('');
  }

  function draw(p) {
    var root = $(p.id);
    if (!root) return;
    var ro = p.readOnly ? ' disabled' : '';
    var filt = p.filters || {};
    var html = '<div class="wac-panel"><div class="wac-panel-title">Merchants</div>' +
      '<div class="wac-toolbar">' +
        h`<input type="search" class="form-control grow" placeholder="Search name or store" value="${p.f.q}" data-input="aud-filter" data-key="q" autocomplete="off">` +
        h`<select class="form-select" data-change="aud-filter" data-key="city">${raw(opts(filt.cities, p.f.city, 'All cities'))}</select>` +
        h`<select class="form-select" data-change="aud-filter" data-key="area">${raw(opts(filt.areas, p.f.area, 'All areas'))}</select>` +
        h`<select class="form-select" data-change="aud-filter" data-key="state">${raw(opts(filt.states, p.f.state, 'All states'))}</select>` +
      '</div>' +
      h`<label class="wac-check"><input type="checkbox" data-change="aud-use-filter"${raw(p.useFilter ? ' checked' : '')}${raw(ro)}> <span>Include <b>every</b> merchant matching these filters` +
      h`${raw(!p.f.q && !p.f.city && !p.f.area && !p.f.state ? ' (no filter set: this means ALL merchants)' : '')}</span></label>` +
      '<div class="wac-hint">Or tick individual merchants below. Merchants that do not qualify are excluded automatically.</div>' +
      '<div id="' + p.id + '_m" class="table-wrap" style="margin-top:10px"></div>' +
      '<div class="wac-pager"><span class="wac-muted" id="' + p.id + '_mc"></span><div>' +
        '<button type="button" class="btn-o" data-act="aud-mprev">&larr; Prev</button> <button type="button" class="btn-o" data-act="aud-mnext">Next &rarr;</button></div></div></div>' +
      '<div class="wac-panel"><div class="wac-panel-title">Imported lists <span class="wac-muted">(manage them in the Audience tab)</span></div><div id="' + p.id + '_i"></div></div>' +
      '<div class="wac-panel"><div class="wac-panel-title">Eligibility preview' +
        '<button type="button" class="btn-p" data-act="aud-preview">Preview audience</button></div><div id="' + p.id + '_pv"><div class="wac-hint">' +
        'Shows who would be eligible. Nothing is saved or sent by previewing.</div></div></div>';
    setHtml(root, html);
    drawMerchants(p); drawImports(p); drawPreview(p);
  }

  function drawMerchants(p) {
    var el = $(p.id + '_m');
    if (!el) return;
    if (p.mErr) { setHtml(el, h`<div class="wac-cell-state is-error">${p.mErr} <button type="button" class="btn-o" data-act="aud-mretry">Retry</button></div>`); return; }
    if (!p.merchants) { setHtml(el, '<div class="wac-cell-state"><i class="fas fa-spinner fa-spin"></i> Loading merchants...</div>'); return; }
    var ro = p.readOnly ? ' disabled' : '';
    var rows = p.merchants.map(function (m) {
      var st0 = (m.stores && m.stores[0]) || {};
      return h`<tr><td class="wac-select-col"><input type="checkbox" data-change="aud-toggle-merchant" data-id="${m.merchant_id}"${raw(p.ids.has(m.merchant_id) ? ' checked' : '')}${raw(ro)}></td>` +
             h`<td><span class="wac-name">${m.name || 'Unnamed'}</span></td><td>${m.phone_masked}</td><td>${st0.city || '—'}${st0.area ? ' · ' + st0.area : ''}</td>` +
             h`<td style="text-align:right">${num(m.store_count)}</td><td>${raw(eligPill(m.eligibility))}</td></tr>`;
    }).join('');
    setHtml(el, '<table class="wac-table" style="min-width:640px"><thead><tr><th class="wac-select-col"></th><th>Merchant</th><th>Number</th><th>Location</th><th>Stores</th><th>Eligibility</th></tr></thead><tbody>' +
      (rows || '<tr><td colspan="6" class="wac-empty">No merchants match.</td></tr>') + '</tbody></table>');
    var c = $(p.id + '_mc');
    if (c) c.textContent = p.total ? 'Page ' + p.page + ' of ' + p.pages + ' · ' + p.total + ' merchants · ' + p.ids.size + ' selected' : (p.ids.size + ' selected');
  }

  async function loadMerchants(p) {
    var seq = ++p.seq;
    p.merchants = null; p.mErr = ''; drawMerchants(p);
    var q = '/audience/merchants?page=' + p.page + '&page_size=25' + ['q', 'city', 'area', 'state'].map(function (k) { return p.f[k] ? '&' + k + '=' + encodeURIComponent(p.f[k]) : ''; }).join('');
    try {
      var d = await api(q);
      if (seq !== p.seq) return;
      p.merchants = d.items || []; p.page = d.page || 1; p.pages = d.pages || 1; p.total = d.total || 0;
    } catch (e) {
      if (seq !== p.seq) return;
      p.mErr = e.message;
    }
    drawMerchants(p);
  }

  function drawImports(p) {
    var el = $(p.id + '_i');
    if (!el) return;
    if (!p.importList) { setHtml(el, '<div class="wac-hint"><i class="fas fa-spinner fa-spin"></i> Loading...</div>'); return; }
    if (p.importList.error) { setHtml(el, h`<div class="wac-cell-state is-error">${p.importList.error}</div>`); return; }
    var ro = p.readOnly ? ' disabled' : '';
    var items = p.importList;
    setHtml(el, items.length ? items.map(function (i) {
      var c = i.counts || {};
      return h`<label class="wac-check" style="margin:4px 0"><input type="checkbox" data-change="aud-toggle-import" data-id="${i.import_id}"${raw(p.imports.has(i.import_id) ? ' checked' : '')}${raw(ro)}> ` +
             h`<span><b>${i.filename || (i.kind === 'manual' ? 'Manual list' : 'Import')}</b> <span class="wac-muted">${K.fmtDateTime(i.created_at)} · ${num(c.valid)} valid, ${num(c.invalid)} rejected, ${num(c.duplicates)} duplicates</span></span></label>`;
    }).join('') : '<div class="wac-hint">No imported lists yet.</div>');
  }

  async function loadImports(p) {
    try { p.importList = (await api('/audience/imports')).items || []; }
    catch (e) { p.importList = { error: e.message }; }
    drawImports(p);
  }

  function drawPreview(p) {
    var el = $(p.id + '_pv');
    if (!el || !p.pv) return;
    var d = p.pv;
    if (d.loading) { setHtml(el, '<div class="wac-hint"><i class="fas fa-spinner fa-spin"></i> Checking eligibility...</div>'); return; }
    if (d.error) { setHtml(el, h`<div class="wac-note bad">${d.error}</div>`); return; }
    var s = d.summary || {}, reasons = s.by_reason || {};
    var chips = ['all', 'eligible', 'excluded'].map(function (v) {
      return h`<button type="button" class="wac-chip ${p.pvStatus === v ? 'active' : ''}" data-act="aud-pv-status" data-status="${v}">${v.charAt(0).toUpperCase() + v.slice(1)}</button>`;
    }).join('');
    var rows = (d.recipients || []).map(function (r) {
      return h`<tr><td>${r.name || '—'}</td><td>${r.phone_masked}</td><td>${r.source}</td><td>${raw(r.eligible ? '<span class="wac-pill ok">Eligible</span>' : h`<span class="wac-pill bad">${reason(r.exclusion_reason)}</span>`)}</td><td>${r.consent_source || '—'}</td></tr>`;
    }).join('');
    setHtml(el, (s.consent_check_ok === false ? '<div class="wac-note bad">Consent could not be verified for this preview. Nothing counts as eligible until it can.</div>' : '') +
      '<div class="wac-stats">' + [['Total', s.total], ['Eligible', s.eligible], ['Excluded', s.excluded], ['Rejected at import', s.rejected_at_import], ['Duplicates', s.duplicates_at_import]]
        .map(function (x) { return h`<div class="wac-stat"><b>${num(x[1])}</b><span>${x[0]}</span></div>`; }).join('') + '</div>' +
      (Object.keys(reasons).length ? '<div class="wac-hint" style="margin-top:8px">Excluded because: ' + Object.keys(reasons).map(function (k) { return h`${reason(k)} (${reasons[k]})`; }).join(' · ') + '</div>' : '') +
      '<div class="wac-chips" style="margin-top:12px">' + chips + '</div><div class="table-wrap"><table class="wac-table" style="min-width:600px"><thead><tr><th>Name</th><th>Number</th><th>Source</th><th>Result</th><th>Consent basis</th></tr></thead><tbody>' +
      (rows || '<tr><td colspan="5" class="wac-empty">Nothing to show.</td></tr>') + '</tbody></table></div>' +
      h`<div class="wac-pager"><span class="wac-muted">Page ${d.page} of ${d.pages} · ${d.filtered_total} shown</span><div><button type="button" class="btn-o" data-act="aud-pv-page" data-d="-1"${raw(d.page <= 1 ? ' disabled' : '')}>&larr; Prev</button> ` +
      h`<button type="button" class="btn-o" data-act="aud-pv-page" data-d="1"${raw(d.page >= d.pages ? ' disabled' : '')}>Next &rarr;</button></div></div>`);
  }

  async function runPreview(p) {
    var seq = ++p.pvSeq, a = audienceValue(p);
    if (!a.merchants && !a.imports.length) { p.pv = { error: 'Select merchants and/or an imported list first.' }; drawPreview(p); return; }
    p.pv = { loading: true }; drawPreview(p);
    try {
      var d = await api('/audience/preview', { method: 'POST', json: { merchants: a.merchants, imports: a.imports, page: p.pvPage, page_size: 25, status: p.pvStatus } });
      if (seq !== p.pvSeq) return;
      p.pv = d;
    } catch (e) {
      if (seq !== p.pvSeq) return;
      p.pv = { error: e.message };
    }
    drawPreview(p);
  }

  function mountPicker(rootId, initial, readOnly) {
    var p = newPicker(rootId, initial);
    p.readOnly = !!readOnly;
    pickers[rootId] = p;
    var root = $(rootId);
    if (root) root.setAttribute('data-aud-root', '1');
    draw(p);
    api('/audience/filters').then(function (f) { p.filters = f; draw(p); loadMerchants(p); }).catch(function () { p.filters = {}; draw(p); loadMerchants(p); });
    loadImports(p);
    return { get: function () { return audienceValue(p); }, refreshImports: function () { loadImports(p); } };
  }

  var debounce = null;
  var H = {
    'aud-filter': function (el) {
      var p = pk(el); if (!p) return;
      p.f[el.getAttribute('data-key')] = el.value; p.page = 1;
      clearTimeout(debounce); debounce = setTimeout(function () { loadMerchants(p); }, el.tagName === 'INPUT' ? 350 : 0);
    },
    'aud-use-filter': function (el) { var p = pk(el); if (p) { p.useFilter = el.checked; } },
    'aud-toggle-merchant': function (el) { var p = pk(el); if (!p) return; var id = el.getAttribute('data-id'); if (el.checked) p.ids.add(id); else p.ids.delete(id); drawMerchants(p); },
    'aud-toggle-import': function (el) { var p = pk(el); if (!p) return; var id = el.getAttribute('data-id'); if (el.checked) p.imports.add(id); else p.imports.delete(id); },
    'aud-mprev': function (el) { var p = pk(el); if (p && p.page > 1) { p.page -= 1; loadMerchants(p); } },
    'aud-mnext': function (el) { var p = pk(el); if (p && p.page < p.pages) { p.page += 1; loadMerchants(p); } },
    'aud-mretry': function (el) { var p = pk(el); if (p) loadMerchants(p); },
    'aud-preview': function (el) { var p = pk(el); if (p) { p.pvPage = 1; K.busy(el, function () { return runPreview(p); }); } },
    'aud-pv-status': function (el) { var p = pk(el); if (p) { p.pvStatus = el.getAttribute('data-status'); p.pvPage = 1; runPreview(p); } },
    'aud-pv-page': function (el) { var p = pk(el); if (p) { p.pvPage = Math.max(1, p.pvPage + Number(el.getAttribute('data-d'))); runPreview(p); } }
  };
  // picker handlers use mixed attributes (data-act / data-change / data-input): register each name under all three dispatchers
  var handlersOut = {};
  Object.keys(H).forEach(function (k) { handlersOut[k] = H[k]; });

  // ═════════════════ Audience tab ═════════════════
  var tab = { importsSeq: 0, detail: null };

  function drawTab() {
    setHtml($('wac_pane_audience'),
      '<div class="wac-panel"><div class="wac-panel-title">Import recipients</div><div class="wac-grid2">' +
        '<div><div class="wac-field"><label class="form-label" for="wac_imp_text">Paste numbers (one per line: number, name optional)</label>' +
        '<textarea id="wac_imp_text" class="form-control" autocomplete="off" spellcheck="false" placeholder="+91XXXXXXXXXX, Name\n98XXXXXXXX"></textarea>' +
        '<div class="wac-hint">Up to 1000 numbers. The box is cleared after the import and numbers are never shown again in full.</div></div>' +
        (K.can('add') ? '<button type="button" class="btn-p" data-act="imp-manual">Import pasted numbers</button>' : '') + '</div>' +
        '<div><div class="wac-field"><label class="form-label" for="wac_imp_file">Upload a CSV file</label>' +
        '<input type="file" id="wac_imp_file" class="form-control" accept=".csv,text/csv"><div class="wac-hint">CSV only, up to 2 MB. A header row with a phone column is expected.</div></div>' +
        (K.can('add') ? '<button type="button" class="btn-p" data-act="imp-csv">Upload CSV</button>' : '') + '</div></div>' +
        '<div id="wac_imp_msg" class="wac-msg" role="status" style="margin-top:10px"></div><div id="wac_imp_result"></div></div>' +
      '<div class="wac-panel"><div class="wac-panel-title">Imported lists <button type="button" class="btn-o" data-act="imp-reload">Refresh</button></div><div class="table-wrap"><table class="wac-table" style="min-width:680px">' +
        '<thead><tr><th>List</th><th>Created</th><th>By</th><th>Valid</th><th>Rejected</th><th>Duplicates</th><th></th></tr></thead><tbody id="wac_imp_rows"></tbody></table></div></div>' +
      '<div id="wac_imp_detail"></div>' +
      '<div class="wac-panel-title" style="margin-top:6px">Browse merchants and preview an audience</div><div id="wac_aud_tab_picker"></div>');
  }

  async function loadImportRows() {
    var seq = ++tab.importsSeq, tb = $('wac_imp_rows');
    setHtml(tb, '<tr><td colspan="7" class="wac-cell-state"><i class="fas fa-spinner fa-spin"></i> Loading...</td></tr>');
    try {
      var items = (await api('/audience/imports')).items || [];
      if (seq !== tab.importsSeq) return;
      setHtml(tb, items.length ? items.map(function (i) {
        var c = i.counts || {};
        return h`<tr><td><button type="button" class="wac-link wac-name" data-act="imp-view" data-id="${i.import_id}">${i.filename || (i.kind === 'manual' ? 'Manual list' : 'Import')}</button></td>` +
               h`<td>${K.fmtDateTime(i.created_at)}</td><td>${i.created_by || '—'}</td><td>${num(c.valid)}</td><td>${num(c.invalid)}</td><td>${num(c.duplicates)}</td>` +
               h`<td>${raw(K.can('delete') ? h`<button type="button" class="btn-d" data-act="imp-delete" data-id="${i.import_id}">Delete</button>` : '')}</td></tr>`;
      }).join('') : '<tr><td colspan="7" class="wac-empty">No imported lists yet.</td></tr>');
    } catch (e) {
      if (seq !== tab.importsSeq) return;
      setHtml(tb, h`<tr><td colspan="7" class="wac-cell-state is-error">${e.message} <button type="button" class="btn-o" data-act="imp-reload">Retry</button></td></tr>`);
    }
  }

  function importResult(r) {
    var c = r.counts || {};
    var probs = (r.problems || []).slice(0, 50);
    return h`<div class="wac-note ok">Import finished: ${num(c.rows)} rows read, ${num(c.valid)} accepted, ${num(c.invalid)} rejected, ${num(c.duplicates)} duplicates` +
      h`${raw(c.registered_merchants ? h`, ${num(c.registered_merchants)} are existing merchants` : '')}.</div>` +
      ((r.warnings || []).length ? h`<div class="wac-note">${(r.warnings || []).join(' · ')}</div>` : '') +
      (probs.length ? '<div class="table-wrap"><table class="wac-table" style="min-width:420px"><thead><tr><th>Row</th><th>Problem</th><th>Why</th></tr></thead><tbody>' +
        probs.map(function (x) { return h`<tr><td>${x.row}</td><td>${x.problem}</td><td>${reason(x.reason)}</td></tr>`; }).join('') + '</tbody></table></div>' +
        (r.problems_total > probs.length ? h`<div class="wac-hint">Showing the first ${probs.length} of ${r.problems_total} problems.</div>` : '') : '');
  }

  var TH = {
    'imp-manual': function (el) {
      var box = $('wac_imp_text'), lines = String(box.value || '').split(/\r?\n/).map(function (l) { return l.trim(); }).filter(Boolean);
      if (!lines.length) { msg($('wac_imp_msg'), 'Paste at least one number.', 'error'); return; }
      if (lines.length > 1000) { msg($('wac_imp_msg'), 'At most 1000 numbers per paste; use a CSV for more.', 'error'); return; }
      var entries = lines.map(function (l) { var i = l.search(/[,;\t]/); return i < 0 ? { phone: l } : { phone: l.slice(0, i).trim(), name: l.slice(i + 1).trim() }; });
      msg($('wac_imp_msg'), 'Importing...');
      K.busy(el, async function () {
        try {
          var r = await api('/audience/imports', { method: 'POST', json: { entries: entries } });
          box.value = ''; entries = null;                           // numbers are not kept in the browser after the request
          msg($('wac_imp_msg'), ''); setHtml($('wac_imp_result'), importResult(r)); loadImportRows();
          if (tab.picker) tab.picker.refreshImports();
        } catch (e) { msg($('wac_imp_msg'), e.message, 'error'); }
      });
    },
    'imp-csv': function (el) {
      var f = $('wac_imp_file').files[0];
      if (!f) { msg($('wac_imp_msg'), 'Choose a CSV file first.', 'error'); return; }
      if (f.size > MAX_CSV_BYTES) { msg($('wac_imp_msg'), 'The file is larger than 2 MB.', 'error'); return; }
      if (!/\.csv$/i.test(f.name) && f.type !== 'text/csv') { msg($('wac_imp_msg'), 'Only .csv files are accepted.', 'error'); return; }
      msg($('wac_imp_msg'), 'Uploading...');
      K.busy(el, async function () {
        try {
          var fd = new FormData(); fd.append('file', f);
          var r = await api('/audience/imports/csv', { method: 'POST', form: fd });
          $('wac_imp_file').value = '';
          msg($('wac_imp_msg'), ''); setHtml($('wac_imp_result'), importResult(r)); loadImportRows();
          if (tab.picker) tab.picker.refreshImports();
        } catch (e) { msg($('wac_imp_msg'), e.message, 'error'); }
      });
    },
    'imp-reload': function () { loadImportRows(); },
    'imp-view': function (el) {
      var id = el.getAttribute('data-id');
      setHtml($('wac_imp_detail'), '<div class="wac-panel"><i class="fas fa-spinner fa-spin"></i> Loading...</div>');
      api('/audience/imports/' + encodeURIComponent(id)).then(function (r) {
        setHtml($('wac_imp_detail'), '<div class="wac-panel"><div class="wac-panel-title">Import details <button type="button" class="btn-o" data-act="imp-close">Close</button></div>' + importResult(r) + '</div>');
      }).catch(function (e) { setHtml($('wac_imp_detail'), h`<div class="wac-note bad">${e.message}</div>`); });
    },
    'imp-close': function () { setHtml($('wac_imp_detail'), ''); },
    'imp-delete': async function (el) {
      var ok = await K.confirm({ title: 'Delete this imported list?', confirmLabel: 'Delete list', danger: true,
        bodyHtml: '<p>The stored list is removed. Campaigns that already took a recipient snapshot keep their snapshot; drafts that use this list will no longer find it.</p>' });
      if (!ok) return;
      K.busy(el, async function () {
        await api('/audience/imports/' + encodeURIComponent(el.getAttribute('data-id')), { method: 'DELETE' });
        K.toast('Imported list deleted.', 'ok'); loadImportRows();
        if (tab.picker) tab.picker.refreshImports();
      });
    }
  };

  function openTab() {
    drawTab();
    loadImportRows();
    tab.picker = mountPicker('wac_aud_tab_picker', null, false);
  }

  var all = {};
  Object.keys(H).forEach(function (k) { all[k] = H[k]; });
  Object.keys(TH).forEach(function (k) { all[k] = TH[k]; });
  W.register('audience', { open: openTab, handlers: all });
  W.audience = { mountPicker: mountPicker };
})();
