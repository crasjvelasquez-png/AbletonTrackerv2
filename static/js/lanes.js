/* Three Decks: Produce, Mix, and Master lanes with one active Now lane.
   Mounts inside #appDashboard right after #onDeck (or #songPlanner if present)
   and re-attaches across dashboard re-renders. */
window.Lanes = (() => {
  const esc = value => escapeHtml(String(value ?? ''));
  const STAGE_LABELS = {
    idea: 'Idea',
    production: 'Production',
    recording: 'Recording',
    mixing: 'Mixing',
    mastering: 'Mastering',
    released: 'Released',
  };
  const ICON_UP = '<svg viewBox="0 0 14 14" aria-hidden="true"><path d="M3 8.75L7 4.75l4 4"/></svg>';
  const ICON_DOWN = '<svg viewBox="0 0 14 14" aria-hidden="true"><path d="M3 5.25L7 9.25l4-4"/></svg>';
  const ICON_REMOVE = '<svg viewBox="0 0 14 14" aria-hidden="true"><path d="M3.5 3.5l7 7M10.5 3.5l-7 7"/></svg>';

  const state = { model: null, error: '', busy: false };
  let root = null;
  let observer = null;
  let observedHost = null;
  let fetchSeq = 0;

  const localIso = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;

  function stageLabel(stage) {
    const raw = String(stage ?? '').trim();
    if (!raw) return '';
    return STAGE_LABELS[raw.toLowerCase()] || (raw.charAt(0).toUpperCase() + raw.slice(1));
  }

  function formatHours(seconds) {
    const secs = Math.max(0, Number(seconds) || 0);
    if (secs === 0) return '0h';
    const hrs = Math.round(secs / 360) / 10;
    return hrs === 0 ? '<0.1h' : `${hrs}h`;
  }

  function formatDue(iso) {
    const value = String(iso ?? '').trim();
    if (!value) return '';
    const parsed = new Date(`${value}T00:00`);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function isOverdue(iso) {
    const value = String(iso ?? '').trim();
    return Boolean(value) && value < localIso(new Date());
  }

  function place(host) {
    if (!root || !host) return;
    const songPlanner = document.getElementById('songPlanner');
    const onDeck = document.getElementById('onDeck');
    const anchor = (songPlanner && songPlanner.parentElement === host)
      ? songPlanner
      : (onDeck && onDeck.parentElement === host)
        ? onDeck
        : null;
    if (anchor) {
      if (anchor.nextElementSibling !== root) anchor.after(root);
    } else if (root.parentElement !== host || host.firstElementChild !== root) {
      host.prepend(root);
    }
  }

  function mount() {
    const host = document.getElementById('appDashboard');
    if (!host) return;
    if (!root) {
      root = document.createElement('section');
      root.className = 'chart-card chart-card-wide lanes-card';
      root.id = 'lanes';
      root.setAttribute('aria-labelledby', 'lanesHeading');
      root.addEventListener('click', onClick);
      root.addEventListener('change', onChange);
      draw();
      refresh();
    }
    if (observedHost !== host) {
      observer?.disconnect();
      observer = new MutationObserver(() => {
        if (document.getElementById('appDashboard') === host && root) {
          place(host);
        }
      });
      observer.observe(host, { childList: true });
      observedHost = host;
    }
    place(host);
  }

  function isBusy() {
    return state.busy || (!!root?.contains(document.activeElement) && document.activeElement.tagName === 'SELECT');
  }

  async function refresh({ force = false } = {}) {
    if (!force && isBusy()) return;
    const seq = ++fetchSeq;
    try {
      const res = await fetch('/api/lanes', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const model = await res.json();
      if (seq !== fetchSeq || (!force && isBusy())) return;
      state.model = model;
      state.error = '';
    } catch (error) {
      console.error(error);
      if (seq !== fetchSeq) return;
      if (!state.model) state.error = 'Three Decks could not load.';
    }
    draw();
  }

  async function act(body, { pending = null } = {}) {
    if (state.busy) return null;
    state.busy = true;
    fetchSeq++;
    if (pending) pending.disabled = true;
    root?.classList.add('is-busy');
    try {
      const res = await fetch('/api/lanes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (data.lanes) {
        state.model = data.lanes;
        state.error = '';
      }
      if (!res.ok || data.error || data.ok === false) {
        toast(data.error || 'That change did not save. Try again.');
        return null;
      }
      window.OnDeck?.refresh();
      return data.lanes || state.model;
    } catch (error) {
      console.error(error);
      toast('The dashboard server is not responding. Your change was not saved.');
      return null;
    } finally {
      state.busy = false;
      root?.classList.remove('is-busy');
      if (pending?.isConnected) pending.disabled = false;
      draw();
    }
  }

  // ── Rendering ────────────────────────────────────────────────────────────────

  function captureFocus() {
    const active = root?.contains(document.activeElement) ? document.activeElement : null;
    if (!active) return null;
    const col = active.closest('[data-lane-col]');
    return {
      action: active.dataset.lanes || '',
      key: active.dataset.key || '',
      lane: active.dataset.lane || '',
      delta: active.dataset.delta || '',
      colKey: col?.dataset.laneCol || '',
      id: active.id || '',
    };
  }

  function restoreFocus(snapshot) {
    if (!snapshot || !root) return;
    if (snapshot.id) {
      const byId = root.querySelector(`#${CSS.escape(snapshot.id)}`);
      if (byId && !byId.disabled) {
        byId.focus({ preventScroll: true });
        return;
      }
    }
    if (snapshot.action) {
      const exactSelector = `[data-lanes="${CSS.escape(snapshot.action)}"]`
        + (snapshot.key ? `[data-key="${CSS.escape(snapshot.key)}"]` : '')
        + (snapshot.lane ? `[data-lane="${CSS.escape(snapshot.lane)}"]` : '')
        + (snapshot.delta ? `[data-delta="${CSS.escape(snapshot.delta)}"]` : '');
      const exact = root.querySelector(exactSelector);
      if (exact && !exact.disabled) {
        exact.focus({ preventScroll: true });
        return;
      }
      if (snapshot.action === 'move' && snapshot.key) {
        const oppDelta = snapshot.delta === '-1' ? '1' : '-1';
        const fallback = root.querySelector(`[data-lanes="move"][data-key="${CSS.escape(snapshot.key)}"][data-delta="${oppDelta}"]:not(:disabled)`)
          || root.querySelector(`[data-lanes="deck"][data-key="${CSS.escape(snapshot.key)}"]:not(:disabled)`);
        if (fallback) {
          fallback.focus({ preventScroll: true });
          return;
        }
      }
    }
    if (snapshot.colKey) {
      const col = root.querySelector(`[data-lane-col="${CSS.escape(snapshot.colKey)}"]`);
      const target = col?.querySelector('button:not(:disabled), select:not(:disabled)');
      target?.focus({ preventScroll: true });
    }
  }

  function draw() {
    if (!root) return;
    const focusSnapshot = captureFocus();
    const m = state.model;
    let body = '';
    if (!m) {
      body = state.error
        ? `<div class="lanes-status"><p class="deck-hint">${esc(state.error)}</p><button class="btn small" type="button" data-lanes="retry">Retry</button></div>`
        : '<p class="deck-hint">Loading Three Decks…</p>';
    } else {
      body = gridView(m);
    }
    root.innerHTML = `<div class="deck-head lanes-head"><h3 class="section-title" id="lanesHeading">Three Decks</h3></div>${body}`;
    restoreFocus(focusSnapshot);
  }

  function gridView(m) {
    const lanes = Array.isArray(m.lanes) ? m.lanes : [];
    if (!lanes.length) {
      return '<p class="deck-hint">No lanes configured.</p>';
    }
    const available = Array.isArray(m.available) ? m.available : [];
    return `<div class="lanes-grid">${lanes.map(lane => laneView(lane, m.now_lane, available)).join('')}</div>`;
  }

  function laneView(lane, nowLane, available) {
    const laneKey = String(lane?.key ?? '');
    const laneLabel = String(lane?.label || laneKey);
    const isNow = Boolean(laneKey) && nowLane === laneKey;
    const queue = Array.isArray(lane?.queue) ? lane.queue : [];

    return `<article class="lanes-col${isNow ? ' is-now' : ' is-muted'}" data-lane-col="${esc(laneKey)}" aria-label="${esc(laneLabel)} lane${isNow ? ' (Now)' : ''}">
      <header class="lanes-col-head">
        <h4 class="lanes-col-title">${esc(laneLabel)}</h4>
        ${isNow
          ? '<span class="lanes-now-badge">Now</span>'
          : `<button class="btn small lanes-focus-btn" type="button" data-lanes="focus" data-lane="${esc(laneKey)}" aria-label="Focus ${esc(laneLabel)} lane">Focus</button>`}
      </header>
      <div class="lanes-deck-slot">
        ${lane?.deck ? deckCardView(lane.deck, laneLabel) : '<div class="lanes-deck-empty"><p class="deck-hint">No song on deck yet.</p></div>'}
      </div>
      <div class="lanes-queue-section">
        <div class="lanes-subhead">
          <span>Up next</span>
          <span class="lanes-count">${esc(queue.length)}</span>
        </div>
        ${queueView(queue, laneLabel)}
      </div>
      <div class="lanes-add-wrap">
        <select class="text-input lanes-add-select" data-lanes="add" data-lane="${esc(laneKey)}" aria-label="Add song to ${esc(laneLabel)}" ${available.length ? '' : 'disabled'}>
          <option value="">${available.length ? 'Add song…' : 'No available songs'}</option>
          ${available.map(song => {
            const title = String(song?.title ?? '');
            const st = stageLabel(song?.stage);
            return `<option value="${esc(song?.key ?? '')}">${esc(title)}${st ? ` (${esc(st)})` : ''}</option>`;
          }).join('')}
        </select>
      </div>
    </article>`;
  }

  function deckCardView(song, laneLabel) {
    const title = String(song.title ?? '');
    const stage = stageLabel(song.stage);
    const dueRaw = String(song.due_value ?? '').trim();
    const dueDisplay = formatDue(dueRaw);
    const overdue = isOverdue(dueRaw);
    const hours = formatHours(song.seconds);
    const nextTask = String(song.next_task ?? '').trim();

    return `<div class="lanes-deck-card">
      <div class="lanes-deck-top">
        <span class="lanes-deck-kicker">Deck</span>
        <button class="lanes-icon-btn lanes-remove-btn" type="button" data-lanes="remove" data-key="${esc(song.key ?? '')}" aria-label="Remove ${esc(title)} from ${esc(laneLabel)}" title="Remove from ${esc(laneLabel)}">${ICON_REMOVE}</button>
      </div>
      <p class="lanes-deck-title" title="${esc(title)}">${esc(title)}</p>
      <div class="lanes-deck-meta">
        ${stage ? `<span class="lanes-stage-pill">${esc(stage)}</span>` : ''}
        <span class="lanes-hours">${esc(hours)}</span>
        ${dueDisplay
          ? `<span class="lanes-due${overdue ? ' is-overdue' : ''}" title="${esc(dueRaw)}">Due ${esc(dueDisplay)}</span>`
          : '<span class="lanes-due is-empty">No due date</span>'}
      </div>
      ${nextTask
        ? `<p class="lanes-deck-task"><span class="lanes-task-label">Next:</span> ${esc(nextTask)}</p>`
        : '<p class="lanes-deck-task is-empty">No next task</p>'}
    </div>`;
  }

  function queueView(queue, laneLabel) {
    if (!queue.length) {
      return '<p class="lanes-queue-empty">Nothing queued.</p>';
    }
    return `<ul class="lanes-queue" aria-label="Up next in ${esc(laneLabel)}">${queue.map((song, index) => {
      const key = String(song?.key ?? '');
      const title = String(song?.title ?? '');
      const stage = stageLabel(song?.stage);
      const hours = formatHours(song?.seconds);
      const dueDisplay = formatDue(song?.due_value);
      const meta = [stage, hours, dueDisplay ? `Due ${dueDisplay}` : ''].filter(Boolean);
      return `<li class="lanes-queue-item">
        <div class="lanes-queue-info">
          <span class="lanes-queue-title" title="${esc(title)}">${esc(title)}</span>
          ${meta.length ? `<span class="lanes-queue-meta">${meta.map(esc).join(' · ')}</span>` : ''}
        </div>
        <div class="lanes-queue-actions">
          <button class="lanes-icon-btn" type="button" data-lanes="move" data-key="${esc(key)}" data-delta="-1" aria-label="Move ${esc(title)} up" title="Move up" ${index === 0 ? 'disabled' : ''}>${ICON_UP}</button>
          <button class="lanes-icon-btn" type="button" data-lanes="move" data-key="${esc(key)}" data-delta="1" aria-label="Move ${esc(title)} down" title="Move down" ${index === queue.length - 1 ? 'disabled' : ''}>${ICON_DOWN}</button>
          <button class="btn small lanes-make-deck-btn" type="button" data-lanes="deck" data-key="${esc(key)}" aria-label="Make ${esc(title)} deck in ${esc(laneLabel)}">Make deck</button>
          <button class="lanes-icon-btn lanes-remove-btn" type="button" data-lanes="remove" data-key="${esc(key)}" aria-label="Remove ${esc(title)} from ${esc(laneLabel)}" title="Remove">${ICON_REMOVE}</button>
        </div>
      </li>`;
    }).join('')}</ul>`;
  }

  // ── Events ───────────────────────────────────────────────────────────────────

  async function onClick(event) {
    const button = event.target.closest('button[data-lanes]');
    if (!button || button.disabled || state.busy) return;
    const kind = button.dataset.lanes;
    if (kind === 'retry') {
      state.error = '';
      draw();
      await refresh({ force: true });
    } else if (kind === 'focus') {
      await act({ action: 'focus', lane: button.dataset.lane }, { pending: button });
    } else if (kind === 'deck') {
      await act({ action: 'deck', key: button.dataset.key }, { pending: button });
    } else if (kind === 'move') {
      await act({ action: 'move', key: button.dataset.key, delta: Number(button.dataset.delta) }, { pending: button });
    } else if (kind === 'remove') {
      await act({ action: 'assign', key: button.dataset.key, lane: '' }, { pending: button });
    }
  }

  async function onChange(event) {
    const select = event.target.closest('select[data-lanes="add"]');
    if (!select) return;
    const key = select.value;
    const lane = select.dataset.lane;
    select.value = '';
    if (!key || !lane || state.busy) return;
    await act({ action: 'assign', key, lane }, { pending: select });
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('focus', () => refresh());
  setInterval(() => { if (!document.hidden) refresh(); }, 30_000);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount, { once: true });
  } else {
    mount();
  }

  return { mount, refresh };
})();
