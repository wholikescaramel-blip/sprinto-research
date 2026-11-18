/* Outreach Desk UI. Vanilla JS + GSAP (cdnjs). All data comes from the local FastAPI backend. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
gsap.registerPlugin(ScrollTrigger);

async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}

function toast(msg, err = false) {
  const t = document.createElement('div');
  t.className = 'toast' + (err ? ' err' : '');
  t.textContent = msg;
  $('#toasts').appendChild(t);
  gsap.from(t, { x: 60, opacity: 0, duration: 0.35, ease: 'back.out(2)' });
  setTimeout(() => gsap.to(t, { opacity: 0, y: 10, duration: 0.3, onComplete: () => t.remove() }), err ? 6500 : 3800);
}

const state = { status: null, companies: [], filter: 'all', queue: [], label: 'good', touched: false, polling: null };

/* ---------------------------------------------------------------- scroll progress + hero */
gsap.to('#progress i', { scaleX: 1, ease: 'none', scrollTrigger: { trigger: document.body, start: 'top top', end: 'bottom bottom', scrub: 0.3 } });

if (!reduce) {
  const tl = gsap.timeline({ defaults: { ease: 'power3.out' } });
  tl.from('.nav', { y: -30, opacity: 0, duration: 0.6 })
    .from('.hero-frame', { y: 40, opacity: 0, duration: 0.8 }, '-=0.3')
    .from('.eyebrow, .hero-left h1, .lede, .cta-row, .mini-stats', { y: 22, opacity: 0, stagger: 0.09, duration: 0.6 }, '-=0.45')
    .from('.orb', { scale: 0.6, opacity: 0, duration: 1.1, ease: 'power2.out' }, '-=0.9')
    .from('.hero-right .gl', { y: 40, opacity: 0, scale: 0.94, stagger: 0.12, duration: 0.7 }, '-=0.8');
  gsap.to('.orb', { scale: 1.06, rotate: 40, duration: 12, repeat: -1, yoyo: true, ease: 'sine.inOut' });
  gsap.to('.ring', { rotate: 360, duration: 90, repeat: -1, ease: 'none', transformOrigin: '50% 50%' });
  gsap.to('.b1', { x: 140, y: 60, duration: 16, repeat: -1, yoyo: true, ease: 'sine.inOut' });
  gsap.to('.b2', { x: -120, y: -80, duration: 19, repeat: -1, yoyo: true, ease: 'sine.inOut' });
  gsap.to('.b3', { x: 90, y: -50, duration: 22, repeat: -1, yoyo: true, ease: 'sine.inOut' });
  gsap.utils.toArray('.band .h2').forEach((h) => gsap.from(h, { y: 40, opacity: 0, duration: 0.6, scrollTrigger: { trigger: h, start: 'top 85%' } }));
}

/* ---------------------------------------------------------------- pipeline panels */
const STAGES = [
  ['discover', 'Discover', 'Reads your list or searches the ICP. Every domain is verified against its own homepage.', 'ink'],
  ['news', 'News + RSS', 'Google News and RSS go first. A recent breach, layoffs or lawsuit vetoes the company before we scrape it, and one lookup tries to fill a missing funding round.', 'purple'],
  ['scrape', 'Scrape', 'Our own scraper does the heavy lifting: site, jobs, trust and about pages. Obeys robots.txt, 8 fetches per company at most.', 'plum'],
  ['extract', 'Extract', 'Copy-only facts. If the snippet is not on the page, the fact is dropped.', 'purple'],
  ['qualify', 'Qualify', 'Rules in code: hard gates, then a score. Unknown stays unknown.', 'ink'],
  ['poc', 'Contact', 'Hunter first, then your Apollo list. Guessed emails are never sent.', 'plum'],
  ['draft', 'Draft + check', 'Gemini sees verified facts only and picks the fitting Sprinto claim. Code blocks any unsupported sentence.', 'purple'],
  ['review', 'Review', 'You approve, edit or discard. A human presses send.', 'plum'],
];
const STAGE_GROUP = { discover: ['discover'], news: ['news'], scrape: ['scrape', 'extract'], qualify: ['qualify'], poc: ['poc'], draft: ['draft'], review: ['review'] };

