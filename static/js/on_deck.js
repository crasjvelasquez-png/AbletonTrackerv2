/* On Deck: the one song you're finishing, mirrored from the menu-bar panel.
   The section lives inside #appDashboard but survives its re-renders (see render()). */
window.OnDeck = (() => {
  const esc = value => escapeHtml(String(value ?? ''));
  const state = { model: null, error: '', busy: false, menu: null, editing: false, swapKey: null, filter: '' };
  let root = null;
  let menuTrigger = null;

  const localIso = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const daysFromToday = days => { const d = new Date(); d.setDate(d.getDate() + days); return localIso(d); };
  const songRows = rows => (rows || []).map(([key, title, stage]) => ({ key, title, stage }));

  function mount() {
    const host = document.getElementById('appDashboard');
    if (!host) return;
    if (!root) {
      root = document.createElement('section');
      root.className = 'chart-card chart-card-wide on-deck';
      root.id = 'onDeck';
      root.setAttribute('aria-labelledby', 'onDeckHeading');
      root.addEventListener('click', onClick);
      root.addEventListener('submit', onSubmit);
      root.addEventListener('keydown', onKeydown);
      root.addEventListener('input', onInput);
      root.addEventListener('change', onChange);
      draw();
      refresh();
    }
    if (root.parentElement !== host || host.firstElementChild !== root) host.prepend(root);
  }

  function isBusy() {
    return state.busy || !!state.menu || !!root?.contains(document.activeElement) && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
  }

  async function refresh() {
    if (isBusy()) return;
    try {
      const res = await fetch('/api/on-deck', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const model = await res.json();
      if (isBusy()) return;
      state.model = model; state.error = '';
    } catch (error) {
      console.error(error);
      if (!state.model) state.error = 'On Deck could not load.';
    }
    draw();
  }

  async function act(body, { pending = null } = {}) {
    if (state.busy) return null;
    state.busy = true;
    if (pending) { pending.disabled = true; pending.dataset.label = pending.textContent; }
    try {
      const res = await fetch('/api/on-deck', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const data = await res.json().catch(() => ({}));
      if (data.deck) state.model = data.deck;
      if (!res.ok) { toast(data.error || 'That change did not save. Try again.'); return null; }
      window.SongPlanner?.refresh();
      return data.deck;
    } catch (error) {
      console.error(error);
      toast('The dashboard server is not responding. Your change was not saved.');
      return null;
    } finally {
      state.busy = false;
      if (pending?.isConnected) pending.disabled = false;
      draw();
    }
  }

  // ── Rendering ──────────────────────────────────────────────────────────

  function draw() {
    if (!root) return;
    const restoreFocus = root.contains(document.activeElement) ? focusKey(document.activeElement) : null;
    const m = state.model;
    let body;
    if (!m) body = state.error
      ? `<p class="deck-hint">${esc(state.error)}</p><button class="btn small" type="button" data-deck="retry">Retry</button>`
      : '<p class="deck-hint">Loading On Deck…</p>';
    else body = m.mode === 'set' ? setView(m) : emptyView(m);
    root.innerHTML = body + (state.menu ? menuView(m) : '');
    root.classList.toggle('is-set', m?.mode === 'set');
    if (state.menu) positionMenu();
    if (restoreFocus) root.querySelector(restoreFocus)?.focus();
  }

  function focusKey(el) {
    if (el.dataset.deck) return `[data-deck="${el.dataset.deck}"]${el.dataset.key ? `[data-key="${CSS.escape(el.dataset.key)}"]` : ''}${el.dataset.stage ? `[data-stage="${el.dataset.stage}"]` : ''}`;
    return el.id ? `#${CSS.escape(el.id)}` : null;
  }

  function head(actions = '') {
    return `<div class="deck-head"><h3 class="section-title" id="onDeckHeading">On deck</h3>${actions ? `<div class="deck-actions">${actions}</div>` : ''}</div>`;
  }

  function setView(m) {
    const inferred = (m.track_stages || []).findIndex(([stage]) => stage === m.inferred_stage);
    const stages = (m.track_stages || []).map(([stage, label], index) => {
      const cls = index < m.stage_index ? 'is-done' : index === m.stage_index ? 'is-current' : '';
      const locked = index < inferred;
      return `<li><button class="deck-stage ${cls}" type="button" data-deck="stage" data-stage="${esc(stage)}" ${index === m.stage_index ? 'aria-current="step"' : ''} ${locked ? `disabled title="Detected from your ${esc((m.track_stages[inferred] || [])[1] || '')} file"` : `title="Set stage to ${esc(label)}"`}><i aria-hidden="true"></i><span>${esc(label)}</span></button></li>`;
    }).join('');
    const tracked = m.seconds ? `${fmt.dur(m.seconds)} tracked on this song` : 'No tracked time yet';
    return `${head(`<button class="btn small" type="button" data-deck="change" aria-haspopup="dialog" aria-expanded="${state.menu === 'change'}">Change</button><button class="btn small is-primary" type="button" data-deck="open" title="Open ${esc(m.open_name || m.title)} in Live">Open in Live</button>`)}
      <p class="deck-title" title="${esc(m.title)}">${esc(m.display_title || m.title)}</p>
      <p class="deck-meta">${tracked}</p>
      <ol class="deck-stages" aria-label="Stage">${stages}</ol>
      <div class="deck-foot">${taskView(m)}${dueView(m)}</div>`;
  }

  function taskView(m) {
    if (state.editing) {
      return `<form class="deck-task deck-task-form" data-deck-form="task">
        <input class="text-input" id="deckTaskInput" name="task" maxlength="200" autocomplete="off" placeholder="The one next thing for this song" aria-label="Next task" value="${esc(m.task)}">
        <button class="btn small" type="submit">Save</button><button class="btn small subtle" type="button" data-deck="task-cancel">Cancel</button>
      </form>`;
    }
    if (!m.task) {
      return `<div class="deck-task"><button class="deck-task-add" type="button" data-deck="task-edit"><span aria-hidden="true">+</span> Add the next task</button></div>`;
    }
    return `<div class="deck-task">
      <button class="deck-check" type="button" data-deck="task-done" aria-label="Mark done: ${esc(m.task)}" title="Mark done"></button>
      <button class="deck-task-text" type="button" data-deck="task-edit" title="Edit next task"><span class="deck-task-label">Next</span>${esc(m.task)}</button>
    </div>`;
  }

  function dueView(m) {
    const secondary = m.due_source === 'legacy' ? `${esc(m.due_secondary)} · from planner` : esc(m.due_secondary);
    return `<button class="deck-due${m.overdue ? ' is-overdue' : ''}" type="button" data-deck="due" aria-haspopup="dialog" aria-expanded="${state.menu === 'due'}" aria-label="${esc(m.due)}. Change due date"><strong>${esc(m.due_primary)}</strong><span>${secondary}</span></button>`;
  }

  function emptyView(m) {
    const picks = songRows(m.suggestions);
    const all = songRows(m.all);
    if (!picks.length && !all.length) {
      return `${head()}<p class="deck-hint">Songs appear here after you save a set in Live and work on it for a few minutes.</p>`;
    }
    const rows = picks.map(song => `<li><button class="deck-pick" type="button" data-deck="pick" data-key="${esc(song.key)}" aria-label="Put ${esc(song.title)} On Deck. Stage: ${esc(song.stage)}"><span class="deck-pick-title">${esc(song.title)}</span><span class="deck-pick-stage">${esc(song.stage)}</span></button></li>`).join('');
    return `${head(all.length ? `<button class="btn small" type="button" data-deck="change" aria-haspopup="dialog" aria-expanded="${state.menu === 'change'}">All songs</button>` : '')}
      <p class="deck-hint">Pick the song you're finishing. Its stage, due date and next task stay here and in the menu bar.</p>
      ${rows ? `<ul class="deck-picks">${rows}</ul>` : ''}`;
  }

  function menuView(m) {
    if (state.menu === 'due') {
      const presets = [[3, 'In 3 days'], [7, 'In 1 week'], [14, 'In 2 weeks'], [28, 'In 4 weeks']];
      return `<div class="deck-menu" role="dialog" aria-label="Due date">
        <div class="deck-menu-label">Due date</div>
        ${presets.map(([days, label]) => `<button class="deck-menu-item" type="button" data-deck="due-set" data-date="${daysFromToday(days)}">${label}<small>${esc(new Date(daysFromToday(days) + 'T00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' }))}</small></button>`).join('')}
        <label class="deck-menu-date">On a date<input type="date" id="deckDueInput" value="${esc(m.due_value || '')}" min="${localIso(new Date())}"></label>
        ${m.due_source === 'song' ? '<hr><button class="deck-menu-item" type="button" data-deck="due-set" data-date="">Clear due date</button>' : ''}
        ${m.due_source === 'legacy' ? '<p class="deck-menu-note">This date comes from the older project planner. Set a date here to replace it.</p>' : ''}
      </div>`;
    }
    if (state.swapKey) {
      const current = m.display_title || m.title;
      return `<div class="deck-menu deck-menu-wide" role="dialog" aria-label="Snooze the current song">
        <p class="deck-menu-note is-lead">Only one song is On Deck at a time. Snooze “${esc(current)}”? It returns to your suggestions when the snooze ends.</p>
        <div class="deck-menu-row"><button class="btn small" type="button" data-deck="swap" data-days="7">Snooze 1 week</button><button class="btn small" type="button" data-deck="swap" data-days="30">Snooze 1 month</button><button class="btn small subtle" type="button" data-deck="swap-cancel">Back</button></div>
      </div>`;
    }
    const filter = state.filter.trim().toLowerCase();
    const more = m.mode === 'set' ? songRows(m.more) : [];
    const all = songRows(m.all).filter(song => !filter || song.title.toLowerCase().includes(filter));
    const item = song => `<button class="deck-menu-item" type="button" data-deck="pick" data-key="${esc(song.key)}"><span>${esc(song.title)}</span><small>${esc(song.stage)}</small></button>`;
    return `<div class="deck-menu deck-menu-wide" role="dialog" aria-label="Change On Deck">
      ${more.length && !filter ? `<div class="deck-menu-label">Suggestions</div>${more.map(item).join('')}<hr>` : ''}
      <div class="deck-menu-label">All songs</div>
      ${songRows(m.all).length > 8 ? `<input class="text-input deck-menu-filter" id="deckFilter" type="search" placeholder="Filter songs" aria-label="Filter songs" value="${esc(state.filter)}" autocomplete="off">` : ''}
      <div class="deck-menu-list">${all.map(item).join('') || `<p class="deck-menu-note">No songs match “${esc(state.filter)}”.</p>`}</div>
      ${m.mode === 'set' ? `<hr><button class="deck-menu-item" type="button" data-deck="snooze">Snooze for a week, leave On Deck empty</button><button class="deck-menu-item" type="button" data-deck="release">Mark released…</button>` : ''}
    </div>`;
  }

  function positionMenu() {
    const menu = root.querySelector('.deck-menu');
    const trigger = root.querySelector(`[data-deck="${state.menu}"]`);
    if (!menu || !trigger) return;
    const box = root.getBoundingClientRect(), t = trigger.getBoundingClientRect();
    const width = Math.min(menu.offsetWidth, box.width - 16), height = menu.offsetHeight;
    // Open upward when the menu would run past the window and there is room above.
    const above = t.bottom + 6 + height > window.innerHeight && t.top - 6 - height >= 0;
    menu.style.top = `${above ? t.top - box.top - 6 - height : t.bottom - box.top + 6}px`;
    menu.style.left = `${Math.max(8, Math.min(t.right - box.left - width, box.width - width - 8))}px`;
  }

  // ── Menus ──────────────────────────────────────────────────────────────

  function openMenu(name, trigger) {
    menuTrigger = trigger;
    state.menu = name; state.swapKey = null; state.filter = '';
    draw();
    const menu = root.querySelector('.deck-menu');
    (menu?.querySelector('#deckFilter') || menu?.querySelector('button,input'))?.focus({ preventScroll: true });
  }

  function closeMenu({ focus = true } = {}) {
    if (!state.menu) return;
    const name = state.menu;
    state.menu = null; state.swapKey = null; state.filter = '';
    draw();
    if (focus) (root.querySelector(`[data-deck="${name}"]`) || menuTrigger)?.focus();
  }

  document.addEventListener('pointerdown', event => {
    const target = event.target instanceof Element ? event.target : null;
    if (state.menu && !target?.closest('.deck-menu') && !target?.closest(`[data-deck="${state.menu}"]`)) closeMenu({ focus: false });
  });
  window.addEventListener('resize', () => { if (state.menu) positionMenu(); });

  // ── Events ─────────────────────────────────────────────────────────────

  async function onClick(event) {
    const button = event.target.closest('[data-deck]');
    if (!button || button.disabled) return;
    const m = state.model || {};
    const kind = button.dataset.deck;
    if (kind === 'retry') { state.error = ''; draw(); refresh(); }
    else if (kind === 'change' || kind === 'due') { state.menu === kind ? closeMenu() : openMenu(kind, button); }
    else if (kind === 'pick') {
      if (m.mode === 'set' && button.dataset.key !== m.key) { state.swapKey = button.dataset.key; draw(); root.querySelector('[data-deck="swap"]')?.focus(); return; }
      state.menu = null;
      const deck = await act({ action: 'pick', key: button.dataset.key });
      if (deck) { toast(`${deck.display_title || deck.title} is On Deck`); root.querySelector('[data-deck="open"]')?.focus(); }
    }
    else if (kind === 'swap') {
      const key = state.swapKey; state.menu = null; state.swapKey = null;
      const deck = await act({ action: 'pick', key, snooze_days: Number(button.dataset.days) });
      if (deck) { toast(`${deck.display_title || deck.title} is On Deck`); root.querySelector('[data-deck="open"]')?.focus(); }
    }
    else if (kind === 'swap-cancel') { state.swapKey = null; draw(); root.querySelector('.deck-menu button')?.focus(); }
    else if (kind === 'open') {
      button.textContent = 'Opening…';
      if (await act({ action: 'open', key: m.key }, { pending: button })) toast(`Opening ${m.display_title || m.title} in Live`);
    }
    else if (kind === 'stage') {
      if (button.dataset.stage === m.stage) return;
      await act({ action: 'stage', key: m.key, stage: button.dataset.stage });
      root.querySelector(`[data-stage="${button.dataset.stage}"]`)?.focus();
    }
    else if (kind === 'due-set') { await setDue(button.dataset.date); }
    else if (kind === 'task-edit') { state.editing = true; draw(); const input = root.querySelector('#deckTaskInput'); input?.focus(); input?.select(); }
    else if (kind === 'task-cancel') { state.editing = false; draw(); root.querySelector('[data-deck="task-edit"]')?.focus(); }
    else if (kind === 'task-done') {
      const task = m.task, key = m.key;
      button.classList.add('is-checked');
      const deck = await act({ action: 'task_done', key });
      if (!deck) return;
      toast(`Done: ${task}`, { label: 'Undo', onClick: async () => { state.editing = false; await act({ action: 'task', key, text: task }); } });
      state.editing = true; draw(); root.querySelector('#deckTaskInput')?.focus();
    }
    else if (kind === 'snooze') {
      state.menu = null;
      if (await act({ action: 'snooze', key: m.key, days: 7 })) toast(`${m.display_title || m.title} is snoozed for a week`);
    }
    else if (kind === 'release') {
      closeMenu({ focus: false });
      const ok = await confirmDialog({ title: 'Mark as released?', body: `<p>“${esc(m.display_title || m.title)}” leaves On Deck and stops appearing in suggestions. Its tracked time is kept.</p>`, confirmLabel: 'Mark released' });
      if (ok && await act({ action: 'stage', key: m.key, stage: 'released' })) toast(`${m.display_title || m.title} is released`);
      else root.querySelector('[data-deck="change"]')?.focus();
    }
  }

  async function setDue(date) {
    const m = state.model || {};
    state.menu = null;
    const deck = await act({ action: 'due', key: m.key, date });
    root.querySelector('[data-deck="due"]')?.focus();
    if (deck) toast(date ? deck.due : 'Due date cleared');
  }

  async function onSubmit(event) {
    if (event.target.dataset.deckForm !== 'task') return;
    event.preventDefault();
    const text = event.target.elements.task.value.trim();
    const m = state.model || {};
    state.editing = false;
    if (text === (m.task || '')) { draw(); root.querySelector('[data-deck="task-edit"]')?.focus(); return; }
    await act({ action: 'task', key: m.key, text });
    root.querySelector('[data-deck="task-edit"]')?.focus();
  }

  function onChange(event) {
    if (event.target.id === 'deckDueInput' && event.target.value) setDue(event.target.value);
  }

  function onInput(event) {
    if (event.target.id !== 'deckFilter') return;
    state.filter = event.target.value;
    const list = root.querySelector('.deck-menu-list');
    const filter = state.filter.trim().toLowerCase();
    const rows = songRows(state.model?.all).filter(song => !filter || song.title.toLowerCase().includes(filter));
    list.innerHTML = rows.map(song => `<button class="deck-menu-item" type="button" data-deck="pick" data-key="${esc(song.key)}"><span>${esc(song.title)}</span><small>${esc(song.stage)}</small></button>`).join('') || `<p class="deck-menu-note">No songs match “${esc(state.filter)}”.</p>`;
  }

  function onKeydown(event) {
    if (event.key === 'Escape') {
      if (state.menu) { event.preventDefault(); event.stopPropagation(); closeMenu(); }
      else if (state.editing) { event.preventDefault(); state.editing = false; draw(); root.querySelector('[data-deck="task-edit"]')?.focus(); }
      return;
    }
    const menu = event.target.closest('.deck-menu');
    if (menu && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      const items = [...menu.querySelectorAll('button,input')];
      const index = items.indexOf(document.activeElement);
      event.preventDefault();
      items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
    }
    if (menu && event.key === 'Tab') {
      const items = [...menu.querySelectorAll('button,input')];
      const last = items[items.length - 1], first = items[0];
      if (!event.shiftKey && document.activeElement === last || event.shiftKey && document.activeElement === first) closeMenu();
    }
    if (event.target.closest('.deck-stages') && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) {
      const items = [...root.querySelectorAll('.deck-stage:not(:disabled)')];
      const index = items.indexOf(document.activeElement);
      if (index < 0) return;
      event.preventDefault();
      items[Math.max(0, Math.min(items.length - 1, index + (event.key === 'ArrowRight' ? 1 : -1)))]?.focus();
    }
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('focus', refresh);
  setInterval(() => { if (!document.hidden) refresh(); }, 30_000);
  mount();

  return { mount, refresh, isBusy };
})();
