/* Songs planner: search songs, set due date and stage, and manage per-song tasks.
   The search list lives inside #appDashboard right after #onDeck and survives dashboard
   re-renders; the selected song opens in a popup appended to <body>. */
window.SongPlanner = (() => {
  const esc = value => escapeHtml(String(value ?? ''));
  const STAGES = [
    ['idea', 'Idea'],
    ['production', 'Production'],
    ['recording', 'Recording'],
    ['mixing', 'Mixing'],
    ['mastering', 'Mastering'],
  ];
  const state = {
    query: '',
    songs: [],
    loaded: false,
    listError: '',
    selectedKey: '',
    song: null,
    tasks: [],
    detailLoading: false,
    detailError: '',
    busy: false,
    menu: null,
    editingTaskId: null,
    newTaskText: '',
    newTaskDate: '',
  };
  let root = null;
  let popup = null;
  let panel = null;
  let menuTrigger = null;
  let searchTimer = null;
  let listSeq = 0;
  let detailSeq = 0;

  const localIso = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const daysFromToday = days => { const d = new Date(); d.setDate(d.getDate() + days); return localIso(d); };
  const stageLabel = stage => (STAGES.find(([key]) => key === stage) || [])[1] || (stage ? stage.charAt(0).toUpperCase() + stage.slice(1) : '');
  const isTaskOverdue = task => !task?.done && Boolean(task?.due_date) && task.due_date < localIso(new Date());
  const owns = el => Boolean(el) && (!!root?.contains(el) || !!popup?.contains(el));
  const q = selector => root?.querySelector(selector) || panel?.querySelector(selector) || null;

  function mount() {
    const host = document.getElementById('appDashboard');
    if (!host) return;
    if (!root) {
      root = document.createElement('section');
      root.className = 'chart-card chart-card-wide song-planner';
      root.id = 'songPlanner';
      root.setAttribute('aria-labelledby', 'songPlannerHeading');
      root.addEventListener('click', onClick);
      root.addEventListener('submit', onSubmit);
      root.addEventListener('keydown', onKeydown);
      root.addEventListener('input', onInput);
      root.addEventListener('change', onChange);
      popup = document.createElement('div');
      popup.className = 'song-planner-popup';
      popup.id = 'songPlannerPopup';
      popup.hidden = true;
      popup.innerHTML = '<div class="modal song-planner-panel" role="dialog" aria-modal="true" aria-label="Song details" tabindex="-1"></div>';
      panel = popup.firstElementChild;
      popup.addEventListener('click', onClick);
      popup.addEventListener('submit', onSubmit);
      popup.addEventListener('keydown', onKeydown);
      popup.addEventListener('input', onInput);
      popup.addEventListener('change', onChange);
      document.body.append(popup);
      draw();
      refresh();
    }
    const onDeck = document.getElementById('onDeck');
    if (onDeck && onDeck.parentElement === host) {
      if (onDeck.nextElementSibling !== root) onDeck.after(root);
    } else if (root.parentElement !== host || host.firstElementChild !== root) {
      host.prepend(root);
    }
  }

  function isBusy() {
    return state.busy || !!state.menu || (owns(document.activeElement) && /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName));
  }

  async function loadList(query = state.query, { force = false } = {}) {
    if (!force && isBusy()) return;
    const seq = ++listSeq;
    try {
      const res = await fetch(`/api/song-planner?q=${encodeURIComponent(query)}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (seq !== listSeq) return;
      state.songs = Array.isArray(data.songs) ? data.songs : [];
      state.loaded = true;
      state.listError = '';
    } catch (error) {
      console.error(error);
      if (seq !== listSeq) return;
      state.listError = 'Songs could not load.';
    }
    draw();
  }

  async function selectSong(key, { force = true } = {}) {
    if (!key) return;
    if (!force && isBusy()) return;
    const seq = ++detailSeq;
    const switching = state.selectedKey !== key;
    state.selectedKey = key;
    state.detailError = '';
    if (switching) {
      state.song = null;
      state.tasks = [];
      state.detailLoading = true;
      state.menu = null;
      state.editingTaskId = null;
      draw();
    }
    try {
      const res = await fetch(`/api/song-planner?key=${encodeURIComponent(key)}`, { cache: 'no-store' });
      const data = await res.json().catch(() => ({}));
      if (seq !== detailSeq) return;
      if (!res.ok) {
        state.song = null;
        state.tasks = [];
        state.detailError = data.error || 'Song details could not load.';
      } else {
        applyDetail(data.song, data.tasks);
      }
    } catch (error) {
      console.error(error);
      if (seq !== detailSeq) return;
      if (!state.song) state.detailError = 'Song details could not load.';
    } finally {
      if (seq === detailSeq) {
        state.detailLoading = false;
        draw();
      }
    }
  }

  async function refresh() {
    if (isBusy()) return;
    await loadList(state.query);
    if (state.selectedKey && !isBusy()) {
      await selectSong(state.selectedKey, { force: false });
    }
  }

  function applyDetail(song, tasks) {
    if (song) {
      state.song = song;
      state.selectedKey = song.key || state.selectedKey;
    }
    if (Array.isArray(tasks)) state.tasks = tasks;
    state.detailError = '';
    if (state.song) {
      const s = state.song;
      const openCount = state.tasks.filter(t => !t.done).length;
      state.songs = state.songs.map(row => row.key !== s.key ? row : {
        ...row,
        title: s.display_title || row.title || s.title,
        full_title: s.title || row.full_title,
        stage: s.stage ?? row.stage,
        stage_label: s.stage_label || stageLabel(s.stage) || row.stage_label,
        due_value: s.due_value ?? row.due_value,
        due: s.due ?? row.due,
        overdue: Boolean(s.overdue),
        on_deck: Boolean(s.on_deck),
        settled: Boolean(s.settled),
        open_tasks: openCount,
      });
    }
  }

  async function act(body, { pending = null } = {}) {
    if (state.busy) return null;
    state.busy = true;
    if (pending) pending.disabled = true;
    try {
      const res = await fetch('/api/song-planner', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (data.song || data.tasks) applyDetail(data.song, data.tasks);
      if (!res.ok) {
        toast(data.error || 'That change did not save. Try again.');
        return null;
      }
      window.OnDeck?.refresh();
      return data;
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

  // ── Rendering ────────────────────────────────────────────────────────────

  function draw() {
    if (!root) return;
    const active = owns(document.activeElement) ? document.activeElement : null;
    const restoreFocus = active ? focusKey(active) : null;
    const selStart = active && typeof active.selectionStart === 'number' ? active.selectionStart : null;
    const selEnd = active && typeof active.selectionEnd === 'number' ? active.selectionEnd : null;

    root.innerHTML = `${headView()}${listView()}`;
    const open = Boolean(state.selectedKey);
    const scrollTop = panel.scrollTop;
    popup.hidden = !open;
    document.documentElement.classList.toggle('song-planner-open', open);
    panel.innerHTML = open
      ? `<button class="detail-modal-close song-planner-close" type="button" data-planner="close" aria-label="Close song details">×</button>${detailView()}${state.menu && state.song ? menuView(state.song) : ''}`
      : '';
    panel.scrollTop = scrollTop;
    if (state.menu) positionMenu();
    if (restoreFocus) {
      const target = q(restoreFocus);
      if (target) {
        target.focus({ preventScroll: true });
        if (selStart !== null && selEnd !== null && target.tagName === 'INPUT' && target.type !== 'date') {
          try { target.setSelectionRange(selStart, selEnd); } catch {}
        }
      }
    }
  }

  function focusKey(el) {
    if (el.id) return `#${CSS.escape(el.id)}`;
    if (el.dataset.planner) {
      return `[data-planner="${CSS.escape(el.dataset.planner)}"]`
        + (el.dataset.key ? `[data-key="${CSS.escape(el.dataset.key)}"]` : '')
        + (el.dataset.stage ? `[data-stage="${CSS.escape(el.dataset.stage)}"]` : '')
        + (el.dataset.taskId ? `[data-task-id="${CSS.escape(el.dataset.taskId)}"]` : '')
        + (el.dataset.date !== undefined ? `[data-date="${CSS.escape(el.dataset.date)}"]` : '');
    }
    return null;
  }

  function headView() {
    return `<div class="deck-head song-planner-head">
      <h3 class="section-title" id="songPlannerHeading">Songs</h3>
      <input class="text-input song-planner-search" id="songPlannerSearch" type="search" placeholder="Search songs…" aria-label="Search songs" value="${esc(state.query)}" autocomplete="off">
    </div>`;
  }

  function listView() {
    if (state.listError) {
      return `<div class="song-planner-status"><p class="deck-hint">${esc(state.listError)}</p><button class="btn small" type="button" data-planner="retry">Retry</button></div>`;
    }
    if (!state.loaded) {
      return '<p class="deck-hint">Loading songs…</p>';
    }
    if (!state.songs.length) {
      const q = state.query.trim();
      return `<p class="deck-hint">${q ? `No songs match “${esc(q)}”.` : 'Songs appear here after you save a set in Live and work on it for a few minutes.'}</p>`;
    }
    const items = state.songs.map(song => {
      const selected = state.selectedKey === song.key;
      const label = song.stage_label || stageLabel(song.stage);
      const dueText = song.due && song.due !== 'No due date' ? song.due : '';
      const openTasks = Number(song.open_tasks) || 0;
      const tasksText = openTasks > 0 ? `${openTasks} open task${openTasks === 1 ? '' : 's'}` : '';
      return `<li>
        <button class="song-planner-row${selected ? ' is-selected' : ''}" type="button" data-planner="select" data-key="${esc(song.key)}" ${selected ? 'aria-current="true"' : ''} title="${esc(song.full_title || song.title)}">
          <span class="song-planner-row-main">
            <span class="song-planner-row-title">${esc(song.title)}</span>
            ${song.on_deck ? '<span class="song-planner-tag">On deck</span>' : ''}
          </span>
          <span class="song-planner-row-meta">
            ${label ? `<span class="song-planner-stage">${esc(label)}</span>` : ''}
            ${dueText ? `<span class="song-planner-due${song.overdue ? ' is-overdue' : ''}">${esc(dueText)}</span>` : ''}
            ${tasksText ? `<span class="song-planner-tasks">${esc(tasksText)}</span>` : ''}
          </span>
        </button>
      </li>`;
    }).join('');
    return `<ul class="song-planner-list" aria-label="Songs">${items}</ul>`;
  }

  function detailView() {
    if (!state.selectedKey) return '';
    if (state.detailError) {
      return `<div class="song-planner-detail">
        <p class="deck-hint">${esc(state.detailError)}</p>
        <button class="btn small" type="button" data-planner="retry-detail">Retry</button>
      </div>`;
    }
    if (state.detailLoading && !state.song) {
      return '<div class="song-planner-detail"><p class="deck-hint">Loading song…</p></div>';
    }
    const s = state.song;
    if (!s) return '';

    const inferredIdx = STAGES.findIndex(([stage]) => stage === s.inferred_stage);
    const currentIdx = s.stage === 'released' ? STAGES.length : STAGES.findIndex(([stage]) => stage === s.stage);
    const stagesHtml = STAGES.map(([stage, label], index) => {
      const cls = index < currentIdx ? 'is-done' : index === currentIdx ? 'is-current' : '';
      const locked = inferredIdx >= 0 && index < inferredIdx;
      return `<li><button class="deck-stage ${cls}" type="button" data-planner="stage" data-stage="${esc(stage)}" ${index === currentIdx ? 'aria-current="step"' : ''} ${locked ? `disabled title="Detected from your ${esc((STAGES[inferredIdx] || [])[1] || '')} file"` : `title="Set stage to ${esc(label)}"`}><i aria-hidden="true"></i><span>${esc(label)}</span></button></li>`;
    }).join('');

    const tracked = s.seconds ? `${fmt.dur(s.seconds)} tracked on this song` : 'No tracked time yet';
    const releaseControl = s.stage === 'released'
      ? '<span class="song-planner-tag">Released</span>'
      : '<button class="btn small subtle" type="button" data-planner="release">Mark released…</button>';

    return `<div class="song-planner-detail" aria-label="Song details">
      <div class="song-planner-detail-head">
        <div class="song-planner-detail-titles">
          <p class="deck-title" id="songPlannerDetailTitle" title="${esc(s.title)}">${esc(s.display_title || s.title)}</p>
          <p class="deck-meta">${tracked}${s.on_deck ? ' · On deck' : ''}</p>
        </div>
        <div class="deck-actions">
          ${releaseControl}
          ${dueButtonView(s)}
        </div>
      </div>
      <ol class="deck-stages" aria-label="Stage">${stagesHtml}</ol>
      <div class="song-planner-tasks-section">
        ${tasksListView()}
        ${addTaskFormView()}
      </div>
    </div>`;
  }

  function dueButtonView(s) {
    const hasDue = Boolean(s.due_value) || (Boolean(s.due) && s.due !== 'No due date');
    const primary = hasDue ? (s.due || `Due ${s.due_value}`) : 'No due date';
    const secondary = s.due_source === 'legacy'
      ? 'From planner · Change'
      : hasDue ? (s.due_value || 'Change due date') : 'Set due date';
    return `<button class="deck-due${s.overdue ? ' is-overdue' : ''}" type="button" data-planner="due" aria-haspopup="dialog" aria-expanded="${state.menu === 'due'}" aria-label="${esc(primary)}. Change due date"><strong>${esc(primary)}</strong><span>${esc(secondary)}</span></button>`;
  }

  function tasksListView() {
    if (!state.tasks.length) {
      return '<p class="deck-hint song-planner-empty-tasks">No tasks yet for this song.</p>';
    }
    const items = state.tasks.map(task => {
      const id = String(task.id);
      if (String(state.editingTaskId) === id) {
        return `<li class="song-task-row is-editing">
          <form class="deck-task deck-task-form song-task-edit-form" data-planner-form="task-edit" data-task-id="${esc(id)}">
            <input class="text-input" id="songTaskEditInput" name="text" maxlength="200" autocomplete="off" aria-label="Task text" value="${esc(task.text)}">
            <input class="song-task-date${isTaskOverdue(task) ? ' is-overdue' : ''}" type="date" name="due_date" aria-label="Task due date" value="${esc(task.due_date || '')}">
            <button class="btn small" type="submit">Save</button>
            <button class="btn small subtle" type="button" data-planner="task-cancel" data-task-id="${esc(id)}">Cancel</button>
          </form>
        </li>`;
      }
      const overdue = isTaskOverdue(task);
      return `<li class="song-task-row${task.done ? ' is-done' : ''}">
        <div class="deck-task song-task-main">
          <button class="deck-check${task.done ? ' is-checked' : ''}" type="button" data-planner="task-toggle" data-task-id="${esc(id)}" aria-label="${task.done ? 'Reopen' : 'Mark done'}: ${esc(task.text)}" title="${task.done ? 'Reopen task' : 'Mark done'}"></button>
          <button class="deck-task-text${task.done ? ' is-done' : ''}" type="button" data-planner="task-edit" data-task-id="${esc(id)}" title="Edit task">${esc(task.text)}</button>
        </div>
        <div class="song-task-controls">
          <input class="song-task-date${overdue ? ' is-overdue' : ''}" type="date" data-planner="task-date" data-task-id="${esc(id)}" value="${esc(task.due_date || '')}" aria-label="Due date for ${esc(task.text)}">
          <button class="btn small subtle song-task-delete" type="button" data-planner="task-delete" data-task-id="${esc(id)}" aria-label="Delete task: ${esc(task.text)}">Delete</button>
        </div>
      </li>`;
    }).join('');
    return `<ul class="song-task-list" aria-label="Tasks">${items}</ul>`;
  }

  function addTaskFormView() {
    return `<form class="deck-task deck-task-form song-task-add-form" data-planner-form="task-add">
      <input class="text-input" id="songTaskAddInput" name="text" maxlength="200" autocomplete="off" placeholder="Add a task…" aria-label="New task" value="${esc(state.newTaskText)}">
      <input class="song-task-date" id="songTaskAddDate" type="date" name="due_date" aria-label="New task due date" value="${esc(state.newTaskDate)}">
      <button class="btn small" type="submit">Add</button>
    </form>`;
  }

  function menuView(s) {
    const presets = [[3, 'In 3 days'], [7, 'In 1 week'], [14, 'In 2 weeks'], [28, 'In 4 weeks']];
    return `<div class="deck-menu" role="dialog" aria-label="Due date">
      <div class="deck-menu-label">Due date</div>
      ${presets.map(([days, label]) => `<button class="deck-menu-item" type="button" data-planner="due-set" data-date="${daysFromToday(days)}">${label}<small>${esc(new Date(daysFromToday(days) + 'T00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' }))}</small></button>`).join('')}
      <label class="deck-menu-date">On a date<input type="date" id="songPlannerDueInput" value="${esc(s.due_value || '')}" min="${localIso(new Date())}"></label>
      <hr>
      <button class="deck-menu-item" type="button" data-planner="due-set" data-date="">Clear due date</button>
      ${s.due_source === 'legacy' ? '<p class="deck-menu-note">This date comes from the older project planner. Set a date here to replace it.</p>' : ''}
    </div>`;
  }

  function positionMenu() {
    const menu = panel.querySelector('.deck-menu');
    const trigger = panel.querySelector(`[data-planner="${state.menu}"]`);
    if (!menu || !trigger) return;
    const box = panel.getBoundingClientRect(), t = trigger.getBoundingClientRect();
    const width = Math.min(menu.offsetWidth, box.width - 16), height = menu.offsetHeight;
    const above = t.bottom + 6 + height > box.bottom && t.top - 6 - height >= box.top;
    menu.style.top = `${(above ? t.top - box.top - 6 - height : t.bottom - box.top + 6) + panel.scrollTop}px`;
    menu.style.left = `${Math.max(8, Math.min(t.right - box.left - width, box.width - width - 8))}px`;
  }

  function openMenu(name, trigger) {
    menuTrigger = trigger;
    state.menu = name;
    draw();
    const menu = panel.querySelector('.deck-menu');
    menu?.querySelector('button,input')?.focus({ preventScroll: true });
  }

  function closeMenu({ focus = true } = {}) {
    if (!state.menu) return;
    const name = state.menu;
    state.menu = null;
    draw();
    if (focus) (q(`[data-planner="${name}"]`) || menuTrigger)?.focus();
  }

  function closePopup() {
    if (!state.selectedKey) return;
    const key = state.selectedKey;
    detailSeq++;
    state.selectedKey = '';
    state.song = null;
    state.tasks = [];
    state.detailLoading = false;
    state.detailError = '';
    state.menu = null;
    state.editingTaskId = null;
    draw();
    root.querySelector(`[data-planner="select"][data-key="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
  }

  let backdropPress = false;
  document.addEventListener('pointerdown', event => {
    const target = event.target instanceof Element ? event.target : null;
    backdropPress = Boolean(popup) && target === popup;
    if (state.menu && !target?.closest('#songPlannerPopup .deck-menu') && !target?.closest('#songPlannerPopup [data-planner="due"]')) {
      closeMenu({ focus: false });
    }
  });
  document.addEventListener('click', event => {
    if (popup && backdropPress && event.target === popup && !state.busy) closePopup();
    backdropPress = false;
  });
  window.addEventListener('resize', () => { if (state.menu) positionMenu(); });

  // ── Events ───────────────────────────────────────────────────────────────

  function findTask(rawId) {
    return state.tasks.find(t => String(t.id) === String(rawId)) || null;
  }

  function taskIdValue(rawId) {
    const found = findTask(rawId);
    if (found) return found.id;
    const num = Number(rawId);
    return Number.isFinite(num) && String(num) === String(rawId) ? num : rawId;
  }

  async function onClick(event) {
    const button = event.target.closest('button[data-planner]');
    if (!button || button.disabled) return;
    const kind = button.dataset.planner;
    const s = state.song || {};

    if (kind === 'retry') {
      state.listError = '';
      draw();
      await loadList(state.query, { force: true });
    } else if (kind === 'retry-detail') {
      state.detailError = '';
      draw();
      await selectSong(state.selectedKey, { force: true });
    } else if (kind === 'select') {
      const opening = !state.selectedKey;
      const pendingSelect = selectSong(button.dataset.key, { force: true });
      if (opening) panel.focus({ preventScroll: true });
      await pendingSelect;
    } else if (kind === 'close') {
      closePopup();
    } else if (kind === 'due') {
      state.menu === 'due' ? closeMenu() : openMenu('due', button);
    } else if (kind === 'due-set') {
      await setDue(button.dataset.date || '');
    } else if (kind === 'stage') {
      if (!s.key || button.dataset.stage === s.stage) return;
      await act({ action: 'stage', key: s.key, stage: button.dataset.stage }, { pending: button });
      q(`[data-planner="stage"][data-stage="${CSS.escape(button.dataset.stage)}"]`)?.focus();
    } else if (kind === 'release') {
      if (!s.key) return;
      closeMenu({ focus: false });
      const title = s.display_title || s.title;
      const ok = await confirmDialog({
        title: 'Mark as released?',
        body: `<p>“${esc(title)}” leaves On Deck and stops appearing in suggestions. Its tracked time is kept.</p>`,
        confirmLabel: 'Mark released',
      });
      if (ok && await act({ action: 'stage', key: s.key, stage: 'released' })) {
        toast(`${title} is released`);
      } else {
        q('[data-planner="release"]')?.focus();
      }
    } else if (kind === 'task-toggle') {
      const task = findTask(button.dataset.taskId);
      if (!task || !s.key) return;
      await act({ action: 'task_update', key: s.key, id: task.id, done: !task.done }, { pending: button });
      q(`[data-planner="task-toggle"][data-task-id="${CSS.escape(String(task.id))}"]`)?.focus();
    } else if (kind === 'task-edit') {
      const task = findTask(button.dataset.taskId);
      if (!task) return;
      state.editingTaskId = task.id;
      draw();
      const input = q('#songTaskEditInput');
      input?.focus();
      input?.select();
    } else if (kind === 'task-cancel') {
      const rawId = button.dataset.taskId;
      state.editingTaskId = null;
      draw();
      if (rawId) q(`[data-planner="task-edit"][data-task-id="${CSS.escape(rawId)}"]`)?.focus();
    } else if (kind === 'task-delete') {
      const task = findTask(button.dataset.taskId);
      if (!task || !s.key) return;
      const key = s.key;
      const savedText = task.text;
      const savedDue = task.due_date || '';
      const res = await act({ action: 'task_delete', key, id: task.id }, { pending: button });
      if (!res) return;
      toast(`Deleted: ${savedText}`, {
        label: 'Undo',
        onClick: async () => {
          await act({ action: 'task_add', key, text: savedText, due_date: savedDue });
        },
      });
    }
  }

  async function setDue(date) {
    const s = state.song;
    if (!s?.key) return;
    state.menu = null;
    const res = await act({ action: 'due', key: s.key, date });
    q('[data-planner="due"]')?.focus();
    if (res) toast(date ? (state.song?.due || `Due ${date}`) : 'Due date cleared');
  }

  async function onSubmit(event) {
    const form = event.target.closest('[data-planner-form]');
    if (!form) return;
    event.preventDefault();
    const s = state.song;
    if (!s?.key) return;

    const kind = form.dataset.plannerForm;
    if (kind === 'task-add') {
      const text = (form.elements.text?.value || '').trim();
      const due_date = form.elements.due_date?.value || '';
      if (!text) {
        form.elements.text?.focus();
        return;
      }
      state.newTaskText = '';
      state.newTaskDate = '';
      const res = await act({ action: 'task_add', key: s.key, text, due_date });
      if (!res) {
        state.newTaskText = text;
        state.newTaskDate = due_date;
        draw();
      }
      q('#songTaskAddInput')?.focus();
    } else if (kind === 'task-edit') {
      const rawId = form.dataset.taskId;
      const task = findTask(rawId);
      const id = taskIdValue(rawId);
      const text = (form.elements.text?.value || '').trim();
      const due_date = form.elements.due_date?.value || '';
      state.editingTaskId = null;
      if (!text || (task && text === task.text && due_date === (task.due_date || ''))) {
        draw();
        q(`[data-planner="task-edit"][data-task-id="${CSS.escape(String(id))}"]`)?.focus();
        return;
      }
      await act({ action: 'task_update', key: s.key, id, text, due_date });
      q(`[data-planner="task-edit"][data-task-id="${CSS.escape(String(id))}"]`)?.focus();
    }
  }

  function onInput(event) {
    if (event.target.id === 'songPlannerSearch') {
      state.query = event.target.value;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => loadList(state.query, { force: true }), 150);
    } else if (event.target.id === 'songTaskAddInput') {
      state.newTaskText = event.target.value;
    }
  }

  async function onChange(event) {
    const target = event.target;
    if (target.id === 'songPlannerDueInput' && target.value) {
      await setDue(target.value);
    } else if (target.id === 'songTaskAddDate') {
      state.newTaskDate = target.value;
    } else if (target.dataset.planner === 'task-date' && state.song?.key) {
      const id = taskIdValue(target.dataset.taskId);
      await act({ action: 'task_update', key: state.song.key, id, due_date: target.value || '' });
      q(`[data-planner="task-date"][data-task-id="${CSS.escape(String(id))}"]`)?.focus();
    }
  }

  function onKeydown(event) {
    if (event.key === 'Escape') {
      if (state.menu) {
        event.preventDefault();
        event.stopPropagation();
        closeMenu();
      } else if (state.editingTaskId !== null) {
        const rawId = String(state.editingTaskId);
        event.preventDefault();
        state.editingTaskId = null;
        draw();
        q(`[data-planner="task-edit"][data-task-id="${CSS.escape(rawId)}"]`)?.focus();
      } else if (popup.contains(event.target)) {
        event.preventDefault();
        event.stopPropagation();
        closePopup();
      }
      return;
    }
    if (event.key === 'Tab' && !state.menu && popup.contains(event.target)) {
      const items = [...panel.querySelectorAll('button:not(:disabled),input:not(:disabled)')];
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey ? (document.activeElement === first || document.activeElement === panel) : document.activeElement === last) {
        event.preventDefault();
        (event.shiftKey ? last : first)?.focus();
      }
      return;
    }
    if (event.target.id === 'songPlannerSearch' && event.key === 'ArrowDown') {
      const firstRow = root.querySelector('[data-planner="select"]');
      if (firstRow) {
        event.preventDefault();
        firstRow.focus();
      }
      return;
    }
    if (event.target.dataset.planner === 'select' && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      const rows = [...root.querySelectorAll('[data-planner="select"]')];
      const index = rows.indexOf(document.activeElement);
      if (index < 0) return;
      event.preventDefault();
      if (event.key === 'ArrowUp' && index === 0) {
        root.querySelector('#songPlannerSearch')?.focus();
      } else {
        rows[Math.max(0, Math.min(rows.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)))]?.focus();
      }
      return;
    }
    const menu = event.target.closest('.deck-menu');
    if (menu && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      const items = [...menu.querySelectorAll('button,input')];
      const index = items.indexOf(document.activeElement);
      event.preventDefault();
      items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
      return;
    }
    if (menu && event.key === 'Tab') {
      const items = [...menu.querySelectorAll('button,input')];
      const last = items[items.length - 1], first = items[0];
      if ((!event.shiftKey && document.activeElement === last) || (event.shiftKey && document.activeElement === first)) {
        closeMenu();
      }
      return;
    }
    if (event.target.closest('.deck-stages') && (event.key === 'ArrowRight' || event.key === 'ArrowLeft')) {
      const items = [...panel.querySelectorAll('.deck-stage:not(:disabled)')];
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
