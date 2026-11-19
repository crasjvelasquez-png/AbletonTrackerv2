/* Planner tab: Agenda, project plans (hold, star, to-dos, people, notes) and Contacts.
   Renders into #plannerRoot (inside #appPlanner). Talks to /api/planner only; On Deck and
   Three Decks stay on the Dashboard. Vanilla JS, no dependencies. */
window.Planner = (() => {
  const esc = value => escapeHtml(String(value ?? ''));
  // Inline lucide icons (stroke 1.8, round caps, currentColor), same style as the dashboard's SVGs.
  const ICON_PATHS = {
    star: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    'chevron-right': '<path d="m9 18 6-6-6-6"/>',
    'chevron-left': '<path d="m15 18-6-6 6-6"/>',
    more: '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
  };
  const icon = (name, { size = 16, filled = false } = {}) =>
    `<svg class="pl-ico" width="${size}" height="${size}" viewBox="0 0 24 24" fill="${filled ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICON_PATHS[name]}</svg>`;
  const STAGES = [
    ['idea', 'Idea'], ['production', 'Production'], ['recording', 'Recording'],
    ['mixing', 'Mixing'], ['mastering', 'Mastering'], ['released', 'Released'],
  ];
  const LANE_LABELS = { produce: 'Produce', mix: 'Mix', master: 'Master' };
  const BUCKETS = [['overdue', 'Overdue'], ['today', 'Today'], ['week', 'This week'], ['later', 'Later'], ['someday', 'Someday']];
  const ARCHIVE_KEY = 'planner.archiveOpen';
  const GENERIC_ERROR = 'Planner could not load.';

  const state = {
    overview: null,
    loadError: '',
    view: 'agenda',        // 'agenda' | 'project' | 'contacts'
    key: '',
    detail: null,          // GET ?key= payload
    detailError: '',
    contactId: null,       // null | 'new' | number
    contact: null,         // GET ?contact= payload
    contacts: { error: '' },
    query: '',
    archiveOpen: false,
    holdOpen: false,       // inline "Put on hold" form
    holdEdit: false,       // editing an existing hold
    personOpen: false,
    personNew: false,
    menuOpen: false,
    doneOpen: false,
    editingTodo: null,
    drafts: { quickText: '', quickDate: '', todoText: '', todoDate: '' },
    mobile: 'list',
    savedTimer: null,
    notesSavedTimer: null,
  };
  let root = null;
  let navEl = null;
  let detailEl = null;
  let searchEl = null;
  let overviewSeq = 0;
  let detailSeq = 0;
  let contactSeq = 0;
  let notesTimer = null;

  try { state.archiveOpen = localStorage.getItem(ARCHIVE_KEY) === '1'; } catch (_) { /* storage may be blocked */ }

  // ---- helpers -------------------------------------------------------------
  const localIso = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const todayIso = () => state.overview?.today || localIso(new Date());
  const tomorrowIso = () => { const d = new Date(); d.setDate(d.getDate() + 1); return localIso(d); };
  const parseIso = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || ''); return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null; };
  function fmtDate(iso) {
    const d = parseIso(iso);
    if (!d) return '';
    const opts = { month: 'short', day: 'numeric' };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
    return d.toLocaleDateString(undefined, opts);
  }
  const fmtSince = ts => (ts ? fmtDate(localIso(new Date(ts * 1000))) : '');
  const idleText = days => (days <= 0 ? 'today' : days === 1 ? 'yesterday' : `${days} days ago`);
  const stageLabel = stage => (STAGES.find(([k]) => k === stage) || [])[1] || stage || '';
  const savedText = () => (state.savedUntil > Date.now() ? 'Saved' : '');
  const q = selector => root?.querySelector(selector) || null;
  const qa = selector => [...(root?.querySelectorAll(selector) || [])];
  const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const mobileQuery = () => window.matchMedia?.('(max-width: 759px)');

  async function api(url, options) {
    const res = await fetch(url, { cache: 'no-store', ...options });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || 'Something went wrong. Try again.');
    return data;
  }

  function notify(message, action = null) {
    if (typeof toast === 'function') toast(message, action);
  }

  function setMobile(value) {
    state.mobile = value;
    root?.setAttribute('data-mobile', value);
  }

  // ---- loading -------------------------------------------------------------
  async function loadOverview() {
    const seq = ++overviewSeq;
    try {
      const data = await api('/api/planner');
      if (seq !== overviewSeq) return;
      state.overview = data;
      state.loadError = '';
    } catch (error) {
      console.error(error);
      if (seq !== overviewSeq) return;
      state.loadError = GENERIC_ERROR;
    }
    drawNav();
    if (state.view === 'agenda' || state.view === 'contacts' || state.loadError) drawDetailUnlessBusy();
  }

  async function loadProject(key, { quiet = false } = {}) {
    const seq = ++detailSeq;
    if (!quiet) { state.detail = null; state.detailError = ''; drawDetail(); }
    try {
      const data = await api(`/api/planner?key=${encodeURIComponent(key)}`);
      if (seq !== detailSeq || state.key !== key) return;
      state.detail = data;
      state.detailError = '';
    } catch (error) {
      if (seq !== detailSeq) return;
      state.detail = null;
      state.detailError = error.message || 'Song could not load.';
    }
    if (quiet) drawDetailUnlessBusy(); else drawDetail();
  }

  async function loadContact(id, { quiet = false } = {}) {
    const seq = ++contactSeq;
    try {
      const data = await api(`/api/planner?contact=${encodeURIComponent(id)}`);
      if (seq !== contactSeq || state.contactId !== id) return;
      state.contact = data;
      state.contacts.error = '';
    } catch (error) {
      if (seq !== contactSeq) return;
      state.contact = null;
      state.contacts.error = error.message;
    }
    if (quiet) drawDetailUnlessBusy(); else drawDetail();
  }

  /* POST an action. Returns the response, or null after showing the error. */
  async function act(body) {
    try {
      const data = await api('/api/planner', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (data.view === 'project') {
        if (state.view === 'project' && state.key === body.key) { detailSeq++; state.detail = { project: data.project, todos: data.todos, people: data.people, contacts_all: data.contacts_all }; }
      } else if (data.view === 'contact') {
        if (state.view === 'contacts') {
          contactSeq++;
          state.contactId = data.contact.id;
          state.contact = { contact: data.contact, songs: data.songs };
        }
      } else if (data.view === 'overview') {
        state.overview = (({ ok, view, ...rest }) => rest)(data);
      }
      if (data.view !== 'overview') loadOverview();
      else { drawNav(); }
      return data;
    } catch (error) {
      notify(error.message);
      return null;
    }
  }

  // ---- selection -----------------------------------------------------------
  function selectAgenda() {
    state.view = 'agenda'; state.key = ''; state.contactId = null; state.contact = null;
    resetTransient();
    setMobile('detail');
    drawNav(); drawDetail();
    loadOverview();
  }

  function selectProject(key) {
    if (!key) return;
    state.view = 'project'; state.key = key; state.contactId = null;
    resetTransient();
    setMobile('detail');
    drawNav();
    loadProject(key);
  }

  function selectContacts(id = null) {
    state.view = 'contacts'; state.key = '';
    state.contactId = id; state.contact = null;
    resetTransient();
    setMobile('detail');
    drawNav(); drawDetail();
    if (typeof id === 'number') loadContact(id);
    loadOverview();
  }

  function resetTransient() {
    state.holdOpen = false; state.holdEdit = false; state.personOpen = false; state.personNew = false;
    state.menuOpen = false; state.editingTodo = null; state.contacts.error = '';
  }

  // ---- rendering: left list ------------------------------------------------
  function filteredProjects() {
    const projects = state.overview?.projects || [];
    const term = state.query.trim().toLowerCase();
    if (!term) return projects;
    return projects.filter(p => `${p.title} ${p.full_title} ${p.key}`.toLowerCase().includes(term));
  }

  function rowSub(p) {
    if (p.group === 'archive') return p.settled ? 'Released' : `${stageLabel(p.stage)} · idle ${p.days_idle}d`;
    if (p.status === 'on_hold' && !p.hold_expired) return p.waiting_on_name ? `Waiting on ${p.waiting_on_name}` : p.hold_reason || 'On hold';
    return [p.stage_label || stageLabel(p.stage), p.next_todo].filter(Boolean).join(' · ');
  }

  function rowSubHtml(p) {
    if (p.group === 'archive' && !p.settled) return `${esc(stageLabel(p.stage))} · <span class="pl-mono">idle ${p.days_idle}d</span>`;
    return esc(rowSub(p));
  }

  function rowHtml(p) {
    const selected = state.view === 'project' && state.key === p.key;
    const held = p.status === 'on_hold' && !p.hold_expired;
    const chips = [];
    if (held && p.hold_until) chips.push(`<span class="pl-chip">Back ${esc(fmtDate(p.hold_until))}</span>`);
    else if (p.due_value && !p.settled) chips.push(`<span class="pl-chip${p.overdue && p.group !== 'archive' ? ' is-overdue' : ''}">${esc(fmtDate(p.due_value))}</span>`);
    const total = p.todo_open + p.todo_done;
    if (total) chips.push(`<span class="pl-count" title="To-dos done">${p.todo_done}/${total}</span>`);
    if (p.stale && p.group !== 'archive') chips.push(`<span class="pl-idle" title="Not opened in ${p.days_idle} days">idle ${p.days_idle}d</span>`);
    return `<button type="button" class="pl-row${selected ? ' is-active' : ''}" data-pl-project="${esc(p.key)}" title="${esc(p.full_title)}" ${selected ? 'aria-current="true"' : ''}>
      <span class="pl-row-main">
        <span class="pl-row-title">${p.priority ? `<span class="pl-star" aria-label="Starred">${icon('star', { size: 14, filled: true })}</span>` : ''}<span class="pl-trunc">${esc(p.title)}</span></span>
        <span class="pl-row-sub pl-trunc">${rowSubHtml(p)}</span>
      </span>
      <span class="pl-row-side">${chips.join('')}</span>
    </button>`;
  }

  function drawNav() {
    if (!navEl) return;
    const ov = state.overview;
    if (!ov) {
      navEl.innerHTML = state.loadError ? '' : '<div class="pl-skel" aria-hidden="true"></div><div class="pl-skel" aria-hidden="true"></div><div class="pl-skel" aria-hidden="true"></div>';
      return;
    }
    const overdue = ov.counts?.overdue_todos || 0;
    const filtered = filteredProjects();
    const searching = Boolean(state.query.trim());
    const groups = [['active', 'Active'], ['on_hold', 'On hold'], ['archive', 'Settled']];
    let html = `<div class="pl-navgroup">
      <button type="button" class="pl-navrow${state.view === 'agenda' ? ' is-active' : ''}" data-pl-nav="agenda" ${state.view === 'agenda' ? 'aria-current="true"' : ''}>
        <span>Agenda</span>${overdue ? `<span class="pl-badge" title="Overdue to-dos" aria-label="${overdue} overdue">${overdue}</span>` : ''}
      </button>
      <button type="button" class="pl-navrow${state.view === 'contacts' ? ' is-active' : ''}" data-pl-nav="contacts" ${state.view === 'contacts' ? 'aria-current="true"' : ''}>
        <span>Contacts</span><span class="pl-count">${(ov.contacts || []).length}</span>
      </button>
    </div>`;
    for (const [group, label] of groups) {
      const items = filtered.filter(p => p.group === group);
      if (!items.length) continue;
      if (group === 'archive') {
        const open = state.archiveOpen || searching;
        html += `<div class="pl-group">
          <button type="button" class="pl-group-head is-toggle" data-pl-group="archive" aria-expanded="${open}">
            <span class="pl-chevron" aria-hidden="true">${icon('chevron-right', { size: 14 })}</span><span>${label}</span><span class="pl-count">${items.length}</span>
          </button>
          ${open ? `<div class="pl-rows" data-pl-list="archive">${items.map(rowHtml).join('')}</div>` : ''}
        </div>`;
      } else {
        html += `<div class="pl-group">
          <div class="pl-group-head"><span>${label}</span><span class="pl-count">${items.length}</span></div>
          <div class="pl-rows" data-pl-list="${group}">${items.map(rowHtml).join('')}</div>
        </div>`;
      }
    }
    if (searching && !filtered.length) html += '<p class="pl-muted pl-pad">No songs match</p>';
    else if (!searching && !(ov.projects || []).length) html += '<p class="pl-muted pl-pad">No songs yet. They appear once Ableton sessions are tracked.</p>';
    const attrs = ['data-pl-project', 'data-pl-nav', 'data-pl-group'];
    const focused = navEl.contains(document.activeElement) ? document.activeElement : null;
    const keep = focused && attrs.map(a => [a, focused.getAttribute(a)]).find(([, v]) => v !== null);
    navEl.innerHTML = html;
    if (keep) qa(`[${keep[0]}]`).find(el => el.getAttribute(keep[0]) === keep[1])?.focus();
  }

  // ---- rendering: detail pane ---------------------------------------------
  const backButton = `<button type="button" class="pl-back" data-pl="back" aria-label="Back to the list">${icon('chevron-left', { size: 14 })}<span>Back</span></button>`;

  function detailBusy() {
    const el = document.activeElement;
    return Boolean(el && detailEl?.contains(el) && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) || state.menuOpen;
  }

  function drawDetailUnlessBusy() {
    if (!detailBusy()) drawDetail();
  }

  function drawDetail() {
    if (!detailEl) return;
    if (state.loadError && !state.overview) {
      detailEl.innerHTML = `<div class="pl-pane" data-pl-pane="error">${backButton}<div class="pl-empty"><p>${esc(state.loadError)}</p><button type="button" class="btn small" data-pl="retry">Try again</button></div></div>`;
      return;
    }
    if (!state.overview) {
      detailEl.innerHTML = '<div class="pl-pane" aria-busy="true"><div class="pl-skel is-title"></div><div class="pl-skel"></div><div class="pl-skel"></div></div>';
      return;
    }
    const scroll = window.scrollY;
    detailEl.innerHTML = state.view === 'project' ? projectView() : state.view === 'contacts' ? contactsView() : agendaView();
    if (window.scrollY !== scroll) window.scrollTo(0, scroll);
    detailEl.querySelectorAll('textarea[data-autosize]').forEach(autosize);
  }

  function autosize(el) { el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight + 2, 420)}px`; }

  function dateChip(iso, overdue) {
    return iso ? `<span class="pl-chip${overdue ? ' is-overdue' : ''}">${esc(fmtDate(iso))}</span>` : '<span class="pl-chip-slot" aria-hidden="true"></span>';
  }

  function agendaTodoHtml(item) {
    const song = item.song_key
      ? (item.orphan
        ? `<span class="pl-muted pl-trunc" title="This song is no longer in the tracker">${esc(item.song_title)}</span>`
        : `<button type="button" class="pl-link pl-trunc" data-pl-open="${esc(item.song_key)}">${esc(item.song_title)}</button>`)
      : '<span class="pl-muted">General</span>';
    return `<li class="pl-todo" data-todo-id="${item.id}" data-song-key="${esc(item.song_key)}">
      <input type="checkbox" class="pl-check" data-pl-check aria-label="Mark done: ${esc(item.text)}">
      <span class="pl-todo-text">${esc(item.text)}</span>
      <span class="pl-todo-meta">${song}${dateChip(item.due_date, item.overdue)}</span>
    </li>`;
  }

  function agendaView() {
    const ov = state.overview;
    const agenda = ov.agenda || [];
    const deck = ov.deck;
    let html = `<div class="pl-pane" data-pl-pane="agenda">
      <div class="pl-head">${backButton}<h2 class="pl-h1">Agenda</h2></div>`;
    if (deck) {
      html += `<p class="pl-deck"><span class="pl-muted">On Deck</span> <button type="button" class="pl-link" data-pl-open="${esc(deck.key)}">${esc(deck.title)}</button>
        <span class="pl-muted">· ${esc(deck.stage_label)}${deck.due ? ` · ${esc(deck.due)}` : ''}</span></p>`;
    }
    html += `<form class="pl-quick" data-pl-form="quick-add" autocomplete="off">
      <input class="text-input" id="plQuickText" maxlength="200" placeholder="Add a to-do" aria-label="New to-do" value="${esc(state.drafts.quickText)}" data-pl-enter>
      <input class="text-input pl-date" id="plQuickDate" type="date" aria-label="Due date (optional)" value="${esc(state.drafts.quickDate)}">
      <button class="btn is-primary small" type="submit">Add</button>
    </form>`;
    if (!agenda.length) {
      html += '<div class="pl-empty"><p>No open to-dos</p></div>';
    } else {
      for (const [bucket, label] of BUCKETS) {
        const items = agenda.filter(i => i.bucket === bucket);
        if (!items.length) continue;
        html += `<section class="pl-bucket${bucket === 'overdue' ? ' is-overdue' : ''}" data-bucket="${bucket}">
          <h3 class="pl-h3">${label} <span class="pl-count">${items.length}</span></h3>
          <ul class="pl-todos">${items.map(agendaTodoHtml).join('')}</ul>
        </section>`;
      }
    }
    return `${html}</div>`;
  }

  function holdPanelHtml(p, d) {
    const held = p.status === 'on_hold';
    const editing = state.holdOpen || state.holdEdit;
    const contactsAll = d.contacts_all || [];
    if (held && !editing) {
      const since = fmtSince(p.hold_since);
      const lines = [];
      if (p.hold_expired) lines.push(`<p class="pl-hold-lead">Hold ended ${esc(fmtDate(p.hold_until))}. Resume to clear it, or edit to set a new date.</p>`);
      if (p.hold_reason) lines.push(`<p class="pl-hold-reason">${esc(p.hold_reason)}</p>`);
      const meta = [];
      if (p.waiting_on_name) meta.push(`Waiting on ${esc(p.waiting_on_name)}`);
      if (p.hold_until && !p.hold_expired) meta.push(`Back on ${esc(fmtDate(p.hold_until))}`);
      if (since) meta.push(`On hold since ${esc(since)}`);
      return `<section class="pl-hold-panel${p.hold_expired ? ' is-expired' : ''}" aria-label="On hold">
        <div class="pl-hold-head"><strong>On hold</strong><button type="button" class="btn small subtle" data-pl="hold-edit">Edit</button></div>
        ${lines.join('')}
        ${meta.length ? `<p class="pl-muted pl-hold-meta">${meta.map(m => `<span>${m}</span>`).join('')}</p>` : ''}
      </section>`;
    }
    if (!editing) return '';
    return `<section class="pl-hold-panel is-form" aria-label="Put on hold">
      <form data-pl-form="hold" class="pl-form-grid">
        <label class="pl-field pl-span"><span>Reason</span>
          <input class="text-input" id="plHoldReason" maxlength="200" placeholder="What is it waiting for?" value="${esc(p.hold_reason)}"></label>
        <label class="pl-field"><span>Waiting on</span>
          <select class="text-input" id="plHoldWaiting"><option value="">Nobody</option>${contactsAll.map(c => `<option value="${c.id}"${c.id === p.waiting_on ? ' selected' : ''}>${esc(c.name)}${c.role ? ` · ${esc(c.role)}` : ''}</option>`).join('')}</select></label>
        <label class="pl-field"><span>Back on</span>
          <input class="text-input" id="plHoldUntil" type="date" min="${esc(tomorrowIso())}" value="${esc(p.hold_expired ? '' : p.hold_until)}"></label>
        <div class="pl-actions pl-span">
          <button type="submit" class="btn is-primary small" data-pl="hold-save">${held ? 'Save' : 'Put on hold'}</button>
          <button type="button" class="btn small subtle" data-pl="hold-cancel">Cancel</button>
        </div>
      </form>
    </section>`;
  }

  function todoRowHtml(t, done = false) {
    const overdue = !done && t.due_date && t.due_date < todayIso();
    if (state.editingTodo === t.id && !done) {
      return `<li class="pl-todo is-editing" data-todo-id="${t.id}">
        <form class="pl-todo-edit" data-pl-form="todo-edit" data-todo-id="${t.id}">
          <input class="text-input" name="text" maxlength="200" value="${esc(t.text)}" aria-label="To-do text" data-pl-enter>
          <input class="text-input pl-date" name="due_date" type="date" value="${esc(t.due_date)}" aria-label="Due date">
          <button type="submit" class="btn small">Save</button>
          <button type="button" class="btn small subtle" data-pl="todo-cancel">Cancel</button>
        </form>
      </li>`;
    }
    return `<li class="pl-todo${done ? ' is-done' : ''}" data-todo-id="${t.id}">
      <input type="checkbox" class="pl-check" data-pl-check ${done ? 'checked' : ''} aria-label="${done ? 'Reopen' : 'Mark done'}: ${esc(t.text)}">
      ${done
        ? `<span class="pl-todo-text">${esc(t.text)}</span>`
        : `<button type="button" class="pl-todo-text is-editable" data-pl="todo-edit" data-todo-id="${t.id}" title="Edit">${esc(t.text)}</button>`}
      <span class="pl-todo-meta">${dateChip(t.due_date, overdue)}
        <button type="button" class="pl-icon-btn" data-pl="todo-delete" data-todo-id="${t.id}" aria-label="Delete to-do: ${esc(t.text)}" title="Delete">${icon('x')}</button>
      </span>
    </li>`;
  }

  function peopleHtml(d) {
    const people = d.people || [];
    const linkedIds = new Set(people.map(p => p.id));
    const choices = (d.contacts_all || []).filter(c => !linkedIds.has(c.id));
    let html = '<section class="pl-section" aria-labelledby="plPeopleH"><div class="pl-section-head"><h3 class="pl-h3" id="plPeopleH">People</h3>';
    if (!state.personOpen) html += '<button type="button" class="btn small subtle" data-pl="person-add">Add person</button>';
    html += '</div>';
    if (people.length) {
      html += '<ul class="pl-people">' + people.map(c => `<li class="pl-person" data-person-id="${c.id}">
        <div class="pl-person-main"><span class="pl-person-name pl-trunc">${esc(c.name)}</span>
          ${(c.role || c.email || c.phone) ? `<div class="pl-person-meta">${[
            c.role ? `<span>${esc(c.role)}</span>` : '',
            c.email ? `<a class="pl-link" href="mailto:${esc(c.email)}">${esc(c.email)}</a>` : '',
            c.phone ? `<a class="pl-link" href="tel:${esc(c.phone)}">${esc(c.phone)}</a>` : '',
          ].filter(Boolean).join('<span aria-hidden="true"> · </span>')}</div>` : ''}</div>
        <button type="button" class="pl-icon-btn" data-pl="person-remove" data-id="${c.id}" aria-label="Remove ${esc(c.name)} from this song" title="Remove">${icon('x')}</button>
      </li>`).join('') + '</ul>';
    } else if (!state.personOpen) {
      html += '<p class="pl-muted">No one linked yet</p>';
    }
    if (state.personOpen) {
      html += `<div class="pl-person-add">
        <select class="text-input" id="plPersonSelect" aria-label="Choose a person">
          <option value="">Choose a person</option>
          ${choices.map(c => `<option value="${c.id}">${esc(c.name)}${c.role ? ` · ${esc(c.role)}` : ''}</option>`).join('')}
          <option value="__new"${state.personNew ? ' selected' : ''}>New contact</option>
        </select>
        <button type="button" class="btn small subtle" data-pl="person-cancel">Cancel</button>
      </div>`;
      if (state.personNew) {
        html += `<form class="pl-person-new" data-pl-form="person-new">
          <input class="text-input" id="plNewName" maxlength="120" placeholder="Name" aria-label="New contact name" required data-pl-enter>
          <input class="text-input" id="plNewRole" maxlength="80" placeholder="Role (optional)" aria-label="New contact role" data-pl-enter>
          <button type="submit" class="btn small" data-pl="person-create">Create and add</button>
        </form>`;
      }
    }
    return `${html}</section>`;
  }

  function projectView() {
    if (state.detailError) {
      return `<div class="pl-pane" data-pl-pane="project-error">${backButton}<div class="pl-empty"><p>${esc(state.detailError)}</p>
        <button type="button" class="btn small" data-pl="back-agenda">Back to Agenda</button></div></div>`;
    }
    const d = state.detail;
    if (!d) return '<div class="pl-pane" aria-busy="true"><div class="pl-skel is-title"></div><div class="pl-skel"></div><div class="pl-skel"></div></div>';
    const p = d.project;
    const inferredIdx = STAGES.findIndex(([k]) => k === p.inferred_stage);
    const held = p.status === 'on_hold';
    const badges = [];
    if (p.on_deck) badges.push('<span class="pl-badge-pill is-accent">On Deck</span>');
    if (p.in_lane) badges.push(`<span class="pl-badge-pill">Three Decks · ${esc(LANE_LABELS[p.in_lane] || p.in_lane)}</span>`);
    if (p.hold_expired) badges.push('<span class="pl-badge-pill is-accent">Back from hold</span>');
    if (p.settled) badges.push('<span class="pl-badge-pill">Released</span>');
    else if (p.group === 'archive') badges.push('<span class="pl-badge-pill">Settled</span>');
    if (p.stale && p.group !== 'archive') badges.push(`<span class="pl-badge-pill is-warn">Idle ${p.days_idle} days</span>`);

    const TRACK = STAGES.slice(0, 5);
    const currentIdx = p.stage === 'released' ? TRACK.length : TRACK.findIndex(([k]) => k === p.stage);
    const stageTrack = `<ol class="deck-stages" aria-label="Stage">${TRACK.map(([k, label], i) => {
      const cls = i < currentIdx ? ' is-done' : i === currentIdx ? ' is-current' : '';
      const locked = inferredIdx >= 0 && i < inferredIdx;
      const attrs = i === currentIdx ? ' aria-current="step"' : '';
      const lock = locked ? ` aria-disabled="true" title="Detected from your ${esc(stageLabel(p.inferred_stage))} file"` : ` title="${esc(label)}"`;
      return `<li><button class="deck-stage${cls}" type="button" data-pl="stage" data-stage="${k}"${attrs}${lock}><i aria-hidden="true"></i><span>${esc(label)}</span></button></li>`;
    }).join('')}</ol>`;

    const canMove = !p.settled;
    const moveTo = p.group === 'archive' ? 'active' : 'archive';
    const menu = canMove ? `<div class="pl-menu-wrap">
        <button type="button" class="pl-icon-btn is-boxed" data-pl="more" aria-label="More actions" aria-haspopup="menu" aria-expanded="${state.menuOpen}">${icon('more')}</button>
        ${state.menuOpen ? `<div class="pl-menu" role="menu"><button type="button" role="menuitem" data-pl="list-set" data-value="${moveTo}">${moveTo === 'archive' ? 'Move to Settled' : 'Move to Active'}</button><button type="button" role="menuitem" data-pl="release">Mark released…</button></div>` : ''}
      </div>` : '';

    const open = (d.todos || []).filter(t => !t.done);
    const done = (d.todos || []).filter(t => t.done);
    const dueNote = p.due_source === 'legacy' ? '<p class="pl-muted pl-note">This date comes from the older project planner. Set a date here to replace it.</p>' : '';
    const worked = `Last worked ${idleText(p.days_idle)}`;
    const total = p.todo_open + p.todo_done;

    return `<div class="pl-pane" data-pl-pane="project">
      <div class="pl-head">${backButton}
        <div class="pl-title-wrap">
          <h2 class="pl-h1" id="plTitle" title="${esc(p.full_title)}">${esc(p.title)}</h2>
          ${badges.length ? `<div class="pl-badges">${badges.join('')}</div>` : ''}
        </div>
        <button type="button" class="pl-star-btn${p.priority ? ' is-on' : ''}" data-pl="star" aria-pressed="${p.priority ? 'true' : 'false'}" aria-label="${p.priority ? 'Remove star' : 'Star this song'}" title="${p.priority ? 'Starred' : 'Star'}"><span aria-hidden="true">${icon('star', { filled: !!p.priority })}</span></button>
      </div>
      ${stageTrack}
      <div class="pl-toolbar">
        <button type="button" class="btn small" data-pl="open-live">Open in Live</button>
        ${held ? `<button type="button" class="btn small" data-pl="resume">${p.hold_expired ? 'Clear hold' : 'Resume'}</button>`
          : (state.holdOpen || p.settled) ? '' : '<button type="button" class="btn small" data-pl="hold">Put on hold</button>'}
        ${menu}
      </div>
      ${holdPanelHtml(p, d)}
      <dl class="pl-facts">
        <div class="pl-fact"><dt>Due</dt><dd>
          <input class="text-input pl-date" id="plDue" type="date" value="${esc(p.due_source === 'song' ? p.due_value : p.due_source === 'legacy' ? p.due_value : '')}" aria-label="Due date">
          ${p.due_source === 'song' ? `<button type="button" class="pl-icon-btn" data-pl="due-clear" aria-label="Clear due date" title="Clear">${icon('x')}</button>` : ''}
          ${p.due_value ? `<span class="pl-chip${p.overdue && p.group !== 'archive' ? ' is-overdue' : ''}">${esc(p.due)}</span>` : ''}</dd></div>
        <div class="pl-fact"><dt>Activity</dt><dd>${esc(worked)}</dd></div>
        <div class="pl-fact"><dt>To-dos</dt><dd>${total ? `<span class="pl-mono">${p.todo_done}/${total} done</span>` : 'None yet'}</dd></div>
        ${p.next_task ? `<div class="pl-fact"><dt>On Deck next step</dt><dd>${esc(p.next_task)}</dd></div>` : ''}
      </dl>
      ${dueNote}
      <section class="pl-section" aria-labelledby="plTodosH">
        <div class="pl-section-head"><h3 class="pl-h3" id="plTodosH">To-dos</h3></div>
        ${open.length ? `<ul class="pl-todos pl-todos-open">${open.map(t => todoRowHtml(t)).join('')}</ul>` : '<ul class="pl-todos pl-todos-open"></ul>'}
        <form class="pl-quick" data-pl-form="todo-add" autocomplete="off">
          <input class="text-input" id="plTodoText" maxlength="200" placeholder="Add a to-do" aria-label="New to-do" value="${esc(state.drafts.todoText)}" data-pl-enter>
          <input class="text-input pl-date" id="plTodoDate" type="date" aria-label="Due date (optional)" value="${esc(state.drafts.todoDate)}">
          <button class="btn small" type="submit">Add</button>
        </form>
        ${p.todo_done ? `<details class="pl-done" data-pl-done ${state.doneOpen ? 'open' : ''}><summary>${icon('chevron-right', { size: 14 })}<span>Done (${p.todo_done})</span></summary>
          <ul class="pl-todos">${done.map(t => todoRowHtml(t, true)).join('')}</ul></details>` : ''}
      </section>
      ${peopleHtml(d)}
      <section class="pl-section" aria-labelledby="plNotesH">
        <div class="pl-section-head"><h3 class="pl-h3" id="plNotesH">Notes</h3><span class="pl-saved" aria-live="polite"></span></div>
        <textarea class="text-input pl-notes" id="plNotes" data-autosize maxlength="5000" rows="4" placeholder="Anything worth remembering about this song" aria-labelledby="plNotesH" data-key="${esc(p.key)}" data-saved="${esc(p.notes)}">${esc(p.notes)}</textarea>
      </section>
    </div>`;
  }

  function contactsView() {
    const list = state.overview.contacts || [];
    const sel = state.contactId;
    const c = state.contact?.contact;
    let form = '';
    if (sel === 'new' || c) {
      const v = sel === 'new' ? { id: '', name: '', role: '', email: '', phone: '', notes: '' } : c;
      const songs = state.contact?.songs || [];
      form = `<form class="pl-contact-form" data-pl-form="contact" data-id="${esc(v.id)}">
        <div class="pl-form-grid">
          <label class="pl-field pl-span"><span>Name</span><input class="text-input" id="plContactName" maxlength="120" required value="${esc(v.name)}"></label>
          <label class="pl-field"><span>Role</span><input class="text-input" id="plContactRole" maxlength="80" placeholder="Vocalist, mixer…" value="${esc(v.role)}"></label>
          <label class="pl-field"><span>Phone</span><input class="text-input" id="plContactPhone" maxlength="60" type="tel" value="${esc(v.phone)}"></label>
          <label class="pl-field pl-span"><span>Email</span><input class="text-input" id="plContactEmail" maxlength="200" type="email" value="${esc(v.email)}"></label>
          <label class="pl-field pl-span"><span>Notes</span><textarea class="text-input pl-notes" id="plContactNotes" maxlength="2000" rows="3">${esc(v.notes)}</textarea></label>
        </div>
        <div class="pl-actions">
          <button type="submit" class="btn is-primary small" data-pl="contact-save">${sel === 'new' ? 'Create contact' : 'Save'}</button>
          <span class="pl-saved" aria-live="polite">${savedText()}</span>
          ${sel === 'new' ? '' : '<button type="button" class="btn small danger pl-push" data-pl="contact-delete">Delete</button>'}
        </div>
        ${sel === 'new' ? '' : `<section class="pl-section pl-linked" aria-labelledby="plLinkedH"><h3 class="pl-h3" id="plLinkedH">Songs</h3>
          ${songs.length ? `<ul class="pl-people">${songs.map(s => `<li class="pl-person">
            <div class="pl-person-main">${s.orphan ? `<span class="pl-muted" title="This song is no longer in the tracker">${esc(s.title)}</span>` : `<button type="button" class="pl-link" data-pl-open="${esc(s.key)}">${esc(s.title)}</button>`}
              ${s.stage_label ? `<span class="pl-muted"> · ${esc(s.stage_label)}</span>` : ''}</div>
            <button type="button" class="pl-icon-btn" data-pl="contact-unlink" data-key="${esc(s.key)}" aria-label="Unlink ${esc(s.title)}" title="Unlink">${icon('x')}</button>
          </li>`).join('')}</ul>` : '<p class="pl-muted">Not linked to any song. Add them from a song\'s People section.</p>'}
        </section>`}
      </form>`;
    } else if (state.contacts.error) {
      form = `<div class="pl-empty"><p>${esc(state.contacts.error)}</p></div>`;
    } else if (typeof sel === 'number') {
      form = '<div class="pl-skel"></div><div class="pl-skel"></div>';
    } else {
      form = `<div class="pl-empty"><p>${list.length ? 'Choose a person to see their details' : 'No contacts yet. Add the people you work with.'}</p></div>`;
    }
    return `<div class="pl-pane" data-pl-pane="contacts">
      <div class="pl-head">${backButton}<h2 class="pl-h1">Contacts</h2>
        <button type="button" class="btn small pl-push" data-pl="contact-new">New contact</button></div>
      <div class="pl-contacts">
        <ul class="pl-contact-list" aria-label="Contacts">${list.map(x => `<li><button type="button" class="pl-row${sel === x.id ? ' is-active' : ''}" data-pl-contact="${x.id}" ${sel === x.id ? 'aria-current="true"' : ''}>
          <span class="pl-row-main"><span class="pl-row-title"><span class="pl-trunc">${esc(x.name)}</span></span><span class="pl-row-sub pl-trunc">${esc(x.role || 'No role')}</span></span>
          <span class="pl-row-side"><span class="pl-count" title="Linked songs">${x.song_count}</span></span></button></li>`).join('')}</ul>
        <div class="pl-contact-detail">${form}</div>
      </div>
    </div>`;
  }

  // ---- actions -------------------------------------------------------------
  function flashSaved(host) {
    state.savedUntil = Date.now() + 1500;
    const el = (host || detailEl)?.querySelector('.pl-saved');
    if (el) el.textContent = 'Saved';
    clearTimeout(state.savedTimer);
    state.savedTimer = setTimeout(() => {
      state.savedUntil = 0;
      const live = detailEl?.querySelector('.pl-saved');
      if (live) live.textContent = '';
    }, 1500);
  }

  async function confirm(opts) {
    if (typeof confirmDialog === 'function') return confirmDialog(opts);
    return window.confirm(opts.title);
  }

  async function toggleTodo(checkbox, key, id, text, done) {
    const row = checkbox.closest('.pl-todo');
    if (done) row?.classList.add('is-done');
    const wait = reducedMotion() ? Promise.resolve() : new Promise(r => setTimeout(r, 140));
    const res = await act({ action: 'todo_update', key, id, done });
    await wait;
    if (!res) { drawDetail(); return; }
    if (state.view === 'agenda' || res.view === 'overview') { if (res.view !== 'overview') await loadOverview(); drawDetail(); }
    else drawDetail();
    drawNav();
    notify(done ? `Done: ${text}` : `Reopened: ${text}`, {
      label: 'Undo',
      onClick: async () => { await act({ action: 'todo_update', key, id, done: !done }); await loadOverview(); drawDetail(); },
    });
  }

  const todoAddBusy = { quick: false, todo: false };
  async function addTodo(key, text, due, kind) {
    if (todoAddBusy[kind]) return;
    todoAddBusy[kind] = true;
    try {
      const res = await act({ action: 'todo_add', key, text, due_date: due });
      if (!res) return;
      if (kind === 'quick') { state.drafts.quickText = ''; state.drafts.quickDate = ''; await loadOverview(); drawDetail(); q('#plQuickText')?.focus(); }
      else { state.drafts.todoText = ''; state.drafts.todoDate = ''; drawDetail(); q('#plTodoText')?.focus(); }
    } finally {
      todoAddBusy[kind] = false;
    }
  }

  async function saveHold(form) {
    const key = state.key;
    const waiting = form.querySelector('#plHoldWaiting')?.value;
    const body = {
      action: 'hold', key,
      reason: form.querySelector('#plHoldReason').value.trim(),
      until: form.querySelector('#plHoldUntil').value || '',
      waiting_on: waiting ? Number(waiting) : null,
    };
    const res = await act(body);
    if (!res) return;
    state.holdOpen = false; state.holdEdit = false;
    drawDetail();
    q('[data-pl="resume"]')?.focus();
  }

  async function saveNotes(textarea) {
    clearTimeout(notesTimer);
    const key = textarea.dataset.key;
    const text = textarea.value;
    if (!key || text === textarea.dataset.saved) return;
    try {
      const data = await api('/api/planner', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'notes', key, text }) });
      const stored = data.project?.notes ?? text;
      textarea.dataset.saved = stored;
      if (state.detail?.project?.key === key) { state.detail = { ...state.detail, project: { ...state.detail.project, notes: stored } }; }
      if (state.view === 'project' && state.key === key) flashSaved(detailEl);
      loadOverview();
    } catch (error) { notify(error.message); }
  }

  async function onClick(event) {
    const t = event.target;
    if (state.menuOpen && !t.closest('.pl-menu-wrap')) { state.menuOpen = false; drawDetail(); }
    const nav = t.closest('[data-pl-nav]');
    if (nav) { nav.dataset.plNav === 'agenda' ? selectAgenda() : selectContacts(); return; }
    const projectRow = t.closest('[data-pl-project]');
    if (projectRow) { selectProject(projectRow.dataset.plProject); return; }
    const open = t.closest('[data-pl-open]');
    if (open) { selectProject(open.dataset.plOpen); return; }
    const contactRow = t.closest('[data-pl-contact]');
    if (contactRow) { selectContacts(Number(contactRow.dataset.plContact)); return; }
    const group = t.closest('[data-pl-group]');
    if (group) {
      state.archiveOpen = !state.archiveOpen;
      try { localStorage.setItem(ARCHIVE_KEY, state.archiveOpen ? '1' : '0'); } catch (_) { /* ignore */ }
      drawNav();
      q('[data-pl-group="archive"]')?.focus();
      return;
    }
    const check = t.closest('[data-pl-check]');
    if (check) {
      const li = check.closest('.pl-todo');
      const id = Number(li.dataset.todoId);
      const key = li.dataset.songKey ?? (state.view === 'project' ? state.key : '');
      const item = state.view === 'agenda' ? (state.overview.agenda || []).find(a => a.id === id) : (state.detail?.todos || []).find(x => x.id === id);
      const done = check.checked;
      toggleTodo(check, key, id, item?.text || 'to-do', done);
      return;
    }
    const button = t.closest('[data-pl]');
    if (!button) return;
    const kind = button.dataset.pl;
    const p = state.detail?.project;
    switch (kind) {
      case 'back': setMobile('list'); break;
      case 'back-agenda': selectAgenda(); break;
      case 'retry': state.loadError = ''; loadOverview(); break;
      case 'star': {
        if (!p) break;
        const res = await act({ action: 'priority', key: p.key, value: !p.priority });
        if (res) { drawDetail(); q('[data-pl="star"]')?.focus(); }
        break;
      }
      case 'open-live': {
        button.disabled = true;
        const res = await act({ action: 'open', key: state.key });
        button.disabled = false;
        if (res) notify('Opening in Live…');
        break;
      }
      case 'hold': state.holdOpen = true; drawDetail(); q('#plHoldReason')?.focus(); break;
      case 'hold-edit': state.holdEdit = true; drawDetail(); q('#plHoldReason')?.focus(); break;
      case 'hold-cancel': state.holdOpen = false; state.holdEdit = false; drawDetail(); q('[data-pl="hold"], [data-pl="hold-edit"]')?.focus(); break;
      case 'resume': {
        const res = await act({ action: 'resume', key: state.key });
        if (res) { state.holdOpen = false; state.holdEdit = false; drawDetail(); q('[data-pl="hold"]')?.focus(); }
        break;
      }
      case 'more': state.menuOpen = !state.menuOpen; drawDetail(); q('[data-pl="more"]')?.focus(); break;
      case 'stage': {
        if (!p || button.getAttribute('aria-disabled') === 'true' || button.getAttribute('aria-current') === 'step') break;
        const stage = button.dataset.stage;
        const res = await act({ action: 'stage', key: p.key, stage });
        if (res) { drawDetail(); q(`[data-stage="${stage}"]`)?.focus(); }
        break;
      }
      case 'release': {
        if (!p) break;
        state.menuOpen = false;
        const ok = await confirm({ title: 'Mark as released?', body: `<p>“${esc(p.title)}” leaves On Deck and moves to Settled. Its tracked time is kept.</p>`, confirmLabel: 'Mark released' });
        if (!ok) { drawDetail(); q('[data-pl="more"]')?.focus(); break; }
        const res = await act({ action: 'stage', key: p.key, stage: 'released' });
        drawDetail();
        if (res) notify(`${p.title} is released`);
        break;
      }
      case 'list-set': {
        state.menuOpen = false;
        const res = await act({ action: 'list', key: state.key, value: button.dataset.value });
        if (res) { drawDetail(); notify(button.dataset.value === 'archive' ? 'Moved to Settled' : 'Moved to Active'); }
        break;
      }
      case 'due-clear': {
        const res = await act({ action: 'due', key: state.key, date: '' });
        if (res) { drawDetail(); notify('Due date cleared'); }
        break;
      }
      case 'todo-edit': state.editingTodo = Number(button.dataset.todoId); drawDetail(); { const i = q('.pl-todo-edit input[name="text"]'); i?.focus(); i?.select(); } break;
      case 'todo-cancel': state.editingTodo = null; drawDetail(); break;
      case 'todo-delete': {
        const id = Number(button.dataset.todoId);
        const key = state.key;
        const todo = (state.detail?.todos || []).find(x => x.id === id);
        if (!todo) break;
        const res = await act({ action: 'todo_delete', key, id });
        if (res) {
          drawDetail();
          notify(`Deleted: ${todo.text}`, { label: 'Undo', onClick: async () => { await act({ action: 'todo_add', key, text: todo.text, due_date: todo.due_date || '' }); drawDetail(); } });
        }
        break;
      }
      case 'person-add': state.personOpen = true; state.personNew = false; drawDetail(); q('#plPersonSelect')?.focus(); break;
      case 'person-cancel': state.personOpen = false; state.personNew = false; drawDetail(); q('[data-pl="person-add"]')?.focus(); break;
      case 'person-remove': {
        const person = (state.detail?.people || []).find(x => x.id === Number(button.dataset.id));
        const res = await act({ action: 'contact_unlink', id: Number(button.dataset.id), key: state.key });
        if (res) { drawDetail(); if (person) notify(`Removed ${person.name}`, { label: 'Undo', onClick: async () => { await act({ action: 'contact_link', id: person.id, key: p.key }); drawDetail(); } }); }
        break;
      }
      case 'contact-new': selectContacts('new'); q('#plContactName')?.focus(); break;
      case 'contact-delete': {
        const c = state.contact?.contact;
        if (!c) break;
        const ok = await confirm({ title: 'Delete this contact?', body: `<p>${esc(c.name)} is removed from every song. Songs and to-dos are not affected.</p>`, confirmLabel: 'Delete contact' });
        if (!ok) break;
        const res = await act({ action: 'contact_delete', id: c.id });
        if (res) { state.contactId = null; state.contact = null; drawNav(); drawDetail(); notify(`Deleted ${c.name}`); }
        break;
      }
      case 'contact-unlink': {
        const c = state.contact?.contact;
        if (!c) break;
        const res = await act({ action: 'contact_unlink', id: c.id, key: button.dataset.key, from: 'contact' });
        if (res) drawDetail();
        break;
      }
      default: break;
    }
  }

  async function onSubmit(event) {
    const form = event.target.closest('[data-pl-form]');
    if (!form) return;
    event.preventDefault();
    const kind = form.dataset.plForm;
    if (kind === 'quick-add' || kind === 'todo-add') {
      const quick = kind === 'quick-add';
      const text = (form.querySelector('input[type="text"], input:not([type])')?.value || '').trim();
      const due = form.querySelector('input[type="date"]')?.value || '';
      if (!text) { form.querySelector('input')?.focus(); return; }
      await addTodo(quick ? '' : state.key, text, due, quick ? 'quick' : 'todo');
    } else if (kind === 'todo-edit') {
      const id = Number(form.dataset.todoId);
      const text = form.elements.text.value.trim();
      const due = form.elements.due_date.value || '';
      if (!text) { form.elements.text.focus(); return; }
      state.editingTodo = null;
      const res = await act({ action: 'todo_update', key: state.key, id, text, due_date: due });
      drawDetail();
      if (res) q(`[data-pl="todo-edit"][data-todo-id="${id}"]`)?.focus();
    } else if (kind === 'hold') {
      await saveHold(form);
    } else if (kind === 'person-new') {
      const name = form.querySelector('#plNewName').value.trim();
      const role = form.querySelector('#plNewRole').value.trim();
      if (!name) { form.querySelector('#plNewName').focus(); return; }
      const key = state.key;
      const created = await act({ action: 'contact_save', name, role });
      if (!created) return;
      const linked = await act({ action: 'contact_link', id: created.contact.id, key });
      state.personOpen = false; state.personNew = false;
      if (linked) { drawDetail(); q('[data-pl="person-add"]')?.focus(); }
      else { drawDetail(); }
    } else if (kind === 'contact') {
      const id = form.dataset.id;
      const body = {
        action: 'contact_save', name: form.querySelector('#plContactName').value.trim(),
        role: form.querySelector('#plContactRole').value.trim(), email: form.querySelector('#plContactEmail').value.trim(),
        phone: form.querySelector('#plContactPhone').value.trim(), notes: form.querySelector('#plContactNotes').value,
      };
      if (id) body.id = Number(id);
      const res = await act(body);
      if (res) { drawNav(); drawDetail(); flashSaved(detailEl); }
    }
  }

  async function onChange(event) {
    const t = event.target;
    if (t.id === 'plDue') {
      const res = await act({ action: 'due', key: state.key, date: t.value || '' });
      drawDetail();
      if (res) q('#plDue')?.focus();
    } else if (t.id === 'plPersonSelect') {
      if (t.value === '__new') { state.personNew = true; drawDetail(); q('#plNewName')?.focus(); }
      else if (t.value) {
        const res = await act({ action: 'contact_link', id: Number(t.value), key: state.key });
        state.personOpen = false; state.personNew = false; drawDetail();
        if (res) q('[data-pl="person-add"]')?.focus();
      } else { state.personNew = false; drawDetail(); q('#plPersonSelect')?.focus(); }
    }
  }

  function onInput(event) {
    const t = event.target;
    if (t.id === 'plSearch') { state.query = t.value; drawNav(); return; }
    if (t.id === 'plQuickText') state.drafts.quickText = t.value;
    else if (t.id === 'plQuickDate') state.drafts.quickDate = t.value;
    else if (t.id === 'plTodoText') state.drafts.todoText = t.value;
    else if (t.id === 'plTodoDate') state.drafts.todoDate = t.value;
    else if (t.id === 'plNotes') { autosize(t); clearTimeout(notesTimer); notesTimer = setTimeout(() => saveNotes(t), 1200); }
  }

  function onKeydown(event) {
    const t = event.target;
    if (event.key === 'Enter' && t.matches?.('[data-pl-enter]') && !event.isComposing) {
      event.preventDefault();
      t.form?.requestSubmit();
      return;
    }
    if (event.key === 'Escape') {
      if (t.id === 'plSearch') { t.value = ''; state.query = ''; drawNav(); return; }
      if (state.menuOpen) { state.menuOpen = false; drawDetail(); q('[data-pl="more"]')?.focus(); return; }
      if (t.closest?.('.pl-todo-edit')) { state.editingTodo = null; drawDetail(); return; }
      if (t.closest?.('[data-pl-form="hold"]')) { state.holdOpen = false; state.holdEdit = false; drawDetail(); return; }
    }
    if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && t.matches?.('.deck-stage')) {
      const steps = [...(t.closest('.deck-stages')?.querySelectorAll('.deck-stage:not([aria-disabled="true"])') || [])];
      const i = steps.indexOf(t);
      if (i >= 0) {
        event.preventDefault();
        steps[Math.max(0, Math.min(steps.length - 1, i + (event.key === 'ArrowRight' ? 1 : -1)))]?.focus();
      }
      return;
    }
    if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && navEl && (navEl.contains(t) || t.id === 'plSearch')) {
      const items = [searchEl, ...navEl.querySelectorAll('button')].filter(Boolean);
      const i = items.indexOf(t);
      if (i < 0) return;
      const next = items[Math.max(0, Math.min(items.length - 1, i + (event.key === 'ArrowDown' ? 1 : -1)))];
      if (next && next !== t) { event.preventDefault(); next.focus(); }
    }
  }

  function onFocusOut(event) {
    if (event.target.id === 'plNotes') saveNotes(event.target);
  }

  function onToggle(event) {
    if (event.target.matches?.('[data-pl-done]')) state.doneOpen = event.target.open;
  }

  // ---- mount / show --------------------------------------------------------
  function mount() {
    const host = document.getElementById('plannerRoot');
    if (!host || root) return Boolean(root);
    host.innerHTML = `<div class="pl" id="plRoot" data-mobile="${state.mobile}">
      <aside class="pl-list" aria-label="Planner">
        <div class="pl-search"><input class="text-input" id="plSearch" type="search" placeholder="Search songs" aria-label="Search songs" autocomplete="off" spellcheck="false"></div>
        <nav class="pl-nav" id="plNav" aria-label="Songs"></nav>
      </aside>
      <section class="pl-detail" id="plDetail"></section>
    </div>`;
    root = host.firstElementChild;
    navEl = root.querySelector('#plNav');
    detailEl = root.querySelector('#plDetail');
    searchEl = root.querySelector('#plSearch');
    root.addEventListener('click', onClick);
    root.addEventListener('submit', onSubmit);
    root.addEventListener('change', onChange);
    root.addEventListener('input', onInput);
    root.addEventListener('keydown', onKeydown);
    root.addEventListener('focusout', onFocusOut);
    root.addEventListener('toggle', onToggle, true);
    return true;
  }

  function show() {
    try {
      if (!mount()) return;
      if (!state.overview) { drawNav(); drawDetail(); }
      if (state.view === 'project' && state.key) loadProject(state.key, { quiet: Boolean(state.detail) });
      else if (state.view === 'contacts' && typeof state.contactId === 'number') loadContact(state.contactId, { quiet: true });
      loadOverview();
    } catch (error) {
      console.error(error);
    }
  }

  // The dashboard may open #planner before this script has loaded; catch up once it has.
  const startup = document.getElementById('appPlanner');
  if (startup && !startup.hidden) setTimeout(show, 0);

  return { show };
})();