$('#panels').innerHTML = STAGES.map(([k, t, d, c], i) =>
  `<div class="panel pn" data-k="${k}"><span class="chip-num ${c}">${String(i + 1).padStart(2, '0')}</span><h3>${t}</h3><p>${d}</p></div>`).join('');
if (!reduce) {
  ScrollTrigger.batch('.pn', { start: 'top 88%', onEnter: (els) => gsap.from(els, { y: 50, opacity: 0, rotate: () => gsap.utils.random(-3, 3), stagger: 0.08, duration: 0.55, ease: 'back.out(1.6)' }) });
}

function setStage(stage, finished) {
  const order = STAGES.map((s) => s[0]);
  const active = STAGE_GROUP[stage] || [];
  const idx = Math.max(...active.map((k) => order.indexOf(k)), -1);
  $$('.pn').forEach((p) => {
    const i = order.indexOf(p.dataset.k);
    p.classList.toggle('active', !finished && active.includes(p.dataset.k));
    p.classList.toggle('done', finished || i < Math.min(...active.map((k) => order.indexOf(k)), 99));
  });
  const pulse = $(`.pn.active`);
  if (pulse && !reduce) gsap.fromTo(pulse, { scale: 1.04 }, { scale: 1, duration: 0.5, ease: 'back.out(3)' });
  $('#run-bar').style.width = finished ? '100%' : `${Math.max(4, ((idx + 1) / order.length) * 100)}%`;
}

/* ---------------------------------------------------------------- status */
async function loadStatus() {
  const s = (state.status = await api('/api/status'));
  const mode = $('#mode-chip');
  mode.textContent = s.mode; mode.classList.toggle('live', s.mode === 'LIVE');
  $('#foot-mode').textContent = `mode ${s.mode} · ${s.my_email_set ? 'test inbox set' : 'MY_EMAIL missing'}`;
  $('#demo-banner').hidden = !s.demo;
  const chip = (ok, label) => `<span class="chip ${ok ? 'ok' : 'bad'}"><i></i>${label}</span>`;
  $('#status-chips').innerHTML = [
    chip(s.gemini_ready, 'Gemini'), chip(s.hunter_ready, `Hunter · ${s.hunter_used}/50 used`),
    chip(s.gmail_ready, 'Gmail'), chip(s.my_email_set, `Mode ${s.mode}`)].join('');
  $('#send-note').textContent = s.mode === 'TEST'
    ? 'TEST mode: sends to your own inbox, subject starts with [TEST], and the intended recipient is shown in a banner.'
    : 'LIVE mode: creates a Gmail draft with the real recipient. You open it and press send.';
  const reviewed = Object.entries(s.drafts).filter(([k]) => !['pending_review', 'blocked'].includes(k)).reduce((a, [, n]) => a + n, 0);
  state.stats = { companies: s.companies, qualified: state.companies.filter((c) => c.passes).length, reviewed };
  paintStats(); paintHero();
  if (s.run.running && !state.polling) startPolling();
}

/* Hero data cards: live numbers from the API only, nothing invented. */
function paintHero() {
  const st = state.stats || {}, q = state.queue || [], s = state.status;
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('#hs-companies', st.companies ?? '–'); set('#hs-qualified', st.qualified ?? '–'); set('#hs-reviewed', st.reviewed ?? '–');
  set('#h-wait', s ? (s.drafts.pending_review || 0) : '–');
  const top = q.find((d) => d.status === 'pending_review');
  if (top) {
    set('#h-subject', top.subject || 'Draft');
    const hook = (top.sentences || []).find((x) => x.type === 'hook');
    set('#h-hook', hook ? hook.text : top.company);
    $('#h-cites').innerHTML = [...new Set((top.sentences || []).flatMap((x) => x.fact_ids))].slice(0, 5).map((id) => `<i>${esc(id)}</i>`).join('');
  } else {
    set('#h-subject', 'No drafts waiting'); set('#h-hook', 'Start a run and the first email appears here.'); $('#h-cites').innerHTML = '';
  }
}

let statsShown = false;
function paintStats() {
  const st = state.stats || {};
  $$('.count').forEach((el) => {
    const to = st[el.dataset.k] || 0;
    if (reduce || !statsShown) { el.textContent = to; if (!reduce) { el._to = to; } }
    gsap.to(el, { innerText: to, duration: 1.1, snap: { innerText: 1 }, ease: 'power2.out',
      scrollTrigger: statsShown ? undefined : { trigger: el, start: 'top 90%', once: true } });
  });
  statsShown = true;
}

