/* Projects view — window.Projects namespace */
(function () {
  'use strict';

  let _lastData     = null;
  let _sortOrder    = 'time';   // 'time' | 'name' | 'recent'
  let _searchQuery  = '';
  let _categoryFilter = '';     // '' = all, or category_key
  let _timeFilter   = 'all';    // 'all' | 'month' | 'week'
  let _viewMode     = 'grid';   // 'grid' | 'list'
  let _reportAbort  = null;
  let _reportProject = null;    // project name currently open in modal

  /* ── helpers ──────────────────────────────────────────────────── */

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
    );
  }

  function durFmt(s) {
    if (!s || s < 60) return s > 0 ? '<1m' : '0m';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  }

  function getColor(project) {
    if (project.category_color) return project.category_color;
    if (project.category_key && window.CATEGORY_BY_KEY) {
      const meta = window.CATEGORY_BY_KEY[project.category_key];
      if (meta?.color) return meta.color;
    }
    return (window.PROJECT_COLORS || ['#7C5CFF'])[0];
  }

  function getCatLabel(project) {
    if (project.category_label) return project.category_label;
    if (project.category_key && window.CATEGORY_BY_KEY) {
      return window.CATEGORY_BY_KEY[project.category_key]?.label || 'Uncategorized';
    }
    return 'Uncategorized';
  }

  function fmtDate(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  }

  /* ── sort / filter ────────────────────────────────────────────── */

  function getWeekStart() {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - d.getDay());
    return d.getTime() / 1000;
  }

  function getMonthStart() {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(1);
    return d.getTime() / 1000;
  }

  function sortProjects(projects) {
    const copy = [...projects];
    if (_sortOrder === 'name') {
      copy.sort((a, b) => a.project_name.localeCompare(b.project_name));
    } else if (_sortOrder === 'recent') {
      copy.sort((a, b) => (b.last_session || 0) - (a.last_session || 0));
    } else {
      copy.sort((a, b) => (b.total_seconds || 0) - (a.total_seconds || 0));
    }
    return copy;
  }

  function filterProjects(projects) {
    let result = projects;

    if (_searchQuery) {
      const q = _searchQuery.toLowerCase();
      result = result.filter(p => p.project_name.toLowerCase().includes(q));
    }

    if (_categoryFilter) {
      result = result.filter(p => (p.category_key || '') === _categoryFilter);
    }

    if (_timeFilter === 'week') {
      const weekStart = getWeekStart();
      result = result.filter(p => (p.last_session || 0) >= weekStart);
    } else if (_timeFilter === 'month') {
      const monthStart = getMonthStart();
      result = result.filter(p => (p.last_session || 0) >= monthStart);
    }

    return result;
  }

  /* ── render ───────────────────────────────────────────────────── */

  function groupByCategory(projects) {
    const buckets = new Map();
    projects.forEach(p => {
      const key = p.category_key || '__none';
      const label = getCatLabel(p);
      const color = p.category_color ||
        (p.category_key && window.CATEGORY_BY_KEY?.[p.category_key]?.color) || '#8E8E93';
      if (!buckets.has(key)) buckets.set(key, { key, label, color, projects: [] });
      buckets.get(key).projects.push(p);
    });
    return [...buckets.values()].sort((a, b) => {
      const ta = a.projects.reduce((s, p) => s + (p.total_seconds || 0), 0);
      const tb = b.projects.reduce((s, p) => s + (p.total_seconds || 0), 0);
      return tb - ta;
    });
  }

  function renderProjectCard(project) {
    const color = getColor(project);
    const isUntitled = /^untitled\s*(project)?$/i.test(project.project_name.trim());
    return `
      <button class="proj-card" type="button"
        data-project-report="${esc(project.project_name)}"
        style="--proj-color:${color}">
        <span class="proj-card-dot"></span>
        <span class="proj-card-name ${isUntitled ? 'proj-card-name--untitled' : ''}">${esc(project.project_name)}</span>
        <span class="proj-card-meta">
          <span class="proj-card-time">${durFmt(project.total_seconds)}</span>
          <span class="proj-card-sessions">${project.session_count} session${project.session_count !== 1 ? 's' : ''}</span>
        </span>
      </button>`;
  }

  function renderCategoryGroup(cat) {
    const sorted = sortProjects(cat.projects);
    const cards = sorted.map(p => renderProjectCard(p)).join('');
    const total = cat.projects.reduce((s, p) => s + (p.total_seconds || 0), 0);
    return `
      <section class="proj-cat-group">
        <div class="proj-cat-header">
          <span class="proj-cat-swatch" style="background:${cat.color}"></span>
          <span class="proj-cat-label">${esc(cat.label)}</span>
          <span class="proj-cat-count">${cat.projects.length} project${cat.projects.length !== 1 ? 's' : ''}</span>
          <span class="proj-cat-total">${durFmt(total)}</span>
        </div>
        <div class="proj-cat-cards">${cards}</div>
      </section>`;
  }

  function renderListView(projects) {
    const categories = groupByCategory(projects);
    return categories.map(cat => {
      const sorted = sortProjects(cat.projects);
      const rows = sorted.map(p => {
        const color = getColor(p);
        const isUntitled = /^untitled\s*(project)?$/i.test(p.project_name.trim());
        return `
          <tr class="proj-list-row" data-project-report="${esc(p.project_name)}">
            <td class="proj-list-name">
              <span class="proj-list-dot" style="background:${color}"></span>
              <span class="${isUntitled ? 'proj-list-name--untitled' : ''}">${esc(p.project_name)}</span>
            </td>
            <td class="proj-list-dur">${durFmt(p.total_seconds)}</td>
            <td class="proj-list-sessions">${p.session_count} session${p.session_count !== 1 ? 's' : ''}</td>
            <td class="proj-list-last">${fmtDate(p.last_session)}</td>
          </tr>`;
      }).join('');
      const total = cat.projects.reduce((s, p) => s + (p.total_seconds || 0), 0);
      return `
        <section class="proj-list-section">
          <div class="proj-list-section-header">
            <span class="proj-cat-swatch" style="background:${cat.color}"></span>
            <span class="proj-cat-label">${esc(cat.label)}</span>
            <span class="proj-cat-count">${cat.projects.length} project${cat.projects.length !== 1 ? 's' : ''}</span>
            <span class="proj-cat-total">${durFmt(total)}</span>
          </div>
          <table class="proj-list-table">
            <thead><tr>
              <th>Project</th><th>Time</th><th>Sessions</th><th>Last Session</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </section>`;
    }).join('');
  }

  function buildCategoryOptions(allProjects) {
    const seen = new Map();
    allProjects.forEach(p => {
      const key = p.category_key || '';
      if (!seen.has(key)) seen.set(key, getCatLabel(p));
    });
    let opts = `<option value="">All Categories</option>`;
    seen.forEach((label, key) => {
      opts += `<option value="${esc(key)}" ${_categoryFilter === key ? 'selected' : ''}>${esc(label)}</option>`;
    });
    return opts;
  }

  function renderView(data) {
    const allProjects = data.projects || [];
    const filtered = filterProjects(allProjects);

    let body;
    if (allProjects.length === 0) {
      body = `<div class="empty"><p>No projects yet</p><small>start Ableton to begin tracking</small></div>`;
    } else if (filtered.length === 0) {
      body = `<div class="empty"><p>No projects match the current filters</p><small>try adjusting your search or filters</small></div>`;
    } else if (_viewMode === 'list') {
      body = renderListView(filtered);
    } else {
      const categories = groupByCategory(filtered);
      body = categories.map(cat => renderCategoryGroup(cat)).join('');
    }

    return `
      <div class="chart-card chart-card-wide" style="margin-bottom:20px">
        <div class="section-head">
          <h3 class="section-title">By Category</h3>
        </div>
        <div class="chart-wrap"><div id="categoryChart"></div></div>
      </div>
      <div class="proj-toolbar">
        <div class="proj-search-wrap">
          <svg class="proj-search-icon" viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="6.5" cy="6.5" r="4.5"/>
            <path d="M10.5 10.5L14 14"/>
          </svg>
          <input class="proj-search" id="projSearch" type="search"
            placeholder="Search projects…" value="${esc(_searchQuery)}" autocomplete="off">
        </div>
        <div class="proj-filter-wrap">
          <label class="proj-filter-label" for="projCatFilter">Category</label>
          <select class="proj-filter" id="projCatFilter">
            ${buildCategoryOptions(allProjects)}
          </select>
        </div>
        <div class="proj-filter-wrap">
          <label class="proj-filter-label" for="projTimeFilter">Time</label>
          <select class="proj-filter" id="projTimeFilter">
            <option value="all" ${_timeFilter === 'all' ? 'selected' : ''}>All Time</option>
            <option value="month" ${_timeFilter === 'month' ? 'selected' : ''}>This Month</option>
            <option value="week" ${_timeFilter === 'week' ? 'selected' : ''}>This Week</option>
          </select>
        </div>
        <div class="proj-filter-wrap">
          <label class="proj-filter-label" for="projSort">Sort</label>
          <select class="proj-sort proj-filter" id="projSort">
            <option value="time" ${_sortOrder === 'time' ? 'selected' : ''}>Most Time</option>
            <option value="name" ${_sortOrder === 'name' ? 'selected' : ''}>Name A–Z</option>
            <option value="recent" ${_sortOrder === 'recent' ? 'selected' : ''}>Recent</option>
          </select>
        </div>
        <div class="proj-view-toggle" role="group" aria-label="View mode">
          <button class="proj-view-btn ${_viewMode === 'grid' ? 'is-active' : ''}" id="projViewGrid" type="button" title="Grid view" aria-pressed="${_viewMode === 'grid'}">
            <svg viewBox="0 0 14 14"><rect x="0" y="0" width="6" height="6" rx="1"/><rect x="8" y="0" width="6" height="6" rx="1"/><rect x="0" y="8" width="6" height="6" rx="1"/><rect x="8" y="8" width="6" height="6" rx="1"/></svg>
          </button>
          <button class="proj-view-btn ${_viewMode === 'list' ? 'is-active' : ''}" id="projViewList" type="button" title="List view" aria-pressed="${_viewMode === 'list'}">
            <svg viewBox="0 0 14 14"><line x1="0" y1="2" x2="14" y2="2"/><line x1="0" y1="7" x2="14" y2="7"/><line x1="0" y1="12" x2="14" y2="12"/></svg>
          </button>
        </div>
      </div>
      <div class="proj-body" id="projBody">${body}</div>`;
  }

  function repaintBody() {
    const body = document.getElementById('projBody');
    if (!body) { render(_lastData); return; }
    const allProjects = _lastData.projects || [];
    const filtered = filterProjects(allProjects);

    if (allProjects.length === 0) {
      body.innerHTML = `<div class="empty"><p>No projects yet</p><small>start Ableton to begin tracking</small></div>`;
    } else if (filtered.length === 0) {
      body.innerHTML = `<div class="empty"><p>No projects match the current filters</p><small>try adjusting your search or filters</small></div>`;
    } else if (_viewMode === 'list') {
      body.innerHTML = renderListView(filtered);
    } else {
      const categories = groupByCategory(filtered);
      body.innerHTML = categories.map(cat => renderCategoryGroup(cat)).join('');
    }
    bindCards();
  }

  /* ── modal ────────────────────────────────────────────────────── */

  function buildReportActions(projectName, report) {
    const actions = document.getElementById('prmActions');
    if (!actions) return;

    // Find current category for this project
    const proj = (_lastData?.projects || []).find(p => p.project_name === projectName);
    const catKey   = proj?.category_key || null;
    const catColor = proj?.category_color ||
      (catKey && window.CATEGORY_BY_KEY?.[catKey]?.color) || '#8E8E93';
    const catLabel = proj
      ? (proj.category_label || window.CATEGORY_BY_KEY?.[catKey]?.label || 'Uncategorized')
      : 'Uncategorized';

    const catPill = `
      <button class="prm-action-pill" id="prmChangeCat" type="button" title="Change category"
        data-project="${esc(projectName)}" data-cat-key="${esc(catKey || '')}">
        <span class="prm-action-pill-dot" style="background:${catColor}"></span>
        ${esc(catLabel)}
      </button>`;

    const csvBtn = `
      <a class="prm-action-icon" href="/api/project-report/download?project=${encodeURIComponent(projectName)}&format=csv"
        download="${esc(projectName)}.csv" title="Download CSV" role="button">
        <svg viewBox="0 0 14 14"><path d="M7 1v8M4 6l3 3 3-3M1 10v1a2 2 0 002 2h8a2 2 0 002-2v-1"/></svg>
      </a>`;

    actions.innerHTML = catPill + csvBtn;

    document.getElementById('prmChangeCat')?.addEventListener('click', () => {
      if (typeof openCategoryPicker === 'function') {
        openCategoryPicker({ projectName, categoryKey: catKey });
      }
    });
  }

  function openModal(projectName) {
    const backdrop = document.getElementById('projectReportModal');
    if (!backdrop) return;

    _reportProject = projectName;

    const title   = backdrop.querySelector('.prm-title');
    const meta    = backdrop.querySelector('.prm-meta');
    const body    = backdrop.querySelector('.prm-body');
    const actions = document.getElementById('prmActions');

    if (title)   title.textContent = projectName;
    if (meta)    meta.textContent  = '';
    if (body)    body.innerHTML    = '<div class="prm-loading">Loading…</div>';
    if (actions) actions.innerHTML = '';

    backdrop.classList.add('is-open');
    backdrop.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';

    if (_reportAbort) _reportAbort.abort();
    _reportAbort = new AbortController();

    fetch(`/api/project-report?project=${encodeURIComponent(projectName)}`, { signal: _reportAbort.signal })
      .then(r => r.json())
      .then(report => {
        if (meta) meta.textContent = `${durFmt(report.total_duration_seconds)} total · ${report.session_count} session${report.session_count !== 1 ? 's' : ''}`;
        if (body) body.innerHTML = renderReport(report, projectName);
        buildReportActions(projectName, report);
        bindReportActions(report, projectName);
      })
      .catch(err => {
        if (err.name === 'AbortError') return;
        if (body) body.innerHTML = `<div class="empty"><p>Could not load report</p><small>${esc(err.message)}</small></div>`;
      });
  }

  function closeModal() {
    const backdrop = document.getElementById('projectReportModal');
    if (!backdrop) return;
    backdrop.classList.remove('is-open');
    backdrop.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    if (_reportAbort) { _reportAbort.abort(); _reportAbort = null; }
    _reportProject = null;
  }

  function renderReport(report, projectName) {
    if (!report.sessions || report.sessions.length === 0) {
      return `<div class="empty"><p>No sessions found</p></div>`;
    }
    const rows = report.sessions.map((s, idx) => {
      const notes = s.notes ? `<div class="prm-notes"><span class="prm-notes-label">Notes</span>${esc(s.notes)}</div>` : '';
      const todos = s.todo_notes ? `<div class="prm-notes"><span class="prm-notes-label">To-do</span>${esc(s.todo_notes)}</div>` : '';
      const sessionIds = (s.session_ids || []).join(',');
      return `
        <div class="prm-session" data-session-index="${idx}">
          <div class="prm-session-head">
            <span class="prm-session-date">${esc(s.date)}</span>
            <span class="prm-session-time">${esc(s.start_time)} – ${esc(s.end_time)}</span>
            <span class="prm-session-dur">${esc(s.duration)}</span>
            <div class="prm-session-actions">
              <button class="prm-session-action" type="button"
                title="Edit notes"
                data-action="edit-notes"
                data-session-index="${idx}"
                data-session-ids="${esc(sessionIds)}"
                data-project="${esc(projectName || '')}">
                <svg viewBox="0 0 14 14"><path d="M9.5 1.5l3 3-8 8H1.5v-3l8-8z"/><path d="M8 3l3 3"/></svg>
              </button>
              <button class="prm-session-action is-delete" type="button"
                title="Delete session"
                data-action="delete-session"
                data-session-ids="${esc(sessionIds)}"
                data-session-index="${idx}">
                <svg viewBox="0 0 14 14"><polyline points="1,3 13,3"/><path d="M5 3V2h4v1M3 3l1 9h6l1-9"/><line x1="5" y1="6" x2="5" y2="10"/><line x1="9" y1="6" x2="9" y2="10"/></svg>
              </button>
            </div>
          </div>
          ${notes}${todos}
        </div>`;
    }).join('');
    return `<div class="prm-sessions">${rows}</div>`;
  }

  function buildNotesEntries(report, projectName) {
    return report.sessions.map(s => {
      const ids = s.session_ids || [];
      const notesMap = {};
      const todoNotesMap = {};
      const todosMap = {};
      const startTimesMap = {};
      const endTimesMap = {};
      const lastSeenTimesMap = {};
      ids.forEach(sid => {
        const k = String(sid);
        notesMap[k]      = (report.notes      || {})[k] || '';
        todoNotesMap[k]  = (report.todo_notes || {})[k] || '';
        todosMap[k]      = (report.todos       || {})[k] || [];
        startTimesMap[k]    = (report.start_times     || {})[k] || 0;
        endTimesMap[k]      = (report.end_times       || {})[k] ?? null;
        lastSeenTimesMap[k] = (report.last_seen_times || {})[k] || 0;
      });
      return {
        sessionIds:    ids,
        projectName:   projectName,
        notes:         notesMap,
        todoNotes:     todoNotesMap,
        todos:         todosMap,
        startTimes:    startTimesMap,
        endTimes:      endTimesMap,
        lastSeenTimes: lastSeenTimesMap,
      };
    });
  }

  function bindReportActions(report, projectName) {
    const body = document.getElementById('prmBody');
    if (!body) return;

    const entries = buildNotesEntries(report, projectName);

    body.querySelectorAll('[data-action="edit-notes"]').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.dataset.sessionIndex, 10);
        if (typeof window.openNotesModalWithData === 'function') {
          window.openNotesModalWithData(entries, idx);
        }
      });
    });

    body.querySelectorAll('[data-action="delete-session"]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const ids = (btn.dataset.sessionIds || '')
          .split(',').map(s => parseInt(s, 10)).filter(n => Number.isFinite(n));
        if (!ids.length) return;
        if (!confirm(`Delete this session? This cannot be undone.`)) return;
        try {
          const r = await fetch('/api/delete-session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_ids: ids }),
          });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          // Refresh modal content
          if (_reportProject) openModal(_reportProject);
        } catch (err) {
          alert(`Failed to delete session: ${err.message}`);
        }
      });
    });
  }

  /* ── event binding ────────────────────────────────────────────── */

  function bindCards() {
    const container = document.getElementById('appProjects');
    if (!container) return;
    container.querySelectorAll('[data-project-report]').forEach(btn => {
      btn.addEventListener('click', () => openModal(btn.dataset.projectReport));
    });
  }

  function bindToolbar() {
    const container = document.getElementById('appProjects');
    if (!container) return;

    const search     = container.querySelector('#projSearch');
    const sort       = container.querySelector('#projSort');
    const catFilter  = container.querySelector('#projCatFilter');
    const timeFilter = container.querySelector('#projTimeFilter');
    const gridBtn    = container.querySelector('#projViewGrid');
    const listBtn    = container.querySelector('#projViewList');

    if (search) {
      search.addEventListener('input', e => { _searchQuery = e.target.value; repaintBody(); });
    }
    if (sort) {
      sort.addEventListener('change', e => { _sortOrder = e.target.value; repaintBody(); });
    }
    if (catFilter) {
      catFilter.addEventListener('change', e => { _categoryFilter = e.target.value; repaintBody(); });
    }
    if (timeFilter) {
      timeFilter.addEventListener('change', e => { _timeFilter = e.target.value; repaintBody(); });
    }
    if (gridBtn) {
      gridBtn.addEventListener('click', () => {
        if (_viewMode === 'grid') return;
        _viewMode = 'grid';
        gridBtn.classList.add('is-active');
        gridBtn.setAttribute('aria-pressed', 'true');
        listBtn?.classList.remove('is-active');
        listBtn?.setAttribute('aria-pressed', 'false');
        repaintBody();
      });
    }
    if (listBtn) {
      listBtn.addEventListener('click', () => {
        if (_viewMode === 'list') return;
        _viewMode = 'list';
        listBtn.classList.add('is-active');
        listBtn.setAttribute('aria-pressed', 'true');
        gridBtn?.classList.remove('is-active');
        gridBtn?.setAttribute('aria-pressed', 'false');
        repaintBody();
      });
    }
  }

  function bindModalControls() {
    const backdrop = document.getElementById('projectReportModal');
    if (!backdrop) return;

    const closeBtn = backdrop.querySelector('.prm-close');
    if (closeBtn) closeBtn.addEventListener('click', closeModal);

    backdrop.addEventListener('click', e => {
      if (e.target === backdrop) closeModal();
    });

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && backdrop.classList.contains('is-open')) closeModal();
    });
  }

  /* ── public API ───────────────────────────────────────────────── */

  function init() {
    bindModalControls();
  }

  function render(data) {
    _lastData = data;
    const container = document.getElementById('appProjects');
    if (!container) return;
    container.innerHTML = renderView(data);
    bindToolbar();
    bindCards();
    if (typeof renderCategoryChart === 'function') {
      renderCategoryChart(data.projects || [], data.summary || {});
    }
  }

  window.Projects = { init, render };
})();
