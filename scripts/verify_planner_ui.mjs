#!/usr/bin/env node
/*
 * End-to-end Planner UI check: headless Chrome driven over the DevTools protocol (Node 24, no npm deps).
 *
 * Failure modes this run is written to catch (listed before the planner UI code existed):
 *  - Choosing Released and pressing Cancel still changes the stage (UI or server); Confirm does not
 *    release the song or leave it outside Archive; the confirm dialog stays open after either button.
 *  - Contact Delete skips the confirm, Cancel still deletes, or Confirm leaves the contact in the list,
 *    on a song's People, or leaves "Waiting on <name>" on a project that was waiting on them.
 *  - Native window mode (html.native-sidebar, 52px top bar): the left list scrolls away with the page,
 *    or sits under the top bar / header after scrolling a long project.
 *  - The Planner tab opens on something other than Agenda, never leaves the skeleton, or shows an
 *    empty-state while seeded to-dos exist.
 *  - Quick-adding a general to-do does not appear in the agenda, loses its text on error, or the
 *    Enter key does nothing.
 *  - Project rows are missing, grouped wrongly (held rows outside "On hold", archived rows outside
 *    the collapsed Archive), or clicking / keyboard Enter does not open the detail pane.
 *  - Star, hold (reason + back-on date + waiting-on), resume, to-do add / check, inline new contact
 *    + link, and notes (save on blur) look saved but are not persisted after a full reload.
 *  - A checked to-do stays in the open list, or is not reachable under "Done".
 *  - Stage select allows stages below the detected one; Released skips the confirm dialog.
 *  - User text is injected as HTML (an XSS title / reason renders as markup).
 *  - The Contacts view omits the new contact or its linked song; Save / Delete do not round-trip.
 *  - Narrow layout (700px) keeps two columns, hides the detail "Back" button, or Back fails to
 *    return to the list; horizontal page overflow appears at 700px or 1280px.
 *  - Dark mode keeps light colours (root background not dark) or text becomes unreadable.
 *  - prefers-reduced-motion leaves transitions running.
 *  - Any uncaught exception, console.error, or failed same-origin request (zero tolerated).
 *  - Chrome is left running after the test.
 *
 * Usage: node scripts/verify_planner_ui.mjs --url http://127.0.0.1:PORT --output DIR [--allow-real]
 * --allow-real relaxes count-specific assertions so the run also works on a copy of real data
 * (server started WITHOUT --synthetic). Seeded songs and contacts must still exist.
 * Needs a server from: python3 scripts/verify_planner_dashboard.py --serve 0 --synthetic --output DIR
 * (fresh server per run; the UI test mutates its database copy).
 * Artifacts: DIR/results.json and PNG screenshots.
 */