/* ---------------------------------------------------------------- run */
$('#run-btn').addEventListener('click', async () => {
  const body = { inputs: $('#inputs').value, min_companies: +$('#min').value || 10, limit: $('#limit').value ? +$('#limit').value : null };
  try {
    await api('/api/run', { method: 'POST', body });
    $('#log').textContent = 'Starting…';
    startPolling();
  } catch (e) { toast(e.message, true); }
});

function startPolling() {
  $('#run-btn').disabled = true;
  clearInterval(state.polling);
  const tick = async () => {
    const r = await api('/api/run');
    $('#run-stage').textContent = r.running ? `running · ${r.stage || ''}` : (r.error ? 'failed' : 'done');
    $('#run-found').textContent = r.found ? `${r.found} companies` : '';
    const log = $('#log');
    log.textContent = r.log.join('\n') + (r.error ? `\n\nERROR: ${r.error}` : '');
    log.scrollTop = log.scrollHeight;
    setStage(r.stage || 'discover', !r.running && !r.error);
    if (!r.running) {
      clearInterval(state.polling); state.polling = null;
      $('#run-btn').disabled = false;
      if (r.error) toast(r.error, true);
      else {
        const notes = (r.summary && r.summary.notes) || [];
        toast(notes.length ? notes[notes.length - 1] : 'Run finished.');
        await refreshAll();
        $('#companies').scrollIntoView({ behavior: 'smooth' });
      }
    }
  };
  state.polling = setInterval(tick, 1500); tick();
}

/* ---------------------------------------------------------------- companies */
const DEC = { GO: ['GO', 'go'], GO_FLAGGED: ['GO · FLAGGED', 'flag'], NO_GO: ['NO-GO', 'no'], INSUFFICIENT_DATA: ['NEEDS DATA', 'ins'] };

async function loadCompanies() {
  state.companies = await api('/api/companies');
  renderCards();
}
function renderCards() {
  const f = state.filter;
  const list = state.companies.filter((c) => f === 'all' || (f === 'qualified' ? c.passes : !c.passes));
  $('#cards-empty').hidden = state.companies.length > 0;
  $('#cards').innerHTML = list.map((c) => {
    const [label, cls] = DEC[c.decision] || ['PENDING', 'ins'];
    return `<article class="panel card" data-d="${esc(c.domain)}">
      <div class="score">${c.score ?? '–'}</div>
      <h4>${esc(c.name)}</h4><div class="dom">${esc(c.domain)}</div>
      <span class="stamp ${cls}">${label}</span>
      <div class="why">${esc(c.top_reason || 'Not qualified yet.')}</div>
      <div class="tags">${(c.unknowns || []).map((u) => `<span class="tag unk" title="Unverified, so it never appears in the email">? ${esc(u)}</span>`).join('')}<span class="tag">${c.n_facts} facts</span></div>
      <div class="who">${c.poc && c.poc.name ? `${esc(c.poc.name)} · ${esc(c.poc.title || '')}` : '<span class="muted">No contact yet</span>'}</div>
      <div class="card-foot"><span class="fine">${c.draft_status ? 'draft: ' + esc(c.draft_status.replace('_', ' ')) : 'no draft'}</span><button class="btn btn-white" data-trace="${esc(c.domain)}">Trace</button></div>
    </article>`;
  }).join('');
  if (!reduce) gsap.from('#cards .card', { y: 40, opacity: 0, rotate: () => gsap.utils.random(-2, 2), stagger: 0.07, duration: 0.5, ease: 'back.out(1.5)', clearProps: 'all' });
}
$('#filters').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  state.filter = b.dataset.f;
  $$('#filters button').forEach((x) => x.classList.toggle('on', x === b));
  renderCards();
});
document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-trace]'); if (t) openTrace(t.dataset.trace);
  const c = e.target.closest('.cite'); if (c) { e.preventDefault(); openTrace(c.dataset.dom); }
});

