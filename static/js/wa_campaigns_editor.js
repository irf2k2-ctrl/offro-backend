/* WhatsApp Campaigns (Phase 2G) - campaign screens: create / edit, preview, review and schedule, test send, run and report, recipients.
 * Depends on wa_campaigns.js and wa_campaigns_audience.js. Every action calls the existing lifecycle / run API, which enforces permissions,
 * consent, eligibility, the environment guard and the kill switch. There is NO "send now": the only things that can reach a phone are
 * the single-number test send (explicitly confirmed, allow-listed numbers only) and the server-side worker for a SCHEDULED campaign.
 * Saving, validating or opening a campaign never sends. A number typed for a test send is sent once, cleared and never shown back. */
(function () {
  'use strict';
  var W = window.WaCampaigns;
  if (!W) return;
  var K = W.kit, h = K.h, raw = K.raw, setHtml = K.setHtml, $ = K.$, api = K.api, num = K.num;

  var E = { id: null, c: null, tpl: null, sub: 'setup', seq: 0, picker: null, timer: null, rseq: 0, rf: { state: '', last4: '', page: 1 }, cfg: null, testLockUntil: 0 };
  var SUBS = [['setup', 'Setup'], ['review', 'Review & schedule'], ['test', 'Test send'], ['run', 'Run & report'], ['recipients', 'Recipients']];
  var PROBLEM = { header_media_required: 'This template needs header media (image, video or document).', header_media_type_mismatch: 'The attached header media is the wrong type for this template.',
                  header_media_not_needed: 'This template has no media header; remove the attached media.', template_not_found: 'The selected template no longer exists.',
                  status_not_approved: 'The template is not approved by Meta.', mapping_stale: 'The template changed at Meta; re-save its mapping in Templates.',
                  not_enabled: 'The template is not enabled for campaigns.', not_configured: 'The template has no saved variable mapping.', mapping_incomplete: 'Some template variables are not mapped.',
                  no_template: 'Choose a template.', no_audience: 'Choose an audience.', no_eligible_recipients: 'Nobody in the audience is eligible to receive this.',
                  consent_check_failed: 'Consent could not be verified right now. Nothing was saved; try again.', missing_from_meta: 'The template no longer exists at Meta.',
                  category_not_allowed: 'Only MARKETING templates can be used.', unsupported_components: 'The template uses components campaigns do not support.' };
  var REASON = { opted_out: 'Opted out (STOP)', unknown_consent: 'No recorded consent', invalid_number: 'Invalid number', duplicate: 'Duplicate', merchant_inactive: 'Merchant inactive',
                 merchant_no_active_store: 'No active store', consent_lookup_failed: 'Consent could not be checked', invalid_variable_values: 'Template values not valid' };
  var WARN = { sending_disabled: 'Sending is currently switched off, so a scheduled campaign will wait until it is enabled.' };
  var RUN_REASON = { campaign_paused: 'Paused by an administrator', meta_rate_limited: 'Paused briefly because WhatsApp is rate limiting', lease_lost: 'Another worker took over',
                     'kill_switch:production_blocked': 'Stopped: environment guard', 'kill_switch:switch_off': 'Stopped: kill switch turned off', 'kill_switch:env_disabled': 'Stopped: environment key off' };
  function reason(c) { return REASON[c] || String(c || '').replace(/_/g, ' '); }
  function problem(c) { return PROBLEM[c] || String(c || '').replace(/_/g, ' '); }
  function msg(el, text, kind) { if (el) { el.textContent = text || ''; el.className = 'wac-msg' + (kind === 'error' ? ' is-error' : ''); } }
  function editable() { return !!E.c && E.c.status === 'draft' && K.can('edit'); }
  function path(suffix) { return '/campaigns/' + encodeURIComponent(E.id) + (suffix || ''); }
  function stopTimer() { if (E.timer) { clearInterval(E.timer); E.timer = null; } }

  // ═════════════ list <-> detail ═════════════
  function showList() { stopTimer(); E.id = null; $('wac_list_view').hidden = false; $('wac_detail').hidden = true; }
  function showDetail() { $('wac_list_view').hidden = true; $('wac_detail').hidden = false; }

  function newCampaign() {
    stopTimer(); E.id = null; showDetail();
    setHtml($('wac_detail'), '<div class="wac-back"><button type="button" class="wac-link" data-act="ed-back">&larr; All campaigns</button></div>' +
      '<div class="wac-panel"><div class="wac-panel-title">New campaign</div><form data-submit="ed-create" autocomplete="off"><div class="wac-field"><label class="form-label" for="wac_new_name">Campaign name</label>' +
      '<input id="wac_new_name" class="form-control" maxlength="120" required></div><div class="wac-field"><label class="form-label" for="wac_new_desc">Description (optional)</label>' +
      '<input id="wac_new_desc" class="form-control" maxlength="500"></div><div class="wac-note ok">A new campaign is a draft. Creating it does not send anything.</div>' +
      '<div id="wac_new_msg" class="wac-msg" role="status"></div>' + (K.can('add') ? '<button type="submit" class="btn-p" id="wac_new_go">Create draft</button>' : '<div class="wac-note bad">You do not have permission to create campaigns.</div>') + '</form></div>');
    var n = $('wac_new_name'); if (n) n.focus();
  }

  async function openCampaign(id, keepSub) {
    var seq = ++E.seq;
    stopTimer(); showDetail();
    if (!keepSub) E.sub = 'setup';
    E.id = id;
    setHtml($('wac_detail'), '<div class="wac-panel"><i class="fas fa-spinner fa-spin"></i> Loading campaign...</div>');
    try {
      var c = await api(path());
      if (seq !== E.seq) return;
      E.c = c; E.tpl = null;
      if (c.template && c.template.name) {
        try { E.tpl = await api('/templates/' + encodeURIComponent(c.template.name) + '/' + encodeURIComponent(c.template.language)); } catch (e) { E.tpl = null; }
        if (seq !== E.seq) return;
      }
      drawFrame();
    } catch (e) {
      if (seq !== E.seq) return;
      if (e.status === 403) { K.showDenied(e.message); return; }
      setHtml($('wac_detail'), h`<div class="wac-back"><button type="button" class="wac-link" data-act="ed-back">&larr; All campaigns</button></div><div class="wac-note bad">${e.message} ` +
        h`<button type="button" class="btn-o" data-act="ed-reload">Retry</button></div>`);
    }
  }

  function drawFrame() {
    var c = E.c;
    var closed = c.status === 'cancelled' || c.status === 'completed';
    setHtml($('wac_detail'),
      '<div class="wac-back"><button type="button" class="wac-link" data-act="ed-back">&larr; All campaigns</button></div>' +
      '<div class="wac-detail-head"><h2>' + h`${c.name || 'Untitled'}` + '</h2><div>' + K.badge(c.status) + h` <span class="wac-muted">version ${c.version}</span>` +
      ((c.status === 'draft' || c.status === 'cancelled') && K.can('delete') ? ' <button type="button" class="btn-d" data-act="ed-delete">Delete</button>' : '') + '</div></div>' +
      (c.status !== 'draft' ? h`<div class="wac-note">${closed ? 'This campaign is ' + c.status + ' and can no longer be changed.' : 'Only drafts can be edited. ' + (c.status === 'ready' ? 'Use "Revert to draft" in Review & schedule to edit.' : 'Unschedule or cancel it first in Review & schedule.')}</div>` : '') +
      '<div class="wac-subtabs" role="tablist">' + SUBS.map(function (s) {
        return h`<button type="button" role="tab" class="wac-chip ${E.sub === s[0] ? 'active' : ''}" aria-selected="${E.sub === s[0]}" data-act="ed-sub" data-sub="${s[0]}">${s[1]}</button>`;
      }).join('') + '</div><div id="wac_ed_body"></div><div id="wac_ed_tail"></div>');
    drawSub();
  }

  function drawSub() {
    stopTimer();
    var body = $('wac_ed_body');
    E.picker = null;
    if (E.sub === 'setup') drawSetup();
    else if (E.sub === 'review') drawReview();
    else if (E.sub === 'test') drawTest();
    else if (E.sub === 'run') drawRun();
    else if (E.sub === 'recipients') drawRecipients();
    else setHtml(body, '');
  }

  // ═════════════ Setup ═════════════
  function requiredMediaType() {
    var p = E.tpl && E.tpl.parsed, hd = p && p.header;
    if (!hd || !hd.requires_media) return null;
    return String(hd.format || '').toLowerCase();
  }

  function mediaPanel() {
    var t = requiredMediaType(), c = E.c;
    if (!t) return c.header_media ? '<div class="wac-note">This template has no media header. Remove the attached media below.</div><button type="button" class="btn-o" data-act="ed-media-clear">Remove attached media</button>' : '';
    var attached = c.header_media ? h`<div class="wac-note ok">Attached: ${c.header_media.type}${c.header_media.link ? ' (link)' : ' (uploaded to WhatsApp)'}.` +
      (editable() ? ' <button type="button" class="btn-o" data-act="ed-media-clear">Remove</button>' : '') + '</div>' : '<div class="wac-note">Header media is required: attach one before validating.</div>';
    var accept = t === 'image' ? 'image/jpeg,image/png' : (t === 'video' ? 'video/mp4,video/3gpp' : 'application/pdf');
    var limit = t === 'image' ? '5 MB' : (t === 'video' ? '16 MB' : '10 MB');
    return '<div class="wac-panel-title">Header media (' + h`${t}` + ')</div>' + attached +
      (editable() ? h`<div class="wac-grid2"><div class="wac-field"><label class="form-label" for="wac_ed_file">Upload a file (max ${limit})</label><input type="file" id="wac_ed_file" class="form-control" accept="${accept}">` +
        '<button type="button" class="btn-p" style="margin-top:8px" data-act="ed-media-upload">Upload and attach</button></div>' +
        h`<div class="wac-field"><label class="form-label" for="wac_ed_link">...or a public https link</label><input type="url" id="wac_ed_link" class="form-control" placeholder="https://..." autocomplete="off">` +
        '<button type="button" class="btn-o" style="margin-top:8px" data-act="ed-media-link">Use this link</button></div></div>' : '') +
      '<div id="wac_ed_mmsg" class="wac-msg" role="status"></div>';
  }

  async function loadTemplateOptions(sel) {
    try {
      var d = await api('/templates?campaign=usable&page_size=100'), cur = E.c.template ? E.c.template.name + '|' + E.c.template.language : '';
      var items = d.items || [], html = '<option value="">Choose a template...</option>';
      var found = false;
      items.forEach(function (t) { var v = t.name + '|' + t.language; if (v === cur) found = true; html += h`<option value="${v}"${raw(v === cur ? ' selected' : '')}>${t.name} (${t.language})${t.requires_header_media ? ' · needs media' : ''}</option>`; });
      if (cur && !found) html += h`<option value="${cur}" selected>${E.c.template.name} (${E.c.template.language}) · no longer available</option>`;
      setHtml(sel, html);
    } catch (e) { setHtml(sel, h`<option value="">Could not load templates (${e.message})</option>`); }
  }

  function drawSetup() {
    var c = E.c, ro = !editable() ? ' disabled' : '';
    setHtml($('wac_ed_body'),
      '<div class="wac-panel"><div class="wac-panel-title">Details</div><div class="wac-grid2"><div class="wac-field"><label class="form-label" for="wac_ed_name">Name</label>' +
      h`<input id="wac_ed_name" class="form-control" maxlength="120" value="${c.name}"${raw(ro)}></div><div class="wac-field"><label class="form-label" for="wac_ed_desc">Description</label>` +
      h`<input id="wac_ed_desc" class="form-control" maxlength="500" value="${c.description || ''}"${raw(ro)}></div></div>` +
      '<div class="wac-field"><label class="form-label" for="wac_ed_tpl">Template (approved marketing templates enabled for campaigns)</label>' +
      h`<select id="wac_ed_tpl" class="form-select" data-change="ed-tpl"${raw(ro)}><option value="">Loading...</option></select>` +
      '<div class="wac-hint">Variables are mapped per template in the Templates tab. Changing the template removes any attached header media.</div></div>' +
      '<div id="wac_ed_media">' + mediaPanel() + '</div></div>' +
      '<div class="wac-panel"><div class="wac-panel-title">Message preview <button type="button" class="btn-o" data-act="ed-preview">Preview message</button></div><div id="wac_ed_preview"><div class="wac-hint">Uses sample recipient data. Nothing is sent.</div></div></div>' +
      '<div id="wac_ed_picker"></div>' +
      '<div id="wac_ed_smsg" class="wac-msg" role="status"></div>' +
      (editable() ? '<div class="wac-actions"><button type="button" class="btn-p" data-act="ed-save">Save draft</button></div>' : ''));
    loadTemplateOptions($('wac_ed_tpl'));
    E.picker = W.audience ? W.audience.mountPicker('wac_ed_picker', c.audience, !editable()) : null;
  }

  async function saveDraft(el, quiet) {
    var body = { expected_version: E.c.version, name: $('wac_ed_name').value, description: $('wac_ed_desc').value };
    var v = $('wac_ed_tpl').value;
    if (v) { var i = v.indexOf('|'); var t = { name: v.slice(0, i), language: v.slice(i + 1) }; if (!E.c.template || E.c.template.name !== t.name || E.c.template.language !== t.language) body.template = t; }
    if (E.picker) body.audience = E.picker.get();
    try {
      var c = await api(path(), { method: 'PATCH', json: body });
      E.c = c;
      if (body.template) { try { E.tpl = await api('/templates/' + encodeURIComponent(c.template.name) + '/' + encodeURIComponent(c.template.language)); } catch (e) { E.tpl = null; } }
      if (!quiet) K.toast('Draft saved. Nothing was sent.', 'ok');
      $('wac_ed_media') && setHtml($('wac_ed_media'), mediaPanel());
      return true;
    } catch (e) { msg($('wac_ed_smsg'), conflictText(e), 'error'); return false; }
  }

  function conflictText(e) { return e.status === 409 && /version|changed|conflict/i.test(e.message) ? 'Someone else changed this campaign. Reload it and try again.' : e.message; }

  async function patchMedia(el, hm, okText) {
    var m = $('wac_ed_mmsg');
    try {
      E.c = await api(path(), { method: 'PATCH', json: { expected_version: E.c.version, header_media: hm } });
      setHtml($('wac_ed_media'), mediaPanel());
      K.toast(okText, 'ok');
    } catch (e) { msg(m, conflictText(e), 'error'); }
  }

  function previewBlock(r) {
    var pv = r.preview || {}, tp = r.template_problems || [];
    return '<div class="wac-bubble">' + (pv.header ? h`<b>${pv.header.text || pv.header}</b>\n` : '') + h`${pv.body || ''}` + (pv.footer ? h`<span class="f">${pv.footer}</span>` : '') + '</div>' +
      ((r.missing_variables || []).length ? h`<div class="wac-note" style="margin-top:8px">Missing values for: ${r.missing_variables.join(', ')}</div>` : '') +
      (tp.length ? '<div class="wac-note bad" style="margin-top:8px">' + tp.map(function (x) { return h`${problem(x)}`; }).join('<br>') + '</div>' : '') +
      (r.sample_recipient ? h`<div class="wac-hint" style="margin-top:6px">Sample data: ${r.sample_recipient.merchant_name}, ${r.sample_recipient.store_name}, ${r.sample_recipient.store_city}</div>` : '');
  }

  // ═════════════ Review & schedule ═════════════
  function snapshotPanel(c) {
    var s = c.snapshot;
    if (!s) return '<div class="wac-note">No recipient snapshot yet. Validating the campaign checks every recipient\'s consent and freezes the list.</div>';
    var br = s.by_reason || {}, src = s.by_source || {};
    return '<dl class="wac-kv">' + h`<dt>Eligible recipients</dt><dd><b>${num(s.eligible)}</b> of ${num(s.total)}</dd><dt>Excluded</dt><dd>${num(s.excluded)}</dd>` +
      h`<dt>Why excluded</dt><dd>${raw(Object.keys(br).length ? Object.keys(br).map(function (k) { return h`${reason(k)}: ${br[k]}`; }).join(' · ') : '—')}</dd>` +
      h`<dt>By source</dt><dd>${raw(Object.keys(src).map(function (k) { return h`${k}: ${num((src[k] || {}).eligible)}/${num((src[k] || {}).total)}`; }).join(' · ') || '—')}</dd>` +
      h`<dt>Template</dt><dd>${c.template ? c.template.name + ' (' + c.template.language + ')' : '—'}</dd><dt>Snapshot taken</dt><dd>${K.fmtDateTime(s.at)} IST</dd>` +
      h`<dt>Template version</dt><dd><code>${String(s.content_hash || '').slice(0, 10)}</code></dd></dl>` +
      '<div class="wac-hint">Consent is checked again immediately before each message is sent; anyone who sends STOP after this snapshot is skipped.</div>';
  }

  async function drawReview() {
    var c = E.c, body = $('wac_ed_body'), seq = E.seq;
    var html = '<div class="wac-panel"><div class="wac-panel-title">Audience snapshot</div>' + snapshotPanel(c) + '</div>';
    if ((c.template_problems || []).length) html += '<div class="wac-note bad">' + c.template_problems.map(function (x) { return h`${problem(x)}`; }).join('<br>') + '</div>';
    html += '<div id="wac_ed_confirm"></div><div id="wac_ed_actions"></div><div id="wac_ed_rmsg" class="wac-msg" role="status"></div>';
    setHtml(body, html);
    drawReviewActions();
    if (c.snapshot && (c.status === 'ready' || c.status === 'scheduled')) {
      try {
        var cf = await api(path('/confirmation'));
        if (seq !== E.seq || E.sub !== 'review') return;
        E.confirm = cf;
        setHtml($('wac_ed_confirm'), '<div class="wac-panel"><div class="wac-panel-title">Before you schedule</div>' +
          ((cf.warnings || []).length ? cf.warnings.map(function (w) { return h`<div class="wac-note">${WARN[w.code] || w.message}</div>`; }).join('') : '<div class="wac-note ok">No warnings.</div>') +
          '<ul class="wac-hint" style="margin:0 0 0 16px"><li>Nothing is sent when you schedule; the worker sends at the scheduled time only if sending is enabled.</li>' +
          '<li>Recipients without consent, or who send STOP, are skipped automatically.</li></ul></div>');
      } catch (e) { if (seq === E.seq) setHtml($('wac_ed_confirm'), h`<div class="wac-note bad">Could not load the confirmation checklist: ${e.message}</div>`); }
    }
  }

  function istInputMin() {
    var d = new Date(Date.now() + 20 * 60000), p = function (n) { return (n < 10 ? '0' : '') + n; };
    var ist = new Date(d.getTime() + (330 + d.getTimezoneOffset()) * 60000);
    return ist.getFullYear() + '-' + p(ist.getMonth() + 1) + '-' + p(ist.getDate()) + 'T' + p(ist.getHours()) + ':' + p(ist.getMinutes());
  }

  function drawReviewActions() {
    var c = E.c, el = $('wac_ed_actions'), a = '';
    if (c.status === 'draft') {
      a = '<div class="wac-panel"><div class="wac-panel-title">Validate</div><div class="wac-hint">Validating checks the template and audience, verifies consent for every recipient and freezes the recipient list. It sends nothing.</div>' +
        (K.can('edit') ? '<div class="wac-actions"><button type="button" class="btn-p" data-act="ed-validate">Validate campaign</button></div>' : '') + '</div>';
    } else if (c.status === 'ready') {
      a = '<div class="wac-panel"><div class="wac-panel-title">Schedule</div>' +
        (K.can('approve') ? '<div class="wac-grid2"><div class="wac-field"><label class="form-label" for="wac_ed_when">Send at (IST)</label>' +
          h`<input type="datetime-local" id="wac_ed_when" class="form-control" min="${istInputMin()}" step="60"><div class="wac-hint">At least 15 minutes from now and within 90 days. Times are Indian Standard Time.</div></div></div>` +
          '<div class="wac-actions"><button type="button" class="btn-p" data-act="ed-schedule">Schedule campaign...</button>' : '<div class="wac-note">You need the Approve permission to schedule.</div><div class="wac-actions">') +
        (K.can('edit') ? '<button type="button" class="btn-o" data-act="ed-revert">Revert to draft</button><button type="button" class="btn-d" data-act="ed-cancel">Cancel campaign</button>' : '') + '</div></div>';
    } else if (c.status === 'scheduled') {
      var sc = c.schedule || {}, mode = (c.run_config && c.run_config.mode) || 'test';
      a = '<div class="wac-panel"><div class="wac-panel-title">Scheduled</div><dl class="wac-kv">' + h`<dt>Send at</dt><dd>${K.fmtDateTime(sc.send_at)} IST</dd><dt>Run mode</dt><dd>${raw(mode === 'live' ? '<span class="wac-pill warn">Live: every eligible recipient</span>' : '<span class="wac-pill">Test: allow-listed numbers only</span>')}</dd></dl>` +
        '<div class="wac-hint">Test mode can only reach allow-listed test numbers. Live mode needs a separate explicit confirmation and the live-mode key on the server.</div><div class="wac-actions">' +
        (K.can('approve') ? '<button type="button" class="btn-p" data-act="ed-runmode-test">Use test mode</button><button type="button" class="btn-d" data-act="ed-runmode-live">Switch to LIVE mode...</button>' : '') +
        (K.can('edit') ? '<button type="button" class="btn-o" data-act="ed-unschedule">Unschedule</button><button type="button" class="btn-d" data-act="ed-cancel">Cancel campaign</button>' : '') + '</div></div>';
    } else if (c.status === 'running' || c.status === 'paused') {
      a = '<div class="wac-note">This campaign is ' + h`${c.status}` + '. Manage it from "Run & report".</div>';
    }
    setHtml(el, a);
  }

  function futureIso(local) {
    if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(local || '')) return null;
    return local + ':00+05:30';
  }

  async function transition(el, suffix, okText, json) {
    return K.busy(el, async function () {
      try {
        E.c = await api(path(suffix), { method: 'POST', json: json || {} });
        K.toast(okText, 'ok'); await refreshAfter();
      } catch (e) { msg($('wac_ed_rmsg'), conflictText(e), 'error'); }
    });
  }
  async function refreshAfter() { await openCampaign(E.id, true); }              // reloads the campaign and redraws the current sub-tab

  // ═════════════ Test send ═════════════
  async function drawTest() {
    var c = E.c, seq = E.seq;
    var closed = c.status === 'cancelled' || c.status === 'completed';
    setHtml($('wac_ed_body'),
      '<div class="wac-panel"><div class="wac-panel-title">Message</div><div id="wac_t_prev"><i class="fas fa-spinner fa-spin"></i></div></div>' +
      '<div class="wac-panel"><div class="wac-panel-title">Send a test message</div><div id="wac_t_cfg" class="wac-hint">Checking sending status...</div>' +
      (closed ? '<div class="wac-note">Test sends are not available for a ' + h`${c.status}` + ' campaign.</div>' :
      (!K.can('approve') ? '<div class="wac-note bad">You need the Approve permission to send a test message.</div>' :
      '<div class="wac-field" style="max-width:340px"><label class="form-label" for="wac_t_num">Test number</label>' +
      '<input id="wac_t_num" class="form-control" type="tel" inputmode="tel" autocomplete="off" placeholder="+91..." spellcheck="false"><div class="wac-hint">Must be on the test-number allow-list and have recorded consent. It is sent once and never shown back in full.</div></div>' +
      '<label class="wac-check"><input type="checkbox" id="wac_t_dry" checked> <span><b>Dry run</b>: check everything and show what would be sent, but do not send.</span></label>' +
      '<label class="wac-check"><input type="checkbox" id="wac_t_ok"> <span>I confirm this number is one of our own allow-listed test numbers and has consented to receive this message.</span></label>' +
      '<div class="wac-actions"><button type="button" class="btn-p" id="wac_t_go" data-act="ed-test">Run test</button></div>')) +
      '<div id="wac_t_res" style="margin-top:12px"></div></div>');
    api(path('/preview'), { method: 'POST', json: {} }).then(function (r) { if (seq === E.seq && E.sub === 'test') setHtml($('wac_t_prev'), previewBlock(r)); })
      .catch(function (e) { setHtml($('wac_t_prev'), h`<div class="wac-note bad">${e.message}</div>`); });
    api('/config').then(function (cfg) {
      if (seq !== E.seq || E.sub !== 'test') return;
      E.cfg = cfg; var s = cfg.sending || {}, env = cfg.environment || {};
      setHtml($('wac_t_cfg'), h`Environment: <b>${env.classification || 'unknown'}</b> · Sending: <b>${s.allowed ? 'ON' : 'OFF (' + (s.reason || '') + ')'}</b> · ` +
        h`Allow-listed numbers: ${(cfg.test_numbers_masked || []).join(', ') || 'none configured'}` + (s.allowed ? '' : '. Real test sends are refused while sending is off; a dry run still works.'));
    }).catch(function () { setHtml($('wac_t_cfg'), ''); });
  }

  function testResult(r, dry) {
    if (dry || r.dry_run) return h`<div class="wac-note ok"><b>Dry run passed.</b> Everything checked out; <b>nothing was sent</b>. A real test would go to ***${r.to_last4 || ''}.</div>`;
    if (r.outcome === 'uncertain') return h`<div class="wac-note bad"><b>Outcome unknown.</b> ${r.warning || 'Do not resend automatically. Check the phone and WhatsApp.'}</div>`;
    if (r.sent) return h`<div class="wac-note ok"><b>Test message accepted by WhatsApp</b> for ***${r.to_last4}. Delivery is confirmed separately by WhatsApp.</div>`;
    return h`<div class="wac-note bad"><b>Test message was not sent.</b> WhatsApp refused it${r.error_code ? ' (code ' + r.error_code + ')' : ''}.</div>`;
  }

  async function runTest(el) {
    var inp = $('wac_t_num'), phone = (inp.value || '').trim(), dry = $('wac_t_dry').checked, res = $('wac_t_res');
    if (!phone) { setHtml(res, '<div class="wac-note bad">Enter the test number.</div>'); return; }
    if (!$('wac_t_ok').checked) { setHtml(res, '<div class="wac-note bad">Tick the confirmation first.</div>'); return; }
    if (Date.now() < E.testLockUntil && !dry) { setHtml(res, '<div class="wac-note">A test message was just sent. Wait a minute before sending another.</div>'); return; }
    if (!dry) {
      var yes = await K.confirm({ title: 'Send a REAL test message?', confirmLabel: 'Send test message', danger: true,
        checkbox: 'Send this template to my test number now.', bodyHtml: h`<p>One WhatsApp message will be sent to ***${phone.replace(/\D/g, '').slice(-4)} using <b>${E.c.template ? E.c.template.name : ''}</b>.</p>` });
      if (!yes) return;
    }
    K.busy(el, async function () {
      setHtml(res, '<div class="wac-hint"><i class="fas fa-spinner fa-spin"></i> ' + (dry ? 'Checking...' : 'Sending...') + '</div>');
      try {
        var r = await api(path('/test-send'), { method: 'POST', json: { phone: phone, confirm_test_send: true, dry_run: dry } });
        setHtml(res, testResult(r, dry));
        if (!dry) E.testLockUntil = Date.now() + 60000;          // the server also enforces a cooldown and refuses a duplicate
      } catch (e) {
        if (!dry && e.status !== 409 && e.status !== 403 && e.status !== 400) E.testLockUntil = Date.now() + 60000;
        setHtml(res, h`<div class="wac-note bad">${friendlyTestError(e)}</div>`);
      } finally { inp.value = ''; phone = null; }                 // the number never stays in the page
    });
  }
  var TEST_ERR = { sending_disabled: 'Sending is switched off, so no real test message can be sent. A dry run still works.', not_a_test_number: 'This number is not on the test-number allow-list.',
                   recipient_not_eligible: 'This number cannot be messaged (no recorded consent, or it has opted out).', production_blocked: 'Test sending is blocked because this environment is Production.',
                   environment_unknown: 'Test sending is blocked because this server has not identified its environment.', rate_limited: 'Test-send limit reached. Try again later.',
                   confirmation_required: 'Confirmation is required.', template_not_ready: 'The template is not ready for sending.', invalid_phone: 'That is not a valid phone number (international numbers need +country code).' };
  function friendlyTestError(e) {
    var d = e.detail;
    if (d && typeof d === 'object' && d.code && TEST_ERR[d.code]) return TEST_ERR[d.code];
    if (d && typeof d === 'object' && d.code && /claim|duplicate|cooldown|already/i.test(d.code)) return 'A test message for this number was already sent recently. It was not sent again.';
    return e.message;
  }

  // ═════════════ Run & report ═════════════
  var STATE_ORDER = [['total', 'Total'], ['eligible', 'Eligible'], ['queued', 'Queued'], ['sending', 'Sending'], ['sent', 'Sent'], ['delivered', 'Delivered'], ['read', 'Read'],
                     ['failed', 'Failed'], ['uncertain', 'Uncertain'], ['excluded', 'Excluded'], ['cancelled', 'Cancelled']];

  function drawRun() {
    setHtml($('wac_ed_body'), '<div id="wac_run"><div class="wac-panel"><i class="fas fa-spinner fa-spin"></i> Loading report...</div></div>');
    loadRun();
    if (E.c && (E.c.status === 'running' || E.c.status === 'scheduled' || E.c.status === 'paused')) E.timer = setInterval(function () { if (E.sub === 'run' && $('wac_run')) loadRun(true); else stopTimer(); }, 10000);
  }

  async function loadRun(quiet) {
    var seq = ++E.rseq, id = E.id;
    try {
      var r = await api(path('/run')), fresh = await api(path());
      if (seq !== E.rseq || id !== E.id || E.sub !== 'run') return;
      E.c = fresh;
      renderRun(r);
    } catch (e) { if (!quiet && seq === E.rseq) setHtml($('wac_run'), h`<div class="wac-note bad">${e.message} <button type="button" class="btn-o" data-act="ed-run-reload">Retry</button></div>`); }
  }

  function renderRun(r) {
    var s = r.stats || {}, c = E.c, snap = c.snapshot || {};
    var done = (s.delivered || 0) + (s.read || 0) + (s.sent || 0) + (s.failed || 0) + (s.uncertain || 0) + (s.cancelled || 0);
    var total = s.total || 0, pct = total ? Math.round(done * 100 / total) : 0;
    var seg = function (cls, n) { return total && n ? h`<i class="${cls}" style="width:${(n * 100 / total).toFixed(2)}%"></i>` : ''; };
    var vals = { total: s.total, eligible: snap.eligible, queued: (s.queued || 0) + (s.snapshot || 0), sending: s.sending, sent: s.sent, delivered: s.delivered, read: s.read, failed: s.failed, uncertain: s.uncertain, excluded: snap.excluded, cancelled: s.cancelled };
    var ctl = '';
    if (r.status === 'running' && K.can('edit')) ctl += '<button type="button" class="btn-o" data-act="ed-pause">Pause</button>';
    if (r.status === 'paused' && K.can('approve')) ctl += '<button type="button" class="btn-p" data-act="ed-resume">Resume...</button>';
    if ((r.status === 'running' || r.status === 'paused') && K.can('edit')) ctl += '<button type="button" class="btn-d" data-act="ed-cancel-run">Cancel run...</button>';
    var why = r.pause_reason ? (RUN_REASON[r.pause_reason] || r.pause_reason.replace(/_/g, ' ')) : '';
    setHtml($('wac_run'),
      '<div class="wac-panel"><div class="wac-panel-title">Status ' + K.badge(r.status) + '<button type="button" class="btn-o" data-act="ed-run-reload">Refresh</button></div><dl class="wac-kv">' +
      h`<dt>Mode</dt><dd>${r.mode === 'live' ? 'Live' : 'Test (allow-listed numbers only)'}</dd><dt>Started</dt><dd>${K.fmtDateTime(r.started_at)}</dd><dt>Finished</dt><dd>${K.fmtDateTime(r.finished_at)}</dd>` +
      (why ? h`<dt>Paused because</dt><dd>${why}</dd>` : '') + (r.backoff_until ? h`<dt>Waiting until</dt><dd>${K.fmtDateTime(r.backoff_until)}</dd>` : '') +
      h`<dt>Last updated</dt><dd>${K.fmtDateTime(c.updated_at)} IST (checked ${new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' })})</dd></dl>` +
      (ctl ? '<div class="wac-actions">' + ctl + '</div>' : '') + '<div id="wac_ed_rmsg" class="wac-msg" role="status"></div></div>' +
      '<div class="wac-panel"><div class="wac-panel-title">Progress <span class="wac-muted">' + h`${pct}% processed (${done} of ${total})` + '</span></div><div class="wac-progress" aria-label="progress">' +
      seg('p-read', s.read) + seg('p-delivered', s.delivered) + seg('p-sent', s.sent) + seg('p-uncertain', s.uncertain) + seg('p-failed', s.failed) + '</div>' +
      '<div class="wac-stats" style="margin-top:12px">' + STATE_ORDER.map(function (x) { return h`<div class="wac-stat"><b>${num(vals[x[0]])}</b><span>${x[1]}</span></div>`; }).join('') + '</div>' +
      (s.pending_retry ? h`<div class="wac-hint" style="margin-top:8px">${s.pending_retry} message${s.pending_retry === 1 ? ' is' : 's are'} waiting for an automatic retry.</div>` : '') +
      (s.uncertain ? '<div class="wac-note bad" style="margin-top:8px">Uncertain messages may or may not have been delivered. They are never resent automatically.</div>' : '') +
      (s.consistent === false ? '<div class="wac-note">The counts do not add up yet; they are being updated.</div>' : '') + '</div>');
  }

  // ═════════════ Recipients ═════════════
  function drawRecipients() {
    var c = E.c;
    if (!c.snapshot) { setHtml($('wac_ed_body'), '<div class="wac-note">There is no recipient list yet. Validate the campaign (Review & schedule) to create it.</div>'); return; }
    var states = ['', 'snapshot', 'queued', 'sending', 'sent', 'delivered', 'read', 'failed', 'uncertain', 'cancelled'];
    setHtml($('wac_ed_body'), '<div class="wac-panel"><div class="wac-panel-title">Recipients (numbers are masked)</div><div class="wac-toolbar">' +
      '<select class="form-select" data-change="ed-rfilter" data-key="state">' + states.map(function (v) { return h`<option value="${v}"${raw(v === E.rf.state ? ' selected' : '')}>${v ? v.charAt(0).toUpperCase() + v.slice(1) : 'Any state'}</option>`; }).join('') + '</select>' +
      h`<input class="form-control" inputmode="numeric" maxlength="4" placeholder="Last 4 digits" data-input="ed-rfilter" data-key="last4" value="${E.rf.last4}" autocomplete="off" style="width:140px">` +
      '<button type="button" class="btn-o" data-act="ed-rreload">Refresh</button></div><div id="wac_r_msg" class="wac-msg" role="status"></div>' +
      '<div class="table-wrap"><table class="wac-table" style="min-width:980px"><thead><tr><th>Number</th><th>Source</th><th>State</th><th>Consent now</th><th>Sent</th><th>Last status</th><th>Failure</th><th>Attempts</th><th>Ref</th></tr></thead><tbody id="wac_r_rows"></tbody></table></div>' +
      '<div class="wac-pager"><span id="wac_r_count" class="wac-muted"></span><div><button type="button" class="btn-o" data-act="ed-rprev">&larr; Prev</button> <button type="button" class="btn-o" data-act="ed-rnext">Next &rarr;</button></div></div></div>');
    loadRecipients();
  }

  async function loadRecipients() {
    var seq = ++E.rseq, tb = $('wac_r_rows'), f = E.rf;
    setHtml(tb, '<tr><td colspan="9" class="wac-cell-state"><i class="fas fa-spinner fa-spin"></i> Loading...</td></tr>');
    var q = '/recipient-details?page=' + f.page + '&page_size=50' + (f.state ? '&state=' + encodeURIComponent(f.state) : '') + (/^\d{4}$/.test(f.last4) ? '&last4=' + f.last4 : '');
    try {
      var d = await api(path(q));
      if (seq !== E.rseq || E.sub !== 'recipients') return;
      E.rpages = d.pages; f.page = d.page;
      setHtml(tb, (d.items || []).length ? d.items.map(function (r) {
        var cn = r.eligibility.consent_now, cp = cn === 'opted_in' ? 'ok' : (cn === 'opted_out' ? 'bad' : 'warn');
        return h`<tr><td>${r.phone_masked}</td><td>${r.source}</td><td>${raw(stateBadge(r.state))}</td><td><span class="wac-pill ${cp}">${String(cn || '').replace(/_/g, ' ')}</span></td>` +
               h`<td>${K.fmtDateTime(r.timestamps.sent_at)}</td><td>${K.fmtDateTime(r.timestamps.last_status_at)}</td><td>${r.failure ? reason(r.failure.class) + (r.failure.code ? ' (' + r.failure.code + ')' : '') : '—'}</td>` +
               h`<td>${r.retry.attempts}${r.retry.next_attempt_at ? ' · next ' + K.fmtDateTime(r.retry.next_attempt_at) : ''}</td><td><code>${r.message_ref || '—'}</code></td></tr>`;
      }).join('') : '<tr><td colspan="9" class="wac-empty">No recipients match.</td></tr>');
      $('wac_r_count').textContent = 'Page ' + d.page + ' of ' + d.pages + ' · ' + d.total + ' recipients';
    } catch (e) { if (seq === E.rseq) setHtml(tb, h`<tr><td colspan="9" class="wac-cell-state is-error">${e.message}</td></tr>`); }
  }
  function stateBadge(s) {
    var cls = { delivered: 'ok', read: 'ok', sent: 'ok', failed: 'bad', uncertain: 'warn', cancelled: 'bad' }[s] || '';
    return h`<span class="wac-pill ${cls}">${s}</span>`;
  }

  // ═════════════ handlers ═════════════
  var H = {
    'ed-back': function () { W.showTab('campaigns'); },
    'ed-reload': function () { openCampaign(E.id, true); },
    'ed-sub': function (el) { E.sub = el.getAttribute('data-sub'); drawFrame(); },
    'ed-create': function () {
      var go = $('wac_new_go'), name = $('wac_new_name').value.trim();
      if (!name) { msg($('wac_new_msg'), 'Enter a name.', 'error'); return; }
      K.busy(go, async function () {
        try { var c = await api('/campaigns', { method: 'POST', json: { name: name, description: $('wac_new_desc').value.trim() } }); K.toast('Draft created. Nothing was sent.', 'ok'); K.loadOverview(); K.loadList(); openCampaign(c.id); }
        catch (e) { msg($('wac_new_msg'), e.message, 'error'); }
      });
    },
    'ed-delete': async function (el) {
      var ok = await K.confirm({ title: 'Delete this campaign?', confirmLabel: 'Delete campaign', danger: true, bodyHtml: h`<p><b>${E.c.name}</b> will be permanently deleted. This cannot be undone.</p>` });
      if (!ok) return;
      K.busy(el, async function () { await api(path(), { method: 'DELETE' }); K.toast('Campaign deleted.', 'ok'); W.showTab('campaigns'); });
    },
    'ed-tpl': function () { msg($('wac_ed_smsg'), 'Save the draft to apply the new template.'); },
    'ed-save': function (el) { K.busy(el, function () { return saveDraft(el); }); },
    'ed-preview': function (el) {
      K.busy(el, async function () {
        if (editable()) { if (!await saveDraft(el, true)) return; }
        try { setHtml($('wac_ed_preview'), previewBlock(await api(path('/preview'), { method: 'POST', json: {} }))); }
        catch (e) { setHtml($('wac_ed_preview'), h`<div class="wac-note bad">${e.message}</div>`); }
      });
    },
    'ed-media-upload': function (el) {
      var f = $('wac_ed_file').files[0], t = requiredMediaType(), m = $('wac_ed_mmsg');
      if (!f) { msg(m, 'Choose a file first.', 'error'); return; }
      K.busy(el, async function () {
        msg(m, 'Uploading to WhatsApp...');
        try {
          var fd = new FormData(); fd.append('file', f);
          var r = await api('/media/header', { method: 'POST', form: fd });
          if (r.header_media.type !== t) { msg(m, 'That file is a ' + r.header_media.type + ' but this template needs a ' + t + '.', 'error'); return; }
          await patchMedia(el, r.header_media, 'Media attached.');
        } catch (e) { msg(m, e.message, 'error'); }
      });
    },
    'ed-media-link': function (el) {
      var link = ($('wac_ed_link').value || '').trim(), t = requiredMediaType(), m = $('wac_ed_mmsg');
      if (!/^https:\/\//i.test(link)) { msg(m, 'Enter a public https:// link.', 'error'); return; }
      K.busy(el, function () { return patchMedia(el, { type: t, link: link }, 'Media link attached.'); });
    },
    'ed-media-clear': function (el) { K.busy(el, function () { return patchMedia(el, null, 'Media removed.'); }); },
    'ed-validate': function (el) {
      K.busy(el, async function () {
        try {
          var r = await api(path('/validate'), { method: 'POST', json: {} });
          if (r.ok) { K.toast('Campaign validated: ' + r.snapshot.eligible + ' eligible recipients. Nothing was sent.', 'ok'); await refreshAfter(); }
          else { msg($('wac_ed_rmsg'), 'Cannot validate: ' + ((r.errors || []).map(problem).join('; ') || r.message || 'unknown problem') + (r.message ? ' ' + r.message : ''), 'error'); }
        } catch (e) { msg($('wac_ed_rmsg'), validateText(e), 'error'); }
      });
    },
    'ed-revert': async function (el) {
      var ok = await K.confirm({ title: 'Revert to draft?', confirmLabel: 'Revert to draft', bodyHtml: '<p>The frozen recipient list is dropped. You can edit the campaign and validate it again.</p>' });
      if (ok) transition(el, '/revert-to-draft', 'Campaign is a draft again.');
    },
    'ed-cancel': async function (el) {
      var ok = await K.confirm({ title: 'Cancel this campaign?', confirmLabel: 'Cancel campaign', danger: true, checkbox: 'I understand a cancelled campaign cannot be restarted.',
        bodyHtml: h`<p><b>${E.c.name}</b> will be cancelled permanently. Nothing will be sent.</p>` });
      if (ok) transition(el, '/cancel', 'Campaign cancelled.');
    },
    'ed-unschedule': async function (el) {
      var ok = await K.confirm({ title: 'Unschedule this campaign?', confirmLabel: 'Unschedule', bodyHtml: '<p>The campaign goes back to Ready and will not be sent at the scheduled time.</p>' });
      if (ok) transition(el, '/unschedule', 'Campaign unscheduled.');
    },
    'ed-schedule': async function (el) {
      var c = E.c, snap = c.snapshot || {}, when = futureIso($('wac_ed_when').value);
      if (!when) { msg($('wac_ed_rmsg'), 'Choose the date and time to send (IST).', 'error'); return; }
      var tpl = c.template ? c.template.name + '/' + c.template.language : '';
      var ok = await K.confirm({ title: 'Schedule this campaign?', confirmLabel: 'Schedule campaign', danger: true, type: String(snap.eligible),
        checkbox: 'I have reviewed the message, the audience and the consent summary.',
        bodyHtml: h`<p><b>${num(snap.eligible)}</b> eligible recipients will be messaged with <b>${tpl}</b> at <b>${K.fmtDateTime(when)} IST</b>, if sending is enabled then.</p>` +
                  h`<p>Recipients without consent, or who reply STOP before then, are skipped. The campaign starts in test mode (allow-listed numbers only) until you explicitly switch it to live.</p>` });
      if (!ok) return;
      transition(el, '/schedule', 'Campaign scheduled. Nothing is sent until the scheduled time and only if sending is enabled.', { send_at: when, confirm: { acknowledge: true, eligible_count: snap.eligible, template: tpl } });
    },
    'ed-runmode-test': function (el) { setMode(el, 'test'); },
    'ed-runmode-live': function (el) { setMode(el, 'live'); },
    'ed-test': function (el) { runTest(el); },
    'ed-run-reload': function () { loadRun(); },
    'ed-pause': async function (el) {
      var ok = await K.confirm({ title: 'Pause this campaign?', confirmLabel: 'Pause', bodyHtml: '<p>Sending stops after the current messages. You can resume it later.</p>' });
      if (ok) runAction(el, '/pause', 'Campaign paused.');
    },
    'ed-resume': async function (el) {
      var ok = await K.confirm({ title: 'Resume sending?', confirmLabel: 'Resume sending', danger: true, checkbox: 'I understand messages will start sending again.',
        bodyHtml: '<p>The server still re-checks the kill switch, the environment guard and every recipient\'s consent before each message.</p>' });
      if (ok) runAction(el, '/resume', 'Campaign resumed.');
    },
    'ed-cancel-run': async function (el) {
      var ok = await K.confirm({ title: 'Cancel this run?', confirmLabel: 'Cancel run', danger: true, checkbox: 'I understand queued messages will not be sent and this cannot be undone.',
        bodyHtml: '<p>Messages already sent are kept in the report. Everything still queued is cancelled.</p>' });
      if (ok) runAction(el, '/cancel-run', 'Run cancelled.');
    },
    'ed-rfilter': function (el) {
      var key = el.getAttribute('data-key');
      E.rf[key] = key === 'last4' ? el.value.replace(/\D/g, '') : el.value; E.rf.page = 1;
      clearTimeout(H._d); H._d = setTimeout(function () { if (E.sub === 'recipients' && (E.rf.last4 === '' || /^\d{4}$/.test(E.rf.last4))) loadRecipients(); }, el.tagName === 'INPUT' ? 350 : 0);
    },
    'ed-rreload': function () { loadRecipients(); },
    'ed-rprev': function () { if (E.rf.page > 1) { E.rf.page -= 1; loadRecipients(); } },
    'ed-rnext': function () { if (E.rf.page < (E.rpages || 1)) { E.rf.page += 1; loadRecipients(); } }
  };

  function validateText(e) {
    var d = e.detail;
    if (d && typeof d === 'object' && d.errors) return 'Cannot validate: ' + d.errors.map(problem).join('; ');
    return e.message;
  }

  async function setMode(el, mode) {
    var snap = E.c.snapshot || {};
    if (mode === 'test') {
      return K.busy(el, async function () {
        try { E.c = await api(path('/run-config'), { method: 'POST', json: { mode: 'test' } }); K.toast('Run mode: test (allow-listed numbers only).', 'ok'); await refreshAfter(); }
        catch (e) { msg($('wac_ed_rmsg'), conflictText(e), 'error'); }
      });
    }
    var ok = await K.confirm({ title: 'Switch to LIVE mode?', confirmLabel: 'Enable live mode', danger: true, type: String(snap.eligible),
      checkbox: 'I understand that at the scheduled time this campaign will message every eligible recipient, not just test numbers.',
      bodyHtml: h`<p>Live mode will send to <b>${num(snap.eligible)}</b> recipients. The server additionally requires the live-mode key, a non-Production environment and the sending kill switch; each recipient's consent is re-checked before each message.</p>` });
    if (!ok) return;
    K.busy(el, async function () {
      try {
        E.c = await api(path('/run-config'), { method: 'POST', json: { mode: 'live', confirm: { acknowledge: true, mode: 'live', eligible_count: snap.eligible } } });
        K.toast('Run mode: LIVE.', 'ok'); await refreshAfter();
      } catch (e) { msg($('wac_ed_rmsg'), conflictText(e), 'error'); }
    });
  }

  function runAction(el, suffix, okText) {
    K.busy(el, async function () {
      try { await api(path(suffix), { method: 'POST', json: {} }); K.toast(okText, 'ok'); await loadRun(); }
      catch (e) { msg($('wac_ed_rmsg'), e.message, 'error'); }
    });
  }

  W.register('editor', { open: showList, showList: showList, openCampaign: openCampaign, newCampaign: newCampaign, handlers: H });
})();
