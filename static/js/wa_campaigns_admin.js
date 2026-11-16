/* WhatsApp Campaigns (Phase 2G) - Consent and Audit screens.
 * Depends on wa_campaigns.js. Consent listing and history are READ-ONLY views of the server's records (numbers masked). The one write is
 * the existing "record consent with evidence" action: it needs the Approve permission, an attestation, and the server refuses it for any
 * number that has sent STOP (STOP always wins). A number typed into that form is sent once, cleared, and never shown back in full. */
(function () {
  'use strict';
  var W = window.WaCampaigns;
  if (!W) return;
  var K = W.kit, h = K.h, raw = K.raw, setHtml = K.setHtml, $ = K.$, api = K.api;

  var SRC = { signup_form: 'Sign-up form', written_form: 'Written form', email: 'Email', in_person: 'In person', phone_call: 'Phone call', whatsapp_optin_message: 'WhatsApp opt-in message', other: 'Other' };
  var CH = { whatsapp_keyword: 'Customer replied on WhatsApp', admin_recorded: 'Recorded by an administrator' };
  var EV = { opt_in: 'START (opted in)', opt_out: 'STOP (opted out)', admin_opt_in: 'Admin-recorded consent' };
  var cs = { page: 1, pages: 1, status: '', last4: '', seq: 0, sources: null, ev: { page: 1, pages: 1, event: '', last4: '', seq: 0 } };
  var as = { entries: [], action: '', campaign: '', seq: 0, more: false };

  function msg(el, text, kind) { if (el) { el.textContent = text || ''; el.className = 'wac-msg' + (kind === 'error' ? ' is-error' : ''); } }
  function last4ok(v) { return v === '' || /^\d{4}$/.test(v); }
  function evidenceText(e) {
    if (!e) return '—';
    var parts = [CH[e.channel] || e.channel || ''];
    if (e.channel === 'admin_recorded') { parts.push(SRC[e.consent_source] || e.consent_source || ''); if (e.evidence_ref) parts.push('ref: ' + e.evidence_ref); if (e.recorded_by) parts.push('by ' + e.recorded_by + (e.recorded_by_role ? ' (' + e.recorded_by_role + ')' : '')); }
    else if (e.keyword) parts.push('"' + e.keyword + '"');
    return parts.filter(Boolean).join(' · ');
  }
  function statusPill(s) { return h`<span class="wac-pill ${s === 'opted_in' ? 'ok' : 'bad'}">${s === 'opted_in' ? 'Opted in' : (s === 'opted_out' ? 'Opted out (STOP)' : s)}</span>`; }

  // ═════════════ Consent ═════════════
  function drawConsent() {
    var srcs = cs.sources || Object.keys(SRC);
    setHtml($('wac_pane_consent'),
      '<div class="wac-note">Consent records are read-only here. A customer\'s STOP always overrides consent recorded by an administrator, and only the customer\'s own START restores it. Recipients without recorded consent are never messaged.</div>' +
      (K.can('approve') ? '<div class="wac-panel"><div class="wac-panel-title">Record consent given outside WhatsApp</div><form data-submit="con-record" autocomplete="off"><div class="wac-grid2">' +
        '<div class="wac-field"><label class="form-label" for="wac_c_phone">Phone number</label><input id="wac_c_phone" class="form-control" type="tel" inputmode="tel" autocomplete="off" spellcheck="false" placeholder="+91..."><div class="wac-hint">Used once, then cleared. It is never displayed in full.</div></div>' +
        '<div class="wac-field"><label class="form-label" for="wac_c_src">How consent was given</label><select id="wac_c_src" class="form-select"><option value="">Choose...</option>' +
          srcs.map(function (s) { return h`<option value="${s}">${SRC[s] || s}</option>`; }).join('') + '</select></div>' +
        '<div class="wac-field"><label class="form-label" for="wac_c_ref">Evidence reference</label><input id="wac_c_ref" class="form-control" maxlength="300" placeholder="Form no., document or message reference (6+ characters)"></div>' +
        '<div class="wac-field"><label class="form-label" for="wac_c_when">Consent given on (IST)</label><input id="wac_c_when" class="form-control" type="datetime-local"></div></div>' +
        '<div class="wac-field"><label class="form-label" for="wac_c_note">Note (optional)</label><input id="wac_c_note" class="form-control" maxlength="500"></div>' +
        '<label class="wac-check"><input type="checkbox" id="wac_c_att"> <span>I confirm this evidence exists, is genuine, and shows the customer agreed to receive WhatsApp marketing messages from OffrO.</span></label>' +
        '<div id="wac_c_msg" class="wac-msg" role="status"></div><div class="wac-actions"><button type="submit" class="btn-p" id="wac_c_go">Record consent</button></div></form></div>' : '') +
      '<div class="wac-panel"><div class="wac-panel-title">Consent status</div><div class="wac-toolbar"><select class="form-select" data-change="con-filter" data-key="status"><option value="">Any status</option><option value="opted_in">Opted in</option><option value="opted_out">Opted out (STOP)</option></select>' +
      '<input class="form-control" inputmode="numeric" maxlength="4" placeholder="Last 4 digits" data-input="con-filter" data-key="last4" autocomplete="off" style="width:140px"><button type="button" class="btn-o" data-act="con-reload">Refresh</button></div>' +
      '<div class="table-wrap"><table class="wac-table" style="min-width:760px"><thead><tr><th>Number</th><th>Status</th><th>Opted in</th><th>Opted out</th><th>Source / evidence</th><th>Updated</th></tr></thead><tbody id="wac_c_rows"></tbody></table></div>' +
      '<div class="wac-pager"><span id="wac_c_count" class="wac-muted"></span><div><button type="button" class="btn-o" data-act="con-prev">&larr; Prev</button> <button type="button" class="btn-o" data-act="con-next">Next &rarr;</button></div></div></div>' +
      '<div class="wac-panel"><div class="wac-panel-title">STOP / START history</div><div class="wac-toolbar"><select class="form-select" data-change="ev-filter" data-key="event"><option value="">All events</option><option value="opt_out">STOP</option><option value="opt_in">START</option><option value="admin_opt_in">Admin-recorded</option></select>' +
      '<input class="form-control" inputmode="numeric" maxlength="4" placeholder="Last 4 digits" data-input="ev-filter" data-key="last4" autocomplete="off" style="width:140px"></div>' +
      '<div class="table-wrap"><table class="wac-table" style="min-width:700px"><thead><tr><th>When</th><th>Number</th><th>Event</th><th>Source / evidence</th></tr></thead><tbody id="wac_e_rows"></tbody></table></div>' +
      '<div class="wac-pager"><span id="wac_e_count" class="wac-muted"></span><div><button type="button" class="btn-o" data-act="ev-prev">&larr; Prev</button> <button type="button" class="btn-o" data-act="ev-next">Next &rarr;</button></div></div></div>');
  }

  async function loadConsents() {
    var seq = ++cs.seq, tb = $('wac_c_rows');
    setHtml(tb, '<tr><td colspan="6" class="wac-cell-state"><i class="fas fa-spinner fa-spin"></i> Loading...</td></tr>');
    try {
      var d = await api('/consents?page=' + cs.page + '&page_size=25' + (cs.status ? '&status=' + cs.status : '') + (cs.last4 ? '&last4=' + cs.last4 : ''));
      if (seq !== cs.seq) return;
      cs.page = d.page; cs.pages = d.pages;
      if (!cs.sources && d.admin_consent_sources) { cs.sources = d.admin_consent_sources; var sel = $('wac_c_src'); if (sel) setHtml(sel, '<option value="">Choose...</option>' + cs.sources.map(function (s) { return h`<option value="${s}">${SRC[s] || s}</option>`; }).join('')); }
      setHtml(tb, (d.items || []).length ? d.items.map(function (r) {
        return h`<tr><td>${r.phone_masked}</td><td>${raw(statusPill(r.status))}</td><td>${K.fmtDateTime(r.opted_in_at)}</td><td>${K.fmtDateTime(r.opted_out_at)}</td><td>${evidenceText(r.last_event)}</td><td>${K.fmtDateTime(r.updated_at)}</td></tr>`;
      }).join('') : '<tr><td colspan="6" class="wac-empty">No consent records match.</td></tr>');
      $('wac_c_count').textContent = d.total ? 'Page ' + d.page + ' of ' + d.pages + ' · ' + d.total + ' contacts' : '';
    } catch (e) {
      if (seq !== cs.seq) return;
      if (e.status === 403) { K.showDenied(e.message); return; }
      setHtml(tb, h`<tr><td colspan="6" class="wac-cell-state is-error">${e.message} <button type="button" class="btn-o" data-act="con-reload">Retry</button></td></tr>`);
    }
  }

  async function loadEvents() {
    var f = cs.ev, seq = ++f.seq, tb = $('wac_e_rows');
    setHtml(tb, '<tr><td colspan="4" class="wac-cell-state"><i class="fas fa-spinner fa-spin"></i> Loading...</td></tr>');
    try {
      var d = await api('/consent-events?page=' + f.page + '&page_size=25' + (f.event ? '&event=' + f.event : '') + (f.last4 ? '&last4=' + f.last4 : ''));
      if (seq !== f.seq) return;
      f.page = d.page; f.pages = d.pages;
      setHtml(tb, (d.items || []).length ? d.items.map(function (r) {
        return h`<tr><td>${K.fmtDateTime(r.at)}</td><td>${r.phone_masked}</td><td>${EV[r.event] || r.event}</td><td>${evidenceText(r)}</td></tr>`;
      }).join('') : '<tr><td colspan="4" class="wac-empty">No events match.</td></tr>');
      $('wac_e_count').textContent = d.total ? 'Page ' + d.page + ' of ' + d.pages + ' · ' + d.total + ' events' : '';
    } catch (e) { if (seq === f.seq) setHtml(tb, h`<tr><td colspan="4" class="wac-cell-state is-error">${e.message}</td></tr>`); }
  }

  var CONSENT_MSG = { opted_out: 'This number has opted out (STOP). Consent recorded by an administrator cannot override that; only the customer\'s own START can.' };

  var timers = {};
  function later(key, fn, ms) { clearTimeout(timers[key]); timers[key] = setTimeout(fn, ms); }
  var H = {
    'con-filter': function (el) {
      var k = el.getAttribute('data-key'), v = el.value;
      if (k === 'last4') v = v.replace(/\D/g, '');
      cs[k] = v; cs.page = 1;
      if (k === 'last4' && !last4ok(v)) return;
      later('c', loadConsents, el.tagName === 'INPUT' ? 350 : 0);
    },
    'con-reload': function () { loadConsents(); loadEvents(); },
    'con-prev': function () { if (cs.page > 1) { cs.page -= 1; loadConsents(); } },
    'con-next': function () { if (cs.page < cs.pages) { cs.page += 1; loadConsents(); } },
    'ev-filter': function (el) {
      var k = el.getAttribute('data-key'), v = el.value;
      if (k === 'last4') v = v.replace(/\D/g, '');
      cs.ev[k] = v; cs.ev.page = 1;
      if (k === 'last4' && !last4ok(v)) return;
      later('e', loadEvents, el.tagName === 'INPUT' ? 350 : 0);
    },
    'ev-prev': function () { if (cs.ev.page > 1) { cs.ev.page -= 1; loadEvents(); } },
    'ev-next': function () { if (cs.ev.page < cs.ev.pages) { cs.ev.page += 1; loadEvents(); } },
    'con-record': async function () {
      var m = $('wac_c_msg'), phone = ($('wac_c_phone').value || '').trim(), src = $('wac_c_src').value, ref = ($('wac_c_ref').value || '').trim();
      var when = $('wac_c_when').value, att = $('wac_c_att').checked;
      if (!phone || !src || ref.length < 6 || !when) { msg(m, 'Fill in the number, how consent was given, an evidence reference (6+ characters) and the date.', 'error'); return; }
      if (!att) { msg(m, 'Tick the confirmation that the evidence is genuine.', 'error'); return; }
      var ok = await K.confirm({ title: 'Record this consent?', confirmLabel: 'Record consent', danger: true, checkbox: 'This is permanent evidence recorded under my name.',
        bodyHtml: h`<p>Consent for ***${phone.replace(/\D/g, '').slice(-4)} will be recorded as <b>${SRC[src] || src}</b>, reference <b>${ref}</b>. The action is audited with your identity.</p>` });
      if (!ok) return;
      K.busy($('wac_c_go'), async function () {
        try {
          var r = await api('/consent', { method: 'POST', json: { phone: phone, source: src, evidence_ref: ref, consented_at: when + ':00+05:30', attested: true, note: ($('wac_c_note').value || '').trim() } });
          K.toast(r.result === 'already_opted_in' ? 'This number already had consent on record (***' + r.phone_last4 + ').' : 'Consent recorded for ***' + r.phone_last4 + '.', 'ok');
          ['wac_c_phone', 'wac_c_ref', 'wac_c_note', 'wac_c_when'].forEach(function (id) { $(id).value = ''; });
          $('wac_c_att').checked = false; $('wac_c_src').value = ''; msg(m, '');
          loadConsents(); loadEvents();
        } catch (e) { msg(m, (e.status === 409 && /opted out/i.test(e.message)) ? CONSENT_MSG.opted_out : e.message, 'error'); }
        finally { $('wac_c_phone') && ($('wac_c_phone').value = ''); }     // the number never stays in the page
      });
    }
  };

  function openConsent() { cs.page = 1; cs.status = ''; cs.last4 = ''; cs.ev.page = 1; cs.ev.event = ''; cs.ev.last4 = ''; drawConsent(); loadConsents(); loadEvents(); }

  // ═════════════ Audit ═════════════
  var ACTIONS = ['campaign.create', 'campaign.update', 'campaign.validate', 'campaign.schedule', 'campaign.unschedule', 'campaign.cancel', 'campaign.test_send', 'campaign.run_config', 'campaign.media_upload',
                 'sending.enable', 'sending.disable', 'consent.admin_record', 'templates.sync', 'template.campaign_enable'];

  function drawAudit() {
    setHtml($('wac_pane_audit'),
      '<div class="wac-note">Every sensitive action is recorded with the acting administrator. Numbers appear only as their last four digits.</div>' +
      '<div class="wac-panel"><div class="wac-toolbar"><select class="form-select" data-change="aud2-filter" data-key="action"><option value="">All actions</option>' + ACTIONS.map(function (a) { return h`<option value="${a}">${a}</option>`; }).join('') + '</select>' +
      '<button type="button" class="btn-o" data-act="aud2-reload">Refresh</button></div><div id="wac_a_msg" class="wac-msg" role="status"></div>' +
      '<div class="table-wrap"><table class="wac-table" style="min-width:860px"><thead><tr><th>When (IST)</th><th>Action</th><th>Result</th><th>By</th><th>Campaign</th><th>Target</th><th>Details</th></tr></thead><tbody id="wac_a_rows"></tbody></table></div>' +
      '<div class="wac-pager"><span id="wac_a_count" class="wac-muted"></span><div><button type="button" class="btn-o" data-act="aud2-more" id="wac_a_more">Load older entries</button></div></div></div>');
  }

  function detailsText(d) {
    if (!d || typeof d !== 'object') return '—';
    var out = Object.keys(d).slice(0, 6).map(function (k) { var v = d[k]; return k + ': ' + (v && typeof v === 'object' ? JSON.stringify(v) : String(v)); }).join('; ');
    return out.length > 160 ? out.slice(0, 157) + '...' : (out || '—');
  }

  function auditRow(e) {
    var ok = e.outcome === 'ok' || e.outcome === 'recorded' || e.outcome === 'sent' || e.outcome === 'dry_run';
    var cls = ok ? 'ok' : (/^(blocked|refused|failed)/.test(e.outcome) ? 'bad' : '');
    return h`<tr><td>${K.fmtDateTime(e.at)}</td><td><code>${e.action}</code></td><td><span class="wac-pill ${cls}">${e.outcome}</span></td><td>${(e.admin && e.admin.name) || '—'}${raw(e.admin && e.admin.role ? h` <span class="wac-muted">(${e.admin.role})</span>` : '')}</td>` +
           h`<td>${e.campaign_id ? String(e.campaign_id).slice(-6) : '—'}</td><td>${e.target && e.target.phone_last4 ? '***' + e.target.phone_last4 : (e.target && e.target.template) || '—'}</td><td>${detailsText(e.details)}</td></tr>`;
  }

  async function loadAudit(append) {
    var seq = ++as.seq, tb = $('wac_a_rows');
    if (!append) { as.entries = []; setHtml(tb, '<tr><td colspan="7" class="wac-cell-state"><i class="fas fa-spinner fa-spin"></i> Loading...</td></tr>'); }
    var before = append && as.entries.length ? as.entries[as.entries.length - 1]._id : '';
    try {
      var d = await api('/audit?limit=50' + (as.action ? '&action=' + encodeURIComponent(as.action) : '') + (before ? '&before=' + encodeURIComponent(before) : ''));
      if (seq !== as.seq) return;
      as.entries = as.entries.concat(d.entries || []);
      as.more = (d.entries || []).length === 50;
      setHtml(tb, as.entries.length ? as.entries.map(auditRow).join('') : '<tr><td colspan="7" class="wac-empty">No audit entries yet.</td></tr>');
      $('wac_a_count').textContent = as.entries.length + ' entr' + (as.entries.length === 1 ? 'y' : 'ies') + ' shown';
      $('wac_a_more').hidden = !as.more;
    } catch (e) {
      if (seq !== as.seq) return;
      if (e.status === 403) { K.showDenied(e.message); return; }
      setHtml(tb, h`<tr><td colspan="7" class="wac-cell-state is-error">${e.message} <button type="button" class="btn-o" data-act="aud2-reload">Retry</button></td></tr>`);
    }
  }

  H['aud2-filter'] = function (el) { as.action = el.value; loadAudit(false); };
  H['aud2-reload'] = function () { loadAudit(false); };
  H['aud2-more'] = function () { loadAudit(true); };

  W.register('consent', { open: openConsent, handlers: H });
  W.register('audit', { open: function () { as.action = ''; drawAudit(); loadAudit(false); }, handlers: {} });
})();