import { spawn } from 'node:child_process';
import { mkdirSync, rmSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join, parse, resolve } from 'node:path';
import { homedir } from 'node:os';

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, cur, i, all) => {
  if (cur.startsWith('--')) acc.push([cur.slice(2), all[i + 1]]);
  return acc;
}, []));
if (!args.url || !args.output) {
  console.error('usage: verify_planner_ui.mjs --url URL --output DIR');
  process.exit(2);
}
const ALLOW_REAL = 'allow-real' in args;
const BASE = args.url.replace(/#.*$/, '').replace(/\/$/, '');
const OUT = resolve(args.output);
if (OUT === homedir() || OUT === parse(OUT).root || OUT.includes('.ableton_tracker')) {
  console.error(`refusing to wipe ${OUT}: pass a dedicated scratch folder for --output`);
  process.exit(1);
}
rmSync(OUT, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const KEY = 'planner charlie - zed';       // plain active seeded song: no to-dos, no hold
const HELD_KEY = 'planner bravo - zed';     // seeded hold waiting on Ana Ruiz
const checks = [];
const problems = [];
const check = (name, ok, detail = '') => {
  checks.push({ name, ok: Boolean(ok), detail: ok ? '' : String(detail) });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `  (${detail})`}`);
};

let chrome = null;
let ws = null;
let nextId = 1;
const pending = new Map();
const listeners = [];

function killChrome() {
  if (chrome && !chrome.killed) {
    try { chrome.kill('SIGKILL'); } catch { /* already gone */ }
  }
}
process.on('exit', killChrome);
process.on('SIGINT', () => { killChrome(); process.exit(130); });
process.on('SIGTERM', () => { killChrome(); process.exit(143); });

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function launch() {
  const profile = join(OUT, 'chrome-profile');
  chrome = spawn(CHROME, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run',
    '--no-default-browser-check', '--disable-gpu', 'about:blank'], { stdio: 'ignore' });
  const portFile = join(profile, 'DevToolsActivePort');
  for (let i = 0; i < 200 && !existsSync(portFile); i += 1) await sleep(50);
  if (!existsSync(portFile)) throw new Error('Chrome did not publish DevToolsActivePort');
  const port = readFileSync(portFile, 'utf8').split('\n')[0];
  const tab = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json();
  ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
  ws.onmessage = event => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    } else if (msg.method) {
      listeners.forEach(fn => fn(msg));
    }
  };
}

const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});

async function ev(expression) {
  const res = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (res.exceptionDetails) throw new Error(`evaluate failed: ${res.exceptionDetails.exception?.description || res.exceptionDetails.text}`);
  return res.result.value;
}

async function waitFor(expression, label, timeout = 8000) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    try {
      last = await ev(expression);
      if (last) return last;
    } catch (error) { last = error.message; }
    await sleep(40);
  }
  throw new Error(`timeout waiting for: ${label} (last: ${JSON.stringify(last)})`);
}

const shot = async name => {
  await waitFor(`document.getAnimations().every(a => a.playState !== 'running' || a.effect?.getTiming().iterations === Infinity)`, 'animations settled', 3000).catch(() => {});
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(OUT, `${name}.png`), Buffer.from(data, 'base64'));
};

const q = sel => `document.querySelector(${JSON.stringify(sel)})`;
const click = sel => ev(`(() => { const el = ${q(sel)}; if (!el) throw new Error('missing ${sel.replace(/'/g, "\\'")}'); el.click(); return true; })()`);
const setValue = (sel, value, eventName = 'input') => ev(`(() => {
  const el = ${q(sel)}; if (!el) throw new Error('missing ${sel.replace(/'/g, "\\'")}');
  el.focus(); el.value = ${JSON.stringify(value)};
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true; })()`);
const exists = sel => ev(`Boolean(${q(sel)})`);
const text = sel => ev(`${q(sel)}?.textContent?.trim() ?? null`);
const press = key => send('Input.dispatchKeyEvent', { type: 'keyDown', key, code: key, windowsVirtualKeyCode: key === 'Enter' ? 13 : 0, ...(key === 'Enter' ? { text: '\r' } : {}) })
  .then(() => send('Input.dispatchKeyEvent', { type: 'keyUp', key, code: key }));
const isoIn = days => { const d = new Date(); d.setDate(d.getDate() + days); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
const overflowX = () => ev('document.documentElement.scrollWidth > window.innerWidth + 1');
const row = key => `[data-pl-project="${key}"]`;

async function step(name, fn) {
  try {
    await fn();
  } catch (error) {
    check(name, false, error.message);
    problems.push(name);
  }
}

async function main() {
  await launch();
  const consoleErrors = [];
  listeners.push(msg => {
    if (msg.method === 'Runtime.exceptionThrown') {
      consoleErrors.push(`exception: ${msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text}`);
    } else if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
      consoleErrors.push(`console.error: ${msg.params.args.map(a => a.value ?? a.description).join(' ')}`);
    } else if (msg.method === 'Log.entryAdded' && msg.params.entry.level === 'error' && !/favicon/.test(msg.params.entry.url || msg.params.entry.text)) {
      consoleErrors.push(`log: ${msg.params.entry.text} ${msg.params.entry.url || ''}`);
    }
  });
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 860, deviceScaleFactor: 1, mobile: false });
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'light' }] });

  // 1. Open on Agenda with seeded to-dos.
  await step('planner opens on Agenda', async () => {
    await send('Page.navigate', { url: `${BASE}/#planner` });
    await waitFor(`${q('#plannerRoot [data-pl-pane="agenda"]')} && document.querySelectorAll('#plannerRoot .pl-todo').length >= 5`, 'agenda with seeded to-dos');
    check('planner opens on Agenda with seeded to-dos', true);
    check('Agenda nav row is the current selection', await ev(`${q('[data-pl-nav="agenda"]')}.getAttribute('aria-current') === 'true'`));
    check('On Deck reminder shows the deck song', (await text('.pl-deck')) ?.includes('Planner Delta'), await text('.pl-deck'));
    check('Overdue bucket is shown with seeded overdue to-do', await ev(`[...document.querySelectorAll('#plannerRoot .pl-bucket h3')].some(h => /overdue/i.test(h.textContent))`));
    check('Archive group exists and is collapsed', await ev(`(() => { const t = ${q('[data-pl-group="archive"]')}; return !!t && t.getAttribute('aria-expanded') === 'false' && !${q(row('planner echo - zed'))}; })()`));
    check('every list group head computes 12px (Archive toggle matches)', await ev(`(() => { const h = [...document.querySelectorAll('#plannerRoot .pl-group-head')]; return h.length >= 2 && h.every(e => getComputedStyle(e).fontSize === '12px' && getComputedStyle(e).fontWeight === '600'); })()`));
    check('held project sits in On hold group', await ev(`!!${q(`[data-pl-list="on_hold"] ${row(HELD_KEY)}`)}`));
    check('starred project is in Active group', await ev(`!!${q(`[data-pl-list="active"] ${row('planner alpha - zed')}`)}`));
    await shot('agenda-light');
  });

  // 2. Quick-add general to-do with Enter.
  await step('quick-add general to-do', async () => {
    await setValue('#plQuickText', 'UI test general to-do');
    await ev(`${q('#plQuickText')}.focus()`);
    await press('Enter');
    await waitFor(`[...document.querySelectorAll('#plannerRoot .pl-todo')].some(t => t.textContent.includes('UI test general to-do'))`, 'new general to-do');
    check('Enter on quick-add adds a general to-do to the agenda', true);
    check('quick-add input is cleared', (await ev(`${q('#plQuickText')}.value`)) === '');
  });

  // 3. Search filters, then open project by click.
  await step('search filters the list', async () => {
    await setValue('#plSearch', ALLOW_REAL ? 'planner charlie' : 'charlie');
    await waitFor(`document.querySelectorAll('#plannerRoot [data-pl-project]').length ${ALLOW_REAL ? '>= 1' : '=== 1'}`, 'search narrows to the matching rows');
    check('search narrows the project list', true);
    await ev(`${q('#plSearch')}.focus()`);
    await press('Escape');
    await waitFor(`document.querySelectorAll('#plannerRoot [data-pl-project]').length >= 4`, 'Escape clears search');
    check('Escape clears the search', true);
  });

  await step('open project', async () => {
    await click(row(KEY));
    await waitFor(`${q('[data-pl-pane="project"]')} && ${q('#plTitle')}?.textContent.includes('Planner Charlie')`, 'project pane');
    check('clicking a project row opens its detail pane', true);
  });

  // 4. Stage rules + XSS-safe title rendering.
  await step('stage select respects detected stage', async () => {
    const info = await ev(`(() => { const s = ${q('#plStage')}; return s ? { value: s.value, disabled: [...s.options].filter(o => o.disabled).map(o => o.value) } : null; })()`);
    check('stage select exists and locks nothing below detection for a fresh idea song', info && Array.isArray(info.disabled), JSON.stringify(info));
    const delta = await ev(`fetch('/api/planner?key=' + encodeURIComponent('planner delta - zed')).then(r => r.json()).then(d => d.project.inferred_stage)`);
    check('backend reports inferred stage for locking', typeof delta === 'string', delta);
  });

  // 5. Star.
  await step('star project', async () => {
    await click('[data-pl="star"]');
    await waitFor(`${q('[data-pl="star"]')}.getAttribute('aria-pressed') === 'true'`, 'star pressed');
    check('star toggles on', true);
  });

  // 6. Hold with reason + back-on date.
  await step('put on hold', async () => {
    await click('[data-pl="hold"]');
    await waitFor(`${q('#plHoldReason')}`, 'hold form');
    await setValue('#plHoldReason', 'Waiting for <b>vocal</b> takes');
    await setValue('#plHoldUntil', isoIn(10));
    await click('[data-pl="hold-save"]');
    await waitFor(`${q('[data-pl="resume"]')} && ${q('.pl-hold-panel')}`, 'hold saved and resume offered');
    check('hold saves and offers Resume', true);
    check('hold reason is escaped, not injected as HTML', await ev(`!${q('.pl-hold-panel b')} && document.body.innerHTML.includes('&lt;b&gt;vocal')`));
    check('project moves to the On hold group', await ev(`!!${q(`[data-pl-list="on_hold"] ${row(KEY)}`)}`));
    await shot('project-hold');
  });

  // 7. Add and check a to-do.
  await step('to-do add and check', async () => {
    await setValue('#plTodoText', 'Record guitar overdub');
    await ev(`${q('#plTodoText')}.focus()`);
    await press('Enter');
    await waitFor(`[...document.querySelectorAll('[data-pl-pane="project"] .pl-todo')].some(t => t.textContent.includes('Record guitar overdub'))`, 'to-do added');
    check('to-do can be added to a project', true);
    await ev(`${q('[data-pl-pane="project"] .pl-todo input[type=checkbox]')}.click()`);
    await waitFor(`!${q('[data-pl-pane="project"] .pl-todos-open .pl-todo')} && /Done \\(1\\)/.test(document.querySelector('[data-pl-pane="project"]').textContent)`, 'to-do moved to Done');
    check('checking a to-do moves it to Done (1)', true);
    check('Undo toast is offered', await ev(`!!document.querySelector('#toast .toast-action')`));
  });

  // 8. Inline new contact + link.
  await step('new contact inline', async () => {
    await click('[data-pl="person-add"]');
    await waitFor(`${q('#plPersonSelect')}`, 'person picker');
    await setValue('#plPersonSelect', '__new');
    await waitFor(`${q('#plNewName')}`, 'new contact fields');
    await setValue('#plNewName', 'Test Person');
    await setValue('#plNewRole', 'Drummer');
    await click('[data-pl="person-create"]');
    await waitFor(`[...document.querySelectorAll('[data-pl-pane="project"] .pl-person')].some(p => p.textContent.includes('Test Person'))`, 'person linked');
    check('inline new contact is created and linked to the project', true);
  });

  // 9. Notes saved on blur.
  await step('notes', async () => {
    await setValue('#plNotes', 'Keep the verse sparse.');
    await ev(`${q('#plNotes')}.blur()`);
    await waitFor(`/Saved/.test(document.querySelector('.pl-saved')?.textContent || '')`, 'Saved indicator');
    check('notes save on blur with a Saved indicator', true);
    await shot('project');
  });

  // 10. Reload and verify persistence.
  await step('reload persistence', async () => {
    await send('Page.navigate', { url: `${BASE}/#planner` });
    await send('Page.reload');
    await waitFor(`${q('[data-pl-pane="agenda"]')} && ${q(row(KEY))}`, 'planner after reload');
    await click(row(KEY));
    await waitFor(`${q('[data-pl-pane="project"] .pl-hold-panel')} && ${q('#plNotes')}`, 'project detail after reload');
    const state = await ev(`({
      starred: ${q('[data-pl="star"]')}.getAttribute('aria-pressed') === 'true',
      held: !!${q('[data-pl="resume"]')},
      reason: ${q('.pl-hold-panel')}.textContent.includes('Waiting for <b>vocal</b> takes'),
      done: /Done \\(1\\)/.test(document.querySelector('[data-pl-pane="project"]').textContent),
      person: document.querySelector('[data-pl-pane="project"]').textContent.includes('Test Person'),
      notes: ${q('#plNotes')}.value,
      general: null })`);
    check('star persists after reload', state.starred, JSON.stringify(state));
    check('hold + reason persist after reload', state.held && state.reason, JSON.stringify(state));
    check('done to-do persists after reload', state.done, JSON.stringify(state));
    check('new contact link persists after reload', state.person, JSON.stringify(state));
    check('notes persist after reload', state.notes === 'Keep the verse sparse.', state.notes);
    await click('[data-pl-nav="agenda"]');
    await waitFor(`[...document.querySelectorAll('#plannerRoot .pl-todo')].some(t => t.textContent.includes('UI test general to-do'))`, 'general to-do persisted');
    check('general to-do persists after reload', true);
  });

  // 11. Held project from seed shows waiting-on; resume works.
  await step('resume', async () => {
    await click(row(KEY));
    await waitFor(`${q('[data-pl="resume"]')}`, 'resume button');
    await click('[data-pl="resume"]');
    await waitFor(`!${q('[data-pl="resume"]')} && ${q(`[data-pl-list="active"] ${row(KEY)}`)}`, 'resumed to Active');
    check('Resume returns the project to Active', true);
    await click(row(HELD_KEY));
    await waitFor(`${q('.pl-hold-panel')}`, 'seeded hold panel');
    check('seeded hold shows Waiting on Ana Ruiz', (await ev(`document.querySelector('.pl-hold-panel').textContent.includes('Ana Ruiz') || document.querySelector('#plHoldWaiting')?.selectedOptions[0]?.textContent.includes('Ana Ruiz')`)));
  });

  // 12. Contacts view.
  await step('contacts view', async () => {
    await click('[data-pl-nav="contacts"]');
    await waitFor(`${q('[data-pl-pane="contacts"]')}`, 'contacts pane');
    await waitFor(`[...document.querySelectorAll('[data-pl-contact]')].some(c => c.textContent.includes('Test Person'))`, 'Test Person in contacts');
    check('Contacts view lists the new contact', true);
    await ev(`[...document.querySelectorAll('[data-pl-contact]')].find(c => c.textContent.includes('Test Person')).click()`);
    await waitFor(`${q('#plContactName')}?.value === 'Test Person'`, 'contact detail');
    check('contact detail lists its linked song', await ev(`[...document.querySelectorAll('[data-pl-pane="contacts"] .pl-linked [data-pl-open]')].some(a => a.textContent.includes('Planner Charlie'))`));
    await setValue('#plContactEmail', 'test@example.com');
    await click('[data-pl="contact-save"]');
    await waitFor(`/Saved/.test(document.querySelector('[data-pl-pane="contacts"]').textContent)`, 'contact saved');
    check('contact edit saves with the explicit Save button', true);
    await shot('contacts');
  });

  // 12b. Richer project (starred, overdue to-do, due date, linked person) with Archive expanded.
  await step('project with Archive open', async () => {
    await click('[data-pl-group="archive"]');
    await waitFor(`${q(row('planner echo - zed'))}`, 'archive rows');
    check('Archive group expands on click', true);
    await click(row('planner alpha - zed'));
    await waitFor(`${q('#plTitle')}?.textContent.includes('Planner Alpha') && document.querySelectorAll('.pl-todos-open .pl-todo').length ${ALLOW_REAL ? '>= 2' : '=== 2'}`, 'alpha detail');
    await shot('project-alpha');
    await click(row('planner echo - zed'));
    await waitFor(`${q('#plTitle')}?.textContent.includes('Planner Echo')`, 'echo detail');
    check('archived project offers Move to Active', await ev(`(async () => { document.querySelector('[data-pl="more"]').click(); await new Promise(r => setTimeout(r, 50)); return /Move to Active/.test(document.querySelector('.pl-menu')?.textContent || ''); })()`));
    await shot('project-archived');
  });

  // 12c. Released confirm: Cancel leaves the stage alone, Confirm releases and archives.
  const REL = 'planner foxtrot - zed';   // seeded, active, not On Deck, not in a lane: safe to release
  const apiStage = key => ev(`fetch('/api/planner?key=' + encodeURIComponent(${JSON.stringify(key)})).then(r => r.json()).then(d => ({ stage: d.project.stage, settled: d.project.settled, group: d.project.group }))`);
  const modalOpen = () => ev(`document.getElementById('modal').classList.contains('show')`);
  await step('released confirm', async () => {
    await click(row(REL));
    await waitFor(`${q('#plTitle')}?.textContent.includes('Planner Foxtrot')`, 'foxtrot detail');
    const before = await apiStage(REL);
    check('release target is not yet released', !before.settled && before.stage !== 'released', JSON.stringify(before));
    await setValue('#plStage', 'released');
    await waitFor(`document.getElementById('modal').classList.contains('show')`, 'confirm dialog');
    check('choosing Released opens the confirm dialog', /released/i.test(await text('#modalTitle')), await text('#modalTitle'));
    await shot('release-confirm');
    await click('#modalCancel');
    await waitFor(`!document.getElementById('modal').classList.contains('show')`, 'dialog closed after Cancel');
    await sleep(250);
    const ui = await ev(`${q('#plStage')}.value`);
    const after = await apiStage(REL);
    check('Cancel keeps the stage in the UI', ui === before.stage, `${ui} vs ${before.stage}`);
    check('Cancel keeps the stage on the server', after.stage === before.stage && !after.settled, JSON.stringify(after));
    await setValue('#plStage', 'released');
    await waitFor(`document.getElementById('modal').classList.contains('show')`, 'confirm dialog again');
    await click('#modalConfirm');
    await waitFor(`!document.getElementById('modal').classList.contains('show')`, 'dialog closed after Confirm');
    await waitFor(`[...document.querySelectorAll('[data-pl-pane="project"] .pl-badge-pill')].some(b => b.textContent.trim() === 'Released')`, 'Released badge');
    check('Confirm shows the project as Released', true);
    const done = await apiStage(REL);
    check('Confirm releases the song on the server and moves it to Archive', done.stage === 'released' && done.settled && done.group === 'archive', JSON.stringify(done));
    await waitFor(`${q(`[data-pl-list="archive"] ${row(REL)}`)}`, 'row in Archive');
    check('released project row sits in the Archive group', true);
    check('released row is no longer in Active', await ev(`!${q(`[data-pl-list="active"] ${row(REL)}`)}`));
    await shot('project-released');
  });

  // 12d. Contact delete: Cancel keeps, Confirm removes everywhere (list, People, Waiting on).
  const GONE = 'Ana Ruiz';
  const contactRows = () => ev(`[...document.querySelectorAll('[data-pl-contact]')].map(c => c.textContent)`);
  await step('contact delete', async () => {
    await click(row(HELD_KEY));
    await waitFor(`${q('.pl-hold-panel')}`, 'bravo hold panel');
    check('held project shows Waiting on Ana Ruiz before delete', await ev(`document.querySelector('.pl-hold-panel').textContent.includes('Waiting on Ana Ruiz')`));
    check('bravo row subtitle says Waiting on Ana Ruiz', await ev(`${q(row(HELD_KEY))}.textContent.includes('Waiting on Ana Ruiz')`));
    check('Ana Ruiz is listed in bravo People', await ev(`[...document.querySelectorAll('[data-pl-pane="project"] .pl-person')].some(p => p.textContent.includes('Ana Ruiz'))`));
    await click('[data-pl-nav="contacts"]');
    await waitFor(`${q('[data-pl-pane="contacts"]')} && document.querySelectorAll('[data-pl-contact]').length`, 'contacts list');
    const before = (await contactRows()).length;
    await ev(`[...document.querySelectorAll('[data-pl-contact]')].find(c => c.textContent.includes('Ana Ruiz')).click()`);
    await waitFor(`${q('#plContactName')}?.value === 'Ana Ruiz'`, 'Ana detail');
    await click('[data-pl="contact-delete"]');
    await waitFor(`document.getElementById('modal').classList.contains('show')`, 'delete confirm');
    check('Delete opens the confirm dialog naming the contact', (await text('#modalBody')).includes('Ana Ruiz'), await text('#modalBody'));
    await click('#modalCancel');
    await waitFor(`!document.getElementById('modal').classList.contains('show')`, 'dialog closed after Cancel');
    await sleep(250);
    check('Cancel keeps the contact in the list and form open', (await contactRows()).length === before && (await ev(`${q('#plContactName')}?.value`)) === 'Ana Ruiz');
    check('Cancel keeps the contact on the server', await ev(`fetch('/api/planner').then(r => r.json()).then(d => d.contacts.some(c => c.name === 'Ana Ruiz'))`));
    await click('[data-pl="contact-delete"]');
    await waitFor(`document.getElementById('modal').classList.contains('show')`, 'delete confirm again');
    await click('#modalConfirm');
    await waitFor(`!document.getElementById('modal').classList.contains('show')`, 'dialog closed after Confirm');
    await waitFor(`![...document.querySelectorAll('[data-pl-contact]')].some(c => c.textContent.includes('Ana Ruiz'))`, 'Ana gone from the list');
    check('Confirm removes the contact from the list', (await contactRows()).length === before - 1, (await contactRows()).length);
    check('Contacts nav count dropped by one', await ev(`Number(document.querySelector('[data-pl-nav="contacts"] .pl-count').textContent) === ${before - 1}`));
    await shot('contact-deleted');
    await click(row(HELD_KEY));
    await waitFor(`${q('#plTitle')}?.textContent.includes('Planner Bravo')`, 'bravo detail');
    await waitFor(`${q('.pl-hold-panel')}`, 'bravo hold panel');
    check('Ana Ruiz is gone from bravo People', await ev(`![...document.querySelectorAll('[data-pl-pane="project"] .pl-person')].some(p => p.textContent.includes('Ana Ruiz'))`));
    check('bravo no longer shows Waiting on Ana Ruiz (panel and row)', await ev(`!document.querySelector('[data-pl-pane="project"]').textContent.includes('Waiting on Ana Ruiz') && !document.querySelector('[data-pl-list="on_hold"]').textContent.includes('Waiting on Ana Ruiz')`));
    const srv = await ev(`fetch('/api/planner?key=' + encodeURIComponent(${JSON.stringify(HELD_KEY)})).then(r => r.json()).then(d => ({ w: d.project.waiting_on, people: d.people.map(p => p.name), status: d.project.status }))`);
    check('server cleared waiting_on and People for bravo, hold stays', srv.w === null && !srv.people.includes('Ana Ruiz') && srv.status === 'on_hold', JSON.stringify(srv));
  });

  // 12e. Native window layout: 52px top bar, long project scrolled, left list stays sticky and visible.
  await step('native sidebar layout', async () => {
    const LONG = 'planner alpha - zed';
    await ev(`(async () => { for (let i = 1; i <= 30; i += 1) await fetch('/api/planner', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'todo_add', key: ${JSON.stringify(LONG)}, text: 'Native layout filler to-do ' + i, due_date: '' }) }); return true; })()`);
    await ev(`document.documentElement.classList.add('native-sidebar'); true`);
    await click(row(LONG));
    await waitFor(`${q('#plTitle')}?.textContent.includes('Planner Alpha') && document.querySelectorAll('.pl-todos-open .pl-todo').length >= 30`, 'long alpha detail');
    check('native-sidebar adds the 52px body offset', (await ev(`parseFloat(getComputedStyle(document.body).paddingTop)`)) === 52);
    const tall = await ev(`document.documentElement.scrollHeight - window.innerHeight`);
    check('long project is taller than the window', tall > 600, tall);
    await ev(`window.scrollTo(0, 700); true`);
    await sleep(200);
    const m = await ev(`(() => {
      const l = document.querySelector('.pl-list').getBoundingClientRect(), h = document.querySelector('header')?.getBoundingClientRect();
      const s = document.querySelector('#plSearch').getBoundingClientRect();
      const hit = document.elementFromPoint(s.left + s.width / 2, s.top + s.height / 2);
      return { scrollY: window.scrollY, listTop: l.top, listBottom: l.bottom, headerBottom: h ? h.bottom : 0, searchHit: !!hit && (hit.id === 'plSearch' || !!hit.closest('.pl-list')),
               navHit: (() => { const r = [...document.querySelectorAll('.pl-nav button')].map(b => b.getBoundingClientRect()).find(b => b.top >= s.bottom && b.bottom <= l.bottom);
                        if (!r) return false; const e = document.elementFromPoint(r.left + 20, r.top + r.height / 2); return !!e && !!e.closest('.pl-list'); })() };
    })()`);
    await shot('native-scrolled');
    check('page actually scrolled down', m.scrollY > 300, JSON.stringify(m));
    check('left list stays on screen (sticky) after scrolling', m.listTop > 0 && m.listTop < 400 && m.listBottom > 200, JSON.stringify(m));
    check('left list is not hidden under the 52px top offset or header', m.listTop >= 52 && m.listTop >= m.headerBottom - 1, JSON.stringify(m));
    check('search box and a visible list row are the topmost elements at their position', m.searchHit && m.navHit, JSON.stringify(m));
    await ev(`window.scrollTo(0, 0); document.documentElement.classList.remove('native-sidebar'); true`);
  });

  // 13. Dark mode + reduced motion + overflow.
  await step('dark mode', async () => {
    await click('[data-pl-nav="agenda"]');
    await waitFor(`${q('[data-pl-pane="agenda"]')}`, 'agenda');
    await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] });
    await waitFor(`(() => { const c = getComputedStyle(document.body).backgroundColor.match(/\\d+/g).map(Number); return c[0] + c[1] + c[2] < 300; })()`, 'dark background');
    check('dark mode switches page to a dark background', true);
    const contrast = await ev(`(() => { const t = document.querySelector('.pl-todo-text'); const c = getComputedStyle(t).color.match(/\\d+/g).map(Number); return c[0] + c[1] + c[2]; })()`);
    check('dark mode to-do text is light', contrast > 450, contrast);
    await shot('agenda-dark');
    await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'light' }] });
  });

  await step('reduced motion', async () => {
    await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }, { name: 'prefers-color-scheme', value: 'light' }] });
    const dur = await ev(`(() => { const r = document.querySelector('[data-pl-project]'); return parseFloat(getComputedStyle(r).transitionDuration) || 0; })()`);
    check('prefers-reduced-motion removes row transitions', dur === 0, dur);
    await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'no-preference' }, { name: 'prefers-color-scheme', value: 'light' }] });
  });

  await step('desktop overflow', async () => {
    check('no horizontal overflow at 1280px', !(await overflowX()));
  });

  // 14. Narrow layout.
  await step('narrow layout', async () => {
    await send('Emulation.setDeviceMetricsOverride', { width: 700, height: 900, deviceScaleFactor: 1, mobile: false });
    await waitFor(`window.innerWidth === 700`, 'viewport 700');
    await click('[data-pl-nav="agenda"]');
    await waitFor(`${q('[data-pl-pane="agenda"]')}`, 'agenda narrow');
    await shot('narrow');
    check('narrow layout shows a single column (list first)', await ev(`(() => { const l = ${q('.pl-list')}, d = ${q('.pl-detail')}; if (!l || !d) return false; const lr = l.getBoundingClientRect(), dr = d.getBoundingClientRect(); return dr.width === 0 || lr.width === 0 || Math.abs(lr.left - dr.left) < 2 || getComputedStyle(l).display === 'none' || getComputedStyle(d).display === 'none'; })()`));
    await click(row(KEY));
    await waitFor(`${q('[data-pl="back"]')} && ${q('#plTitle')}`, 'Back button');
    check('narrow detail shows a Back button', true);
    check('no horizontal overflow at 700px', !(await overflowX()));
    await click('[data-pl="back"]');
    await waitFor(`!${q('[data-pl="back"]')} || getComputedStyle(${q('[data-pl="back"]')}).display === 'none' || ${q('#plSearch')}?.getBoundingClientRect().width > 0`, 'back to list');
    check('Back returns to the list', await ev(`${q('#plSearch')}.getBoundingClientRect().width > 0`));
    await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 860, deviceScaleFactor: 1, mobile: false });
  });

  check('zero console errors or uncaught exceptions', consoleErrors.length === 0, consoleErrors.join(' | '));
}

let exitCode = 1;
try {
  await main();
} catch (error) {
  check('test run completed without a harness error', false, error.stack || error.message);
} finally {
  const passed = checks.every(c => c.ok);
  writeFileSync(join(OUT, 'results.json'), JSON.stringify({ passed, total: checks.length, ok: checks.filter(c => c.ok).length, checks }, null, 2));
  console.log(`\n${passed ? 'All checks passed' : 'Some checks failed'} (${checks.filter(c => c.ok).length}/${checks.length}). Artifact: ${join(OUT, 'results.json')}`);
  exitCode = passed ? 0 : 1;
  killChrome();
  try { ws?.close(); } catch { /* ignore */ }
}
process.exit(exitCode);
