/* Admin -> Cities - admin dashboard module.
 *
 * A thin client for /admin/cities. The server is the single source of truth for permissions, validation, duplicates and Popular
 * ordering; the browser shows what the API returns and hides controls the caller cannot use. One City Master serves every country.
 * Nothing here runs an import or a migration by itself: Import and Verify are explicit buttons, and an import is always previewed first.
 *
 * Output safety: every HTML string is built with the h`...` tag, which escapes every interpolated value unless wrapped in raw()
 * (only used for markup produced by this file). innerHTML is assigned only through setHtml(). Server values are never evaluated.
 */
(function () {
  'use strict';
  var API = '/admin/cities';

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

  var st = {
    country: '', countries: [], unassigned: 0, rows: [], q: '', status: 'all', popOnly: false, noAr: false,
    draft: null, msg: null, loaded: false, loading: false, err: null, seq: 0, busy: false, modal: null, imp: null
  };

  // ── permissions (display only; the server enforces them) ───────────────────────────────────────────
  function can(action) {
    var me = window._rbacMe;
    if (!me) return true;
    if (me.role === 'Super Admin') return true;
    var p = (me.permissions || {}).Cities;
    return !!(p && p[action]);
  }

  // ── API ────────────────────────────────────────────────────────────────────────────────────────────
  function errText(data, status) {
    var d = data && data.detail;
    if (d && typeof d === 'object' && !Array.isArray(d) && d.message) return d.message;
    if (typeof d === 'string') return d;
    if (Array.isArray(d) && d.length && d[0].msg) return d[0].msg;
    return 'Request failed (' + status + ')';
  }
  function api(method, path, body, form) {
    var opt = { method: method, credentials: 'include', headers: {} };
    if (form) { opt.body = form; } else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    return fetch(API + path, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) { return { ok: r.ok, status: r.status, data: data }; });
    }).catch(function () { return { ok: false, status: 0, data: { detail: 'Could not reach the server. Check your connection and try again.' } }; });
  }
  function say(kind, text) { st.msg = text ? { kind: kind, text: text } : null; renderMsg(); }
  function renderMsg() {
    var cls = st.msg ? (st.msg.kind === 'ok' ? 'ok' : st.msg.kind === 'note' ? 'note' : 'err') : '';
    setHtml($('ci_msg'), st.msg ? h`<div class="ci-alert ${cls}" role="${st.msg.kind === 'err' ? 'alert' : 'status'}">${st.msg.text}</div>` : '');
  }

  // ── loading ────────────────────────────────────────────────────────────────────────────────────────
  function loadSummary() {
    return api('GET', '/summary').then(function (r) {
      if (!r.ok) { st.err = errText(r.data, r.status); return false; }
      st.countries = r.data.countries || [];
      st.unassigned = r.data.unassigned || 0;
      var valid = st.country === 'unassigned' ? st.unassigned > 0 : st.countries.some(function (c) { return c.code === st.country; });
      if (!valid) {
        var first = st.countries.filter(function (c) { return c.total > 0; })[0] || st.countries[0];
        st.country = first ? first.code : (st.unassigned ? 'unassigned' : '');
      }
      return true;
    });
  }
  function loadRows() {
    if (!st.country) { st.rows = []; return Promise.resolve(true); }
    var seq = ++st.seq;
    return api('GET', '?country=' + encodeURIComponent(st.country)).then(function (r) {
      if (seq !== st.seq) return false;
      if (!r.ok) { st.err = errText(r.data, r.status); return false; }
      st.rows = r.data || [];
      st.draft = null;
      return true;
    });
  }
  function reload() {
    st.err = null;
    return loadSummary().then(function (ok) { return ok ? loadRows() : false; }).then(function () { render(); });
  }
  function open() {
    var denied = !can('view');
    var d = $('ci_denied'), b = $('ci_body');
    if (d) d.hidden = !denied;
    if (b) b.hidden = denied;
    if (denied) return;
    st.msg = null;
    setHtml($('ci_pane'), '<div class="ci-empty">Loading cities…</div>');
    reload();
  }

  // ── helpers ────────────────────────────────────────────────────────────────────────────────────────
  function cname(code) {
    if (code === 'unassigned') return 'Unassigned';
    for (var i = 0; i < st.countries.length; i++) if (st.countries[i].code === code) return st.countries[i].name;
    return code;
  }
  function arOf(r) { return (r.names && r.names.ar) || ''; }
  function popular() {
    var list = st.rows.filter(function (r) { return r.is_popular; }).sort(function (a, b) { return (a.popular_order || 1e6) - (b.popular_order || 1e6); });
    if (st.draft) {
      var by = {}; list.forEach(function (r) { by[r.id] = r; });
      return st.draft.map(function (id) { return by[id]; }).filter(Boolean);
    }
    return list;
  }
  function filtered() {
    var q = st.q.trim().toLowerCase();
    return st.rows.filter(function (r) {
      if (st.status !== 'all' && ((r.status === 'active') !== (st.status === 'active'))) return false;
      if (st.popOnly && !r.is_popular) return false;
      if (st.noAr && arOf(r)) return false;
      if (q && (r.name + ' ' + arOf(r) + ' ' + r.state).toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
  }

  // ── main render ────────────────────────────────────────────────────────────────────────────────────
  function render() {
    var tabs = st.countries.map(function (c) {
      return h`<button class="ci-tab${raw(c.code === st.country ? ' active' : '')}" data-ci-tab="${c.code}" role="tab">${c.name} (${c.code})<small>${c.active}/${c.total}</small></button>`;
    });
    if (st.unassigned) tabs.push(h`<button class="ci-tab${raw(st.country === 'unassigned' ? ' active' : '')}" data-ci-tab="unassigned" role="tab">Unassigned<small>${st.unassigned}</small></button>`);
    setHtml($('ci_tabs'), tabs.join(''));
    renderMsg();
    if (st.err) { setHtml($('ci_pane'), h`<div class="ci-alert err">${st.err} <button class="ci-btn sm" data-ci="retry">Try again</button></div>`); return; }
    var unas = st.country === 'unassigned';
    var rows = filtered(), pops = popular();
    var cur = null; st.countries.forEach(function (c) { if (c.code === st.country) cur = c; });
    var out = '';
    out += h`<div class="ci-card"><div class="ci-row"><input type="text" id="ci_q" placeholder="Search English, Arabic or state" value="${st.q}" style="width:240px" aria-label="Search cities">`;
    [['all', 'All'], ['active', 'Active'], ['inactive', 'Inactive']].forEach(function (s) {
      out += h`<button class="ci-chip${raw(st.status === s[0] ? ' on' : '')}" data-ci-status="${s[0]}">${s[1]}</button>`;
    });
    out += h`<button class="ci-chip${raw(st.popOnly ? ' on' : '')}" data-ci="poponly">★ Popular only</button><button class="ci-chip${raw(st.noAr ? ' on' : '')}" data-ci="noar">Missing Arabic name</button><span class="ci-grow"></span>`;
    if (can('add')) out += '<button class="ci-btn" data-ci="import">Import cities</button>';
    out += '<button class="ci-btn" data-ci="verify">Verify</button>';
    if (can('add')) out += '<button class="ci-btn pri" data-ci="add">+ Add city</button>';
    out += '</div>';
    var total = st.rows.length, act = st.rows.filter(function (r) { return r.status === 'active'; }).length;
    out += h`<div class="ci-counts"><div><b>${total}</b>cities</div><div><b>${act}</b>active</div><div><b>${st.rows.filter(function (r) { return r.is_popular; }).length}</b>popular</div><div><b>${st.rows.filter(function (r) { return !arOf(r); }).length}</b>without Arabic name</div></div></div>`;

    if (!unas) {
      if (st.country === 'IN' && !pops.length) {
        out += '<div class="ci-alert note"><b>Legacy Popular rule is active for India.</b> No Indian city is flagged, so the app still shows the first six server cities, topped up from its built-in metro list. Flag a city (★) and the flags take over.</div>';
      }
      out += h`<div class="ci-card"><div class="ci-row"><span class="ci-title">Popular cities · ${cname(st.country)}</span><span class="ci-grow"></span><span class="ci-help">The app shows the first 6, in this order.</span></div><div style="margin-top:8px">`;
      if (!pops.length) out += h`<div class="ci-help">${st.country === 'IN' ? 'None flagged (legacy rule in use).' : 'None flagged. The app shows All Cities only, with no Popular heading.'}</div>`;
      pops.forEach(function (r, i) {
        out += h`<div class="ci-pop"><span class="no">${i + 1}</span><span class="nm">${r.name}${raw(arOf(r) ? ' · <span class="ci-ar">' + esc(arOf(r)) + '</span>' : '')}${raw(i >= 6 ? ' <span class="pill w">not shown in app</span>' : '')}</span>`;
        if (can('edit')) out += h`<button class="ci-btn sm" data-ci-up="${r.id}"${raw(i === 0 ? ' disabled' : '')}>↑</button><button class="ci-btn sm" data-ci-dn="${r.id}"${raw(i === pops.length - 1 ? ' disabled' : '')}>↓</button><button class="ci-btn sm" data-ci-rm="${r.id}">Remove</button>`;
        out += '</div>';
      });
      out += '</div>';
      if (can('edit') && pops.length) out += h`<div class="ci-row" style="margin-top:6px"><button class="ci-btn pri" data-ci="saveorder"${raw(st.draft ? '' : ' disabled')}>Save order</button><span class="ci-help">${st.draft ? 'Unsaved changes.' : 'Saves all positions in one step.'}</span></div>`;
      out += '</div>';
    } else {
      out += '<div class="ci-alert note">These cities have no country, so the app does not offer them to a country-specific request. Edit a city to give it a country, or use Verify to assign them all after a preview.</div>';
    }

    if (!st.rows.length) {
      out += h`<div class="ci-card ci-empty"><div class="ci-title">No cities yet for ${cname(st.country)}</div><p class="ci-help" style="margin:6px 0 12px">The app shows “No cities available for your country yet” until cities are added.</p>${raw(can('add') && !unas ? '<button class="ci-btn" data-ci="import">Import cities</button> <button class="ci-btn pri" data-ci="add">+ Add city</button>' : '')}</div>`;
    } else {
      out += '<div class="ci-tw"><table class="ci-table"><thead><tr><th></th><th>City (English)</th><th>Arabic</th><th>State / Province</th><th>Popular</th><th>Sort</th><th>Status</th><th></th></tr></thead><tbody>';
      rows.forEach(function (r) {
        var img = r.has_image ? h`<img class="ci-thumb" src="${r.image_url}" alt="" loading="lazy">` : '<div class="ci-noimg">none</div>';
        out += h`<tr><td>${raw(img)}</td><td><b>${r.name}</b></td><td>${raw(arOf(r) ? '<span class="ci-ar">' + esc(arOf(r)) + '</span>' : '<span class="ci-fb">uses English</span>')}</td><td>${r.state}</td>`;
        out += h`<td><button class="ci-star${raw(r.is_popular ? ' on' : '')}" data-ci-star="${r.id}" aria-label="Toggle popular for ${r.name}"${raw(can('edit') && !unas ? '' : ' disabled')}>★</button>${raw(r.is_popular ? '<span class="pill a">#' + esc(r.popular_order) + '</span>' : '')}</td><td>${r.sort_order}</td>`;
        out += h`<td><div class="ci-row" style="flex-wrap:nowrap"><button class="ci-sw${raw(r.status === 'active' ? ' on' : '')}" data-ci-act="${r.id}" aria-label="Toggle active for ${r.name}"${raw(can('edit') ? '' : ' disabled')}></button><span class="pill ${raw(r.status === 'active' ? 'a' : 'i')}">${r.status === 'active' ? 'Active' : 'Inactive'}</span></div></td>`;
        out += h`<td>${raw(can('edit') ? h`<button class="ci-btn sm" data-ci-edit="${r.id}">Edit</button>` : '')}</td></tr>`;
      });
      if (!rows.length) out += '<tr><td colspan="8" class="ci-empty">No city matches these filters.</td></tr>';
      out += '</tbody></table></div>';
    }
    var keep = document.activeElement && document.activeElement.id === 'ci_q';
    setHtml($('ci_pane'), out);
    if (keep && $('ci_q')) { var q = $('ci_q'); q.focus(); q.setSelectionRange(q.value.length, q.value.length); }
  }

  // ── actions ────────────────────────────────────────────────────────────────────────────────────────
  function byId(id) { for (var i = 0; i < st.rows.length; i++) if (st.rows[i].id === id) return st.rows[i]; return null; }
  function act(p) {
    if (st.busy) return Promise.resolve();
    st.busy = true;
    return p.then(function (r) { st.busy = false; return r; }, function (e) { st.busy = false; throw e; });
  }
  function toggleStar(id) {
    var r = byId(id); if (!r) return;
    act(api('PUT', '/' + id, { is_popular: !r.is_popular })).then(function (res) {
      if (!res) return;
      if (!res.ok) { say('err', errText(res.data, res.status)); return; }
      say('ok', r.name + (r.is_popular ? ' removed from Popular cities.' : ' added to Popular cities (end of the list).'));
      reload();
    });
  }
  function toggleActive(id) {
    var r = byId(id); if (!r) return;
    act(api('POST', '/' + id + '/status', { active: r.status !== 'active' })).then(function (res) {
      if (!res) return;
      if (!res.ok) { say('err', errText(res.data, res.status)); return; }
      say('ok', r.name + (r.status === 'active' ? ' deactivated. Its stores stay, but customers can no longer pick it.' : ' activated.'));
      reload();
    });
  }
  function moveDraft(id, delta) {
    var ids = popular().map(function (r) { return r.id; });
    var i = ids.indexOf(id), j = i + delta;
    if (i < 0 || j < 0 || j >= ids.length) return;
    var t = ids[i]; ids[i] = ids[j]; ids[j] = t;
    st.draft = ids; render();
  }
  function removeDraft(id) {
    st.draft = popular().map(function (r) { return r.id; }).filter(function (x) { return x !== id; });
    render();
  }
  function saveOrder() {
    var ids = popular().map(function (r) { return r.id; });
    act(api('PUT', '/popular-order', { country: st.country, ids: ids })).then(function (res) {
      if (!res) return;
      if (!res.ok) { say('err', errText(res.data, res.status)); return; }
      say('ok', 'Popular order saved (' + res.data.popular + ' cities).');
      reload();
    });
  }

  // ── add / edit dialog ──────────────────────────────────────────────────────────────────────────────
  function closeModal() { st.modal = null; st.imp = null; var m = $('ci_modal'); if (m) { m.hidden = true; setHtml(m, ''); } }
  function showModal(html, wide) {
    var m = $('ci_modal');
    setHtml(m, h`<div class="ci-modal${raw(wide ? ' wide' : '')}" role="dialog" aria-modal="true">${raw(html)}</div>`);
    m.hidden = false;
    var f = m.querySelector('input,select,button.ci-btn'); if (f) f.focus();
  }
  function countryOptions(sel, withBlank) {
    var o = withBlank ? '<option value="">(no country)</option>' : '';
    st.countries.forEach(function (c) { o += h`<option value="${c.code}"${raw(c.code === sel ? ' selected' : '')}>${c.name} (${c.code})</option>`; });
    return o;
  }
  function openForm(id) {
    var r = id ? byId(id) : null;
    st.modal = { id: id || '', confirmDelete: false };
    var sel = r ? r.country : (st.country && st.country !== 'unassigned' ? st.country : (st.countries[0] || {}).code);
    var inUse = r && r.status !== undefined;
    var img = r && r.has_image ? h`<img class="big" src="${r.image_url}" alt="">` : '<div class="ci-noimg" style="width:88px;height:66px">No image</div>';
    var body = h`<div class="ci-form">
      <div><label class="ci-lbl" for="cf_country">Country *</label><select id="cf_country">${raw(countryOptions(sel, !!r && !r.country))}</select><div class="ci-help">${r ? 'Changing the country moves the city to another country’s list.' : 'Required for a new city.'}</div></div>
      <div><label class="ci-lbl" for="cf_state">State / Province</label><input type="text" id="cf_state" maxlength="60" value="${r ? r.state : ''}"><div class="ci-help">Shown under the city name in the app.</div></div>
      <div><label class="ci-lbl" for="cf_name">City name (English) *</label><input type="text" id="cf_name" maxlength="80" value="${r ? r.name : ''}"><div class="ci-help">Canonical name. Stores and accounts keep this value.</div></div>
      <div><label class="ci-lbl" for="cf_ar">City name (Arabic)</label><input type="text" id="cf_ar" class="ci-ar" dir="rtl" maxlength="80" value="${r ? arOf(r) : ''}"><div class="ci-help">Optional. Empty means the app shows the English name.</div></div>
      <div><label class="ci-lbl" for="cf_sort">Sort order</label><input type="number" id="cf_sort" min="0" step="1" value="${r ? r.sort_order : 0}"><div class="ci-help">Lower first. Tie-break for lists.</div></div>
      <div><label class="ci-lbl" for="cf_active">Status</label><select id="cf_active"><option value="1"${raw(!r || r.status === 'active' ? ' selected' : '')}>Active</option><option value="0"${raw(r && r.status !== 'active' ? ' selected' : '')}>Inactive</option></select></div>
      <div class="full ci-row" style="gap:20px"><label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="cf_pop"${raw(r && r.is_popular ? ' checked' : '')}> Popular city</label>
        <div><label class="ci-lbl" for="cf_pord">Popular order</label><input type="number" id="cf_pord" min="1" step="1" style="width:90px" value="${r && r.is_popular ? r.popular_order : ''}"></div><span class="ci-help">1 is first. Leave empty to add at the end. A taken position moves the others down.</span></div>
      <div class="full"><label class="ci-lbl">City image</label><div class="ci-img">${raw(img)}<div><input type="file" id="cf_file" accept="image/*"><div class="ci-help">Optional. Max 2 MB. Saving the form without choosing a file never changes the current image.</div>${raw(r && r.has_image ? '<label style="display:flex;gap:6px;align-items:center;margin-top:6px;font-size:12px"><input type="checkbox" id="cf_rmimg"> Remove current image</label>' : '')}</div></div></div>
    </div><div id="cf_err" class="ci-alert err" style="margin:12px 0 0" hidden></div>`;
    var foot = '';
    if (r && can('delete')) foot += '<button class="ci-btn danger" data-ci="delete" style="margin-right:auto">Delete…</button>';
    foot += '<button class="ci-btn" data-ci="close">Cancel</button><button class="ci-btn pri" data-ci="saveform">' + (r ? 'Save changes' : 'Add city') + '</button>';
    showModal(h`<div class="ci-mh"><span>${r ? 'Edit city · ' + r.name : 'Add city'}</span><button data-ci="close" aria-label="Close">×</button></div><div class="ci-mb">${raw(body)}</div><div class="ci-mf">${raw(foot)}</div>`);
  }
  function formErr(text) { var e = $('cf_err'); if (e) { e.textContent = text; e.hidden = !text; } }
  function saveForm() {
    var id = st.modal && st.modal.id;
    var file = $('cf_file') && $('cf_file').files && $('cf_file').files[0];
    if (file && file.size > 2 * 1024 * 1024) { formErr('The image is larger than 2 MB. Choose a smaller file.'); return; }
    var pop = $('cf_pop').checked, pord = $('cf_pord').value.trim();
    var body = { country: $('cf_country').value, name: $('cf_name').value, name_ar: $('cf_ar').value, state: $('cf_state').value,
                 sort_order: $('cf_sort').value === '' ? 0 : Number($('cf_sort').value), active: $('cf_active').value === '1', is_popular: pop };
    if (pop && pord !== '') body.popular_order = Number(pord);
    if (id && $('cf_rmimg') && $('cf_rmimg').checked) body.image_url = '';
    if (id && !body.country) delete body.country;
    formErr('');
    act(api(id ? 'PUT' : 'POST', id ? '/' + id : '', body)).then(function (res) {
      if (!res) return;
      if (!res.ok) { formErr(errText(res.data, res.status)); return; }
      var cid = id || res.data.id;
      var after = function () { closeModal(); say('ok', (id ? 'Saved ' : 'Added ') + body.name + '.'); if (body.country) st.country = body.country; reload(); };
      if (!file) { after(); return; }
      var fd = new FormData(); fd.append('file', file);
      api('POST', '/' + cid + '/upload-image', undefined, fd).then(function (u) {
        if (!u.ok) { closeModal(); say('err', 'The city was saved but the image upload failed: ' + errText(u.data, u.status)); reload(); return; }
        after();
      });
    });
  }
  function deleteCity() {
    var id = st.modal && st.modal.id; if (!id) return;
    if (!st.modal.confirmDelete) {
      st.modal.confirmDelete = true;
      var b = document.querySelector('#ci_modal [data-ci="delete"]'); if (b) b.textContent = 'Click again to delete';
      return;
    }
    act(api('DELETE', '/' + id)).then(function (res) {
      if (!res) return;
      if (!res.ok) { formErr(errText(res.data, res.status)); st.modal.confirmDelete = false; var b = document.querySelector('#ci_modal [data-ci="delete"]'); if (b) b.textContent = 'Delete…'; return; }
      closeModal(); say('ok', 'City deleted.'); reload();
    });
  }

  // ── import ─────────────────────────────────────────────────────────────────────────────────────────
  function openImport(sourceHint) {
    api('GET', '/import/sources').then(function (r) {
      var list = r.ok ? (r.data.sources || []).filter(function (s) { return s.country === st.country; }) : [];
      st.imp = { step: 1, sources: list, source: sourceHint || (list[0] ? list[0].source : 'text'), text: '', fillState: false, activeOpt: '', preview: null, result: null, confirm: false, err: '' };
      renderImport();
    });
  }
  function importBody(confirm) {
    var s = st.imp, b = { country: st.country };
    if (s.source.indexOf('reference:') === 0) b.source = s.source;
    else {
      var t = (s.text || '').trim();
      if (t.charAt(0) === '[') { try { b.rows = JSON.parse(t); } catch (e) { return null; } } else b.csv = s.text;
    }
    if (s.fillState) b.fill_empty_state = true;
    if (s.activeOpt !== '') b.active = s.activeOpt === '1';
    if (confirm) b.confirm = true;
    return b;
  }
  function resLabel(r) {
    return { 'new': ['n', 'New'], 'exists': ['g', 'Exists, skipped'], 'exists_fill_state': ['w', 'Exists, state filled'], 'error': ['i', 'Error'] }[r] || ['g', r];
  }
  function renderImport() {
    var s = st.imp; if (!s) return;
    var steps = h`<div class="ci-steps"><span class="${raw(s.step === 1 ? 'on' : '')}">1 Source</span><span class="${raw(s.step === 2 ? 'on' : '')}">2 Preview</span><span class="${raw(s.step === 3 ? 'on' : '')}">3 Results</span></div>`;
    var body = '', foot = '';
    if (s.step === 1) {
      var opts = s.sources.map(function (x) { return h`<option value="${x.source}"${raw(s.source === x.source ? ' selected' : '')}>Reference list: ${x.source.replace('reference:', '')} (${x.count} cities)</option>`; }).join('');
      opts += h`<option value="text"${raw(s.source === 'text' ? ' selected' : '')}>CSV file or pasted CSV / JSON</option>`;
      body = h`${raw(steps)}<div class="ci-form"><div><label class="ci-lbl">Country</label><input type="text" value="${cname(st.country)}" disabled><div class="ci-help">The country comes from the selected tab, never from the file.</div></div>
        <div><label class="ci-lbl" for="ci_src">Source</label><select id="ci_src">${raw(opts)}</select></div>
        <div class="full" id="ci_textwrap"${raw(s.source === 'text' ? '' : ' hidden')}><label class="ci-lbl" for="ci_file">CSV file</label><input type="file" id="ci_file" accept=".csv,text/csv,.json"><label class="ci-lbl" for="ci_text" style="margin-top:8px">…or paste CSV / JSON rows</label><textarea id="ci_text" rows="6" placeholder="name,name_ar,state,sort_order,is_popular,popular_order,active">${s.text}</textarea><div class="ci-help">Columns: name (required), name_ar, state, sort_order, is_popular, popular_order, active. Up to 2,000 rows or 1 MB.</div></div>
        <div><label class="ci-lbl" for="ci_act">Import cities as</label><select id="ci_act"><option value=""${raw(s.activeOpt === '' ? ' selected' : '')}>The list’s default</option><option value="1"${raw(s.activeOpt === '1' ? ' selected' : '')}>Active (OffrO serves them)</option><option value="0"${raw(s.activeOpt === '0' ? ' selected' : '')}>Inactive (known, not served)</option></select><div class="ci-help">The India reference list defaults to Inactive so the service area does not change.</div></div>
        <div style="align-self:end"><label style="display:flex;gap:8px;align-items:center;font-size:12px"><input type="checkbox" id="ci_fill"${raw(s.fillState ? ' checked' : '')}> Fill a missing state on cities that already exist</label></div></div>
        <div id="ci_err" class="ci-alert err" style="margin:12px 0 0"${raw(s.err ? '' : ' hidden')}>${s.err}</div>`;
      foot = '<button class="ci-btn" data-ci="close">Cancel</button><button class="ci-btn pri" data-ci="imp-preview">Preview</button>';
    } else if (s.step === 2) {
      var p = s.preview;
      var table = '<div class="ci-tw" style="max-height:340px;overflow:auto"><table class="ci-table" style="min-width:640px"><thead><tr><th>Row</th><th>English</th><th>Arabic</th><th>State</th><th>Result</th></tr></thead><tbody>';
      p.rows.slice(0, 300).forEach(function (r) {
        var l = resLabel(r.result);
        table += h`<tr><td>${r.row}</td><td>${r.name}</td><td class="ci-ar">${r.name_ar}</td><td>${r.state}</td><td><span class="pill ${raw(l[0])}">${l[1]}</span>${raw(r.result === 'error' ? ' ' + esc(r.message || '') : '')}</td></tr>`;
      });
      table += '</tbody></table></div>' + (p.rows.length > 300 ? h`<div class="ci-help">Showing the first 300 of ${p.rows.length} rows.</div>` : '');
      body = h`${raw(steps)}<div class="ci-alert ok"><b>Preview only. Nothing has been written.</b> ${p.read} rows read: <b>${p.new} will be added</b> (${p.active_default ? 'as Active' : 'as Inactive'}), ${p.exists} already exist and will be skipped${raw(p.state_filled ? ', ' + esc(p.state_filled) + ' will get a missing state' : '')}, ${p.errors} with errors.</div>${raw(table)}
        <label style="display:flex;gap:8px;align-items:center;margin-top:12px;font-size:12.5px"><input type="checkbox" id="ci_confirm"${raw(s.confirm ? ' checked' : '')}> I have reviewed the preview and want to add ${p.new} cities to ${cname(st.country)}</label>
        <div class="ci-help">Rows with errors are never imported; the other rows still can be. Existing cities, their images and order are never changed.</div><div id="ci_err" class="ci-alert err" style="margin:12px 0 0"${raw(s.err ? '' : ' hidden')}>${s.err}</div>`;
      foot = '<button class="ci-btn" data-ci="imp-back">Back</button><button class="ci-btn pri" data-ci="imp-apply"' + (s.confirm && p.new + p.state_filled > 0 ? '' : ' disabled') + '>Confirm import</button>';
    } else {
      var r = s.result;
      var errs = (s.preview ? s.preview.rows : []).filter(function (x) { return x.result === 'error'; });
      var et = errs.length ? '<div class="ci-tw"><table class="ci-table" style="min-width:560px"><thead><tr><th>Row</th><th>Field</th><th>Problem</th></tr></thead><tbody>' + errs.map(function (x) { return h`<tr><td>${x.row}</td><td>${x.field}</td><td>${x.message}</td></tr>`; }).join('') + '</tbody></table></div>' : '';
      body = h`${raw(steps)}<div class="ci-alert ok"><b>Import finished.</b> ${r.added} added · ${r.skipped} skipped (already existed) · ${r.errors} not imported (error)${raw(r.state_filled ? ' · ' + esc(r.state_filled) + ' states filled' : '')}. Batch <code>${r.batch}</code></div>${raw(et)}
        <div class="ci-help" style="margin-top:8px">Undo removes only the cities this batch added, and only those no store or account uses. It never touches older cities.</div><div id="ci_err" class="ci-alert err" style="margin:12px 0 0"${raw(s.err ? '' : ' hidden')}>${s.err}</div>`;
      foot = (can('delete') && r.added ? '<button class="ci-btn danger" data-ci="imp-undo" style="margin-right:auto">Undo this import…</button>' : '') + '<button class="ci-btn pri" data-ci="close">Done</button>';
    }
    showModal(h`<div class="ci-mh"><span>Import cities · ${cname(st.country)}</span><button data-ci="close" aria-label="Close">×</button></div><div class="ci-mb">${raw(body)}</div><div class="ci-mf">${raw(foot)}</div>`, true);
  }
  function impPreview() {
    var s = st.imp;
    s.source = $('ci_src').value; s.text = $('ci_text') ? $('ci_text').value : s.text; s.activeOpt = $('ci_act').value; s.fillState = $('ci_fill').checked;
    var b = importBody(false);
    if (!b) { s.err = 'The pasted JSON could not be read. Check the brackets and quotes.'; renderImport(); return; }
    if (!b.source && !(b.csv || b.rows)) { s.err = 'Choose a reference list or add a CSV / JSON.'; renderImport(); return; }
    s.err = '';
    act(api('POST', '/import/preview', b)).then(function (res) {
      if (!res) return;
      if (!res.ok) { s.err = errText(res.data, res.status); renderImport(); return; }
      s.preview = res.data; s.step = 2; s.confirm = false; renderImport();
    });
  }
  function impApply() {
    var s = st.imp; var b = importBody(true);
    act(api('POST', '/import/apply', b)).then(function (res) {
      if (!res) return;
      if (!res.ok) { s.err = errText(res.data, res.status); renderImport(); return; }
      s.result = res.data; s.step = 3; renderImport(); reload();
    });
  }
  function impUndo() {
    var s = st.imp; if (!s || !s.result) return;
    if (!s.undoArmed) { s.undoArmed = true; var b = document.querySelector('#ci_modal [data-ci="imp-undo"]'); if (b) b.textContent = 'Click again to undo'; return; }
    act(api('POST', '/import/' + encodeURIComponent(s.result.batch) + '/undo')).then(function (res) {
      if (!res) return;
      if (!res.ok) { s.err = errText(res.data, res.status); renderImport(); return; }
      closeModal(); say('ok', 'Import undone: ' + res.data.removed + ' cities removed' + (res.data.kept_in_use.length ? '; kept because they are in use: ' + res.data.kept_in_use.join(', ') + '.' : '.')); reload();
    });
  }

  // ── verify ─────────────────────────────────────────────────────────────────────────────────────────
  function openVerify() {
    var c = st.country === 'unassigned' ? ((st.countries[0] || {}).code || 'IN') : st.country;
    showModal(h`<div class="ci-mh"><span>Verify · ${cname(c)}</span><button data-ci="close" aria-label="Close">×</button></div><div class="ci-mb"><div class="ci-empty">Checking…</div></div>`, true);
    api('GET', '/verify?country=' + encodeURIComponent(c)).then(function (r) {
      var b = document.querySelector('#ci_modal .ci-mb'); if (!b) return;
      if (!r.ok) { setHtml(b, h`<div class="ci-alert err">${errText(r.data, r.status)}</div>`); return; }
      var v = r.data, pill = function (n, bad) { return h`<span class="pill ${raw(n === 0 ? 'a' : bad ? 'w' : 'g')}">${n}</span>`; };
      var out = h`<div class="ci-help">Read-only report. Nothing is changed by opening it.</div><div class="ci-tw" style="margin-top:8px"><table class="ci-table" style="min-width:560px"><thead><tr><th>Check</th><th>Result</th><th>Action</th></tr></thead><tbody>
        <tr><td>Cities with no country (not offered to a country request)</td><td>${raw(pill(v.unassigned_count, true))}</td><td>${raw(v.unassigned_count && can('edit') ? '<button class="ci-btn sm" data-ci-assign="' + esc(c) + '">Preview “assign to ' + esc(cname(c)) + '”</button>' : '')}</td></tr>
        <tr><td>Cities in the City Master for ${cname(c)} (${v.active} active, ${v.popular_flagged} popular)</td><td>${raw(pill(v.in_master, false))}</td><td></td></tr>`;
      if (v.reference) out += h`<tr><td>Reference list cities missing from the City Master</td><td>${raw(pill(v.missing_count, true))}</td><td>${raw(v.missing_count && can('add') ? '<button class="ci-btn sm" data-ci-import="' + esc(v.reference) + '">Import reference…</button>' : '')}</td></tr>
        <tr><td>City Master cities not in the reference list (kept)</td><td>${raw(pill(v.not_in_reference_count, false))}</td><td></td></tr>`;
      out += h`<tr><td>Possible duplicates (same name, same or empty state)</td><td>${raw(pill(v.possible_duplicate_count, true))}</td><td>${raw(v.possible_duplicates.map(function (d) { return esc(d.name); }).join(', '))}</td></tr></tbody></table></div><div id="ci_assign"></div>`;
      setHtml(b, out);
    });
  }
  function assignPreview(c) {
    api('POST', '/assign-country', { country: c, preview: true }).then(function (r) { renderAssign(c, r); });
  }
  function renderAssign(c, r) {
    var el = $('ci_assign'); if (!el) return;
    if (!r.ok) { setHtml(el, h`<div class="ci-alert err" style="margin-top:10px">${errText(r.data, r.status)}</div>`); return; }
    var d = r.data;
    setHtml(el, h`<div class="ci-alert note" style="margin-top:10px"><b>Preview only.</b> ${d.would_update} cities without a country would be assigned to ${cname(c)}: ${d.cities.map(function (x) { return x.name; }).slice(0, 40).join(', ')}${d.cities.length > 40 ? '…' : ''}.
      ${raw(can('edit') && d.would_update ? '<div style="margin-top:8px"><label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="ci_assign_ok"> I have reviewed this list</label><button class="ci-btn pri sm" data-ci-assignrun="' + esc(c) + '" style="margin-top:6px">Assign to ' + esc(cname(c)) + '</button></div>' : '')}</div>`);
  }
  function assignRun(c) {
    if (!$('ci_assign_ok') || !$('ci_assign_ok').checked) return;
    act(api('POST', '/assign-country', { country: c })).then(function (r) {
      if (!r) return;
      if (!r.ok) { renderAssign(c, r); return; }
      closeModal(); say('ok', r.data.updated + ' cities assigned to ' + cname(c) + '.'); reload();
    });
  }

  // ── events ─────────────────────────────────────────────────────────────────────────────────────────
  function onClick(e) {
    var t = e.target.closest('button'); if (!t) return;
    var d = t.dataset;
    if (d.ciTab !== undefined) { st.country = d.ciTab; st.q = ''; st.msg = null; loadRows().then(render); return; }
    if (d.ciStatus !== undefined) { st.status = d.ciStatus; render(); return; }
    if (d.ciStar !== undefined) { toggleStar(d.ciStar); return; }
    if (d.ciAct !== undefined) { toggleActive(d.ciAct); return; }
    if (d.ciEdit !== undefined) { openForm(d.ciEdit); return; }
    if (d.ciUp !== undefined) { moveDraft(d.ciUp, -1); return; }
    if (d.ciDn !== undefined) { moveDraft(d.ciDn, 1); return; }
    if (d.ciRm !== undefined) { removeDraft(d.ciRm); return; }
    if (d.ciAssign !== undefined) { assignPreview(d.ciAssign); return; }
    if (d.ciAssignrun !== undefined) { assignRun(d.ciAssignrun); return; }
    if (d.ciImport !== undefined) { closeModal(); openImport(d.ciImport); return; }
    switch (d.ci) {
      case 'retry': reload(); break;
      case 'poponly': st.popOnly = !st.popOnly; render(); break;
      case 'noar': st.noAr = !st.noAr; render(); break;
      case 'add': openForm(''); break;
      case 'import': openImport(); break;
      case 'verify': openVerify(); break;
      case 'saveorder': saveOrder(); break;
      case 'close': closeModal(); break;
      case 'saveform': saveForm(); break;
      case 'delete': deleteCity(); break;
      case 'imp-preview': impPreview(); break;
      case 'imp-back': st.imp.step = 1; renderImport(); break;
      case 'imp-apply': impApply(); break;
      case 'imp-undo': impUndo(); break;
    }
  }
  function onInput(e) {
    if (e.target.id === 'ci_q') { st.q = e.target.value; render(); }
    if (e.target.id === 'ci_confirm') { st.imp.confirm = e.target.checked; var b = document.querySelector('#ci_modal [data-ci="imp-apply"]'); if (b && st.imp.preview) b.disabled = !(st.imp.confirm && st.imp.preview.new + st.imp.preview.state_filled > 0); }
  }
  function onChange(e) {
    if (e.target.id === 'ci_src' && st.imp) { st.imp.source = e.target.value; var w = $('ci_textwrap'); if (w) w.hidden = st.imp.source !== 'text'; }
    if (e.target.id === 'ci_file' && e.target.files && e.target.files[0]) {
      var f = e.target.files[0];
      if (f.size > 1024 * 1024) { st.imp.err = 'The file is larger than 1 MB.'; renderImport(); return; }
      var rd = new FileReader();
      rd.onload = function () { st.imp.text = String(rd.result || ''); var ta = $('ci_text'); if (ta) ta.value = st.imp.text; };
      rd.readAsText(f);
    }
  }
  function init() {
    var root = $('section-cities'); if (!root || root._ciBound) return;
    root._ciBound = true;
    root.addEventListener('click', onClick);
    root.addEventListener('input', onInput);
    root.addEventListener('change', onChange);
    root.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !$('ci_modal').hidden) closeModal(); });
  }
  window.CitiesAdmin = { open: function () { init(); open(); }, _state: st };
})();
