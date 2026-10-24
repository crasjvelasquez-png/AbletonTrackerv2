/* Progressive dashboard features; recorded activity remains owned by the tracker. */
window.DashboardExtras = (() => {
  const escape = value => escapeHtml(String(value ?? ''));
  const state = { data: null, openSections: new Set(), projectRequest: null, commands: [], selection: 0, lastSuccess: Date.now(), unavailable: false, trigger: null };
  const $ = id => document.getElementById(id);
  const clock = seconds => {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${String(Math.floor(value / 3600)).padStart(2, '0')}:${String(Math.floor(value / 60) % 60).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
  };
  const isEditing = () => !!document.querySelector('dialog[open],.modal-backdrop.show,.detail-modal-backdrop.is-open,#weeklyGoalEdit:not([hidden])') || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');

  document.body.insertAdjacentHTML('beforeend', `
    <dialog class="warm-dialog command-dialog" id="commandDialog" aria-labelledby="commandTitle">
      <div class="warm-dialog-head"><h3 id="commandTitle">Go to…</h3><button class="btn small" id="commandClose" aria-label="Close command palette">×</button></div>
      <div class="warm-dialog-content"><input id="commandSearch" class="command-search" type="search" placeholder="Search projects and actions…" aria-label="Search projects and actions" role="combobox" aria-expanded="false" aria-controls="commandResults" aria-autocomplete="list" autocomplete="off"><div class="command-results" id="commandResults" role="listbox" aria-label="Projects and actions"></div><p class="project-hint" id="commandHint">↑ ↓ to move · Enter to open · Escape to close</p></div>
    </dialog>
    <dialog class="warm-dialog" id="projectDialog" aria-labelledby="projectTitle"><div class="warm-dialog-head"><div><h3 id="projectTitle">Project details</h3><p class="project-hint">Recorded activity · all time</p></div><button class="btn small" id="projectClose" aria-label="Close project details">×</button></div><div class="warm-dialog-content" id="projectContent"></div></dialog>
    <dialog class="warm-dialog" id="activityDialog" aria-labelledby="activityTitle"><div class="warm-dialog-head"><h3 id="activityTitle">Activity by hour</h3><button class="btn small" id="activityClose" aria-label="Close activity detail">×</button></div><div class="warm-dialog-content" id="activityContent"></div></dialog>
  `);

  function focusBack(element) {
    if (element?.isConnected && !element.closest('[hidden]')) element.focus();
    else $('commandTrigger')?.focus();
  }
  function wireDialog(id, closeId) {
    const dialog = $(id);
    let origin = null;
    dialog.addEventListener('close', () => { if (!document.querySelector('dialog[open]')) focusBack(origin); lastETag = null; });
    $(closeId).onclick = () => dialog.close();
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
    return () => { origin = document.activeElement; if (!dialog.open) dialog.showModal(); };
  }
  const showProject = wireDialog('projectDialog', 'projectClose');
  const showActivity = wireDialog('activityDialog', 'activityClose');
  const showCommands = wireDialog('commandDialog', 'commandClose');
  $('commandDialog').addEventListener('close', () => $('commandSearch').setAttribute('aria-expanded','false'));

  function commands() {
    const actions = [
      { name: 'Dashboard', type: 'Page', run: () => setActiveView('dashboard') },
      { name: 'Settings', type: 'Page', run: () => setActiveView('settings') },
      { name: 'Weekly review', type: 'View', run: () => reveal('weeklyReview') },
      { name: 'Year activity', type: 'View', run: () => reveal('yearActivity') },
      { name: 'Recent sessions', type: 'View', run: async () => { await setActiveView('dashboard'); const d = document.querySelector('.history-card details'); if (d) { d.open = true; d.querySelector('summary').focus(); d.scrollIntoView({ block: 'center' }); } } },
      { name: 'Switch theme', type: 'Action', run: toggleTheme },
    ];
    return [...(state.data?.projects || []).map(project => ({ name: project.project_name, type: 'Project', run: () => openProject(project.project_name) })), ...actions];
  }
  function renderCommands() {
    const query = $('commandSearch').value.trim().toLocaleLowerCase();
    state.commands = commands().filter(command => command.name.toLocaleLowerCase().includes(query));
    state.selection = Math.min(Math.max(state.selection, 0), Math.max(0, state.commands.length - 1));
    $('commandResults').innerHTML = state.commands.length ? state.commands.map((command, i) => `<button type="button" class="command-result" id="commandOption${i}" role="option" tabindex="-1" aria-selected="${i === state.selection}" data-command-index="${i}"><span>${escape(command.name)}</span><small>${command.type}</small></button>`).join('') : '<p class="project-hint" role="status">No results. Try another project or action.</p>';
    const input = $('commandSearch');
    input.setAttribute('aria-expanded', 'true');
    if (state.commands.length) input.setAttribute('aria-activedescendant', `commandOption${state.selection}`);
    else input.removeAttribute('aria-activedescendant');
  }
  function openCommands() {
    if (document.querySelector('dialog[open],.modal-backdrop.show,.detail-modal-backdrop.is-open')) return;
    showCommands(); $('commandSearch').value = ''; state.selection = 0; renderCommands(); $('commandSearch').focus();
  }
  function runCommand(index) {
    const command = state.commands[index];
    if (!command) return;
    $('commandDialog').close();
    // Wait for native dialog focus restoration before focusing the destination.
    setTimeout(() => Promise.resolve(command.run()).catch(error => toast(error.message)),0);
  }
  $('commandSearch').oninput = () => { state.selection = 0; renderCommands(); };
  $('commandSearch').onkeydown = event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); const count = state.commands.length;
      if (count) { state.selection = (state.selection + (event.key === 'ArrowDown' ? 1 : -1) + count) % count; renderCommands(); $(`commandOption${state.selection}`)?.scrollIntoView({ block: 'nearest' }); }
    } else if (event.key === 'Enter') { event.preventDefault(); runCommand(state.selection); }
  };
  $('commandResults').onclick = event => {
    const option = event.target.closest('[data-command-index]');
    if (option) runCommand(Number(option.dataset.commandIndex));
  };
  $('commandTrigger').onclick = openCommands;
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && $('commandDialog').open) { event.preventDefault(); $('commandDialog').close(); return; }
    if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'k' && !event.isComposing) {
      if (event.target.isContentEditable || event.target.closest('input,textarea,select,[role="textbox"]')) return;
      if (document.querySelector('.modal-backdrop.show,.detail-modal-backdrop.is-open,dialog[open]')) return;
      event.preventDefault(); openCommands();
    }
  });

  async function reveal(id) {
    await setActiveView('dashboard');
    const section = $(id);
    if (!section) return;
    // An explicit navigation request also reveals a widget hidden in Appearance.
    if (id === 'yearActivity') document.documentElement.removeAttribute('data-hide-heatmap');
    section.open = true; section.querySelector('summary').focus(); section.scrollIntoView({ block: 'start' });
  }

  async function openProject(name) {
    state.projectRequest?.abort();
    const request = new AbortController(); state.projectRequest = request;
    $('projectTitle').textContent = name; $('projectContent').innerHTML = '<p role="status">Loading project history…</p>'; showProject();
    try {
      const response = await fetch(`/api/project-report?project=${encodeURIComponent(name)}`, { signal: request.signal });
      const report = await response.json();
      if (!response.ok || report.error) throw new Error(report.error || 'Project history could not load.');
      if (request.signal.aborted || !$('projectDialog').open) return;
      const sessions = [...report.sessions].reverse();
      const project = state.data?.projects.find(p => p.project_name === name);
      const first = sessions.at(-1)?.date;
      const last = sessions[0]?.date;
      const url = `/api/project-report/download?project=${encodeURIComponent(name)}`;
      $('projectContent').innerHTML = `
        <dl class="project-metrics"><div><dt>Total recorded</dt><dd>${fmt.dur(report.total_duration_seconds)}</dd></div><div><dt>Session entries</dt><dd>${report.session_count}</dd></div><div><dt>Category</dt><dd>${escape(project?.category_label || 'Uncategorized')}</dd></div></dl>
        <p class="project-hint">${first ? `${escape(first)} to ${escape(last)} · nearby sessions grouped using your tracking settings.` : 'No recorded sessions for this project.'}</p>
        <div class="project-controls"><a class="btn small" href="${url}&format=csv" download>Export CSV</a><a class="btn small" href="${url}&format=text" download>Export text</a></div>
        <div class="project-controls"><label>From date<input id="projectFrom" type="date" aria-label="Project history from date"></label><label>To date<input id="projectTo" type="date" aria-label="Project history to date"></label><button class="btn small" id="projectReset" type="button">Reset dates</button></div>
        <p class="project-hint" id="projectHistoryCount" role="status"></p><ol class="project-history" id="projectHistory"></ol>`;
      function history() {
        const from = $('projectFrom').value; const to = $('projectTo').value;
        const visible = sessions.filter(session => (!from || session.date >= from) && (!to || session.date <= to));
        $('projectHistoryCount').textContent = from && to && from > to ? 'Choose a to date on or after the from date.' : `${visible.length} of ${sessions.length} entries · ${fmt.dur(visible.reduce((sum,s) => sum + s.duration_seconds,0))} shown`;
        $('projectHistory').innerHTML = visible.length ? visible.map(session => `<li><div class="project-session-head"><strong>${escape(session.date)}</strong><span>${escape(session.start_time)}–${escape(session.end_time)} · ${escape(session.duration)}</span></div><p class="project-session-notes">${escape(session.notes || 'No notes yet.')}</p><button type="button" class="btn small" data-project-notes="${sessions.indexOf(session)}">${session.notes ? 'Edit notes' : 'Add notes'}</button></li>`).join('') : '<li class="project-hint">No sessions in this date range.</li>';
      }
      $('projectFrom').onchange = history; $('projectTo').onchange = history;
      $('projectReset').onclick = () => { $('projectFrom').value = ''; $('projectTo').value = ''; history(); };
      $('projectHistory').onclick = event => {
        const button = event.target.closest('[data-project-notes]');
        if (!button) return;
        const session = sessions[Number(button.dataset.projectNotes)];
        $('projectDialog').close();
        setTimeout(() => {
          const activeSeconds = Object.fromEntries((session.session_entries || []).map(entry => [String(entry.id),entry.active_seconds || 0]));
          openNotesPopup({ sessionIds: session.session_ids, projectName: name, notes: report.notes || {}, todos: report.todos || {}, startTimes: report.start_times || {}, endTimes: report.end_times || {}, lastSeenTimes: report.last_seen_times || {}, activeSeconds });
          const observer = new MutationObserver(() => {
            if (!$('notesModal').classList.contains('show')) { observer.disconnect(); openProject(name); }
          });
          observer.observe($('notesModal'), { attributes:true, attributeFilter:['class'] });
        },0);
      };
      history();
    } catch (error) {
      if (error.name === 'AbortError') return;
      $('projectContent').innerHTML = '<p class="project-error" role="alert">Project history could not load. Check the dashboard connection.</p><button class="btn" id="projectRetry">Try again</button>';
      $('projectRetry').onclick = () => openProject(name);
    }
  }
  $('projectDialog').addEventListener('close', () => state.projectRequest?.abort());

  function reviewHtml(summary) {
    const review = summary.weekly_review;
    if (!review) return '<p class="project-hint">Weekly review is unavailable. Restart the dashboard to load the current API.</p>';
    const top = review.top_project; const best = review.best_day;
    let comparison = 'Comparison begins after the first complete day of this week.';
    const days = review.completed_days;
    if (days) {
      const current = review.comparison_seconds; const previous = review.previous_comparison_seconds;
      const delta = current - previous;
      const percent = previous > 0 ? delta === 0 ? 'same recorded time' : `${Math.abs(delta / previous * 100).toFixed(1).replace(/\.0$/, '')}% ${delta > 0 ? 'more' : 'less'}` : null;
      comparison = `First ${days} complete day${days === 1 ? '' : 's'}: ${fmt.dur(current)} vs ${fmt.dur(previous)} in the previous week${percent ? ` · ${percent}` : ' · no previous activity to compare'}. Today is excluded from both periods.`;
    }
    return `<p class="review-period">${escape(shortRange(review.week_start,review.week_end))} · current week, in progress</p>
      ${review.total_seconds ? `<dl class="review-metrics"><div><dt>Recorded so far</dt><dd>${fmt.dur(review.total_seconds)}</dd></div><div><dt>Top project</dt><dd>${top ? `<button class="project-link" data-project-name="${encodeURIComponent(top.project_name)}">${escape(top.project_name)}</button><div class="project-hint">${fmt.dur(top.total_seconds)}</div>` : '—'}</dd></div><div><dt>Best day so far</dt><dd>${best ? `${escape(shortDate(best.day))}<div class="project-hint">${fmt.dur(best.total_seconds)}</div>` : '—'}</dd></div></dl>` : '<p class="project-hint">No recorded activity this week.</p>'}
      <p class="review-comparison">${escape(comparison)}</p>`;
  }

  function yearHtml(data) {
    const today = new Date(); today.setHours(12,0,0,0);
    const start = addDays(today,-364); const padded = startOfWeek(start);
    const daily = dailySecondsByKey(data.year_daily || []);
    const max = Math.max(1,...daily.values());
    let cells = '';
    for (let day = new Date(padded); day <= today; day = addDays(day,1)) {
      const key = localDateKey(day); const seconds = daily.get(key) || 0;
      if (day < start) { cells += '<span aria-hidden="true"></span>'; continue; }
      cells += `<button class="year-cell" type="button" data-year-date="${key}" title="${key} · ${fmt.dur(seconds)}" aria-label="${key}, ${fmt.dur(seconds)} recorded" style="background:${heatmapColor(seconds,max)}"></button>`;
    }
    const dateLabel = day => day.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'});
    return `<p class="project-hint">${escape(dateLabel(start))} – ${escape(dateLabel(today))} · rolling year</p><div class="year-legend"><span>${[...daily.values()].filter(v=>v>0).length} active days</span><span>Daily time · stronger accent means more · peak ${fmt.dur(max === 1 ? 0 : max)}</span></div><div class="year-scroll" tabindex="0" aria-label="Year activity, scroll horizontally"><div class="year-grid">${cells}</div></div><p class="project-hint" id="yearSelection" role="status">Select a day to see its recorded time. Arrow keys move between days.</p><button class="btn small year-detail" id="hourlyActivity">Activity by hour</button>`;
  }
  function activityDetail(dateKey = '') {
    showActivity();
    const today = localDateKey(new Date());
    const oldest = localDateKey(addDays(dateFromKey(today),-364));
    if (dateKey && (dateKey < oldest || dateKey > today)) {
      $('activityContent').innerHTML = `<p class="project-hint">Hourly detail is available from ${escape(oldest)} through ${escape(today)}. The weekly totals and daily bars use the full recorded history.</p>`;
      return;
    }
    $('activityContent').innerHTML = dateKey ? `<p class="project-hint">${escape(dateKey)} · ${fmt.dur(dailySecondsByKey(state.data.year_daily).get(dateKey) || 0)} recorded</p><div id="hourlyMount"></div>` : '<div id="hourlyMount"></div>';
    renderActivityHeatmap(state.data.year_daily || [],state.data.year_hourly || [],$('hourlyMount'),dateKey);
  }

  function refreshStatus() {
    const summary = state.data?.summary;
    const status = $('sessionStatus'); const label = $('sessionStatusText'); const timer = $('sessionTimer');
    if (!summary) {
      if (state.unavailable) { status.dataset.state = 'unavailable'; label.textContent = 'Tracking status unavailable'; timer.hidden = true; }
      return;
    }
    if (state.unavailable || Date.now() - state.lastSuccess > 95000) {
      status.dataset.state = 'unavailable'; label.textContent = 'Tracking status unavailable';
    } else if (summary.tracking_paused) {
      status.dataset.state = 'paused'; label.textContent = 'Paused';
    } else if (summary.tracking_active && summary.live_session_last_seen_time && Date.now()/1000 - summary.live_session_last_seen_time <= 90) {
      status.dataset.state = 'live'; label.textContent = summary.live_project ? `Working on: ${summary.live_project}` : 'Tracking';
    } else if (summary.ableton_has_project || summary.ableton_running) {
      status.dataset.state = 'idle'; label.textContent = summary.ableton_has_project ? 'Idle' : 'Ableton open (no project)';
    } else { status.dataset.state = 'off'; label.textContent = 'Ableton not running'; }
    timer.hidden = !summary.live_project || !Number.isFinite(summary.live_session_recorded_seconds);
    timer.textContent = `${clock(summary.live_session_recorded_seconds)} recorded`;
    timer.title = 'Recorded active time in the current session. Updates when the tracker records activity; pauses and idle time are excluded.';
  }
  function acceptData(data) {
    if (!data?.summary) { state.unavailable = true; refreshStatus(); return; }
    state.data = data; state.lastSuccess = Date.now(); state.unavailable = false; refreshStatus();
  }
  function render(data) {
    acceptData(data);
    const root = $('appDashboard');
    root.querySelectorAll('.overview-disclosure').forEach((section,index) => {
      const key = `existing-${index}`;
      if (state.openSections.has(key)) section.open = true;
      section.addEventListener('toggle', () => section.open ? state.openSections.add(key) : state.openSections.delete(key));
    });
    const container = document.createElement('div');
    container.innerHTML = `<details class="optional-section" id="weeklyReview" ${state.openSections.has('weeklyReview') ? 'open' : ''}><summary>Weekly review <small>This week so far</small></summary>${reviewHtml(data.summary)}</details><details class="optional-section" id="yearActivity" ${state.openSections.has('yearActivity') ? 'open' : ''}><summary>Year activity <small>Recorded time by day</small></summary>${yearHtml(data)}</details>`;
    root.insertBefore(container, $('detailModal'));
    container.querySelectorAll('details').forEach(section => section.addEventListener('toggle', () => {
      if (section.open) state.openSections.add(section.id); else state.openSections.delete(section.id);
      if (section.open && section.id === 'yearActivity') { const scroll = section.querySelector('.year-scroll'); scroll.scrollLeft = scroll.scrollWidth; }
    }));
    $('hourlyActivity').onclick = () => activityDetail();
    const yearButtons = [...container.querySelectorAll('[data-year-date]')];
    yearButtons.forEach((button,index) => {
      button.tabIndex = index === yearButtons.length-1 ? 0 : -1;
      button.onclick = () => { $('yearSelection').textContent = `${button.dataset.yearDate} · ${fmt.dur(dailySecondsByKey(data.year_daily).get(button.dataset.yearDate) || 0)} recorded`; };
      button.onkeydown = event => {
        const direction = {ArrowLeft:-7,ArrowRight:7,ArrowUp:-1,ArrowDown:1,Home:-index,End:yearButtons.length-1-index}[event.key];
        if (direction === undefined) return;
        event.preventDefault(); const next = yearButtons[Math.min(Math.max(index+direction,0),yearButtons.length-1)];
        button.tabIndex = -1; next.tabIndex = 0; next.focus(); next.click();
      };
    });
    root.querySelectorAll('[data-activity-date]').forEach(button => button.onclick = event => { event.stopPropagation(); activityDetail(button.dataset.activityDate); });
  }
  document.addEventListener('click', event => {
    const project = event.target.closest('[data-open-project]');
    if (project) { openProject(decodeURIComponent(project.dataset.openProject)); return; }
    const review = event.target.closest('#weeklyReview [data-project-name]');
    if (review) openProject(decodeURIComponent(review.dataset.projectName));
  });

  // Existing dialogs share the same keyboard containment and restore their trigger.
  let legacyOrigin = null; let legacyOpen = false;
  document.addEventListener('click', event => { if (!legacy()) state.trigger = event.target.closest('button,a,summary,[tabindex="0"]') || document.activeElement; }, true);
  document.addEventListener('keydown', event => { if (!legacy() && (event.key === 'Enter' || event.key === ' ')) state.trigger = document.activeElement; }, true);
  const legacy = () => [...document.querySelectorAll('.modal-backdrop.show,.detail-modal-backdrop.is-open')].at(-1);
  const observer = new MutationObserver(records => {
    if (!records.some(record => record.type === 'childList' || record.attributeName === 'class')) return;
    const open = !!legacy();
    if (open === legacyOpen) return;
    if (open) legacyOrigin = state.trigger;
    document.querySelector('.page').inert = open;
    document.querySelector('header').inert = open;
    // Detail dialogs live inside .page, so inert only their siblings.
    if (open && legacy().closest('.page')) {
      document.querySelector('.page').inert = false;
      document.querySelectorAll('.intro,.mark,#appSettings,#appDashboard > :not(.detail-modal-backdrop)').forEach(el => { el.inert = true; });
    } else {
      document.querySelectorAll('.intro,.mark,#appSettings,#appDashboard > :not(.detail-modal-backdrop)').forEach(el => { el.inert = false; });
    }
    legacyOpen = open;
    if (!open) { focusBack(legacyOrigin); lastETag = null; }
  });
  observer.observe(document.body,{subtree:true,attributes:true,attributeFilter:['class'],childList:true});
  document.addEventListener('keydown', event => {
    if (event.key !== 'Tab' || !legacy()) return;
    const elements = [...legacy().querySelectorAll('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href],[tabindex="0"]')].filter(el=>el.getClientRects().length);
    if (!elements.length) return;
    const first = elements[0]; const last = elements.at(-1);
    if (event.shiftKey && (document.activeElement === first || !legacy().contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !legacy().contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  });
  setInterval(refreshStatus,1000);
  return {render,acceptData,isEditing,refreshStatus,openProject,activityDetail,connectionFailed:()=>{state.unavailable=true;refreshStatus();}};
})();