/* ---------------------------------------------------------------- trace drawer */
const factHtml = (f) => `<div class="fact"><b>${esc(f.id)}</b> ${esc(f.field)} = ${esc(f.value)}${f.snippet ? `<q>${esc(f.snippet)}</q>` : ''}${/^https?:/.test(f.source_url || '') ? `<a href="${esc(f.source_url)}" target="_blank" rel="noopener">${esc(f.source_url)}</a>` : `<span class="mono"> ${esc(f.source_url)}</span>`}</div>`;

async function openTrace(domain) {
  let d;
  try { d = await api('/api/company/' + encodeURIComponent(domain)); } catch (e) { return toast(e.message, true); }
  const q = d.qualification, p = d.poc;
  $('#drawer-body').innerHTML = `
    <h3 style="font-size:30px;margin-top:6px">${esc(domain)}</h3>
    <div class="fine">${esc(d.origin)}${d.source_url ? ' · <a target="_blank" rel="noopener" href="' + esc(d.source_url) + '">source</a>' : ''}</div>
    <div class="sec">Why this company</div>
    ${q ? `<div class="trace"><b>${esc(q.decision)}</b> · score ${q.score} · confidence ${esc(q.confidence)} · rubric v${q.rubric_version}
       ${q.unknowns.length ? `<div class="fine">Unknown, flagged, never used in the email: ${q.unknowns.map(esc).join(', ')}</div>` : ''}</div>
       ${q.reasons.map((r) => `<div class="trace">${esc(r.text)}${r.facts.map(factHtml).join('')}</div>`).join('')}` : '<div class="trace">Not qualified yet.</div>'}
    <div class="sec">Why this person</div>
    ${p ? `<div class="trace"><b>${esc(p.name || 'No named contact')}</b> ${esc(p.title || '')}<br>${esc(p.email || '')} <span class="tag">${esc(p.email_status)}</span> <span class="tag">${esc(p.source || '')}</span>
       <div class="fine">${esc(p.reason)}</div>${p.guessed_email ? `<div class="fine">Unverified suggestion for a human only: ${esc(p.guessed_email)}</div>` : ''}</div>` : '<div class="trace">No contact search yet.</div>'}
    <div class="sec">Why the email says what it says</div>
    ${d.drafts.length ? d.drafts.map((x) => `<div class="trace"><b>Draft ${x.id}</b> · ${esc(x.status)} · rubric v${x.rubric_version} · checker ${x.check.passed ? 'PASS' : 'FAIL'}
       ${(x.check.failures || []).map((f) => `<div class="fail-list">${esc(f)}</div>`).join('')}
       <pre style="white-space:pre-wrap;font:14px var(--body);margin:10px 0">${esc(x.subject)}\n\n${esc(x.body)}</pre>
       ${x.pain_points.map((pp) => `<div class="fine"><b>Pain hypothesis:</b> ${esc(pp.text)} — ${esc(pp.rationale)}</div>${pp.facts.map(factHtml).join('')}`).join('')}
       ${x.sentences.map((s) => `<div style="margin-top:8px"><span class="tag">${esc(s.type)}</span> ${esc(s.text)}${s.facts.map(factHtml).join('')}</div>`).join('')}
       ${x.gmail_id ? `<div class="fine">Delivered: ${esc(x.sent_mode)} · ${esc(x.gmail_id)}</div>` : ''}</div>`).join('') : '<div class="trace">No draft yet.</div>'}
    <div class="sec">Pipeline stages</div>
    <div class="trace">${d.stages.map((s) => `<span class="tag">${esc(s.stage)}: ${esc(s.status)}</span>`).join(' ') || '–'}</div>`;
  $('#veil').hidden = false;
  gsap.to('#drawer', { x: 0, duration: reduce ? 0 : 0.45, ease: 'power3.out' });
  if (!reduce) gsap.from('#drawer-body > *', { y: 18, opacity: 0, stagger: 0.05, duration: 0.4, delay: 0.15 });
  $('#drawer').setAttribute('aria-hidden', 'false');
}
function closeTrace() {
  gsap.to('#drawer', { x: '105%', duration: reduce ? 0 : 0.35, ease: 'power3.in', onComplete: () => { $('#veil').hidden = true; } });
  $('#drawer').setAttribute('aria-hidden', 'true');
}
$('#veil').addEventListener('click', closeTrace);
$('#drawer-x').addEventListener('click', closeTrace);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeTrace(); $('#modal').hidden = true; } });
gsap.set('#drawer', { x: '105%' });

