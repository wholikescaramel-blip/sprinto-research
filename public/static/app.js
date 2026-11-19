/* Outreach Desk UI. Vanilla JS + GSAP (cdnjs). All data comes from the local FastAPI backend. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
gsap.registerPlugin(ScrollTrigger);

/* Hosted demo (Vercel): window.DEMO_STATIC is set by the build. Reads come from static JSON snapshots of the
   fictional demo data; approve/discard are applied in the browser only; send and run are disabled. */
const DEMO = window.DEMO_STATIC === true;
const demoOverlay = {}; // draft id -> 'approved' | 'discarded' (lives in this tab only)
async function demoJson(file) {
  const r = await fetch('/data/' + file);
  if (!r.ok) throw new Error('Not available in the demo');
  return r.json();
}
async function demoApi(path, opts = {}) {
  if ((opts.method || 'GET') !== 'GET') {
    const m = path.match(/^\/api\/drafts\/(\d+)\/review$/);
    if (m) {
      const b = opts.body || {};
      if (!['approve', 'edit', 'discard'].includes(b.action) || !['good', 'bad'].includes(b.label)) throw new Error('Invalid review');
      demoOverlay[m[1]] = b.action === 'discard' ? 'discarded' : 'approved';
      return { ok: true };
    }
    if (/\/send$/.test(path)) throw new Error('Demo is read-only: nothing is sent. Run the app locally to send a TEST email to your own inbox.');
    if (path === '/api/run') throw new Error('Demo is read-only. Run the pipeline locally on real companies (see the README).');
    throw new Error('Not available in the demo');
  }
  if (path === '/api/run') return { running: false, log: [], stage: null, found: 0, summary: null, error: null };
  if (path === '/api/companies') return demoJson('companies.json');
  if (path.startsWith('/api/company/')) return demoJson('company/' + decodeURIComponent(path.split('/').pop()) + '.json');
  const base = await demoJson('review-queue.json');
  if (path === '/api/review-queue') {
    return base.filter((d) => demoOverlay[d.id] !== 'discarded').map((d) => (demoOverlay[d.id] === 'approved' ? { ...d, status: 'approved' } : d));
  }
  if (path === '/api/status') {
    const s = await demoJson('status.json'), counts = {};
    base.forEach((d) => { const st = demoOverlay[d.id] === 'discarded' ? 'discarded' : demoOverlay[d.id] === 'approved' ? 'approved' : d.status; counts[st] = (counts[st] || 0) + 1; });
    return { ...s, drafts: counts };
  }
  throw new Error('Not available in the demo');
}

async function api(path, opts = {}) {
  if (DEMO) return demoApi(path, opts);
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

const state = { status: null, companies: [], filter: 'qualified', filterTouched: false, queue: [], label: 'good', touched: false, polling: null };

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
  $('#foot-mode').textContent = s.readonly ? 'read-only demo · fictional data' : `mode ${s.mode} · ${s.my_email_set ? 'test inbox set' : 'MY_EMAIL missing'}`;
  $('#demo-banner').hidden = !s.demo;
  const sb = $('#samples-bar');
  if (s.samples_loaded) {
    sb.hidden = false;
    sb.innerHTML = 'Showing <b>sample results</b>: real pipeline output captured earlier (contact emails masked). <button class="btn" id="clear-samples">Clear samples</button>';
  } else if (s.samples_available) {
    sb.hidden = false;
    sb.innerHTML = 'Want to review every stage without waiting for a live run? <button class="btn btn-solid" id="load-samples">Load sample results</button> <span class="fine">Real captured pipeline output, not invented.</span>';
  } else { sb.hidden = true; }
  const chip = (ok, label) => `<span class="chip ${ok ? 'ok' : 'bad'}"><i></i>${label}</span>`;
  $('#status-chips').innerHTML = (s.readonly
    ? [chip(true, 'Read-only demo'), chip(true, 'Fictional data'), chip(true, `Mode ${s.mode}`)]
    : s.hosted
      ? [chip(true, 'Live app'), chip(s.gemini_ready, 'Gemini'), chip(s.hunter_ready, `Hunter · ${s.hunter_used}/50 used`),
         chip(true, 'Shared database'), chip(true, 'Test sends only')]
      : [chip(s.gemini_ready, 'Gemini'), chip(s.hunter_ready, `Hunter · ${s.hunter_used}/50 used`),
         chip(s.mail_ready, 'Resend'), chip(s.my_email_set, `Mode ${s.mode}`)]).join('');
  $('#send-note').textContent = s.readonly
    ? 'Demo: Send is disabled here. In the real app, TEST mode sends to your own inbox with a [TEST] subject.'
    : s.hosted
      ? 'Public app: it can only send a [TEST] copy to the owner\'s inbox. Copy the email or download it to send it yourself. Contact addresses are masked here.'
      : s.mode === 'TEST'
      ? 'TEST mode: sends to your own inbox, subject starts with [TEST], and the intended recipient is shown in a banner.'
      : 'LIVE mode: sends to the real recipient through Resend, only for drafts you approved. Each send asks you to confirm.';
  if (s.readonly) {
    $('#run-btn').disabled = true;
    $('#run-note').textContent = 'This hosted demo is read-only. Run the pipeline locally on real companies (see the README).';
    $('#log').textContent = 'Demo mode: runs are disabled here. Browse the companies, open a Trace, and try Approve / Discard in Review.';
  }
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
  if (state.status && state.status.hosted) return runHosted(body);
  try {
    await api('/api/run', { method: 'POST', body });
    $('#log').textContent = 'Starting…';
    startPolling();
  } catch (e) { toast(e.message, true); }
});

