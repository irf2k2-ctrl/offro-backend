/* WhatsApp Campaigns (Phase 2G) - Templates screen: Meta sync, list, variable mapping, preview, enable / disable for campaigns.
 * Depends on wa_campaigns.js. Mapping is stored per template (not per campaign) by the existing campaign-use endpoint; the server validates
 * the mapping and decides whether a template may be enabled (APPROVED, MARKETING, supported components). Nothing here sends a message. */
(function () {
  'use strict';
  var W = window.WaCampaigns;
  if (!W) return;
  var K = W.kit, h = K.h, raw = K.raw, setHtml = K.setHtml, $ = K.$, api = K.api, num = K.num;

  var READY = { missing_from_meta: 'No longer exists at Meta', status_not_approved: 'Not approved by Meta', category_not_allowed: 'Not a marketing template',
                unsupported_components: 'Uses components campaigns do not support', not_configured: 'Variable mapping not saved yet', mapping_incomplete: 'Some variables are not mapped',
                mapping_stale: 'Template changed at Meta; re-save the mapping', not_enabled: 'Not enabled for campaigns' };
  var SOURCE_LABEL = { static: 'Fixed text', merchant_name: 'Merchant name', store_name: 'Store name', store_city: 'Store city', store_area: 'Store area', recipient_name: 'Name from the imported list' };
  var st = { page: 1, pages: 1, total: 0, f: { q: '', status: '', category: '', language: '', campaign: '' }, facets: null, seq: 0, cur: null, curSeq: 0, syncing: false };

  function statusPill(s) {
    var cls = s === 'APPROVED' ? 'ok' : (s === 'PENDING' || s === 'IN_APPEAL' ? 'warn' : 'bad');
    return h`<span class="wac-pill ${cls}">${String(s || '—').replace(/_/g, ' ').toLowerCase()}</span>`;
  }
  function opt(list, cur, label) {
    return h`<option value="">${label}</option>` + (list || []).map(function (v) { return h`<option value="${v}"${raw(v === cur ? ' selected' : '')}>${v}</option>`; }).join('');
  }

  function drawShell() {
    setHtml($('wac_pane_templates'),
      '<div class="wac-note">Templates are created and approved in Meta Business Manager. This screen only reads them, stores a variable mapping and lets you enable approved marketing templates for campaigns.</div>' +
      '<div class="wac-toolbar"><input type="search" id="wac_t_q" class="form-control grow" placeholder="Search template name" autocomplete="off" data-input="tpl-filter" data-key="q">' +
      '<select id="wac_t_status" class="form-select" data-change="tpl-filter" data-key="status"></select><select id="wac_t_cat" class="form-select" data-change="tpl-filter" data-key="category"></select>' +
      '<select id="wac_t_lang" class="form-select" data-change="tpl-filter" data-key="language"></select>' +
      '<select id="wac_t_use" class="form-select" data-change="tpl-filter" data-key="campaign"><option value="">All templates</option><option value="usable">Ready for campaigns</option><option value="enabled">Enabled</option><option value="not_ready">Not ready</option></select>' +
      (K.can('edit') ? '<button type="button" class="btn-p" data-act="tpl-sync" id="wac_t_sync"><i class="fas fa-sync me-1"></i> Sync from Meta</button>' : '') + '</div>' +
      '<div id="wac_t_msg" class="wac-msg" role="status"></div><div class="table-wrap"><table class="wac-table" style="min-width:820px"><thead><tr><th>Template</th><th>Language</th><th>Category</th><th>Meta status</th><th>Variables</th><th>Header media</th><th>For campaigns</th></tr></thead>' +
      '<tbody id="wac_t_rows"></tbody></table></div><div class="wac-pager"><span id="wac_t_count" class="wac-muted"></span><div><button type="button" class="btn-o" data-act="tpl-prev">&larr; Prev</button> <button type="button" class="btn-o" data-act="tpl-next">Next &rarr;</button></div></div>' +
      '<div id="wac_t_detail" style="margin-top:14px"></div>');
  }

  function applyFacets() {
    var f = st.facets || {};
    setHtml($('wac_t_status'), opt(f.statuses, st.f.status, 'Any Meta status'));
    setHtml($('wac_t_cat'), opt(f.categories, st.f.category, 'Any category'));
    setHtml($('wac_t_lang'), opt(f.languages, st.f.language, 'Any language'));
  }

  async function load() {
    var seq = ++st.seq, tb = $('wac_t_rows');
    setHtml(tb, '<tr><td colspan="7" class="wac-cell-state"><i class="fas fa-spinner fa-spin"></i> Loading templates...</td></tr>');
    var q = '/templates?page=' + st.page + '&page_size=25' + Object.keys(st.f).map(function (k) { return st.f[k] ? '&' + k + '=' + encodeURIComponent(st.f[k]) : ''; }).join('');
    try {
      var d = await api(q);
      if (seq !== st.seq) return;
      st.page = d.page; st.pages = d.pages; st.total = d.total;
      if (!st.facets) { st.facets = d.facets; applyFacets(); }
      setHtml(tb, (d.items || []).length ? d.items.map(function (t) {
        var use = t.campaign_enabled ? (t.usable_for_campaign ? '<span class="wac-pill ok">Enabled</span>' : '<span class="wac-pill warn">Enabled, needs attention</span>')
                : (t.usable_for_campaign ? '<span class="wac-pill">Ready</span>' : '<span class="wac-pill bad">Not ready</span>');
        return h`<tr><td><button type="button" class="wac-link wac-name" data-act="tpl-open" data-name="${t.name}" data-lang="${t.language}">${t.name}</button></td><td>${t.language}</td><td>${t.category}</td>` +
               h`<td>${raw(statusPill(t.status))}${raw(t.missing_from_meta ? ' <span class="wac-pill bad">removed at Meta</span>' : '')}</td><td>${num(t.variable_count)}</td><td>${t.requires_header_media ? 'Required' : '—'}</td><td>${raw(use)}</td></tr>`;
      }).join('') : '<tr><td colspan="7" class="wac-empty">No templates found. Use "Sync from Meta" to fetch them.</td></tr>');
      $('wac_t_count').textContent = st.total ? 'Page ' + st.page + ' of ' + st.pages + ' · ' + st.total + ' templates' : '';
    } catch (e) {
      if (seq !== st.seq) return;
      if (e.status === 403) { K.showDenied(e.message); return; }
      setHtml(tb, h`<tr><td colspan="7" class="wac-cell-state is-error">${e.message} <button type="button" class="btn-o" data-act="tpl-retry">Retry</button></td></tr>`);
    }
  }

  // ── detail ────────────────────────────────────────────────────────────────────────────────
  function mappingRow(v, cu, sources, ro) {
    var m = ((cu || {}).variables || {})[v.key] || {}, samples = (cu || {}).sample_values || {};
    var srcOpts = Object.keys(sources).map(function (k) { return h`<option value="${k}"${raw(m.source === k ? ' selected' : '')}>${SOURCE_LABEL[k] || k}</option>`; }).join('');
    var isStatic = (m.source || 'static') === 'static';
    var dis = ro ? ' disabled' : '';
    return h`<tr data-var="${v.key}"><td><code>${v.key.replace('.', ' · ')}</code><div class="wac-hint">Meta example: ${v.example || '—'}</div></td>` +
      h`<td><select class="form-select wac-map-src" data-change="tpl-src"${raw(dis)}><option value="">Choose...</option>${raw(srcOpts)}</select></td>` +
      h`<td><input type="text" class="form-control wac-map-val" maxlength="200" value="${isStatic ? (m.value || '') : (m.fallback || '')}" placeholder="${isStatic ? 'Text to send' : 'Fallback when empty'}" autocomplete="off"${raw(dis)}></td>` +
      h`<td><input type="text" class="form-control wac-map-sample" maxlength="200" value="${samples[v.key] || ''}" placeholder="Sample for preview" autocomplete="off"${raw(dis)}></td></tr>`;
  }

  function collect() {
    var variables = {}, samples = {};
    document.querySelectorAll('#wac_t_detail tr[data-var]').forEach(function (tr) {
      var key = tr.getAttribute('data-var'), src = tr.querySelector('.wac-map-src').value, val = tr.querySelector('.wac-map-val').value;
      var smp = tr.querySelector('.wac-map-sample').value;
      if (src) variables[key] = src === 'static' ? { source: 'static', value: val } : { source: src, fallback: val };
      if (smp) samples[key] = smp;
    });
    return { variables: variables, sample_values: samples };
  }

  function drawDetail(t) {
    var p = t.parsed || {}, cu = t.campaign_use || {}, vars = p.variables || [], sources = t.variable_sources || {};
    var canEdit = K.can('edit');
    var reasons = (t.readiness || []).filter(function (r) { return r !== 'not_enabled'; });
    var body = '<div class="wac-panel"><div class="wac-panel-title">' + h`${t.name} <span class="wac-muted">${t.language}</span> ${raw(statusPill(t.status))}` +
      '<button type="button" class="btn-o" data-act="tpl-close">Close</button></div>' +
      h`<dl class="wac-kv"><dt>Category</dt><dd>${t.category}</dd><dt>Last synced</dt><dd>${K.fmtDateTime(t.synced_at)} IST</dd>` +
      h`<dt>Header</dt><dd>${p.header ? (p.header.format + (p.header.requires_media ? ' — media is required: each campaign must attach one' : '')) : 'None'}</dd>` +
      h`<dt>Campaign use</dt><dd>${cu.enabled ? 'Enabled' : 'Not enabled'}${cu.stale ? ' (mapping is stale)' : ''}</dd></dl>` +
      (reasons.length ? '<div class="wac-note" style="margin-top:10px">Not ready for campaigns: ' + reasons.map(function (r) { return h`${READY[r] || r}`; }).join(' · ') + '</div>' : '') +
      (t.rejected_reason ? h`<div class="wac-note bad">Meta reason: ${t.rejected_reason}</div>` : '') +
      ((t.unsupported || []).length ? h`<div class="wac-note bad">Unsupported components: ${t.unsupported.join(', ')}</div>` : '') + '</div>' +
      '<div class="wac-panel"><div class="wac-panel-title">Template text</div>' + (p.header && p.header.text ? h`<div class="wac-pre">Header: ${p.header.text}</div>` : '') +
      h`<div class="wac-pre" style="margin-top:6px">${(p.body && p.body.text) || ''}</div>` + (p.footer ? h`<div class="wac-hint">Footer: ${p.footer.text || p.footer}</div>` : '') + '</div>' +
      '<div class="wac-panel"><div class="wac-panel-title">Variable mapping</div>' + (vars.length ? '<div class="table-wrap"><table class="wac-table" style="min-width:640px"><thead><tr><th>Variable</th><th>Filled with</th><th>Text / fallback</th><th>Preview sample</th></tr></thead><tbody>' +
        vars.map(function (v) { return mappingRow(v, cu, sources, !canEdit); }).join('') + '</tbody></table></div>' : '<div class="wac-hint">This template has no variables.</div>') +
      '<div id="wac_t_dmsg" class="wac-msg" role="status" style="margin-top:8px"></div><div class="wac-actions">' +
      '<button type="button" class="btn-o" data-act="tpl-preview">Preview with sample values</button>' +
      (canEdit ? '<button type="button" class="btn-p" data-act="tpl-save">Save mapping</button>' : '') +
      (K.can('approve') && !cu.enabled ? '<button type="button" class="btn-p" data-act="tpl-enable">Enable for campaigns</button>' : '') +
      (canEdit && cu.enabled ? '<button type="button" class="btn-d" data-act="tpl-disable">Disable for campaigns</button>' : '') + '</div></div>' +
      '<div id="wac_t_preview"></div>';
    setHtml($('wac_t_detail'), body);
  }

  async function openDetail(name, lang) {
    var seq = ++st.curSeq;
    setHtml($('wac_t_detail'), '<div class="wac-panel"><i class="fas fa-spinner fa-spin"></i> Loading template...</div>');
    try {
      var t = await api('/templates/' + encodeURIComponent(name) + '/' + encodeURIComponent(lang));
      if (seq !== st.curSeq) return;
      st.cur = t; drawDetail(t);
      $('wac_t_detail').scrollIntoView && $('wac_t_detail').scrollIntoView({ block: 'nearest' });
    } catch (e) { if (seq === st.curSeq) setHtml($('wac_t_detail'), h`<div class="wac-note bad">${e.message}</div>`); }
  }

  function path(t) { return '/templates/' + encodeURIComponent(t.name) + '/' + encodeURIComponent(t.language); }

  function previewHtml(r) {
    var pv = r.preview || {};
    return '<div class="wac-panel"><div class="wac-panel-title">Preview <span class="wac-muted">nothing is sent</span></div><div class="wac-bubble">' +
      (pv.header ? h`<b>${pv.header.text || pv.header}</b>\n` : '') + h`${pv.body || ''}` + (pv.footer ? h`<span class="f">${pv.footer}</span>` : '') + '</div>' +
      ((r.missing_variables || []).length ? h`<div class="wac-note" style="margin-top:8px">Missing values for: ${r.missing_variables.join(', ')}</div>` : '') +
      (Object.keys(r.invalid_values || {}).length ? h`<div class="wac-note bad" style="margin-top:8px">Invalid values: ${Object.keys(r.invalid_values).join(', ')}</div>` : '') + '</div>';
  }

  function saveBody(enabled) { var c = collect(); return { enabled: enabled, variables: c.variables, sample_values: c.sample_values }; }

  var H = {
    'tpl-filter': function (el) {
      st.f[el.getAttribute('data-key')] = el.value; st.page = 1;
      clearTimeout(H._d); H._d = setTimeout(load, el.tagName === 'INPUT' ? 350 : 0);
    },
    'tpl-prev': function () { if (st.page > 1) { st.page -= 1; load(); } },
    'tpl-next': function () { if (st.page < st.pages) { st.page += 1; load(); } },
    'tpl-retry': function () { load(); },
    'tpl-open': function (el) { openDetail(el.getAttribute('data-name'), el.getAttribute('data-lang')); },
    'tpl-close': function () { setHtml($('wac_t_detail'), ''); st.cur = null; },
    'tpl-src': function (el) {
      var tr = el.closest('tr'), inp = tr && tr.querySelector('.wac-map-val');
      if (inp) inp.placeholder = el.value === 'static' ? 'Text to send' : 'Fallback when empty';
    },
    'tpl-sync': function (el) {
      K.busy(el, async function () {
        K.$('wac_t_msg').textContent = 'Syncing with Meta...';
        try {
          var r = await api('/templates/sync', { method: 'POST', json: {} });
          var c = r.counts || {}, ch = (r.status_changes || []).length;
          K.$('wac_t_msg').textContent = '';
          K.toast('Synced: ' + Object.keys(c).map(function (k) { return k + ' ' + c[k]; }).join(', ') + (ch ? '; ' + ch + ' status change' + (ch === 1 ? '' : 's') : '') + '.', 'ok');
          st.facets = null; load();
        } catch (e) { K.$('wac_t_msg').textContent = e.message; K.$('wac_t_msg').className = 'wac-msg is-error'; }
      });
    },
    'tpl-preview': function (el) {
      var t = st.cur; if (!t) return;
      var values = collect().sample_values;
      K.busy(el, async function () {
        try { setHtml($('wac_t_preview'), previewHtml(await api(path(t) + '/preview', { method: 'POST', json: { values: values, use_samples: true } }))); }
        catch (e) { setHtml($('wac_t_preview'), h`<div class="wac-note bad">${e.message}</div>`); }
      });
    },
    'tpl-save': function (el) {
      var t = st.cur; if (!t) return;
      K.busy(el, async function () {
        try { await api(path(t) + '/campaign-use', { method: 'PUT', json: saveBody(!!(t.campaign_use || {}).enabled) }); K.toast('Mapping saved.', 'ok'); openDetail(t.name, t.language); load(); }
        catch (e) { var m = $('wac_t_dmsg'); m.textContent = describe(e); m.className = 'wac-msg is-error'; }
      });
    },
    'tpl-enable': async function (el) {
      var t = st.cur; if (!t) return;
      var ok = await K.confirm({ title: 'Enable this template for campaigns?', confirmLabel: 'Enable template',
        bodyHtml: h`<p><b>${t.name}</b> (${t.language}) will become selectable in the campaign editor with the mapping shown here. Enabling sends nothing.</p>` });
      if (!ok) return;
      K.busy(el, async function () {
        try { await api(path(t) + '/campaign-use', { method: 'PUT', json: saveBody(true) }); K.toast('Template enabled for campaigns.', 'ok'); openDetail(t.name, t.language); load(); }
        catch (e) { var m = $('wac_t_dmsg'); m.textContent = describe(e); m.className = 'wac-msg is-error'; }
      });
    },
    'tpl-disable': async function (el) {
      var t = st.cur; if (!t) return;
      var ok = await K.confirm({ title: 'Disable this template for campaigns?', confirmLabel: 'Disable template', danger: true,
        bodyHtml: '<p>It will no longer be selectable for new campaigns. Campaigns already using it are not changed, but they cannot be validated or scheduled while it is disabled.</p>' });
      if (!ok) return;
      K.busy(el, async function () {
        try { await api(path(t) + '/campaign-use', { method: 'PUT', json: saveBody(false) }); K.toast('Template disabled.', 'ok'); openDetail(t.name, t.language); load(); }
        catch (e) { var m = $('wac_t_dmsg'); m.textContent = describe(e); m.className = 'wac-msg is-error'; }
      });
    }
  };

  function describe(e) {
    var d = e && e.detail;
    if (d && typeof d === 'object' && !Array.isArray(d)) {
      if (Array.isArray(d.reasons) && d.reasons.length) return 'Cannot enable: ' + d.reasons.map(function (r) { return READY[r] || r; }).join('; ') + '.';
      if (Array.isArray(d.mapping_problems) && d.mapping_problems.length) return 'Fix the variable mapping first (' + d.mapping_problems.length + ' problem' + (d.mapping_problems.length === 1 ? '' : 's') + ').';
      if (d.invalid) return 'Some sample values are not valid: ' + Object.keys(d.invalid).join(', ') + '.';
    }
    return e.message;
  }

  function openTab() { drawShell(); st.facets = null; st.cur = null; load(); }
  W.register('templates', { open: openTab, handlers: H });
})();