/* ---------------------------------------------------------------- review: card swap */
async function loadQueue() { state.queue = await api('/api/review-queue'); renderReview(); }

function bodyHtml(d) {
  const parts = d.body.split('\n\n');
  const mid = parts.slice(1, -1).map((para) => {
    let h = esc(para);
    d.sentences.forEach((s) => {
      const t = esc(s.text);
      if (h.includes(t)) h = h.replace(t, t + s.fact_ids.map((id) => `<a class="cite ${id.startsWith('S') ? 's' : ''}" href="#" data-dom="${esc(d.domain)}" title="${esc(id)}">${esc(id)}</a>`).join(''));
    });
    return `<p>${h}</p>`;
  }).join('');
  return `<div class="subj">${esc(d.subject)}</div><p>${esc(parts[0])}</p>${mid}<p style="white-space:pre-line">${esc(parts[parts.length - 1])}</p>`;
}

function draftCard(d) {
  const poc = d.poc || {};
  return `<article class="panel draft" data-id="${d.id}">
    <span class="chip-num plum">#${d.id}</span>
    <div class="draft-head"><div><h4>${esc(d.company)}</h4><div class="dom">${esc(d.domain)} · score ${d.score ?? '–'} · ${esc(d.decision || '')}</div></div>
      <span class="${d.check.passed ? 'pass' : 'fail'}">${d.check.passed ? '✓ CLAIMS CHECKED' : '✗ BLOCKED'}</span></div>
    <div class="to"><b>To:</b> ${esc(poc.name || '—')} · ${esc(poc.title || '')} &lt;${esc(poc.email || 'no email')}&gt; <span class="tag">${esc(poc.email_status || '')}</span></div>
    <div class="bubble">${bodyHtml(d)}</div>
    <div class="draft-why">${(d.reasons || []).map(esc).join(' · ')}<br><span class="fine">Purple tags are the facts each sentence cites. Click one to see the source.</span></div>
  </article>`;
}

function layoutStack(animate) {
  const cards = $$('#stack .draft');
  cards.forEach((c, i) => {
    const to = { y: i * 16, scale: 1 - i * 0.035, zIndex: 10 - i, opacity: i < 3 ? 1 : 0, rotate: i === 0 ? 0 : (i % 2 ? 1.2 : -1.2) };
    animate && !reduce ? gsap.to(c, { ...to, duration: 0.5, ease: 'back.out(1.4)' }) : gsap.set(c, to);
  });
}

function renderReview() {
  paintHero();
  const pending = state.queue.filter((d) => d.status === 'pending_review');
  const approved = state.queue.filter((d) => d.status === 'approved');
  const blocked = state.queue.filter((d) => d.status === 'blocked');
  $('#queue-count').textContent = `${pending.length} waiting`;
  $('#stack').innerHTML = pending.slice(0, 3).map(draftCard).join('');
  $('#stack-empty').hidden = pending.length > 0; $('#decide').hidden = pending.length === 0;
  $('#stack').style.display = pending.length ? '' : 'none';
  layoutStack(false);
  if (pending.length && !reduce) gsap.from('#stack .draft', { y: 80, opacity: 0, rotate: () => gsap.utils.random(-6, 6), stagger: 0.12, duration: 0.6, ease: 'back.out(1.6)', clearProps: 'rotate' });
  $('#approved').innerHTML = approved.length ? approved.map((d) =>
    `<div class="item"><div><b>${esc(d.company)}</b><small>${esc((d.poc || {}).name || '')} · ${esc((d.poc || {}).email_status || '')}</small></div><button class="btn btn-ink" data-send="${d.id}">${state.status && state.status.mode === 'LIVE' ? 'Create draft' : 'Send test'}</button></div>`).join('')
    : '<p class="fine" style="margin-top:10px">Approved drafts show up here.</p>';
  $('#blocked-tray').hidden = blocked.length === 0;
  $('#blocked').innerHTML = blocked.map((d) => `<div class="item" style="display:block"><b>${esc(d.company)}</b><div class="fail-list">${(d.check.failures || []).map(esc).join('<br>')}</div></div>`).join('');
}