/* Hosted app: serverless functions cannot run for minutes, so THIS page drives the run one company at a time.
   Each request is one step (discover -> research -> draft) and stays well under a function's time limit. */
async function runHosted(body) {
  const logEl = $('#log');
  const log = (m) => { logEl.textContent += (logEl.textContent ? '\n' : '') + m; logEl.scrollTop = logEl.scrollHeight; };
  const target = Math.min(Math.max(+body.min_companies || 3, 1), 5);   // good fits wanted (a public run stays small)
  const maxLook = target * 4;                                          // never research more than this to find them
  logEl.textContent = ''; $('#run-btn').disabled = true; $('#run-stage').textContent = 'running'; $('#run-found').textContent = '';
  const seen = new Set(), notes = new Set(), failures = [];
  let good = 0;
  const note = (n) => { if (!notes.has(n)) { notes.add(n); log(`note: ${n}`); } };
  const update = () => { $('#run-found').textContent = `${good} good fit${good === 1 ? '' : 's'} · ${seen.size} researched`; };

  // One company: research it (news, then scraper); only a good fit gets a contact lookup and a draft.
  const processCompany = async (domain) => {
    if (seen.has(domain)) return;
    seen.add(domain); update();
    try {
      setStage('news'); log(`Researching ${domain} (news first, then the scraper)…`);
      const r = await api('/api/step/research', { method: 'POST', body: { domain } });
      if (!r.ok) { log(`  skipped: ${r.note}`); return; }
      log(`  ${r.decision} (score ${r.score})${r.passes ? '' : ' - not a good fit, no email'}`);
      if (!r.passes) return;
      good++; update();
      setStage('poc'); log('  good fit: finding the contact and drafting…');
      try {
        const d = await api('/api/step/draft', { method: 'POST', body: { domain } });
        log(d.draft_id ? `  draft ${d.draft_id}: ${d.draft_status}${d.poc_status === 'no_named_poc' ? ' (no contact found yet, addressed "Hi there")' : ''}` : '  draft not created');
      } catch (e) { log(`  draft failed: ${e.message}. Use "Draft email" on its card to retry.`); }
    } catch (e) { failures.push(domain); log(`  error on ${domain}: ${e.message}`); if (/Daily limit/.test(e.message)) throw e; }
  };

  try {
    setStage('discover');
    if (body.inputs.trim()) {
      // You named the companies: research exactly those (up to 8), good fit or not.
      log('Resolving the companies you named…');
      const r = await api('/api/step/discover', { method: 'POST', body: { inputs: body.inputs, query_index: 0 } });
      (r.notes || []).forEach(note);
      for (const c of r.companies.slice(0, 8)) await processCompany(c.domain);
    } else {
      // Discovery: look a few candidates at a time and stop as soon as we have enough good fits.
      for (let qi = 0, calls = 0; qi !== null && good < target && seen.size < maxLook && calls < 6; calls++) {
        setStage('discover'); log(`Searching for candidates (query ${qi + 1})…`);
        const r = await api('/api/step/discover', { method: 'POST', body: { inputs: '', query_index: qi } });
        (r.notes || []).forEach(note);
        qi = r.next_query;
        for (const c of r.companies) { if (good >= target || seen.size >= maxLook) break; await processCompany(c.domain); }
      }
    }
    setStage('review', true);
    $('#run-stage').textContent = 'done';
    log(good >= target || body.inputs.trim()
      ? `Done: ${good} good fit${good === 1 ? '' : 's'} after researching ${seen.size}${failures.length ? `, ${failures.length} failed` : ''}.`
      : `Done: found ${good} of the ${target} good fits you asked for (researched ${seen.size}). Not padded with poor fits.`);
    await refreshAll();
    toast(good ? `${good} good fit${good === 1 ? '' : 's'} found. Drafts are in Review.` : 'Run finished: no good fits this time.');
    $('#companies').scrollIntoView({ behavior: 'smooth' });
  } catch (e) {
    $('#run-stage').textContent = 'failed'; log(`ERROR: ${e.message}`); toast(e.message, true);
  } finally { $('#run-btn').disabled = false; }
}

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

function setFilter(f) {
  state.filter = f;
  $$('#filters button').forEach((x) => x.classList.toggle('on', x.dataset.f === f));
}
async function loadCompanies() {
  state.companies = await api('/api/companies');
  // by default show only good fits; if there are none yet, show everything instead of an empty page
  if (!state.filterTouched) setFilter(state.companies.some((c) => c.passes) ? 'qualified' : 'all');
  renderCards();
}
function renderCards() {
  const f = state.filter;
  const list = state.companies.filter((c) => f === 'all' || (f === 'qualified' ? c.passes : !c.passes));
  const empty = $('#cards-empty');
  empty.hidden = list.length > 0;
  empty.textContent = state.companies.length
    ? (f === 'qualified' ? 'No good fits yet. Switch to All or Rejected to see the others.' : 'Nothing here.')
    : 'No companies yet. Start a run above, or load the sample results.';
  $('#cards').innerHTML = list.map((c) => {
    const [label, cls] = DEC[c.decision] || ['PENDING', 'ins'];
    const draftLine = c.draft_status ? 'draft: ' + esc(c.draft_status.replace('_', ' ')) : (c.can_draft ? 'no draft yet' : 'no draft');
    return `<article class="panel card" data-d="${esc(c.domain)}">
      <div class="score">${c.score ?? '–'}</div>
      <h4>${esc(c.name)}</h4><div class="dom">${esc(c.domain)}</div>
      <span class="stamp ${cls}">${label}</span>${c.origin === 'sample' ? ' <span class="tag" title="Real pipeline output captured earlier, loaded for review">sample</span>' : ''}
      <div class="why">${esc(c.top_reason || 'Not qualified yet.')}</div>
      <div class="tags">${(c.unknowns || []).map((u) => `<span class="tag unk" title="Unverified, so it never appears in the email">? ${esc(u)}</span>`).join('')}<span class="tag">${c.n_facts} facts</span></div>
      <div class="who">${c.poc && c.poc.name ? `${esc(c.poc.name)} · ${esc(c.poc.title || '')}` : '<span class="muted">No contact yet</span>'}</div>
      <div class="card-foot"><span class="fine">${draftLine}</span><span class="item-actions">
        ${c.can_unflag ? `<button class="btn" data-act="unflag" data-d="${esc(c.domain)}" title="I checked the unknowns by hand: mark it GO and (re)draft">Unflag</button>` : ''}
        ${c.can_draft ? `<button class="btn" data-act="draft" data-d="${esc(c.domain)}">Draft email</button>` : ''}
        <button class="btn btn-white" data-trace="${esc(c.domain)}">Trace</button></span></div>
    </article>`;
  }).join('');
  if (!reduce) gsap.from('#cards .card', { y: 40, opacity: 0, rotate: () => gsap.utils.random(-2, 2), stagger: 0.07, duration: 0.5, ease: 'back.out(1.5)', clearProps: 'all' });
}
$('#filters').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  state.filterTouched = true;
  setFilter(b.dataset.f);
  renderCards();
});