function resetDecide() {
  state.label = 'good'; state.touched = false;
  $$('#label-seg button').forEach((b) => b.classList.toggle('on', b.dataset.l === 'good'));
  $('#tag').value = 'other'; $('#note').value = '';
}
$('#label-seg').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  state.label = b.dataset.l; state.touched = true;
  $$('#label-seg button').forEach((x) => x.classList.toggle('on', x === b));
});

function topDraft() { return state.queue.find((d) => d.status === 'pending_review'); }

function swapOut(dir) {
  const top = $('#stack .draft:first-child');
  if (reduce || !top) return Promise.resolve();
  return new Promise((res) => {
    gsap.timeline({ onComplete: res })
      .to(top, { x: dir * 760, rotate: dir * 16, opacity: 0, duration: 0.5, ease: 'power3.in' })
      .add(() => $$('#stack .draft').slice(1).forEach((c, i) => gsap.to(c, { y: i * 16, scale: 1 - i * 0.035, rotate: 0, duration: 0.4, ease: 'back.out(1.6)' })), '-=0.25');
  });
}

async function decide(action, finalText) {
  const d = topDraft(); if (!d) return;
  const label = action === 'discard' && !state.touched ? 'bad' : state.label;
  const body = { action, label, reason_tag: $('#tag').value, note: $('#note').value, final_text: finalText || null };
  try {
    await Promise.all([api(`/api/drafts/${d.id}/review`, { method: 'POST', body }), swapOut(action === 'discard' ? -1 : 1)]);
    toast(action === 'discard' ? 'Discarded.' : 'Approved. It is in the Ready to send tray.');
    resetDecide(); await loadQueue(); loadStatus();
  } catch (e) { toast(e.message, true); loadQueue(); }
}
$('#b-approve').addEventListener('click', () => decide('approve'));
$('#b-discard').addEventListener('click', () => decide('discard'));
$('#b-skip').addEventListener('click', () => {
  const pending = state.queue.filter((d) => d.status === 'pending_review'); if (pending.length < 2) return;
  const top = $('#stack .draft:first-child');
  const next = () => { const first = state.queue.findIndex((d) => d.status === 'pending_review'); state.queue.push(state.queue.splice(first, 1)[0]); renderReview(); };
  reduce ? next() : gsap.to(top, { x: -80, y: 40, rotate: -6, duration: 0.3, ease: 'power2.in', onComplete: next });
});
$('#b-edit').addEventListener('click', () => { const d = topDraft(); if (!d) return; $('#edit-text').value = d.body; $('#modal').hidden = false; if (!reduce) gsap.from('.modal-card', { scale: 0.85, rotate: -2, opacity: 0, duration: 0.35, ease: 'back.out(2)' }); });
$('#edit-cancel').addEventListener('click', () => { $('#modal').hidden = true; });
$('#edit-save').addEventListener('click', async () => { const t = $('#edit-text').value; $('#modal').hidden = true; await decide('edit', t); });

document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-send]'); if (!b) return;
  const live = state.status && state.status.mode === 'LIVE';
  if (!confirm(live ? 'Create a Gmail draft addressed to the real recipient? You will press send yourself.' : 'Send this as a TEST email to your own inbox?')) return;
  b.disabled = true;
  try {
    const r = await api(`/api/drafts/${b.dataset.send}/send`, { method: 'POST' });
    toast(live ? 'Gmail draft created.' : 'Test email sent to your inbox.');
    if (!reduce) gsap.to(b.closest('.item'), { x: 80, opacity: 0, duration: 0.4 });
    await loadQueue(); loadStatus();
  } catch (err) { toast(err.message, true); b.disabled = false; }
});

/* ---------------------------------------------------------------- boot */
async function refreshAll() { await loadCompanies(); await Promise.all([loadStatus(), loadQueue()]); }
(async () => {
  try {
    await loadCompanies();
    const s = await api('/api/status');
    $('#tag').innerHTML = s.reason_tags.map((t) => `<option value="${t}"${t === 'other' ? ' selected' : ''}>${t.replace('_', ' ')}</option>`).join('');
    await Promise.all([loadStatus(), loadQueue()]);
  } catch (e) { toast('Could not reach the local API: ' + e.message, true); }
})();