/* Per-company actions: Draft email (retry / first draft) and Unflag (a human checked the unknowns by hand). */
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act]'); if (!b) return;
  const act = b.dataset.act, d = b.dataset.d;
  if (act === 'unflag' && !confirm(`Unflag ${d}? Only do this after you checked its unknowns yourself. It becomes GO and its draft is regenerated.`)) return;
  const label = b.textContent; b.disabled = true; b.textContent = act === 'draft' ? 'Drafting…' : 'Unflagging…';
  try {
    const r = await api(`/api/company/${encodeURIComponent(d)}/${act}`, { method: 'POST' });
    if (act === 'unflag') toast(r.draft_error ? r.draft_error : 'Unflagged. A fresh draft is in Review.', !!r.draft_error);
    else toast(`Draft ${r.draft_status === 'blocked' ? 'was blocked by the claim checker' : 'is in Review'}.`);
    await refreshAll();
  } catch (err) { toast(err.message, true); b.disabled = false; b.textContent = label; }
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
       ${x.message_id ? `<div class="fine">Delivered: ${esc(x.sent_mode)} · ${esc(x.message_id)}</div>` : ''}</div>`).join('') : '<div class="trace">No draft yet.</div>'}
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
    `<div class="item"><div><b>${esc(d.company)}</b><small>${esc((d.poc || {}).name || '')} · ${esc((d.poc || {}).email_status || '')}</small></div><span class="item-actions"><button class="btn" data-copy="${d.id}">Copy</button>${state.status && state.status.hosted ? `<button class="btn" data-dl="${d.id}">.eml</button>` : ''}<button class="btn btn-ink" data-send="${d.id}">${state.status && state.status.mode === 'LIVE' ? 'Send live' : 'Send test'}</button></span></div>`).join('')
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

/* Copy / download an approved email (works everywhere; the only route on the public app to send it yourself). */
document.addEventListener('click', async (e) => {
  const c = e.target.closest('[data-copy]'), dl = e.target.closest('[data-dl]');
  const id = (c || dl) && +((c || dl).dataset.copy || (c || dl).dataset.dl);
  if (!id) return;
  const d = state.queue.find((x) => x.id === id); if (!d) return;
  if (c) {
    try { await navigator.clipboard.writeText(`Subject: ${d.subject}\n\n${d.body}`); toast('Email copied.'); }
    catch { toast('Could not copy; select the text in the draft instead.', true); }
  } else {
    // no To: header on purpose: the public app shows contact addresses masked
    const eml = `Subject: ${d.subject}\nContent-Type: text/plain; charset=utf-8\n\n${d.body}\n`;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([eml], { type: 'message/rfc822' }));
    a.download = `${d.domain}.eml`; a.click(); URL.revokeObjectURL(a.href);
  }
});

document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-send]'); if (!b) return;
  const live = state.status && state.status.mode === 'LIVE';
  if (!confirm(live ? 'LIVE: this sends a real email to the recipient now. Send it?' : 'Send this as a TEST email to your own inbox?')) return;
  b.disabled = true;
  try {
    const r = await api(`/api/drafts/${b.dataset.send}/send`, { method: 'POST' });
    toast(live ? 'Email sent to the recipient.' : 'Test email sent to your inbox.');
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

document.addEventListener('click', async (e) => {
  const ld = e.target.closest('#load-samples'), cl = e.target.closest('#clear-samples');
  if (!ld && !cl) return;
  try {
    const r = await api(ld ? '/api/samples/load' : '/api/samples/clear', { method: 'POST' });
    toast(ld ? `Loaded ${r.loaded} sample companies.` : `Cleared ${r.cleared} sample companies.`);
    state.filterTouched = false; await refreshAll();
  } catch (err) { toast(err.message, true); }
});
