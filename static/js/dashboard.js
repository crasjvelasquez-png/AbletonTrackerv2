const PROJECT_COLORS = [
  '#4f46e5','#0ea5e9','#6366f1','#8b5cf6','#34c759',
  '#f59e0b','#f43f5e','#8e8e93','#aeaeb2','#c7c7cc'
];
const UNTITLED = new Set(['untitled','untitled project','']);
const isUntitled = n => UNTITLED.has((n || '').trim().toLowerCase());
window.THEME_STORAGE_KEY = 'ableton_tracker_theme';
window.UI_SCALE_STORAGE_KEY = 'ableton_tracker_ui_scale';
const WEEK_START_DAY_NAMES = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const DEFAULT_WEEK_START_BACKEND = 4; // Friday in Python weekday()
let WEEK_START_BACKEND_DAY = DEFAULT_WEEK_START_BACKEND; // 0=Mon ... 6=Sun (Python weekday)
const CUSTOM_CATEGORY_LIMIT_FALLBACK = 12;
const CATEGORY_COLOR_PRESETS = [
  '#FF6B6B', '#FF9F43', '#FFD166', '#7BD389',
  '#3EC1D3', '#4D96FF', '#7C5CFF', '#C77DFF',
  '#F15BB5', '#6D6875', '#A3A380', '#F28482',
];
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
let latestDashboardData = null;
let boardSearchQuery = '';
let boardTypeFilter = '';
let activeView = window.location.hash === '#settings'
  ? 'settings'
  : (window.location.hash === '#dashboard' ? 'dashboard' : 'planner');
let CATEGORY_OPTIONS = [];
let CATEGORY_BY_KEY = {};
const dashboardMonthState = {
  selectedMonth: null,
};
const dailyTargetState = {
  viewedDate: localDateKey(new Date()),
  requestToken: 0,
  historyRequestToken: 0,
  historyRows: [],
};
const weeklyTargetState = {
  currentWeekStartDate: null,
  requestToken: 0,
  historyRequestToken: 0,
  historyRows: [],
  dailyRows: [],
};

function debounce(fn, wait = 120) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function activeTheme() {
  const stored = localStorage.getItem(window.THEME_STORAGE_KEY);
  if (stored === 'light' || stored === 'dark') return stored;
  return systemTheme.matches ? 'dark' : 'light';
}

function updateThemeToggle() {
  const theme = activeTheme();
  const next = theme === 'dark' ? 'light' : 'dark';
  const btn = document.getElementById('themeToggle');
  btn.setAttribute('aria-label', `Switch to ${next} mode`);
  btn.title = `Switch to ${next} mode`;
}

function applyStoredTheme() {
  const stored = localStorage.getItem(window.THEME_STORAGE_KEY);
  if (stored === 'light' || stored === 'dark') {
    document.documentElement.dataset.theme = stored;
  } else {
    delete document.documentElement.dataset.theme;
  }
  updateThemeToggle();
}

function toggleTheme() {
  const root = document.documentElement;
  root.classList.add('theme-switching');
  localStorage.setItem(window.THEME_STORAGE_KEY, activeTheme() === 'dark' ? 'light' : 'dark');
  applyStoredTheme();
  requestAnimationFrame(() => {
    requestAnimationFrame(() => root.classList.remove('theme-switching'));
  });
}

function normalizeUiScale(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 100;
  return Math.min(120, Math.max(85, Math.round(numeric / 5) * 5));
}

function applyUiScale(value) {
  const scale = normalizeUiScale(value);
  const factor = scale / 100;
  const root = document.documentElement;
  root.style.fontSize = `${15 * factor}px`;
  root.style.setProperty('--ui-gap', `${14 * factor}px`);
  root.style.setProperty('--ui-section-gap', `${28 * factor}px`);
  root.style.setProperty('--ui-card-pad-y', `${28 * factor}px`);
  root.style.setProperty('--ui-card-pad-x', `${22 * factor}px`);
  root.style.setProperty('--ui-card-min-h', `${203 * factor}px`);
  root.style.setProperty('--ui-page-max', `${1200 + ((factor - 1) * 280)}px`);
  try {
    localStorage.setItem(window.UI_SCALE_STORAGE_KEY, String(scale));
  } catch (_) {}
  return scale;
}

async function hydrateUiScaleSetting() {
  try {
    const res = await fetch('/api/app-settings');
    const settings = await res.json();
    if (settings && settings.ui_scale != null) applyUiScale(settings.ui_scale);
  } catch (_) {}
}

function syncCategoryOptions(options) {
  CATEGORY_OPTIONS = Array.isArray(options) ? options : [];
  CATEGORY_BY_KEY = Object.fromEntries(CATEGORY_OPTIONS.map(option => [option.key, option]));
}

function syncProjectOptions(statusOptions, typeOptions) {
  PROJECT_STATUS_OPTIONS = [['', 'Set status']];
  if (Array.isArray(statusOptions)) {
    statusOptions.forEach(([key, label]) => PROJECT_STATUS_OPTIONS.push([key, label]));
  }
  
  PROJECT_TYPE_OPTIONS = [['', 'Set type']];
  if (Array.isArray(typeOptions)) {
    typeOptions.forEach(([key, label]) => PROJECT_TYPE_OPTIONS.push([key, label]));
  }
}

function normalizeHexColor(value) {
  const text = String(value || '').trim().toUpperCase();
  return /^#[0-9A-F]{6}$/.test(text) ? text : null;
}

function renderColorField({ inputId = '', value = '#7C5CFF', disabled = false, showPresets = true } = {}) {
  const safeValue = normalizeHexColor(value) || '#7C5CFF';
  return `
    <div class="color-field" data-color-field>
      <div class="color-control">
        <span class="color-preview" data-color-preview style="--color-value:${safeValue}"></span>
        <div class="color-summary">
          <span class="color-summary-label">Current color</span>
          <strong class="color-summary-value" data-color-value>${safeValue}</strong>
        </div>
        <label class="color-picker-button">
          <input
            class="color-input"
            ${inputId ? `id="${inputId}"` : ''}
            name="color"
            type="color"
            value="${safeValue}"
            ${disabled ? 'disabled' : ''}
          >
          <span>Browse</span>
        </label>
      </div>
      ${showPresets ? `<div class="color-presets" role="list" aria-label="Suggested colors">
        ${CATEGORY_COLOR_PRESETS.map(color => `
          <button
            class="color-preset ${color === safeValue ? 'is-active' : ''}"
            type="button"
            data-color-preset="${color}"
            aria-label="Use ${color}"
            style="--color-value:${color}"
            ${disabled ? 'disabled' : ''}
          ></button>
        `).join('')}
      </div>` : ''}
    </div>
  `;
}

function bindColorField(root) {
  const field = root?.matches?.('[data-color-field]') ? root : root?.querySelector?.('[data-color-field]');
  if (!field) return;
  const input = field.querySelector('input[name="color"]');
  const preview = field.querySelector('[data-color-preview]');
  const valueLabel = field.querySelector('[data-color-value]');
  const presets = Array.from(field.querySelectorAll('[data-color-preset]'));
  if (!input || !preview || !valueLabel) return;

  const sync = nextValue => {
    const color = normalizeHexColor(nextValue) || '#7C5CFF';
    input.value = color;
    preview.style.setProperty('--color-value', color);
    valueLabel.textContent = color;
    presets.forEach(button => {
      button.classList.toggle('is-active', button.dataset.colorPreset === color);
    });
  };

  input.addEventListener('input', event => sync(event.target.value));
  presets.forEach(button => {
    button.addEventListener('click', () => sync(button.dataset.colorPreset));
  });
  sync(input.value);
}

function setIntroDates() {
  const now = new Date();
  const introDate = document.getElementById('introDate');
  const introTime = document.getElementById('introTime');
  if (introDate) {
    introDate.textContent = now.toLocaleDateString('en-US', {
      weekday: 'long',
      month: 'long',
      day: 'numeric',
    });
  }
  if (introTime) {
    introTime.textContent = now.toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
    });
  }
}

function updateMonthNav(summary = null) {
  const monthKey = summary?.selected_month || currentMonthStateKey();
  const label = document.getElementById('monthNavLabel');
  const nextBtn = document.getElementById('monthNavNext');
  const resetBtn = document.getElementById('monthNavReset');
  const monthNav = document.getElementById('monthNav');
  const isDashboard = activeView === 'dashboard';
  if (monthNav) monthNav.hidden = !isDashboard;
  if (label) label.textContent = summary?.selected_month_label || monthLabel(monthKey);
  if (nextBtn) nextBtn.disabled = monthKey >= currentMonthKey();
  if (resetBtn) resetBtn.hidden = monthKey === currentMonthKey();
}

function updateHeaderForView() {
  const isSettings = activeView === 'settings';
  const isPlanner = activeView === 'planner';
  document.getElementById('navDashboard').classList.toggle('is-active', activeView === 'dashboard');
  document.getElementById('navPlanner').classList.toggle('is-active', isPlanner);
  document.getElementById('navSettings').classList.toggle('is-active', isSettings);
  document.getElementById('pageTitle').innerHTML = isSettings
    ? 'Workspace <em>settings</em>'
    : (isPlanner ? 'Planner' : '<strong class="page-title-dashboard">Dashboard</strong>');
  document.getElementById('pageSubtitle').innerHTML = isSettings
    ? 'Manage categories, colors, and personal organization'
    : (isPlanner
      ? 'Projects, tasks, momentum, and weekly focus'
      : `
      <span class="session-status" id="sessionStatus" data-state="live">
        <span class="session-status__text" id="sessionStatusText">Live</span>
        <span class="session-status__indicator session-status__indicator--live" id="sessionStatusIndicator" aria-hidden="true">
          <span class="session-status__dot"></span>
        </span>
      </span>
      · <span id="introDate"></span> · <span id="introTime"></span>
    `);
  if (activeView === 'dashboard') {
    setIntroDates();
  }
  updateMonthNav(latestDashboardData?.summary || null);
}

function setActiveView(nextView, { pushHash = true } = {}) {
  const prevView = activeView;
  activeView = ['settings', 'planner'].includes(nextView) ? nextView : 'dashboard';
  if (pushHash) {
    const nextHash = activeView === 'settings' ? '#settings' : (activeView === 'planner' ? '#planner' : '#dashboard');
    if (window.location.hash !== nextHash) window.location.hash = nextHash;
  }
  const applyView = () => {
  updateHeaderForView();

  const dashEl = document.getElementById('appDashboard');
  const plannerEl = document.getElementById('appPlanner');
  const setEl  = document.getElementById('appSettings');
  if (dashEl && plannerEl && setEl) {
    const root = document.documentElement;
    const viewChanged = prevView !== activeView;
    if (viewChanged) root.classList.add('view-switching');
    if (activeView === 'settings') {
      // Always ensure the partial is mounted; render() is a no-op if data hasn't loaded yet.
      loadSettingsPartial().then(() => {
        if (latestDashboardData) {
          window.Settings?.render(latestDashboardData);
          setEl.dataset.rendered = 'true';
        }
      });
      setEl.hidden = false;
      dashEl.hidden = true;
      plannerEl.hidden = true;
    } else if (activeView === 'planner') {
      if (latestDashboardData) renderPlanner(latestDashboardData);
      plannerEl.hidden = false;
      dashEl.hidden = true;
      setEl.hidden = true;
    } else {
      dashEl.hidden = false;
      plannerEl.hidden = true;
      setEl.hidden = true;
    }
    if (viewChanged) {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => root.classList.remove('view-switching'));
      });
    }
  }
  };
  if (prevView !== activeView && document.startViewTransition && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    document.startViewTransition(applyView);
  } else {
    applyView();
  }
}

document.getElementById('themeToggle').addEventListener('click', toggleTheme);
systemTheme.addEventListener('change', () => {
  if (!localStorage.getItem(window.THEME_STORAGE_KEY)) updateThemeToggle();
});
applyStoredTheme();
hydrateUiScaleSetting();
document.getElementById('navDashboard').addEventListener('click', () => setActiveView('dashboard'));
document.getElementById('navPlanner').addEventListener('click', () => setActiveView('planner'));
document.getElementById('navSettings').addEventListener('click', () => setActiveView('settings'));
document.getElementById('monthNavPrev').addEventListener('click', () => {
  dashboardMonthState.selectedMonth = monthKeyFromDate(addMonths(dateFromMonthKey(currentMonthStateKey()), -1));
  load();
});
document.getElementById('monthNavNext').addEventListener('click', () => {
  if (currentMonthStateKey() >= currentMonthKey()) return;
  dashboardMonthState.selectedMonth = monthKeyFromDate(addMonths(dateFromMonthKey(currentMonthStateKey()), 1));
  load();
});
document.getElementById('monthNavReset').addEventListener('click', () => {
  dashboardMonthState.selectedMonth = currentMonthKey();
  load();
});
window.addEventListener('hashchange', () => {
  const nextView = window.location.hash === '#settings'
    ? 'settings'
    : (window.location.hash === '#planner' ? 'planner' : 'dashboard');
  if (nextView !== activeView) setActiveView(nextView, { pushHash: false });
});
setActiveView(activeView, { pushHash: false });

const fmt = {
  dur(s) {
    if (!s || s < 60) return s > 0 ? '<1m' : '0m';
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  },
  date(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleDateString('en-US', { month:'short', day:'numeric' });
  },
  datetime(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString('en-US', {
      month:'short', day:'numeric', hour:'numeric', minute:'2-digit'
    });
  },
  time(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString('en-US', {
      hour:'numeric', minute:'2-digit'
    });
  },
  dateOrdinal(ts) {
    if (!ts) return '—';
    const date = new Date(ts * 1000);
    const day = date.getDate();
    const suffix = (day % 100 >= 11 && day % 100 <= 13) ? 'th' : ({1:'st',2:'nd',3:'rd'}[day % 10] || 'th');
    return `${date.toLocaleDateString('en-US', { month:'long' })} ${day}${suffix}`;
  },
  hrs(s) { return Math.round((s || 0) / 360) / 10; }
};

function escapeHtml(s){
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function getCategoryMeta(categoryKey) {
  return categoryKey ? CATEGORY_BY_KEY[categoryKey] || null : null;
}

function projectColor(project, index = 0) {
  return project?.category_color || getCategoryMeta(project?.category_key)?.color || PROJECT_COLORS[index % PROJECT_COLORS.length];
}

function projectBadge(project, index = 0) {
  const untitled = isUntitled(project.project_name);
  return `
    <span class="proj-badge ${untitled ? 'untitled' : ''}" style="--proj-color:${projectColor(project, index)}">
      <span class="tick"></span>
      <span class="name">${escapeHtml(project.project_name)}</span>
    </span>
  `;
}

function categoryPill(project) {
  const category = getCategoryMeta(project.category_key);
  const encodedProject = encodeURIComponent(project.project_name);
  if (!category) {
    return `
      <button
        class="category-pill empty"
        type="button"
        data-category-trigger="true"
        data-project-name="${encodedProject}"
        data-category-key=""
      >
        <span class="category-pill-swatch"></span>
        <span>Set category</span>
      </button>
    `;
  }
  return `
    <button
      class="category-pill"
      type="button"
      style="--category-color:${category.color}"
      data-category-trigger="true"
      data-project-name="${encodedProject}"
      data-category-key="${category.key}"
    >
      <span class="category-pill-swatch"></span>
      <span>${escapeHtml(category.label)}</span>
    </button>
  `;
}

let PROJECT_STATUS_OPTIONS = [
  ['', 'Set status'],
  ['idea', 'Idea'],
  ['needs_work', 'Needs Work'],
  ['in_progress', 'In Progress'],
  ['finishing', 'Finishing'],
  ['finished', 'Finished'],
  ['paused', 'Paused'],
  ['abandoned', 'Abandoned'],
];
let PROJECT_TYPE_OPTIONS = [
  ['', 'Set type'],
  ['personal', 'Personal'],
  ['client', 'Client'],
  ['other', 'Other'],
];
const TASK_PRIORITY_OPTIONS = [
  ['low', 'Low'],
  ['normal', 'Normal'],
  ['high', 'High'],
];
const PROJECT_PRIORITY_OPTIONS = [
  ['', 'None'],
  ['low', 'Low'],
  ['normal', 'Normal'],
  ['high', 'High'],
];
const PLANNER_GOAL_TYPE_OPTIONS = [
  ['sessions_per_week', 'Sessions per week'],
  ['hours_per_week', 'Hours per week'],
  ['projects_finished_per_period', 'Projects finished'],
  ['touch_active_project_every_n_days', 'Touch active projects'],
];
const PLANNER_GOAL_PERIOD_OPTIONS = [
  ['week', 'Week'],
  ['month', 'Month'],
];
const PLANNER_GOAL_SCOPE_OPTIONS = [
  ['all', 'All projects'],
  ['project_type', 'Project type'],
  ['category', 'Category'],
  ['project', 'Project'],
];

function projectMetaSelect(project, field, options) {
  const encodedProject = encodeURIComponent(project.project_name);
  const current = String(project?.[field] || '');
  return `
    <select class="project-meta-select" data-project-meta="${field}" data-project-name="${encodedProject}" aria-label="Project ${field}">
      ${options.map(([value, label]) => `<option value="${value}" ${current === value ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('')}
    </select>
  `;
}

function projectArtistSelect(project) {
  const encodedProject = encodeURIComponent(project.project_name);
  const current = String(project?.artist_id || '');
  const artists = Array.isArray(latestDashboardData?.artists) ? latestDashboardData.artists : [];
  return `
    <select class="project-meta-select" data-project-meta="artist_id" data-project-name="${encodedProject}" aria-label="Project artist">
      <option value="" ${current === '' ? 'selected' : ''}>No artist</option>
      ${artists.map(artist => `<option value="${escapeHtml(artist.id)}" ${current === artist.id ? 'selected' : ''}>${escapeHtml(artist.name)}</option>`).join('')}
    </select>
  `;
}

function projectMetaControls(project, extraHtml = '') {
  return `
    <div class="project-meta-controls" data-project-meta-scope>
      ${projectMetaSelect(project, 'status', PROJECT_STATUS_OPTIONS)}
      ${projectMetaSelect(project, 'type', PROJECT_TYPE_OPTIONS)}
      ${projectArtistSelect(project)}
      ${extraHtml}
      <div class="project-progress-slider-wrap">
        <label>Progress</label>
        <input type="range" class="project-meta-slider" data-project-meta="progress_percent" data-project-name="${encodeURIComponent(project.project_name)}" min="0" max="100" value="${project.progress_percent || 0}">
        <span class="project-progress-val">${project.progress_percent || 0}%</span>
      </div>
    </div>
  `;
}

function projectPriorityLabel(priority) {
  const found = PROJECT_PRIORITY_OPTIONS.find(([value]) => value === (priority || ''));
  return found ? found[1] : 'None';
}

function plannerGoalTypeLabel(type) {
  const found = PLANNER_GOAL_TYPE_OPTIONS.find(([value]) => value === (type || ''));
  return found ? found[1] : 'Goal';
}

function plannerGoalPeriodLabel(period) {
  const found = PLANNER_GOAL_PERIOD_OPTIONS.find(([value]) => value === (period || ''));
  return found ? found[1] : 'Period';
}

function plannerGoalScopeLabel(goal) {
  const scopeType = goal?.scope_type || 'all';
  const scopeValue = goal?.scope_value || '';
  if (scopeType === 'all') return 'All projects';
  if (scopeType === 'project_type') return projectTypeLabel(scopeValue) || scopeValue || 'Project type';
  if (scopeType === 'category') return getCategoryMeta(scopeValue)?.label || scopeValue || 'Category';
  if (scopeType === 'project') return scopeValue || 'Project';
  return scopeValue || scopeType;
}

function plannerGoalNumber(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number)) return '0';
  return Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/\.?0+$/, '');
}

function plannerGoalProgressSummary(goal) {
  const progress = goal?.progress || {};
  const current = plannerGoalNumber(progress.current_value);
  const target = plannerGoalNumber(progress.target_value ?? goal?.target_value);
  const remaining = plannerGoalNumber(progress.remaining_value);
  const unit = progress.unit || 'count';
  return `${current}/${target} ${unit} · ${remaining} remaining`;
}

function projectDeadlineLabel(project) {
  if (project?.deadline_label) return project.deadline_label;
  if (project?.hard_deadline) return `Hard ${project.hard_deadline}`;
  if (project?.due_date) return `Due ${project.due_date}`;
  return '';
}

function deadlinePillClass(state) {
  if (state === 'overdue') return 'is-overdue';
  if (state === 'due_soon') return 'is-due-soon';
  if (state === 'delivered') return 'is-delivered';
  return '';
}

function findProjectByName(projectName) {
  const projects = Array.isArray(latestDashboardData?.projects) ? latestDashboardData.projects : [];
  return projects.find(project => project.project_name === projectName) || null;
}

function projectDeadlineReasons(project) {
  const reasons = Array.isArray(project?.deadline_reasons) ? project.deadline_reasons : [];
  return reasons.map(reason => {
    if (typeof reason === 'string') return { label: reason, className: deadlinePillClass(project?.deadline_state) };
    return {
      label: reason?.label || reason?.reason || String(reason || ''),
      className: reason?.className || reason?.class || deadlinePillClass(project?.deadline_state),
    };
  }).filter(reason => reason.label);
}

function projectTaskSummary(project) {
  const tasks = Array.isArray(project.project_tasks) ? project.project_tasks : [];
  const openCount = tasks.filter(task => task.status !== 'done').length;
  const totalCount = tasks.length;
  if (totalCount === 0) return { openCount, totalCount, label: 'Tasks' };
  if (openCount === 0) return { openCount, totalCount, label: `${totalCount} done` };
  return { openCount, totalCount, label: `${openCount} open` };
}

function projectTaskButton(project) {
  const encodedProject = encodeURIComponent(project.project_name || '');
  const summary = projectTaskSummary(project);
  const emptyClass = summary.totalCount === 0 ? ' is-empty' : '';
  return `
    <button class="project-task-pill${emptyClass}" type="button" data-project-tasks-trigger="true" data-project-name="${encodedProject}">
      <strong>${summary.openCount}</strong>
      <span>${escapeHtml(summary.label)}</span>
    </button>
  `;
}

function localDateKey(d) {
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function monthKeyFromDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

function currentMonthKey() {
  return monthKeyFromDate(new Date());
}

function dateFromMonthKey(key) {
  const [year, month] = String(key).split('-').map(Number);
  return new Date(year, (month || 1) - 1, 1, 12);
}

function addMonths(base, amount) {
  return new Date(base.getFullYear(), base.getMonth() + amount, 1, 12);
}

function monthLabel(monthKey) {
  return dateFromMonthKey(monthKey).toLocaleDateString('en-US', {
    month: 'long',
    year: 'numeric',
  });
}

function currentMonthStateKey() {
  if (!dashboardMonthState.selectedMonth) {
    dashboardMonthState.selectedMonth = currentMonthKey();
  }
  return dashboardMonthState.selectedMonth;
}

function dateFromKey(key) {
  const [year, month, day] = String(key).split('-').map(Number);
  return new Date(year, month - 1, day, 12);
}

function addDays(base, amount) {
  const d = new Date(base);
  d.setDate(d.getDate() + amount);
  return d;
}

function startOfWeek(base) {
  const jsDay = (WEEK_START_BACKEND_DAY + 1) % 7; // convert Python weekday to JS getDay
  return addDays(base, -((base.getDay() - jsDay + 7) % 7));
}

function weekStartDayName() {
  return WEEK_START_DAY_NAMES[WEEK_START_BACKEND_DAY] || 'Friday';
}

function weekEndDayName() {
  return WEEK_START_DAY_NAMES[(WEEK_START_BACKEND_DAY + 6) % 7] || 'Thursday';
}

function shortDate(key) {
  return dateFromKey(key).toLocaleDateString('en-US', { month:'short', day:'numeric' });
}

function shortRange(startKey, endKey) {
  return `${shortDate(startKey)} - ${shortDate(endKey)}`;
}

function humanDailyTargetLabel(dateKey) {
  const today = new Date();
  const todayKey = localDateKey(today);
  const yesterdayKey = localDateKey(addDays(today, -1));
  const tomorrowKey = localDateKey(addDays(today, 1));
  if (dateKey === todayKey) return 'Today';
  if (dateKey === yesterdayKey) return 'Yesterday';
  if (dateKey === tomorrowKey) return 'Tomorrow';
  return dateFromKey(dateKey).toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  });
}

function longDailyTargetLabel(dateKey) {
  return dateFromKey(dateKey).toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'short',
    day: 'numeric',
  });
}

function formatHoursNumber(hours) {
  const rounded = Math.round(hours * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function monthProjectRows(projects) {
  return [...(projects || [])]
    .filter(project => (project.month_seconds || 0) > 0)
    .sort((a, b) => (b.month_seconds || 0) - (a.month_seconds || 0));
}

function currentMonthDays(yearDaily, today = new Date()) {
  const totals = new Map();
  (yearDaily || []).forEach(row => {
    if (row && row.day) totals.set(row.day, row.total_seconds || 0);
  });
  const todayKey = localDateKey(new Date());
  const daysInMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
  return Array.from({ length: daysInMonth }, (_, index) => {
    const date = new Date(today.getFullYear(), today.getMonth(), index + 1);
    const day = localDateKey(date);
    return {
      day,
      total_seconds: totals.get(day) || 0,
      isFuture: day > todayKey,
      isToday: day === todayKey,
    };
  });
}

function selectedMonthDate(summary = {}) {
  return dateFromMonthKey(summary.selected_month || currentMonthStateKey());
}

function selectedMonthLabel(summary = {}) {
  return summary.selected_month_label || monthLabel(summary.selected_month || currentMonthStateKey());
}

function detailRow({ title, meta, value, color, pct }) {
  const width = clamp(Math.round(pct || 0), 0, 100);
  return `
    <div class="detail-row">
      <div class="detail-row-main">
        <div class="detail-row-title">
          <span class="detail-dot" style="--detail-color:${color || 'var(--accent)'}"></span>
          <span class="detail-row-name">${escapeHtml(title)}</span>
        </div>
        <div class="detail-row-meta">${escapeHtml(meta || '')}</div>
        <div class="detail-bar" style="--detail-color:${color || 'var(--accent)'};--detail-pct:${width}%"><span></span></div>
      </div>
      <div class="detail-row-value">${value}</div>
    </div>
  `;
}

function renderTopProjectDetail({ summary, projects }) {
  const rows = monthProjectRows(projects);
  const total = summary.month_seconds || rows.reduce((sum, project) => sum + (project.month_seconds || 0), 0);
  if (rows.length === 0) return `<div class="detail-empty">No project activity in ${escapeHtml(selectedMonthLabel(summary))}.</div>`;
  return `
    <div class="detail-list">
      ${rows.map((project, index) => {
        const seconds = project.month_seconds || 0;
        const pct = total > 0 ? (seconds / total) * 100 : 0;
        return detailRow({
          title: project.project_name || 'Untitled',
          meta: project.category_label || getCategoryMeta(project.category_key)?.label || 'Uncategorized',
          value: `${fmt.dur(seconds)} · ${Math.round(pct)}%`,
          color: projectColor(project, index),
          pct,
        });
      }).join('')}
    </div>
  `;
}

function buildDailyTargetModel(target) {
  const dateKey = target?.date || localDateKey(new Date());
  const progressSeconds = Math.max(0, target?.progress_seconds || 0);
  const hasGoal = Number.isFinite(target?.goal_hours) && target.goal_hours > 0;
  const goalHours = hasGoal ? Math.round(target.goal_hours * 10) / 10 : null;
  const goalSeconds = hasGoal ? Math.round(goalHours * 3600) : 0;
  const progressRatio = hasGoal ? progressSeconds / goalSeconds : 0;
  const tone = targetTone(progressRatio, hasGoal);
  const shortfallSeconds = hasGoal ? Math.max(0, goalSeconds - progressSeconds) : 0;
  return {
    dateKey,
    progressSeconds,
    hasGoal,
    goalHours,
    goalSeconds,
    progressRatio,
    tone,
    isMet: hasGoal && progressSeconds >= goalSeconds,
    progressText: hasGoal ? `${fmt.dur(progressSeconds)} / ${fmt.dur(goalSeconds)}` : `${fmt.dur(progressSeconds)} / No goal`,
    shortfallText: hasGoal ? (shortfallSeconds > 0 ? `${fmt.dur(shortfallSeconds)} short` : goalRemainingText(goalSeconds, progressSeconds)) : 'Set a goal to start tracking target hits',
    remainingLabel: hasGoal ? goalRemainingText(goalSeconds, progressSeconds) : 'Set a goal first',
  };
}

function renderDailyTargetDetail() {
  const rows = (dailyTargetState.historyRows || []).filter(row => row && row.date);
  if (rows.length === 0) return '<div class="detail-empty">No daily target activity yet.</div>';
  return `
    <div class="detail-list">
      ${rows.map(target => {
        const model = buildDailyTargetModel(target);
        const meta = model.hasGoal ? (model.isMet ? 'Goal met' : model.shortfallText) : 'No daily goal saved';
        const value = `${model.progressText} · ${model.isMet ? 'Yes' : 'No'}`;
        return detailRow({
          title: longDailyTargetLabel(model.dateKey),
          meta,
          value,
          color: model.tone.accent,
          pct: clamp(model.progressRatio * 100, 0, 100),
        });
      }).join('')}
    </div>
  `;
}

function dailyTargetHistoryDateKeys(anchorDateKey, count = 14) {
  return Array.from({ length: count }, (_, index) => (
    localDateKey(addDays(dateFromKey(anchorDateKey), -index))
  ));
}

async function loadDailyTargetHistory(anchorDateKey) {
  const token = ++dailyTargetState.historyRequestToken;
  const dateKeys = dailyTargetHistoryDateKeys(anchorDateKey, 14);
  const rows = await Promise.all(dateKeys.map(async dateKey => {
    try {
      const res = await fetch(`/api/daily-target?date=${encodeURIComponent(dateKey)}`);
      const payload = await res.json();
      if (!res.ok || payload.error) throw new Error(payload.error || 'Failed to load daily target history');
      return payload;
    } catch (_) {
      return { date: dateKey, goal_hours: null, progress_seconds: 0 };
    }
  }));
  if (token !== dailyTargetState.historyRequestToken) return;
  dailyTargetState.historyRows = rows;
}

function weeklyTargetRangeLabel(startKey, endKey) {
  return `${shortDate(startKey)} - ${shortDate(endKey)}`;
}

function humanWeeklyTargetLabel(startKey) {
  const currentWeek = currentWeekStartKey();
  const previousWeek = localDateKey(addDays(dateFromKey(currentWeek), -7));
  if (startKey === currentWeek) return 'Current week';
  if (startKey === previousWeek) return 'Last week';
  return `Week of ${shortDate(startKey)}`;
}

function buildWeeklyTargetModel(target) {
  const weekStart = target?.week_start || currentWeekStartKey();
  const weekEnd = target?.week_end || localDateKey(addDays(dateFromKey(weekStart), 6));
  const progressSeconds = Math.max(0, target?.progress_seconds || 0);
  const goalHours = (Number.isFinite(target?.goal_hours) && target.goal_hours > 0)
    ? clamp(Math.round(target.goal_hours * 10) / 10, 1, 100)
    : 0;
  const hasGoal = goalHours > 0;
  const progressRatio = hasGoal ? progressSeconds / (goalHours * 3600) : 0;
  const tone = targetTone(progressRatio, hasGoal);
  const shortfallSeconds = hasGoal ? Math.max(0, Math.round(goalHours * 3600 - progressSeconds)) : 0;
  const percent = hasGoal ? Math.round(progressRatio * 100) : 0;
  const todayKey = localDateKey(new Date());
  const isCurrentWeek = weekStart === currentWeekStartKey();
  const isPastWeek = weekEnd < todayKey;
  const isFutureWeek = weekStart > todayKey;
  const dayIndex = clamp(Math.floor((dateFromKey(todayKey) - dateFromKey(weekStart)) / 86400000), 0, 6);
  const elapsedDays = isFutureWeek ? 0 : (isPastWeek ? 7 : dayIndex + 1);
  const remainingDays = isCurrentWeek ? Math.max(1, 7 - dayIndex) : 0;
  const elapsedFraction = isFutureWeek ? 0 : (isPastWeek ? 1 : elapsedDays / 7);
  const idealSeconds = hasGoal ? Math.round(goalHours * 3600 * elapsedFraction) : 0;
  const paceDeltaSeconds = hasGoal ? Math.round(progressSeconds - idealSeconds) : 0;
  const projectedSeconds = hasGoal
    ? (elapsedDays > 0 ? Math.round((progressSeconds / elapsedDays) * 7) : 0)
    : progressSeconds;
  const requiredPerDaySeconds = hasGoal && remainingDays > 0 ? Math.ceil(shortfallSeconds / remainingDays) : 0;
  const status = weeklyPaceStatus({ hasGoal, progressSeconds, goalHours, paceDeltaSeconds, isPastWeek, isFutureWeek });
  return {
    weekStart,
    weekEnd,
    progressSeconds,
    goalHours,
    hasGoal,
    progressRatio,
    tone,
    percent,
    isMet: hasGoal ? progressSeconds >= goalHours * 3600 : false,
    progressText: hasGoal ? `${fmt.dur(progressSeconds)} of ${formatHoursNumber(goalHours)}h` : fmt.dur(progressSeconds),
    shortfallText: hasGoal ? (shortfallSeconds > 0 ? `${fmt.dur(shortfallSeconds)} short` : goalRemainingText(goalHours * 3600, progressSeconds)) : 'No weekly goal saved',
    remainingLabel: hasGoal ? goalRemainingText(goalHours * 3600, progressSeconds) : 'No weekly goal saved',
    shortfallSeconds,
    todayKey,
    isCurrentWeek,
    isPastWeek,
    isFutureWeek,
    elapsedDays,
    remainingDays,
    idealSeconds,
    paceDeltaSeconds,
    projectedSeconds,
    requiredPerDaySeconds,
    status,
  };
}

function weeklyPaceStatus({ hasGoal, progressSeconds, goalHours, paceDeltaSeconds, isPastWeek, isFutureWeek }) {
  if (!hasGoal) return { label: 'Set a weekly goal', accent: '#8e8e93', className: 'is-empty' };
  const goalSeconds = goalHours * 3600;
  if (progressSeconds >= goalSeconds) return { label: 'Goal met', accent: '#30a455', className: 'is-met' };
  if (isFutureWeek) return { label: 'Starts soon', accent: '#8e8e93', className: 'is-empty' };
  const abs = Math.abs(paceDeltaSeconds);
  if (isPastWeek) return { label: `${fmt.dur(goalSeconds - progressSeconds)} short`, accent: '#a05050', className: 'is-far' };
  if (paceDeltaSeconds >= 0) return { label: `Ahead by ${fmt.dur(abs)}`, accent: '#30a455', className: 'is-met' };
  const amberCutoff = Math.max(30 * 60, goalSeconds * 0.04);
  return {
    label: `Behind by ${fmt.dur(abs)}`,
    accent: abs <= amberCutoff ? '#c89430' : '#a05050',
    className: abs <= amberCutoff ? 'is-mid' : 'is-far',
  };
}

function dailySecondsByKey(rows = []) {
  const map = new Map();
  rows.forEach(row => {
    if (row && row.day) map.set(row.day, Math.max(0, row.total_seconds || 0));
  });
  return map;
}

function renderWeekStrip(model) {
  const byDay = dailySecondsByKey(weeklyTargetState.dailyRows);
  const start = dateFromKey(model.weekStart);
  const todayKey = model.todayKey;
  return `
    <div class="week-strip" aria-label="Weekly activity by day">
      ${Array.from({ length: 7 }, (_, index) => {
        const dateKey = localDateKey(addDays(start, index));
        const seconds = byDay.get(dateKey) || 0;
        const classes = [
          'week-day',
          seconds > 0 ? 'is-logged' : '',
          dateKey === todayKey ? 'is-today' : '',
        ].filter(Boolean).join(' ');
        const dayLabel = dateFromKey(dateKey).toLocaleDateString('en-US', { weekday: 'short' });
        return `
          <div class="${classes}" title="${dayLabel} · ${fmt.dur(seconds)}">
            <i class="week-dot" aria-hidden="true"></i>
            <span>${dayLabel.slice(0, 3)}</span>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function renderWeeklyTargetDetail() {
  const rows = (weeklyTargetState.historyRows || []).filter(row => row && row.week_start && row.week_end);
  if (rows.length === 0) return '<div class="detail-empty">No weekly target activity yet.</div>';
  return `
    <div class="detail-list">
      ${rows.map(target => {
        const model = buildWeeklyTargetModel(target);
        const meta = model.hasGoal
          ? `${Math.min(model.percent, 999)}% complete · ${model.isMet ? 'Target hit' : model.shortfallText}`
          : 'No weekly goal saved';
        const value = model.hasGoal
          ? `${model.progressText} · ${model.isMet ? 'Yes' : 'No'}`
          : `${fmt.dur(model.progressSeconds)} · No goal`;
        return detailRow({
          title: weeklyTargetRangeLabel(model.weekStart, model.weekEnd),
          meta: `${humanWeeklyTargetLabel(model.weekStart)} · ${meta}`,
          value,
          color: model.tone.accent,
          pct: clamp(model.progressRatio * 100, 0, 100),
        });
      }).join('')}
    </div>
  `;
}

function weeklyTargetHistoryWeekKeys(anchorWeekStart, count = 8) {
  return Array.from({ length: count }, (_, index) => (
    localDateKey(addDays(dateFromKey(anchorWeekStart), -index * 7))
  ));
}

async function loadWeeklyTargetHistory(anchorWeekStart) {
  const token = ++weeklyTargetState.historyRequestToken;
  const weekKeys = weeklyTargetHistoryWeekKeys(anchorWeekStart, 8);
  const rows = await Promise.all(weekKeys.map(async weekStart => {
    try {
      const res = await fetch(`/api/weekly-target?date=${encodeURIComponent(weekStart)}`);
      const payload = await res.json();
      if (!res.ok || payload.error) throw new Error(payload.error || 'Failed to load weekly target history');
      return payload;
    } catch (_) {
      return {
        week_start: weekStart,
        week_end: localDateKey(addDays(dateFromKey(weekStart), 6)),
        progress_seconds: 0,
      };
    }
  }));
  if (token !== weeklyTargetState.historyRequestToken) return;
  weeklyTargetState.historyRows = rows;
}

function renderBestDayDetail({ summary, year_daily }) {
  const days = currentMonthDays(year_daily, selectedMonthDate(summary));
  const activeDays = days
    .filter(day => day.total_seconds > 0)
    .sort((a, b) => b.total_seconds - a.total_seconds)
    .slice(0, 10);
  const max = Math.max(...days.map(day => day.total_seconds), 1);
  return `
    <div class="detail-stack">
      <div>
        <div class="detail-section-title">Top days this month</div>
        ${activeDays.length === 0
          ? `<div class="detail-empty">No daily activity in ${escapeHtml(selectedMonthLabel(summary))}.</div>`
          : `<div class="detail-list">
              ${activeDays.map(day => {
                const pct = (day.total_seconds / max) * 100;
                return detailRow({
                  title: longDailyTargetLabel(day.day),
                  meta: day.day,
                  value: fmt.dur(day.total_seconds),
                  color: 'var(--accent)',
                  pct,
                });
              }).join('')}
            </div>`}
      </div>
      <div>
        <div class="detail-section-title">Every day</div>
        <div class="detail-day-chart">
          ${days.map(day => {
            const height = day.total_seconds > 0 ? Math.max(3, (day.total_seconds / max) * 100) : 3;
            const cls = ['detail-day-bar'];
            if (day.isFuture) cls.push('is-future');
            if (day.isToday) cls.push('is-today');
            return `<div class="${cls.join(' ')}" style="--detail-height:${height}%" title="${escapeHtml(`${longDailyTargetLabel(day.day)} · ${fmt.dur(day.total_seconds)}`)}"></div>`;
          }).join('')}
        </div>
      </div>
    </div>
  `;
}

function renderThisMonthDetail({ summary, projects }) {
  const rows = monthProjectRows(projects);
  const total = summary.month_seconds || rows.reduce((sum, project) => sum + (project.month_seconds || 0), 0);
  const activeProjectCount = summary.month_project_count || rows.length;
  const monthDate = selectedMonthDate(summary);
  const dayCount = summary.selected_month_is_current
    ? new Date().getDate()
    : new Date(monthDate.getFullYear(), monthDate.getMonth() + 1, 0).getDate();
  const average = total > 0 ? Math.round(total / dayCount) : 0;
  const buckets = new Map();
  rows.forEach(project => {
    const seconds = project.month_seconds || 0;
    if (seconds <= 0) return;
    const key = project.category_key || '__uncategorized';
    const meta = project.category_key ? getCategoryMeta(project.category_key) : null;
    if (!buckets.has(key)) {
      buckets.set(key, {
        label: meta?.label || project.category_label || 'Uncategorized',
        color: meta?.color || project.category_color || '#8E8E93',
        total_seconds: 0,
      });
    }
    buckets.get(key).total_seconds += seconds;
  });
  const categories = [...buckets.values()].sort((a, b) => b.total_seconds - a.total_seconds);
  return `
    <div class="detail-stack">
      <div class="detail-kpis">
        <div class="detail-kpi"><span>Total time</span><strong>${fmt.dur(total)}</strong></div>
        <div class="detail-kpi"><span>Projects</span><strong>${activeProjectCount}</strong></div>
        <div class="detail-kpi"><span>Daily average</span><strong>${fmt.dur(average)}</strong></div>
      </div>
      <div>
        <div class="detail-section-title">Category breakdown</div>
        ${categories.length === 0
          ? `<div class="detail-empty">No category activity in ${escapeHtml(selectedMonthLabel(summary))}.</div>`
          : `<div class="detail-list">
              ${categories.map(category => {
                const pct = total > 0 ? (category.total_seconds / total) * 100 : 0;
                return detailRow({
                  title: category.label,
                  meta: `${Math.round(pct)}% of month`,
                  value: fmt.dur(category.total_seconds),
                  color: category.color,
                  pct,
                });
              }).join('')}
            </div>`}
      </div>
    </div>
  `;
}

function renderStreakDetail({ summary, year_daily }) {
  const best = longestStreak(year_daily);
  const cur = summary.streak_days || 0;
  const total = Math.max(best, cur, 1);

  const markers = Array.from({ length: total }, (_, i) => {
    const dayNum = i + 1;
    const isActive = dayNum <= cur;
    const isRecord = isActive && cur > best && dayNum === cur;
    return { dayNum, isActive, isRecord };
  });

  let narrative;
  if (best === 0 && cur === 0) {
    narrative = `<strong>Start your first streak.</strong> Open Ableton and log a session to begin.`;
  } else if (cur === 0 && best > 0) {
    narrative = `Your best streak was <strong>${best} day${best !== 1 ? 's' : ''}</strong>. Streak broken — start again today.`;
  } else if (cur > best) {
    narrative = `<strong>New record!</strong> You've surpassed your previous best.`;
  } else if (cur === best && best > 0) {
    narrative = `<strong>Tied with your personal best.</strong>`;
  } else {
    const daysLeft = best - cur;
    narrative = `<strong>${daysLeft} day${daysLeft !== 1 ? 's' : ''}</strong> until you match your personal best.`;
  }

  return `
    <div class="detail-stack">
      <div class="detail-kpis" style="grid-template-columns: repeat(2, minmax(0, 1fr));">
        <div class="detail-kpi"><span>Current streak</span><strong>${cur} day${cur !== 1 ? 's' : ''}</strong></div>
        <div class="detail-kpi"><span>Personal best</span><strong>${best} day${best !== 1 ? 's' : ''}</strong></div>
      </div>
      <div class="streak-vinyl-section">
        <div class="streak-vinyl-track">
          <div class="streak-vinyl-labels">
            ${markers.map(m => `<div class="streak-vinyl-day">${m.dayNum}</div>`).join('')}
          </div>
          <div class="streak-vinyl-markers">
            ${markers.map(m => {
              const cls = m.isRecord ? 'is-record' : m.isActive ? 'is-active' : '';
              return `<div class="streak-vinyl-marker ${cls}"></div>`;
            }).join('')}
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderDetail(kind, data) {
  const monthName = selectedMonthLabel(data.summary || {});
  if (kind === 'top-project') {
    return { title: 'Top Project', subtitle: `All active projects in ${monthName}`, body: renderTopProjectDetail(data) };
  }
  if (kind === 'daily-target') {
    return { title: 'Recent Dates', subtitle: 'Daily target results across recent dates', body: renderDailyTargetDetail() };
  }
  if (kind === 'weekly-target') {
    return { title: 'Recent Weeks', subtitle: 'Weekly target results across recent weeks', body: renderWeeklyTargetDetail() };
  }
  if (kind === 'best-day') {
    return { title: 'Best Day', subtitle: `Top days and the ${monthName} shape`, body: renderBestDayDetail(data) };
  }
  if (kind === 'this-month') {
    return { title: 'Month', subtitle: `${monthName} total, pace, and category mix`, body: renderThisMonthDetail(data) };
  }
  if (kind === 'streak') {
    return { title: 'Streak', subtitle: '', body: renderStreakDetail(data) };
  }
  return null;
}

let detailModalAbort = null;

function bindDetailCards(data) {
  if (detailModalAbort) detailModalAbort.abort();
  detailModalAbort = new AbortController();
  const { signal } = detailModalAbort;
  const detailRoot = document.getElementById('appDashboard');
  const modal = document.getElementById('detailModal');
  const panel = document.getElementById('detailModalPanel');
  const title = document.getElementById('detailModalTitle');
  const subtitle = document.getElementById('detailModalSubtitle');
  const body = document.getElementById('detailModalBody');
  const closeBtn = document.getElementById('detailModalClose');
  if (!detailRoot || !modal || !panel || !title || !subtitle || !body || !closeBtn) return;

  function closeDetail() {
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
  }

  async function openDetail(kind) {
    if (kind === 'daily-target') {
      await loadDailyTargetHistory(dailyTargetState.viewedDate);
    }
    if (kind === 'weekly-target') {
      await loadWeeklyTargetHistory(weeklyTargetState.currentWeekStartDate || currentWeekStartKey());
    }
    const detail = renderDetail(kind, data);
    if (!detail) return;
    title.textContent = detail.title;
    subtitle.textContent = detail.subtitle;
    body.innerHTML = detail.body;
    body.dataset.detailKind = kind;
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
    closeBtn.focus();
  }

  detailRoot.addEventListener('click', event => {
    const card = event.target.closest('[data-detail]');
    if (!card || !detailRoot.contains(card)) return;
    if (card.dataset.detail === 'daily-target' && event.target.closest('.daily-target-nav-btn, #dailyTargetTodayBtn, input, button, label, .goal-input-wrap')) return;
    if (card.dataset.detail === 'weekly-target' && event.target.closest('.daily-target-nav-btn, #weeklyTargetCurrentBtn, input, button, label, .goal-input-wrap')) return;
    openDetail(card.dataset.detail);
  }, { signal });

  detailRoot.addEventListener('keydown', event => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const card = event.target.closest('[data-detail]');
    if (!card || !detailRoot.contains(card)) return;
    if (card.dataset.detail === 'daily-target' && event.target.closest('input, button, label, .goal-input-wrap')) return;
    if (card.dataset.detail === 'weekly-target' && event.target.closest('input, button, label, .goal-input-wrap')) return;
    event.preventDefault();
    openDetail(card.dataset.detail);
  }, { signal });

  modal.addEventListener('click', event => {
    if (event.target === modal) closeDetail();
  }, { signal });
  panel.addEventListener('click', event => event.stopPropagation(), { signal });
  closeBtn.addEventListener('click', closeDetail, { signal });
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape' && modal.classList.contains('is-open')) closeDetail();
  }, { signal });
}

function targetTone(progressRatio, hasGoal = true) {
  if (!hasGoal) {
    return { className: 'is-empty', accent: '#8e8e93', glow: 'rgba(142,142,147,.12)', glowAlpha: 0.12, ratio: 0 };
  }
  const t = clamp(progressRatio, 0, 1);
  // Stay muted longer, then rush to bright green near the end
  const easedT = Math.pow(t, 1.5);
  const rgb = colorStops([
    [170, 70, 70],   // muted red at 0%
    [200, 130, 60],  // muted orange at ~33%
    [210, 190, 70],  // muted yellow at ~66%
    [48, 209, 88]    // bright green at 100%
  ], easedT);
  const accent = rgbString(rgb);
  let className = 'is-far';
  if (t >= 1) className = 'is-met';
  else if (t >= 0.65) className = 'is-near';
  else if (t >= 0.3) className = 'is-mid';
  const glowAlpha = Number((0.12 + Math.pow(t, 1.8) * 0.6).toFixed(2));
  const glow = rgbaString(rgb, glowAlpha);
  return { className, accent, glow, glowAlpha, ratio: t };
}

function goalRemainingText(goalSeconds, completedSeconds) {
  const deltaSeconds = Math.round(goalSeconds - (completedSeconds || 0));
  if (deltaSeconds <= 0) {
    const overSeconds = Math.abs(deltaSeconds);
    return overSeconds > 0
      ? `Goal met · ${fmt.dur(overSeconds)} extra`
      : 'Goal met right on target';
  }
  return `${fmt.dur(deltaSeconds)}`;
}

function formatResetCountdown(secondsUntilReset) {
  const seconds = Math.max(0, Math.round(Number(secondsUntilReset) || 0));
  const dayName = weekStartDayName();
  if (seconds <= 0) return `Cycle resets now · new sprint starts ${dayName}`;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  let chunk;
  if (days > 0) chunk = `${days}d ${hours}h`;
  else if (hours > 0) chunk = `${hours}h ${minutes}m`;
  else chunk = `${minutes}m`;
  return `Resets ${dayName} · ${chunk} left in this sprint`;
}

function progressPercent(progressRatio, hasGoal = true) {
  if (!hasGoal) return 0;
  return clamp(Math.round(progressRatio * 100), 0, 999);
}

let _goalRingId = 0;
function renderGoalRing({ progressRatio = 0, hasGoal = true, label = '', valueMarkup = '' }) {
  const id = ++_goalRingId;
  const progressPct = hasGoal ? Math.max(0, progressRatio * 100) : 0;
  const displayProgressPct = clamp(progressPct, 0, 100);
  const overflowPct = clamp(progressPct - 100, 0, 100);
  const isZero = displayProgressPct === 0;
  const isComplete = hasGoal && displayProgressPct >= 100;
  const hasOverflow = hasGoal && overflowPct > 0;
  const tone = targetTone(progressRatio, hasGoal);
  const percent = progressPercent(progressRatio, hasGoal);
  const gradientId = `goalRingGradient-${id}`;
  const strokeRef = hasGoal ? tone.accent : 'var(--target-track)';
  const ringClasses = [
    'goal-ring',
    isZero ? 'is-zero' : '',
    isComplete ? 'is-complete' : '',
    hasOverflow ? 'has-overflow' : '',
    hasGoal ? '' : 'is-empty',
  ].filter(Boolean).join(' ');
  return `
    <div
      class="${ringClasses}"
      data-progress-ring
      data-progress="${displayProgressPct}"
      data-overflow="${overflowPct}"
      style="--target-progress-pct:0;--target-overflow-pct:0;--target-accent:${tone.accent};--target-glow:${tone.glow};--target-glow-alpha:${tone.glowAlpha}"
      aria-label="${escapeHtml(label || `${percent}% complete`)}"
    >
      <svg class="goal-ring-svg" viewBox="0 0 100 100" aria-hidden="true">
        <defs>
          <linearGradient id="${gradientId}" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stop-color="#aa4646"/>
            <stop offset="50%" stop-color="#c89430"/>
            <stop offset="100%" stop-color="#30d158"/>
          </linearGradient>
        </defs>
        <circle class="goal-ring-track" cx="50" cy="50" r="42" pathLength="100"></circle>
        <circle class="goal-ring-progress" cx="50" cy="50" r="42" pathLength="100" stroke="${strokeRef}"></circle>
        ${hasOverflow ? `<circle class="goal-ring-overflow" cx="50" cy="50" r="42" pathLength="100" stroke="${strokeRef}"></circle>` : ''}
      </svg>
      <div class="goal-ring-core">
        <div class="goal-value">${valueMarkup || `${percent}%`}</div>
      </div>
    </div>
  `;
}

function animateGoalRings(root) {
  requestAnimationFrame(() => {
    root.querySelectorAll('[data-progress-ring]').forEach(ring => {
      const value = clamp(Number(ring.dataset.progress || '0'), 0, 100);
      const overflow = clamp(Number(ring.dataset.overflow || '0'), 0, 100);
      ring.style.setProperty('--target-progress-pct', String(value));
      ring.style.setProperty('--target-overflow-pct', String(overflow));
    });
  });
}

function mixColor(a, b, t) {
  return a.map((value, index) => Math.round(value + ((b[index] - value) * t)));
}

function colorStops(stops, t) {
  if (t <= 0) return stops[0];
  if (t >= 1) return stops[stops.length - 1];
  const scaled = t * (stops.length - 1);
  const index = Math.floor(scaled);
  const localT = scaled - index;
  return mixColor(stops[index], stops[index + 1], localT);
}

function rgbString(parts) {
  return `rgb(${parts[0]}, ${parts[1]}, ${parts[2]})`;
}

function rgbaString(parts, alpha) {
  return `rgba(${parts[0]}, ${parts[1]}, ${parts[2]}, ${alpha})`;
}

function heatmapColorParts(value, maxValue) {
  const t = Math.pow(value / Math.max(maxValue, 1), 1.2);
  return colorStops([
    [50, 145, 75],   // dim forest green
    [90, 185, 85],   // mid green
    [48, 209, 88],   // #30d158 bright ring green
    [110, 255, 125], // peak lime
  ], t);
}

function heatmapColor(value, maxValue) {
  if (value <= 0) return 'rgba(48, 209, 88, 0.08)';
  return rgbString(heatmapColorParts(value, maxValue));
}

function paintHeatmapCanvas(canvas, container, days, activityByDayHour) {
  if (!canvas || !container) return;
  const rect = container.getBoundingClientRect();
  const W = rect.width;
  const H = rect.height;
  if (W <= 0 || H <= 0) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width  = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  canvas.style.width  = W + 'px';
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const cw = W / 7;
  const ch = H / 24;
  // Very tight radius + fast falloff = small bright dots, not haze.
  const radius = Math.max(cw, ch) * 0.48;

  for (let col = 0; col < days.length; col++) {
    const day = days[col];
    if (!day.inRange) continue;
    for (let h = 0; h < 24; h++) {
      const secs = Math.min(activityByDayHour[`${day.key}_${h}`] || 0, 3600);
      if (secs <= 0) continue;
      const row = 23 - h;
      const cx  = col * cw + cw / 2;
      const cy  = row * ch + ch / 2;
      const intensity = Math.pow(secs / 3600, 0.4);
      const [r, g, b] = heatmapColorParts(secs, 3600);
      const g2 = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
      g2.addColorStop(0,    `rgba(${r},${g},${b},${Math.min(1, 1.7 * intensity)})`);
      g2.addColorStop(0.18, `rgba(${r},${g},${b},${0.45 * intensity})`);
      g2.addColorStop(0.38, `rgba(${r},${g},${b},${0.08 * intensity})`);
      g2.addColorStop(1,    `rgba(${r},${g},${b},0)`);
      ctx.fillStyle = g2;
      ctx.fillRect(cx - radius, cy - radius, radius * 2, radius * 2);
    }
  }
}

function heatmapGlowColor(value, maxValue) {
  if (value <= 0) return 'transparent';
  return rgbaString(heatmapColorParts(value, maxValue), 0.58);
}

const RHYTHM_BLOCKS = [
  { key: 'morning', label: 'AM', full: 'Morning', hours: [6, 7, 8, 9, 10, 11] },
  { key: 'afternoon', label: 'PM', full: 'Afternoon', hours: [12, 13, 14, 15, 16] },
  { key: 'evening', label: 'Eve', full: 'Evening', hours: [17, 18, 19, 20, 21] },
  { key: 'night', label: 'Night', full: 'Night', hours: [22, 23, 0, 1, 2, 3, 4, 5] },
];

function buildActivityMaps(yearDaily, yearHourly) {
  const activityByDay = {};
  (yearDaily || []).forEach(d => {
    if (d?.day) activityByDay[d.day] = (d.total_seconds || 0) / 3600;
  });
  const activityByDayHour = {};
  (yearHourly || []).forEach(r => {
    if (r?.day) activityByDayHour[`${r.day}_${r.hour}`] = r.active_seconds || 0;
  });
  return { activityByDay, activityByDayHour };
}

function currentRhythmWeek(activityByDay, activityByDayHour) {
  const today = new Date();
  today.setHours(12, 0, 0, 0);
  const todayKey = localDateKey(today);
  const start = startOfWeek(today);
  const days = Array.from({ length: 7 }, (_, index) => {
    const date = addDays(start, index);
    const key = localDateKey(date);
    const isFuture = date > today;
    const totalHours = isFuture ? 0 : Math.round((activityByDay[key] || 0) * 10) / 10;
    const blocks = RHYTHM_BLOCKS.map(block => {
      const seconds = block.hours.reduce((sum, hour) => sum + (activityByDayHour[`${key}_${hour}`] || 0), 0);
      return { ...block, seconds };
    });
    const peakBlock = blocks.reduce((best, block) => block.seconds > (best?.seconds || 0) ? block : best, null);
    return { key, date, isFuture, isToday: key === todayKey, totalHours, blocks, peakBlock };
  });
  return { startKey: localDateKey(start), endKey: localDateKey(addDays(start, 6)), days };
}

function rhythmGradient(blocks, maxBlockSeconds) {
  if (!blocks.some(block => block.seconds > 0)) return 'var(--bar-bg)';
  const stops = blocks.map((block, index) => {
    const start = index * 25;
    const end = start + 25;
    const alpha = block.seconds > 0
      ? clamp(0.18 + ((block.seconds / Math.max(maxBlockSeconds, 1)) * 0.74), 0.18, 0.92)
      : 0.06;
    return `rgba(48,209,88,${alpha.toFixed(2)}) ${start}%, rgba(48,209,88,${alpha.toFixed(2)}) ${end}%`;
  });
  return `linear-gradient(90deg, ${stops.join(', ')})`;
}

function renderWeeklyRhythm(yearDaily, yearHourly) {
  const mount = document.getElementById('activityHeatmap');
  if (!mount) return;
  const { activityByDay, activityByDayHour } = buildActivityMaps(yearDaily, yearHourly);
  const week = currentRhythmWeek(activityByDay, activityByDayHour);
  const visibleDays = week.days.filter(day => !day.isFuture);
  const totalHours = Math.round(visibleDays.reduce((sum, day) => sum + day.totalHours, 0) * 10) / 10;
  const focusedDays = visibleDays.filter(day => day.totalHours * 3600 >= 15 * 60).length;
  const peakDay = visibleDays.reduce((best, day) => day.totalHours > (best?.totalHours || 0) ? day : best, null);
  const peakLabel = peakDay && peakDay.totalHours > 0
    ? `${peakDay.date.toLocaleDateString('en-US', { weekday: 'short' })} ${peakDay.peakBlock?.label || ''}`.trim()
    : '—';
  const peakBlock = RHYTHM_BLOCKS.map(block => ({
    ...block,
    seconds: visibleDays.reduce((sum, day) => sum + (day.blocks.find(b => b.key === block.key)?.seconds || 0), 0),
  })).reduce((best, block) => block.seconds > (best?.seconds || 0) ? block : best, null);
  const insight = totalHours > 0
    ? `${peakBlock?.full || 'Peak'} · ${shortRange(week.startKey, week.endKey)}`
    : `No activity · ${shortRange(week.startKey, week.endKey)}`;
  const maxBlockSeconds = Math.max(...week.days.flatMap(day => day.blocks.map(block => block.seconds)), 1);
  const weekSummary = `${formatHoursNumber(totalHours)} hours this week. ${focusedDays} focused days. Peak ${peakLabel}.`;

  mount.innerHTML = `
    <div class="rhythm-shell" aria-label="${escapeHtml(weekSummary)}">
      <div class="rhythm-kpis">
        <div class="rhythm-kpi"><span>Week</span><strong>${formatHoursNumber(totalHours)}h</strong></div>
        <div class="rhythm-kpi"><span>Days</span><strong>${focusedDays}</strong></div>
        <div class="rhythm-kpi"><span>Peak</span><strong title="${escapeHtml(peakLabel)}">${escapeHtml(peakLabel)}</strong></div>
      </div>
      <div class="rhythm-week">
        ${week.days.map(day => {
          const totalLabel = day.isFuture ? '—' : `${formatHoursNumber(day.totalHours)}h`;
          const classes = ['rhythm-day', day.isToday ? 'is-today' : '', day.totalHours <= 0 ? 'is-empty' : ''].filter(Boolean).join(' ');
          return `
            <div class="${classes}" title="${escapeHtml(`${day.date.toLocaleDateString('en-US', { weekday:'short', month:'short', day:'numeric' })}: ${day.isFuture ? 'upcoming' : totalLabel}`)}">
              <div class="rhythm-day-head">
                <span class="rhythm-day-name">${day.date.toLocaleDateString('en-US', { weekday:'short' })}</span>
                <span class="rhythm-day-date">${day.date.toLocaleDateString('en-US', { month:'numeric', day:'numeric' })}</span>
              </div>
              <div class="rhythm-day-total">${totalLabel}</div>
              <div class="rhythm-rail" style="--rhythm-gradient:${rhythmGradient(day.blocks, maxBlockSeconds)}" aria-hidden="true"></div>
              <div class="rhythm-block-label">${day.peakBlock?.seconds > 0 ? day.peakBlock.label : '—'}</div>
            </div>
          `;
        }).join('')}
      </div>
      <div class="rhythm-insight">${escapeHtml(insight)}</div>
      <button class="rhythm-detail-toggle" id="activityDetailToggle" type="button" aria-expanded="false" aria-controls="activityHourlyDetail">Hourly detail</button>
      <div class="rhythm-detail" id="activityHourlyDetail"></div>
    </div>
  `;

  const detail = mount.querySelector('#activityHourlyDetail');
  const toggle = mount.querySelector('#activityDetailToggle');
  if (!detail || !toggle) return;
  toggle.addEventListener('click', () => {
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    detail.classList.toggle('is-open', open);
    if (open && !detail.dataset.rendered) {
      renderActivityHeatmap(yearDaily, yearHourly, detail);
      detail.dataset.rendered = 'true';
    }
  });
}

function getStoredGoalHours(storageKey, fallbackHours) {
  const raw = Number(localStorage.getItem(storageKey));
  return Number.isFinite(raw) && raw > 0 ? raw : fallbackHours;
}

function setStoredGoalHours(storageKey, hours) {
  localStorage.setItem(storageKey, String(hours));
}

// ── Toast ──
let toastTimer = null;
function toast(msg, action = null){
  const t = document.getElementById('toast');
  t.replaceChildren(document.createTextNode(msg));
  if (action && action.label && typeof action.onClick === 'function') {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'toast-action';
    button.textContent = action.label;
    button.addEventListener('click', async () => {
      button.disabled = true;
      clearTimeout(toastTimer);
      await action.onClick();
    }, { once: true });
    t.appendChild(button);
  }
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), action ? 7000 : 2400);
}

// ── Confirm ──
function confirmDialog({ title, body, confirmLabel = 'Confirm' }) {
  return new Promise(resolve => {
    const modal = document.getElementById('modal');
    document.getElementById('modalTitle').innerHTML = title;
    document.getElementById('modalBody').innerHTML  = body;
    const confirmBtn = document.getElementById('modalConfirm');
    const cancelBtn  = document.getElementById('modalCancel');
    confirmBtn.innerHTML = `<span class="x">×</span> ${confirmLabel}`;
    modal.classList.add('show');
    const close = (v) => {
      modal.classList.remove('show');
      confirmBtn.removeEventListener('click', onYes);
      cancelBtn.removeEventListener('click', onNo);
      modal.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey);
      resolve(v);
    };
    const onYes = () => close(true);
    const onNo  = () => close(false);
    const onBackdrop = e => { if (e.target === modal) close(false); };
    const onKey = e => { if (e.key === 'Escape') close(false); if (e.key === 'Enter') close(true); };
    confirmBtn.addEventListener('click', onYes);
    cancelBtn.addEventListener('click', onNo);
    modal.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey);
    confirmBtn.focus();
  });
}

async function postAction(path){
  const res = await fetch(path, { method:'POST' });
  if (!res.ok) throw new Error('request failed');
  return res.json();
}

async function postJson(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || 'request failed');
  return data;
}

async function saveProjectCategory(projectName, categoryKey) {
  return postJson('/api/project-category', {
    project_name: projectName,
    category_key: categoryKey,
  });
}

async function getJson(path) {
  const res = await fetch(path);
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || 'request failed');
  return data;
}

function bindCategoryTriggers() {
  document.querySelectorAll('[data-category-trigger]').forEach(button => {
    button.addEventListener('click', () => {
      openCategoryPicker({
        projectName: decodeURIComponent(button.dataset.projectName || ''),
        categoryKey: button.dataset.categoryKey || null,
      });
    });
  });
}

function bindProjectMetaControls() {
  document.querySelectorAll('[data-project-meta]').forEach(input => {
    if (input.dataset.projectMeta === 'progress_percent') {
      input.addEventListener('input', () => {
        const wrap = input.closest('.project-progress-slider-wrap');
        if (wrap) {
          const valEl = wrap.querySelector('.project-progress-val');
          if (valEl) valEl.textContent = `${input.value}%`;
        }
      });
    }

    input.addEventListener('change', async () => {
      const projectName = decodeURIComponent(input.dataset.projectName || '');
      const scope = input.closest('[data-project-meta-scope]') || input.closest('tr');
      const statusSelect = scope?.querySelector('[data-project-meta="status"]');
      const typeSelect = scope?.querySelector('[data-project-meta="type"]');
      const artistSelect = scope?.querySelector('[data-project-meta="artist_id"]');
      const progressInput = scope?.querySelector('[data-project-meta="progress_percent"]');
      if (!projectName || !statusSelect || !typeSelect) return;

      const previous = input.dataset.previousValue || '';
      input.disabled = true;
      try {
        const result = await postJson('/api/project-metadata', {
          project_name: projectName,
          status: statusSelect.value,
          type: typeSelect.value,
          artist_id: artistSelect ? artistSelect.value : '',
          progress_percent: progressInput ? parseInt(progressInput.value, 10) : 0,
        });
        statusSelect.dataset.previousValue = result.metadata?.status || '';
        typeSelect.dataset.previousValue = result.metadata?.type || '';
        if (artistSelect) artistSelect.dataset.previousValue = result.metadata?.artist_id || '';
        if (progressInput) progressInput.dataset.previousValue = result.metadata?.progress_percent || '0';
        toast(`Updated ${projectName}`);
        load();
      } catch (error) {
        input.value = previous;
        if (input.dataset.projectMeta === 'progress_percent') {
          const wrap = input.closest('.project-progress-slider-wrap');
          if (wrap) {
            const valEl = wrap.querySelector('.project-progress-val');
            if (valEl) valEl.textContent = `${previous}%`;
          }
        }
        toast(error.message || 'Failed to save project metadata');
      } finally {
        input.disabled = false;
      }
    });
    input.dataset.previousValue = input.value || '';
  });
}

function bindProjectTaskTriggers() {
  document.querySelectorAll('[data-project-tasks-trigger]').forEach(button => {
    button.addEventListener('click', () => {
      openProjectTasks(decodeURIComponent(button.dataset.projectName || ''));
    });
  });
}

function plannerGoalScopeValueOptions(scopeType, selectedValue = '') {
  if (scopeType === 'project_type') {
    return PROJECT_TYPE_OPTIONS
      .filter(([value]) => value)
      .map(([value, label]) => `<option value="${value}" ${selectedValue === value ? 'selected' : ''}>${escapeHtml(label)}</option>`)
      .join('');
  }
  if (scopeType === 'category') {
    return CATEGORY_OPTIONS
      .map(option => `<option value="${option.key}" ${selectedValue === option.key ? 'selected' : ''}>${escapeHtml(option.label)}</option>`)
      .join('');
  }
  if (scopeType === 'project') {
    const projects = Array.isArray(latestDashboardData?.projects) ? latestDashboardData.projects : [];
    return projects
      .map(project => `<option value="${escapeHtml(project.project_name)}" ${selectedValue === project.project_name ? 'selected' : ''}>${escapeHtml(project.project_name)}</option>`)
      .join('');
  }
  return '';
}

function renderPlannerGoalScopeValueControl(scopeType, selectedValue = '') {
  if (scopeType === 'all') {
    return '<input class="planner-goal-input" data-goal-scope-value type="text" value="" placeholder="All projects" disabled>';
  }
  const options = plannerGoalScopeValueOptions(scopeType, selectedValue);
  if (options) {
    return `<select class="planner-goal-input" data-goal-scope-value>${options}</select>`;
  }
  return `<input class="planner-goal-input" data-goal-scope-value type="text" value="${escapeHtml(selectedValue)}" placeholder="Scope value">`;
}

function syncPlannerGoalScopeValue(form) {
  const scopeSelect = form?.querySelector('[data-goal-scope-type]');
  const valueWrap = form?.querySelector('[data-goal-scope-value-wrap]');
  if (!scopeSelect || !valueWrap) return;
  valueWrap.innerHTML = renderPlannerGoalScopeValueControl(scopeSelect.value || 'all', valueWrap.dataset.selectedValue || '');
}

function plannerGoalPayloadFromForm(form) {
  const scopeType = form.querySelector('[data-goal-scope-type]')?.value || 'all';
  return {
    goal_type: form.querySelector('[data-goal-type]')?.value || 'sessions_per_week',
    target_value: form.querySelector('[data-goal-target]')?.value || 1,
    period: form.querySelector('[data-goal-period]')?.value || 'week',
    scope_type: scopeType,
    scope_value: scopeType === 'all' ? '' : (form.querySelector('[data-goal-scope-value]')?.value || ''),
    active: form.querySelector('[data-goal-active]')?.checked !== false,
  };
}

function bindPlannerGoals() {
  document.querySelectorAll('[data-goal-scope-type]').forEach(select => {
    select.addEventListener('change', () => {
      const form = select.closest('[data-planner-goal-form]');
      const valueWrap = form?.querySelector('[data-goal-scope-value-wrap]');
      if (valueWrap) valueWrap.dataset.selectedValue = '';
      syncPlannerGoalScopeValue(form);
    });
  });

  const createForm = document.getElementById('plannerGoalCreateForm');
  if (createForm) {
    syncPlannerGoalScopeValue(createForm);
    createForm.addEventListener('submit', async event => {
      event.preventDefault();
      const submit = createForm.querySelector('[type="submit"]');
      if (submit) submit.disabled = true;
      try {
        await postJson('/api/planner-goals', plannerGoalPayloadFromForm(createForm));
        createForm.reset();
        syncPlannerGoalScopeValue(createForm);
        toast('Goal added');
        await load();
      } catch (error) {
        toast(error.message || 'Failed to add goal');
      } finally {
        if (submit) submit.disabled = false;
      }
    });
  }

  document.querySelectorAll('[data-goal-toggle]').forEach(button => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await postJson('/api/planner-goals/update', {
          id: button.dataset.goalToggle,
          active: button.dataset.goalActive !== 'true',
        });
        toast(button.dataset.goalActive === 'true' ? 'Goal paused' : 'Goal reactivated');
        await load();
      } catch (error) {
        toast(error.message || 'Failed to update goal');
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll('[data-goal-delete]').forEach(button => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await postJson('/api/planner-goals/delete', { id: button.dataset.goalDelete });
        toast('Goal deleted');
        await load();
      } catch (error) {
        toast(error.message || 'Failed to delete goal');
      } finally {
        button.disabled = false;
      }
    });
  });
}

let projectTaskModalState = null;

function priorityLabel(priority) {
  const found = TASK_PRIORITY_OPTIONS.find(([value]) => value === priority);
  return found ? found[1] : 'Normal';
}

function TASK_PRIORITY_RANK(priority) {
  if (priority === 'high') return 0;
  if (priority === 'normal') return 1;
  return 2;
}

function sortProjectTasks(tasks) {
  return tasks.slice().sort((a, b) => {
    const aDone = a.status === 'done' ? 1 : 0;
    const bDone = b.status === 'done' ? 1 : 0;
    if (aDone !== bDone) return aDone - bDone;
    const aPri = TASK_PRIORITY_RANK(a.priority);
    const bPri = TASK_PRIORITY_RANK(b.priority);
    if (aPri !== bPri) return aPri - bPri;
    return (a.sort_order || 0) - (b.sort_order || 0) || (a.created_at || 0) - (b.created_at || 0) || (a.id || 0) - (b.id || 0);
  });
}

function taskPrioritySelect(task) {
  const current = task.priority || 'normal';
  return `
    <select class="task-priority-select" data-task-priority="${task.id}" aria-label="Priority for ${escapeHtml(task.title)}">
      ${TASK_PRIORITY_OPTIONS.map(([value, label]) => `<option value="${value}" ${current === value ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('')}
    </select>
  `;
}

function renderProjectTaskRows(tasks) {
  if (!Array.isArray(tasks) || tasks.length === 0) {
    return '<div class="task-empty">No project tasks yet.</div>';
  }
  return tasks.map(task => {
    const done = task.status === 'done';
    const priorityClass = ['low', 'normal', 'high'].includes(task.priority) ? `is-priority-${task.priority}` : 'is-priority-normal';
    return `
      <div class="task-row ${priorityClass} ${done ? 'is-done' : ''}" data-task-row="${task.id}">
        <input class="task-check" type="checkbox" data-task-toggle="${task.id}" ${done ? 'checked' : ''} aria-label="${done ? 'Reopen' : 'Complete'} ${escapeHtml(task.title)}">
        <div class="task-title" title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</div>
        ${taskPrioritySelect(task)}
        <button class="task-delete" type="button" data-task-delete="${task.id}" aria-label="Delete ${escapeHtml(task.title)}">×</button>
      </div>
    `;
  }).join('');
}

async function refreshProjectTaskModal(projectName) {
  const list = document.getElementById('taskModalList');
  const toggle = document.getElementById('taskToggleCompleted');
  if (!list) return;
  refreshProjectPlanningForm(projectName);
  list.innerHTML = '<div class="task-empty">Loading tasks…</div>';
  try {
    const result = await getJson(`/api/project-tasks?project=${encodeURIComponent(projectName)}`);
    const allTasks = sortProjectTasks(result.tasks || []);
    if (projectTaskModalState) {
      projectTaskModalState.tasks = allTasks;
    }
    const showCompleted = projectTaskModalState?.showCompleted === true;
    const visible = showCompleted ? allTasks : allTasks.filter(t => t.status !== 'done');
    if (visible.length === 0 && allTasks.length > 0) {
      list.innerHTML = '<div class="task-empty">Completed tasks hidden.</div>';
    } else {
      list.innerHTML = renderProjectTaskRows(visible);
    }
    bindProjectTaskModalRows(projectName);
    if (toggle) {
      const doneCount = allTasks.filter(t => t.status === 'done').length;
      if (showCompleted) {
        toggle.textContent = 'Hide completed';
        toggle.setAttribute('aria-pressed', 'true');
      } else {
        toggle.textContent = doneCount ? `Show completed (${doneCount})` : 'Show completed (0)';
        toggle.setAttribute('aria-pressed', 'false');
      }
    }
  } catch (error) {
    list.innerHTML = `<div class="task-empty">${escapeHtml(error.message || 'Could not load tasks')}</div>`;
  }
}

function refreshProjectPlanningForm(projectName) {
  const project = findProjectByName(projectName) || { project_name: projectName };
  const categoryInput = document.getElementById('projectCategoryInput');
  const statusInput = document.getElementById('projectStatusInput');
  const dueInput = document.getElementById('projectDueDateInput');
  const pinnedInput = document.getElementById('projectPinnedInput');
  const summary = document.getElementById('projectPlanningSummary');
  if (!categoryInput || !statusInput || !dueInput || !pinnedInput) return;

  categoryInput.innerHTML = [
    `<option value="" ${(project.category_key || '') === '' ? 'selected' : ''}>No category</option>`,
    ...CATEGORY_OPTIONS.map(option => `<option value="${option.key}" ${(project.category_key || '') === option.key ? 'selected' : ''}>${escapeHtml(option.label)}</option>`),
  ].join('');
  statusInput.innerHTML = PROJECT_STATUS_OPTIONS
    .map(([value, label]) => `<option value="${value}" ${(project.status || '') === value ? 'selected' : ''}>${escapeHtml(label)}</option>`)
    .join('');
  dueInput.value = project.due_date || '';
  pinnedInput.checked = !!project.pinned;
  if (summary) {
    const bits = [
      projectUrgencyLabel(project),
      project.pinned ? 'Pinned' : '',
    ].filter(Boolean);
    summary.textContent = bits.length ? bits.join(' · ') : 'Set status and due date';
  }
}

async function saveProjectPlanning(projectName) {
  const project = findProjectByName(projectName) || {};
  const categoryInput = document.getElementById('projectCategoryInput');
  const statusInput = document.getElementById('projectStatusInput');
  const dueInput = document.getElementById('projectDueDateInput');
  const pinnedInput = document.getElementById('projectPinnedInput');
  const nextStatus = statusInput?.value || '';
  if (isTerminalProjectStatus(nextStatus) && nextStatus !== (project.status || '')) {
    const ok = await confirmTerminalProjectMove(projectName, nextStatus);
    if (!ok) {
      if (statusInput) statusInput.value = project.status || '';
      return null;
    }
  }
  const payload = {
    project_name: projectName,
    status: nextStatus,
    type: project.type || '',
    priority: project.priority || '',
    due_date: dueInput?.value || '',
    pinned: !!pinnedInput?.checked,
  };
  const result = await postJson('/api/project-metadata', payload);
  const nextCategory = categoryInput?.value || null;
  if ((nextCategory || '') !== (project.category_key || '')) {
    await saveProjectCategory(projectName, nextCategory);
  }
  return result.metadata || null;
}

function bindProjectTaskModalRows(projectName) {
  document.querySelectorAll('[data-task-toggle]').forEach(input => {
    input.addEventListener('change', async () => {
      input.disabled = true;
      try {
        await postJson('/api/project-tasks/update', {
          id: input.dataset.taskToggle,
          status: input.checked ? 'done' : 'open',
        });
        await refreshProjectTaskModal(projectName);
        await load();
      } catch (error) {
        input.checked = !input.checked;
        toast(error.message || 'Failed to update task');
      } finally {
        input.disabled = false;
      }
    });
  });

  document.querySelectorAll('[data-task-priority]').forEach(select => {
    select.addEventListener('change', async () => {
      const previous = select.dataset.previousValue || 'normal';
      select.disabled = true;
      try {
        await postJson('/api/project-tasks/update', {
          id: select.dataset.taskPriority,
          priority: select.value,
        });
        select.dataset.previousValue = select.value;
        await refreshProjectTaskModal(projectName);
        await load();
      } catch (error) {
        select.value = previous;
        toast(error.message || 'Failed to update priority');
      } finally {
        select.disabled = false;
      }
    });
    select.dataset.previousValue = select.value || 'normal';
  });

  document.querySelectorAll('[data-task-delete]').forEach(button => {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await postJson('/api/project-tasks/delete', { id: button.dataset.taskDelete });
        await refreshProjectTaskModal(projectName);
        await load();
      } catch (error) {
        toast(error.message || 'Failed to delete task');
      } finally {
        button.disabled = false;
      }
    });
  });
}

function openProjectTasks(projectName) {
  const modal = document.getElementById('taskModal');
  const projectLabel = document.getElementById('taskModalProject');
  const closeBtn = document.getElementById('taskModalClose');
  const closeFooterBtn = document.getElementById('taskModalCloseBtn');
  const saveBtn = document.getElementById('taskModalDone');
  const form = document.getElementById('taskForm');
  const planningForm = document.getElementById('projectPlanningForm');
  const titleInput = document.getElementById('taskTitleInput');
  const priorityInput = document.getElementById('taskPriorityInput');
  const addBtn = document.getElementById('taskAddButton');
  const toggle = document.getElementById('taskToggleCompleted');
  const notesTextarea = document.getElementById('projectNotesTextarea');
  if (!modal || !projectName) return;

  let isDirty = false;
  saveBtn.disabled = true;
  saveBtn.textContent = 'Save';

  const markDirty = () => {
    isDirty = true;
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save';
  };

  if (projectTaskModalState?.close) projectTaskModalState.close();
  projectLabel.textContent = projectName;
  titleInput.value = '';
  priorityInput.value = 'normal';

  const toggleCompleted = () => {
    const show = !(projectTaskModalState?.showCompleted ?? false);
    if (projectTaskModalState) projectTaskModalState.showCompleted = show;
    refreshProjectTaskModal(projectName);
  };

  let _projectNotesCleanup = null;

  const close = () => {
    modal.classList.remove('show');
    closeBtn.removeEventListener('click', close);
    if (closeFooterBtn) closeFooterBtn.removeEventListener('click', close);
    saveBtn.removeEventListener('click', onSave);
    modal.removeEventListener('click', onBackdrop);
    document.removeEventListener('keydown', onKey);
    form.removeEventListener('submit', onSubmit);
    planningForm?.removeEventListener('submit', onPlanningSubmit);
    planningForm?.removeEventListener('input', markDirty);
    planningForm?.removeEventListener('change', markDirty);
    notesTextarea?.removeEventListener('input', markDirty);
    toggle?.removeEventListener('click', toggleCompleted);
    if (_projectNotesCleanup) {
      _projectNotesCleanup();
      _projectNotesCleanup = null;
    }
    projectTaskModalState = null;
  };
  const onBackdrop = event => { if (event.target === modal) close(); };
  const onKey = event => { if (event.key === 'Escape') close(); };
  const onSubmit = async event => {
    event.preventDefault();
    const title = titleInput.value.trim();
    if (!title) {
      titleInput.focus();
      return;
    }
    addBtn.disabled = true;
    try {
      await postJson('/api/project-tasks', {
        project_name: projectName,
        title,
        priority: priorityInput.value || 'normal',
      });
      titleInput.value = '';
      priorityInput.value = 'normal';
      await refreshProjectTaskModal(projectName);
      await load();
      titleInput.focus();
    } catch (error) {
      toast(error.message || 'Failed to add task');
    } finally {
      addBtn.disabled = false;
    }
  };
  const onPlanningSubmit = async event => {
    event.preventDefault();
    if (isDirty) {
      await onSave();
    }
  };

  const _notes = setupProjectInlineNotes(projectName);
  _projectNotesCleanup = _notes ? _notes.cleanup : null;
  const _saveNotes = _notes ? _notes.save : null;

  const onSave = async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving\u2026';
    try {
      await saveProjectPlanning(projectName);
      if (_saveNotes) await _saveNotes();
      toast(`Updated ${projectName}`);
      await load();
      refreshProjectPlanningForm(projectName);
      isDirty = false;
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saved';
    } catch(error) {
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
      toast(error.message || 'Failed to save');
    }
  };

  projectTaskModalState = { close, showCompleted: false };
  modal.classList.add('show');
  closeBtn.addEventListener('click', close);
  if (closeFooterBtn) closeFooterBtn.addEventListener('click', close);
  saveBtn.addEventListener('click', onSave);
  modal.addEventListener('click', onBackdrop);
  document.addEventListener('keydown', onKey);
  form.addEventListener('submit', onSubmit);
  planningForm?.addEventListener('submit', onPlanningSubmit);
  planningForm?.addEventListener('input', markDirty);
  planningForm?.addEventListener('change', markDirty);
  notesTextarea?.addEventListener('input', markDirty);
  toggle?.addEventListener('click', toggleCompleted);
  refreshProjectTaskModal(projectName);
  titleInput.focus();
}

function setupProjectInlineNotes(projectName) {
  const MAX_PROJECT_NOTE_LEN = 3000;
  const toggle = document.getElementById('projectNotesToggle');
  const globalBtn = document.getElementById('projectGlobalBtn');
  const sessionBtn = document.getElementById('projectSessionBtn');
  const textarea = document.getElementById('projectNotesTextarea');
  const sessionInfo = document.getElementById('projectNotesSessionInfo');
  const nav = document.getElementById('projectNotesNav');
  const prevBtn = document.getElementById('projectNotesPrevBtn');
  const nextBtn = document.getElementById('projectNotesNextBtn');
  const charCount = document.getElementById('projectNotesCharCount');

  if (!toggle || !textarea) return;

  const project = findProjectByName(projectName) || { project_name: projectName };
  const abletonIsRunning = latestDashboardData?.summary?.ableton_running === true;

  let currentMode = 'global';
  let currentProjectNote = (project.project_note || '').trim();
  let editingSessionId = null;
  let previousSessionId = null;
  let nextSessionId = null;
  let hasSessionNav = false;
  let notes = {};
  let startTimes = {};
  let endTimes = {};
  let lastSeenTimes = {};
  let activeSeconds = {};

  const sessions = latestDashboardData?.recent || [];
  const projSessions = sessions.filter(s => s.project_name === projectName);
  const sessionIds = projSessions.length > 0 ? (projSessions[0].session_ids || []) : [];

  if (sessionIds.length > 0) {
    editingSessionId = sessionIds[0];
    sessionIds.forEach(sid => {
      const k = String(sid);
      notes[k] = (projSessions[0].session_notes || {})[k] || '';
      startTimes[k] = (projSessions[0].session_start_times || {})[k] || 0;
      endTimes[k] = (projSessions[0].session_end_times || {})[k] ?? null;
      lastSeenTimes[k] = (projSessions[0].session_last_seen_times || {})[k] || 0;
      activeSeconds[k] = (projSessions[0].session_active_seconds || {})[k] || 0;
    });
  }

  let currentSessionNote = editingSessionId ? (notes[String(editingSessionId)] || '') : '';

  function endTimePlain(sidStr) {
    const et = endTimes[sidStr];
    const lst = lastSeenTimes[sidStr];
    if (et != null) return fmt.time(et);
    if (abletonIsRunning) return 'Active';
    if (lst) return fmt.time(lst);
    return '—';
  }

  function sessionSummary(sidStr) {
    const startTs = startTimes[sidStr] || 0;
    const seconds = activeSeconds?.[sidStr] || 0;
    const dateStr = fmt.dateOrdinal(startTs);
    const range = fmt.time(startTs) + ' to ' + endTimePlain(sidStr);
    return { dateStr: dateStr, range: range, duration: seconds > 0 ? fmt.dur(seconds) : '' };
  }

  function updateNavButtons() {
    if (prevBtn) prevBtn.disabled = !previousSessionId;
    if (nextBtn) nextBtn.disabled = !nextSessionId;
  }

  async function loadNavState() {
    if (sessionIds.length !== 1) {
      hasSessionNav = false;
      if (nav) nav.hidden = true;
      updateNavButtons();
      return;
    }
    try {
      const data = await getJson('/api/session-notes-entry?session_id=' + encodeURIComponent(sessionIds[0]) + '&project=' + encodeURIComponent(projectName || ''));
      previousSessionId = data.previous_session_id || null;
      nextSessionId = data.next_session_id || null;
      hasSessionNav = !!(previousSessionId || nextSessionId);
      updateNavButtons();
    } catch(e) {
      hasSessionNav = false;
      updateNavButtons();
    }
  }

  async function openEntry(sessionId) {
    if (!sessionId) return;
    if (prevBtn) prevBtn.disabled = true;
    if (nextBtn) nextBtn.disabled = true;
    try {
      const data = await getJson('/api/session-notes-entry?session_id=' + encodeURIComponent(sessionId) + '&project=' + encodeURIComponent(projectName || ''));
      const entry = data.session;
      editingSessionId = entry.id;
      notes[String(entry.id)] = entry.notes || '';
      startTimes[String(entry.id)] = entry.start_time || 0;
      endTimes[String(entry.id)] = entry.end_time ?? null;
      lastSeenTimes[String(entry.id)] = entry.last_seen_time || 0;
      activeSeconds[String(entry.id)] = entry.active_seconds || 0;
      currentSessionNote = entry.notes || '';
      previousSessionId = data.previous_session_id || null;
      nextSessionId = data.next_session_id || null;
      hasSessionNav = !!(previousSessionId || nextSessionId);
      updateTextarea('session');
      updateNavButtons();
      if (currentMode === 'session' && nav) {
        nav.hidden = !hasSessionNav;
      }
    } catch(e) {
      toast('Failed to load session notes');
      updateNavButtons();
    }
  }

  function setModeButtons() {
    if (!globalBtn || !sessionBtn) return;
    if (currentMode === 'global') {
      globalBtn.classList.add('is-active');
      sessionBtn.classList.remove('is-active');
      globalBtn.style.background = 'var(--surface)';
      globalBtn.style.color = 'var(--ink)';
      sessionBtn.style.background = 'transparent';
      sessionBtn.style.color = 'var(--ink-3)';
    } else {
      sessionBtn.classList.add('is-active');
      globalBtn.classList.remove('is-active');
      sessionBtn.style.background = 'var(--surface)';
      sessionBtn.style.color = 'var(--ink)';
      globalBtn.style.background = 'transparent';
      globalBtn.style.color = 'var(--ink-3)';
    }
  }

  function updateTextarea(mode) {
    if (!textarea) return;
    const val = textarea.value;
    if (currentMode === 'global') {
      currentProjectNote = val;
    } else if (currentMode === 'session' && editingSessionId) {
      notes[String(editingSessionId)] = val;
      currentSessionNote = val;
    }

    if (mode === 'global') {
      textarea.value = currentProjectNote;
      textarea.placeholder = '';
      textarea.maxLength = MAX_PROJECT_NOTE_LEN;
      if (nav) nav.hidden = true;
      if (sessionInfo) sessionInfo.style.display = 'none';
      if (charCount) charCount.textContent = currentProjectNote.length + ' / ' + MAX_PROJECT_NOTE_LEN;
    } else {
      textarea.value = currentSessionNote;
      textarea.placeholder = '';
      textarea.maxLength = MAX_NOTES_LEN;

      if (editingSessionId && sessionIds.length === 1) {
        const summary = sessionSummary(String(editingSessionId));
        if (sessionInfo) {
          sessionInfo.style.display = 'block';
          sessionInfo.textContent = summary.dateStr + ' \u00b7 ' + summary.range;
        }
        if (nav) nav.hidden = !hasSessionNav;
      } else {
        if (nav) nav.hidden = true;
        if (sessionInfo) sessionInfo.style.display = 'none';
      }

      if (charCount) charCount.textContent = currentSessionNote.length + ' / ' + MAX_NOTES_LEN;
    }
    if (charCount) charCount.classList.toggle('over', false);
  }

  function switchMode(mode) {
    if (currentMode === mode) return;
    updateTextarea(mode);
    currentMode = mode;
    setModeButtons();
  }

  // Initialize UI state
  textarea.value = currentProjectNote;
  textarea.maxLength = MAX_PROJECT_NOTE_LEN;
  textarea.placeholder = '';
  if (charCount) charCount.textContent = currentProjectNote.length + ' / ' + MAX_PROJECT_NOTE_LEN;
  if (nav) nav.hidden = true;
  if (sessionInfo) sessionInfo.style.display = 'none';
  setModeButtons();

  function onTextareaInput() {
    const val = textarea.value;
    if (currentMode === 'global') {
      currentProjectNote = val;
      if (charCount) charCount.textContent = val.length + ' / ' + MAX_PROJECT_NOTE_LEN;
      if (charCount) charCount.classList.toggle('over', val.length > MAX_PROJECT_NOTE_LEN);
    } else {
      if (editingSessionId) notes[String(editingSessionId)] = val;
      currentSessionNote = val;
      if (charCount) charCount.textContent = val.length + ' / ' + MAX_NOTES_LEN;
      if (charCount) charCount.classList.toggle('over', val.length > MAX_NOTES_LEN);
    }
  }

  async function onSave() {
    if (!textarea) return;
    const val = textarea.value.trim();
    if (currentMode === 'global') {
      currentProjectNote = val;
      const proj = findProjectByName(projectName) || { project_name: projectName };
      await postJson('/api/project-metadata', {
        project_name: projectName,
        status: proj.status || '',
        type: proj.type || '',
        priority: proj.priority || '',
        due_date: proj.due_date || '',
        pinned: !!proj.pinned,
        project_note: val,
      });
    } else {
      if (editingSessionId) notes[String(editingSessionId)] = val;
      currentSessionNote = val;
      await postJson('/api/session-notes', { session_id: editingSessionId, notes: val });
    }
    toast('Notes saved');
    load();
  }

  function onPrev() { openEntry(previousSessionId); }
  function onNext() { openEntry(nextSessionId); }
  function onGlobalClick() { switchMode('global'); }
  function onSessionClick() { switchMode('session'); }

  if (globalBtn) globalBtn.addEventListener('click', onGlobalClick);
  if (sessionBtn) sessionBtn.addEventListener('click', onSessionClick);
  if (textarea) textarea.addEventListener('input', onTextareaInput);
  if (prevBtn) prevBtn.addEventListener('click', onPrev);
  if (nextBtn) nextBtn.addEventListener('click', onNext);

  loadNavState().then(function() {
    if (currentMode === 'session') {
      if (editingSessionId && sessionIds.length === 1) {
        if (nav) nav.hidden = !hasSessionNav;
        if (sessionInfo && hasSessionNav) sessionInfo.style.display = 'block';
        if (!hasSessionNav && sessionInfo) sessionInfo.style.display = 'none';
      }
    }
    updateNavButtons();
  });

  return {
    cleanup: function() {
      if (globalBtn) globalBtn.removeEventListener('click', onGlobalClick);
      if (sessionBtn) sessionBtn.removeEventListener('click', onSessionClick);
      if (textarea) textarea.removeEventListener('input', onTextareaInput);
      if (prevBtn) prevBtn.removeEventListener('click', onPrev);
      if (nextBtn) nextBtn.removeEventListener('click', onNext);
    },
    save: onSave,
  };
}

function openCategoryPicker({ projectName, categoryKey }) {
  const modal = document.getElementById('categoryModal');
  const title = document.getElementById('categoryModalProject');
  const list = document.getElementById('categoryModalList');
  const closeBtn = document.getElementById('categoryModalClose');
  const clearBtn = document.getElementById('categoryModalClear');

  title.textContent = projectName;
  list.innerHTML = CATEGORY_OPTIONS.map(option => `
    <button
      class="category-option ${option.key === categoryKey ? 'is-active' : ''}"
      type="button"
      style="--category-color:${option.color}"
      data-category-value="${option.key}"
    >
      <span class="category-option-swatch"></span>
      <span>
        <span class="category-option-label">${escapeHtml(option.label)}</span>
      </span>
      <span class="category-option-check">Selected</span>
    </button>
  `).join('');

  const close = () => {
    modal.classList.remove('show');
    closeBtn.removeEventListener('click', onClose);
    clearBtn.removeEventListener('click', onClear);
    modal.removeEventListener('click', onBackdrop);
    document.removeEventListener('keydown', onKey);
    list.querySelectorAll('[data-category-value]').forEach(button => {
      button.removeEventListener('click', onSelect);
    });
  };

  const save = async (nextKey) => {
    try {
      const result = await saveProjectCategory(projectName, nextKey);
      close();
      const message = result.category
        ? `${projectName} → ${result.category.label}`
        : `Cleared category for ${projectName}`;
      toast(message);
      load();
    } catch (error) {
      toast(error.message || 'Failed to save category');
    }
  };

  const onClose = () => close();
  const onClear = () => save(null);
  const onBackdrop = event => { if (event.target === modal) close(); };
  const onKey = event => { if (event.key === 'Escape') close(); };
  const onSelect = event => save(event.currentTarget.dataset.categoryValue || null);

  modal.classList.add('show');
  closeBtn.addEventListener('click', onClose);
  clearBtn.addEventListener('click', onClear);
  modal.addEventListener('click', onBackdrop);
  document.addEventListener('keydown', onKey);
  list.querySelectorAll('[data-category-value]').forEach(button => {
    button.addEventListener('click', onSelect);
  });
  list.querySelector('[data-category-value]')?.focus();
}

// Settings view: HTML/CSS/JS lives in templates/settings.html and static/js/settings.js.
// This thin shim fetches the partial on first activation, then defers to window.Settings.
let __settingsPartialPromise = null;
function loadSettingsPartial() {
  if (__settingsPartialPromise) return __settingsPartialPromise;
  __settingsPartialPromise = fetch('/partials/settings.html')
    .then(res => {
      if (!res.ok) throw new Error(`partial ${res.status}`);
      return res.text();
    })
    .then(html => {
      const container = document.getElementById('appSettings');
      if (container) container.innerHTML = html;
      window.Settings?.init();
    })
    .catch(err => {
      __settingsPartialPromise = null;
      console.error('Failed to load settings partial', err);
      const container = document.getElementById('appSettings');
      if (container) {
        container.innerHTML = `<div class="empty"><p>Could not load settings.</p><small>${escapeHtml(err.message || 'unknown error')}</small></div>`;
      }
    });
  return __settingsPartialPromise;
}

async function renderSettings(data) {
  await loadSettingsPartial();
  if (data && window.Settings) window.Settings.render(data);
}

async function clearRecent(){
  const ok = await confirmDialog({
    title: 'Clear <em>all logs?</em>',
    body:  'Permanently deletes closed sessions from your history. If Ableton is recording right now, the live session is preserved. This cannot be undone.',
    confirmLabel: 'Clear logs',
  });
  if (!ok) return;
  try {
    const r = await postAction('/api/clear-recent');
    toast(`Cleared ${r.deleted} closed session${r.deleted === 1 ? '' : 's'}`);
    load();
  } catch(e){ toast('Failed to clear logs'); }
}

async function clearUnsaved(){
  const ok = await confirmDialog({
    title: 'Remove <em>unsaved</em> projects?',
    body:  'Deletes closed sessions logged against <code>Untitled</code> or <code>Untitled Project</code>. If an unsaved draft is recording right now, it is preserved until that session ends.',
    confirmLabel: 'Remove drafts',
  });
  if (!ok) return;
  try {
    const r = await postAction('/api/clear-unsaved');
    toast(r.deleted === 0
      ? 'No unsaved sessions found'
      : `Removed ${r.deleted} unsaved session${r.deleted === 1 ? '' : 's'}`);
    load();
  } catch(e){ toast('Failed to remove drafts'); }
}

function bindRowDeleteTriggers(){
  document.querySelectorAll('.row-del').forEach(btn => {
    if (btn.disabled) return;
    btn.addEventListener('click', () => {
      const ids = (btn.dataset.sessionIds || '')
        .split(',')
        .map(s => parseInt(s, 10))
        .filter(n => Number.isFinite(n));
      const projectName = decodeURIComponent(btn.dataset.projectName || '');
      deleteRecentEntry(ids, projectName);
    });
  });
}

async function deleteRecentEntry(sessionIds, projectName){
  if (!sessionIds.length) return;
  const ok = await confirmDialog({
    title: 'Delete <em>this entry?</em>',
    body: `Permanently removes ${sessionIds.length === 1 ? 'this session' : `the ${sessionIds.length} merged sessions`} for <code>${escapeHtml(projectName || 'this project')}</code> from your history. This cannot be undone.`,
    confirmLabel: 'Delete entry',
  });
  if (!ok) return;
  try {
    const r = await postJson('/api/delete-session', { session_ids: sessionIds });
    if (r.skipped_live) {
      toast('Live session preserved');
    } else {
      toast(`Deleted ${r.deleted} session${r.deleted === 1 ? '' : 's'}`);
    }
    load();
  } catch(e){ toast('Failed to delete entry'); }
}

// ── Notes ──
const MAX_NOTES_LEN = 500;

// Bridge for Project Entries → notes modal
function openNotesModalWithData(entries, idx) {
  if (!Array.isArray(entries) || idx < 0 || idx >= entries.length) return;
  const entry = entries[idx];
  openNotesPopup({
    sessionIds: entry.sessionIds || [],
    projectName: entry.projectName || '',
    notes: entry.notes || {},
    startTimes: entry.startTimes || {},
    endTimes: entry.endTimes || {},
    lastSeenTimes: entry.lastSeenTimes || {},
    activeSeconds: entry.activeSeconds || {},
    projectNote: entry.projectNote || '',
  });
}
window.openNotesModalWithData = openNotesModalWithData;

function bindNotesTriggers(){
  document.querySelectorAll('.row-note').forEach(btn => {
    btn.addEventListener('click', () => {
      const ids = (btn.dataset.sessionIds || '')
        .split(',')
        .map(s => parseInt(s, 10))
        .filter(n => Number.isFinite(n));
      const projectName = decodeURIComponent(btn.dataset.projectName || '');
      let notes = {};
      let startTimes = {};
      let endTimes = {};
      let lastSeenTimes = {};
      let activeSeconds = {};
      let projectNote = '';
      try {
        notes = JSON.parse(decodeURIComponent(btn.dataset.sessionNotes || '{}'));
        startTimes = JSON.parse(decodeURIComponent(btn.dataset.sessionStartTimes || '{}'));
        endTimes = JSON.parse(decodeURIComponent(btn.dataset.sessionEndTimes || '{}'));
        lastSeenTimes = JSON.parse(decodeURIComponent(btn.dataset.sessionLastSeenTimes || '{}'));
        activeSeconds = JSON.parse(decodeURIComponent(btn.dataset.sessionActiveSeconds || '{}'));
        projectNote = decodeURIComponent(btn.dataset.projectNote || '');
      } catch(e) {}
      openNotesPopup({ sessionIds: ids, projectName, notes, startTimes, endTimes, lastSeenTimes, activeSeconds, projectNote });
    });
  });
}

let notesModalState = null;

function openNotesPopup({ sessionIds, projectName, notes, startTimes, endTimes, lastSeenTimes, activeSeconds, projectNote, initialMode }){
  if (notesModalState) closeNotesModal();

  const modal = document.getElementById('notesModal');
  const title = document.getElementById('notesModalTitle');
  const subtitle = document.getElementById('notesModalSubtitle');
  const body = document.getElementById('notesModalBody');
  const saveBtn = document.getElementById('notesModalSave');
  const cancelBtn = document.getElementById('notesModalCancel');
  const closeBtn = document.getElementById('notesModalClose');
  const prevBtn = document.getElementById('notesPrevBtn');
  const nextBtn = document.getElementById('notesNextBtn');
  const charCount = document.getElementById('notesCharCount');
  const navEl = document.getElementById('notesNav');
  const dateTimeEl = document.getElementById('notesSessionDateTime');
  const globalBtn = document.getElementById('notesGlobalBtn');
  const sessionBtn = document.getElementById('notesSessionBtn');
  const mergeBtn = document.getElementById('notesMergeBtn');
  const mergeBody = document.getElementById('notesMergeBody');

  const MAX_PROJECT_NOTE_LEN = 3000;
  const abletonIsRunning = latestDashboardData?.summary?.ableton_running === true;

  let currentMode = initialMode || 'global';
  let currentProjectName = projectName;
  let currentProjectNote = (projectNote || '').trim();
  let currentSessionIds = [...sessionIds];
  let editingSessionId = sessionIds.length > 0 ? sessionIds[0] : null;
  let currentSessionNote = editingSessionId ? (notes[String(editingSessionId)] || '') : '';
  let previousSessionId = null;
  let nextSessionId = null;
  let isDirty = false;
  let hasSessionNav = false;

  title.textContent = currentProjectName || 'Untitled session';
  saveBtn.disabled = false;
  saveBtn.textContent = 'Save';
  cancelBtn.textContent = 'Cancel';

  function endTimePlain(sidStr) {
    const et = endTimes[sidStr];
    const lst = lastSeenTimes[sidStr];
    if (et != null) return fmt.time(et);
    if (abletonIsRunning) return 'Active';
    if (lst) return fmt.time(lst);
    return '—';
  }

  function sessionSummary(sidStr) {
    const startTs = startTimes[sidStr] || 0;
    const seconds = activeSeconds?.[sidStr] || 0;
    const dateStr = fmt.dateOrdinal(startTs);
    const range = `${fmt.time(startTs)} to ${endTimePlain(sidStr)}`;
    const duration = seconds > 0 ? fmt.dur(seconds) : '';
    return { dateStr, range, duration };
  }

  function updateNavButtons() {
    prevBtn.disabled = !previousSessionId;
    nextBtn.disabled = !nextSessionId;
  }

  async function loadNavState() {
    if (sessionIds.length !== 1) {
      hasSessionNav = false;
      updateNavButtons();
      return;
    }
    try {
      const data = await getJson(`/api/session-notes-entry?session_id=${encodeURIComponent(sessionIds[0])}&project=${encodeURIComponent(currentProjectName || '')}`);
      previousSessionId = data.previous_session_id || null;
      nextSessionId = data.next_session_id || null;
      hasSessionNav = !!(previousSessionId || nextSessionId);
      updateNavButtons();
    } catch(e) {
      hasSessionNav = false;
      updateNavButtons();
    }
  }

  const openEntry = async sessionId => {
    if (!sessionId) return;
    if (isDirty && !window.confirm('Discard unsaved changes and switch sessions?')) return;
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    try {
      const data = await getJson(`/api/session-notes-entry?session_id=${encodeURIComponent(sessionId)}&project=${encodeURIComponent(currentProjectName || '')}`);
      const entry = data.session;
      closeNotesModal();
      openNotesPopup({
        sessionIds: [entry.id],
        projectName: entry.project_name || currentProjectName,
        notes: { [String(entry.id)]: entry.notes || '' },
        startTimes: { [String(entry.id)]: entry.start_time || 0 },
        endTimes: { [String(entry.id)]: entry.end_time ?? null },
        lastSeenTimes: { [String(entry.id)]: entry.last_seen_time || 0 },
        activeSeconds: { [String(entry.id)]: entry.active_seconds || 0 },
        projectNote: currentProjectNote,
        initialMode: 'session',
      });
    } catch(e) {
      toast('Failed to load session notes');
      updateNavButtons();
    }
  };

  const markDirty = () => {
    isDirty = true;
    cancelBtn.textContent = 'Cancel';
  };

  function setModeButtons() {
    const allBtns = [globalBtn, sessionBtn, mergeBtn];
    allBtns.forEach(btn => {
      if (!btn) return;
      btn.style.background = 'transparent';
      btn.style.color = 'var(--ink-3)';
    });
    const activeBtn = currentMode === 'global' ? globalBtn : currentMode === 'session' ? sessionBtn : mergeBtn;
    if (activeBtn) {
      activeBtn.style.background = 'var(--surface)';
      activeBtn.style.color = 'var(--ink)';
    }
    // Toggle visibility of notes body vs merge body
    const notesFootEl = body.parentElement.querySelector('.notes-modal-foot');
    if (currentMode === 'merge') {
      body.style.display = 'none';
      mergeBody.classList.add('is-active');
      if (notesFootEl) notesFootEl.style.display = 'none';
    } else {
      body.style.display = '';
      mergeBody.classList.remove('is-active');
      if (notesFootEl) notesFootEl.style.display = '';
    }
  }

  function updateTextarea(mode) {
    const textarea = body.querySelector('#notesTextarea');
    if (!textarea) return;

    // Flush current text to the right in-memory slot
    const val = textarea.value;
    if (currentMode === 'global') {
      currentProjectNote = val;
    } else if (currentMode === 'session' && editingSessionId) {
      notes[String(editingSessionId)] = val;
      currentSessionNote = val;
    }

    if (mode === 'global') {
      textarea.value = currentProjectNote;
      textarea.placeholder = '';
      textarea.maxLength = MAX_PROJECT_NOTE_LEN;
      navEl.hidden = true;
      if (dateTimeEl) dateTimeEl.style.display = 'none';
      subtitle.textContent = 'Project-wide — guidance, vibe, big picture';
      charCount.textContent = `${currentProjectNote.length} / ${MAX_PROJECT_NOTE_LEN}`;
    } else {
      textarea.value = currentSessionNote;
      textarea.placeholder = '';
      textarea.maxLength = MAX_NOTES_LEN;

      if (editingSessionId && sessionIds.length === 1) {
        const sidStr = String(editingSessionId);
        const summary = sessionSummary(sidStr);
        subtitle.innerHTML = `${escapeHtml(summary.dateStr)} from ${escapeHtml(summary.range)}`;
        navEl.hidden = !hasSessionNav;
        if (dateTimeEl) {
          dateTimeEl.style.display = 'block';
          dateTimeEl.textContent = `${summary.dateStr} · ${summary.range}`;
        }
      } else {
        navEl.hidden = true;
        if (dateTimeEl) dateTimeEl.style.display = 'none';
        const totalSeconds = currentSessionIds.reduce((sum, sid) => sum + (activeSeconds?.[String(sid)] || 0), 0);
        subtitle.textContent = `${currentSessionIds.length} sessions · ${fmt.dur(totalSeconds)}`;
      }

      charCount.textContent = `${currentSessionNote.length} / ${MAX_NOTES_LEN}`;
    }
    charCount.classList.toggle('over', false);
    textarea.focus();
  }

  const switchMode = (mode) => {
    if (currentMode === mode) return;
    if (mode === 'merge') {
      currentMode = mode;
      setModeButtons();
      renderMergeTab();
      return;
    }
    updateTextarea(mode);
    currentMode = mode;
    setModeButtons();
  };

  // ── Merge tab ──
  async function renderMergeTab() {
    mergeBody.innerHTML = '<div class="notes-merge-empty">Loading…</div>';
    try {
      const [aliasData, projectListData] = await Promise.all([
        getJson(`/api/project-aliases?project=${encodeURIComponent(currentProjectName)}`),
        getJson('/api/project-list?sort=recent'),
      ]);
      const aliases = aliasData.aliases || [];
      const allProjects = (projectListData || []).map(p => typeof p === 'string' ? p : p.project_name).filter(Boolean);
      const otherProjects = allProjects.filter(p => p !== currentProjectName && !aliases.includes(p));

      let html = '';

      // Section 1: Merged projects (aliases)
      html += '<div class="notes-merge-section">';
      html += '<div class="notes-merge-section-title">Merged into this project</div>';
      if (aliases.length === 0) {
        html += '<div class="notes-merge-empty">No projects merged yet.</div>';
      } else {
        html += '<div class="notes-merge-alias-list">';
        aliases.forEach(alias => {
          html += `
            <div class="notes-merge-alias-row">
              <span class="notes-merge-alias-name">${escapeHtml(alias)}</span>
              <button class="notes-merge-unmerge-btn" data-unmerge-alias="${escapeHtml(alias)}" type="button">Unmerge</button>
            </div>`;
        });
        html += '</div>';
      }
      html += '</div>';

      // Section 2: Merge multiple projects into this one
      html += '<div class="notes-merge-section">';
      html += `<div class="notes-merge-section-title">Merge into <span class="notes-merge-target">${escapeHtml(currentProjectName)}</span></div>`;
      if (otherProjects.length === 0) {
        html += '<div class="notes-merge-empty">No other projects available.</div>';
      } else {
        html += '<div class="notes-merge-picker">';
        html += '<input class="notes-merge-search" id="notesMergeSearch" type="search" autocomplete="off" placeholder="Search projects…" aria-label="Search projects to merge">';
        html += '<div class="notes-merge-selection-summary"><span id="notesMergeCount">0 selected</span><button id="notesMergeClear" type="button" hidden>Clear</button></div>';
        html += '<div class="notes-merge-project-list" id="notesMergeProjectList" role="group" aria-label="Projects available to merge">';
        otherProjects.forEach(p => {
          html += `<label class="notes-merge-project-row" data-project-name="${escapeHtml(p.toLocaleLowerCase())}">
            <input type="checkbox" value="${escapeHtml(p)}">
            <span>${escapeHtml(p)}</span>
          </label>`;
        });
        html += '</div>';
        html += '<button class="notes-merge-into-btn" id="notesMergeConfirmBtn" type="button" disabled>Merge selected</button>';
        html += '</div>';
      }
      html += '</div>';

      mergeBody.innerHTML = html;

      // Bind searchable multi-select list
      const mergeSearch = mergeBody.querySelector('#notesMergeSearch');
      const mergeList = mergeBody.querySelector('#notesMergeProjectList');
      const mergeCount = mergeBody.querySelector('#notesMergeCount');
      const mergeClear = mergeBody.querySelector('#notesMergeClear');
      const mergeConfirmBtn = mergeBody.querySelector('#notesMergeConfirmBtn');
      if (mergeList && mergeConfirmBtn) {
        const selectedProjects = () => Array.from(mergeList.querySelectorAll('input:checked')).map(input => input.value);
        const updateSelection = () => {
          const count = selectedProjects().length;
          mergeCount.textContent = `${count} selected`;
          mergeClear.hidden = count === 0;
          mergeConfirmBtn.disabled = count === 0;
          mergeList.querySelectorAll('.notes-merge-project-row').forEach(row => {
            row.classList.toggle('is-selected', row.querySelector('input').checked);
          });
        };
        mergeList.addEventListener('change', updateSelection);
        mergeSearch.addEventListener('input', () => {
          const query = mergeSearch.value.trim().toLocaleLowerCase();
          mergeList.querySelectorAll('.notes-merge-project-row').forEach(row => {
            row.hidden = Boolean(query) && !row.dataset.projectName.includes(query);
          });
        });
        mergeClear.addEventListener('click', () => {
          mergeList.querySelectorAll('input:checked').forEach(input => { input.checked = false; });
          updateSelection();
          mergeSearch.focus();
        });
        mergeConfirmBtn.addEventListener('click', async () => {
          const selected = selectedProjects();
          if (!selected.length) return;
          const projectList = selected.map(name => `<li><code>${escapeHtml(name)}</code></li>`).join('');
          const ok = await confirmDialog({
            title: `Merge ${selected.length} <em>project${selected.length === 1 ? '' : 's'}?</em>`,
            body: `Merge these projects into <code>${escapeHtml(currentProjectName)}</code>:<ul class="merge-confirm-list">${projectList}</ul>Sessions, notes, tasks, and time will be combined.`,
            confirmLabel: 'Merge selected',
          });
          if (!ok) return;
          try {
            const result = await postJson('/api/merge-projects', { canonical_name: currentProjectName, aliases: selected });
            await load();
            renderMergeTab();
            toast(`Merged ${result.merged_count} project${result.merged_count === 1 ? '' : 's'} into ${currentProjectName}`, {
              label: 'Undo',
              onClick: async () => {
                try {
                  await postJson('/api/unmerge-projects', { aliases: result.aliases });
                  toast(`Restored ${result.aliases.length} project${result.aliases.length === 1 ? '' : 's'}`);
                  await load();
                  renderMergeTab();
                } catch (e) {
                  toast(e.message || 'Failed to undo merge');
                }
              },
            });
          } catch (e) {
            toast(e.message || 'Failed to merge projects');
          }
        });
      }

      // Bind unmerge buttons
      mergeBody.querySelectorAll('[data-unmerge-alias]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const alias = btn.dataset.unmergeAlias;
          const ok = await confirmDialog({
            title: 'Unmerge <em>project?</em>',
            body: `<code>${escapeHtml(alias)}</code> will be restored as its own independent project with its original sessions and time.`,
            confirmLabel: 'Unmerge',
          });
          if (!ok) return;
          try {
            await postJson('/api/unmerge-project', { alias_name: alias });
            toast(`Unmerged \u201c${alias}\u201d`);
            await load();
            renderMergeTab();
          } catch (e) {
            toast('Failed to unmerge project');
          }
        });
      });
    } catch (e) {
      mergeBody.innerHTML = '<div class="notes-merge-empty">Failed to load merge data.</div>';
    }
  }

  // Build the single persistent textarea
  const initialValue = currentMode === 'global' ? currentProjectNote : currentSessionNote;
  const initialMax = currentMode === 'global' ? MAX_PROJECT_NOTE_LEN : MAX_NOTES_LEN;
  const initialPlaceholder = currentMode === 'global'
    ? ''
    : '';

  body.innerHTML = `<textarea class="notes-textarea" id="notesTextarea" maxlength="${initialMax}" placeholder="${initialPlaceholder}">${escapeHtml(initialValue)}</textarea>`;

  const textarea = body.querySelector('#notesTextarea');
  if (textarea) {
    textarea.addEventListener('input', () => {
      const val = textarea.value;
      if (currentMode === 'global') {
        currentProjectNote = val;
        charCount.textContent = `${val.length} / ${MAX_PROJECT_NOTE_LEN}`;
        charCount.classList.toggle('over', val.length > MAX_PROJECT_NOTE_LEN);
      } else {
        if (editingSessionId) notes[String(editingSessionId)] = val;
        currentSessionNote = val;
        charCount.textContent = `${val.length} / ${MAX_NOTES_LEN}`;
        charCount.classList.toggle('over', val.length > MAX_NOTES_LEN);
      }
      markDirty();
    });
  }

  // Initialize mode UI
  if (currentMode === 'global') {
    navEl.hidden = true;
    if (dateTimeEl) dateTimeEl.style.display = 'none';
    subtitle.textContent = 'Project-wide — guidance, vibe, big picture';
    charCount.textContent = `${initialValue.length} / ${MAX_PROJECT_NOTE_LEN}`;
  } else {
    if (sessionIds.length === 1 && editingSessionId) {
      const sidStr = String(editingSessionId);
      const summary = sessionSummary(sidStr);
      subtitle.innerHTML = `${escapeHtml(summary.dateStr)} from ${escapeHtml(summary.range)}`;
    } else if (sessionIds.length > 1) {
      const totalSeconds = sessionIds.reduce((sum, sid) => sum + (activeSeconds?.[String(sid)] || 0), 0);
      subtitle.textContent = `${sessionIds.length} sessions · ${fmt.dur(totalSeconds)}`;
    }
    if (dateTimeEl && editingSessionId && sessionIds.length === 1) {
      dateTimeEl.style.display = 'block';
      const summary = sessionSummary(String(editingSessionId));
      dateTimeEl.textContent = `${summary.dateStr} · ${summary.range}`;
    }
    charCount.textContent = `${initialValue.length} / ${MAX_NOTES_LEN}`;
  }

  const onGlobalMode = () => switchMode('global');
  const onSessionMode = () => switchMode('session');
  const onMergeMode = () => switchMode('merge');

  globalBtn.addEventListener('click', onGlobalMode);
  sessionBtn.addEventListener('click', onSessionMode);
  if (mergeBtn) mergeBtn.addEventListener('click', onMergeMode);

  loadNavState().then(() => {
    setModeButtons();
    // Apply current mode UI state (arrows, date/time)
    if (currentMode === 'session') {
      if (editingSessionId && sessionIds.length === 1) {
        navEl.hidden = !hasSessionNav;
        if (dateTimeEl && hasSessionNav) dateTimeEl.style.display = 'block';
        if (!hasSessionNav && dateTimeEl) dateTimeEl.style.display = 'none';
      }
    }
    updateNavButtons();
  });

  // Handlers
  const onSave = async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';
    try {
      const ta = body.querySelector('#notesTextarea');
      const val = ta ? ta.value.trim() : '';

      if (currentMode === 'global') {
        currentProjectNote = val;
        const project = findProjectByName(currentProjectName) || { project_name: currentProjectName };
        await postJson('/api/project-metadata', {
          project_name: currentProjectName,
          status: project.status || '',
          type: project.type || '',
          priority: project.priority || '',
          due_date: project.due_date || '',
          pinned: !!project.pinned,
          project_note: val,
        });
      } else {
        if (editingSessionId) notes[String(editingSessionId)] = val;
        currentSessionNote = val;
        await postJson('/api/session-notes', { session_id: editingSessionId, notes: val });
      }
      toast('Notes saved');
      isDirty = false;
      cancelBtn.textContent = 'Exit';
      await load();
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    } catch(e) {
      toast('Failed to save notes');
      saveBtn.disabled = false;
      saveBtn.textContent = 'Save';
    }
  };

  const onCancel = async () => {
    if (isDirty && window.confirm('Do you want to save changes? Yes or no.')) {
      await onSave();
      return;
    }
    closeNotesModal();
  };
  const onBackdrop = e => { if (e.target === modal) onCancel(); };
  const onKey = e => {
    if (e.key === 'Escape') onCancel();
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) onSave();
  };
  const onPrev = () => openEntry(previousSessionId);
  const onNext = () => openEntry(nextSessionId);

  saveBtn.addEventListener('click', onSave);
  cancelBtn.addEventListener('click', onCancel);
  closeBtn.addEventListener('click', onCancel);
  prevBtn.addEventListener('click', onPrev);
  nextBtn.addEventListener('click', onNext);
  modal.addEventListener('click', onBackdrop);
  document.addEventListener('keydown', onKey);

  modal.classList.add('show');
  if (textarea) textarea.focus();

  notesModalState = { onSave, onCancel, onBackdrop, onKey, onPrev, onNext, onGlobalMode, onSessionMode, onMergeMode, saveBtn, cancelBtn, closeBtn, prevBtn, nextBtn, modal, globalBtn, sessionBtn, mergeBtn, mergeBody };
}

function closeNotesModal(){
  if (!notesModalState) return;
  const { onSave, onCancel, onBackdrop, onKey, onPrev, onNext, onGlobalMode, onSessionMode, onMergeMode, saveBtn, cancelBtn, closeBtn, prevBtn, nextBtn, modal, globalBtn, sessionBtn, mergeBtn, mergeBody } = notesModalState;
  modal.classList.remove('show');
  saveBtn.removeEventListener('click', onSave);
  cancelBtn.removeEventListener('click', onCancel);
  closeBtn.removeEventListener('click', onCancel);
  prevBtn.removeEventListener('click', onPrev);
  nextBtn.removeEventListener('click', onNext);
  if (globalBtn) globalBtn.removeEventListener('click', onGlobalMode);
  if (sessionBtn) sessionBtn.removeEventListener('click', onSessionMode);
  if (mergeBtn) mergeBtn.removeEventListener('click', onMergeMode);
  if (mergeBody) { mergeBody.classList.remove('is-active'); mergeBody.innerHTML = ''; }
  modal.removeEventListener('click', onBackdrop);
  document.removeEventListener('keydown', onKey);
  // Restore notes body and footer visibility
  const notesBodyEl = document.getElementById('notesModalBody');
  if (notesBodyEl) notesBodyEl.style.display = '';
  const notesFootEl = modal.querySelector('.notes-modal-foot');
  if (notesFootEl) notesFootEl.style.display = '';
  notesModalState = null;
}

async function clearPhantoms(){
  const ok = await confirmDialog({
    title: 'Remove phantom <em>sessions</em>?',
    body:  'Deletes closed rows that were clearly captured from export dialogs or plugin windows instead of real Live sets.',
    confirmLabel: 'Clean phantoms',
  });
  if (!ok) return;
  try {
    const r = await postAction('/api/clear-phantoms');
    toast(r.deleted === 0
      ? 'No phantom sessions found'
      : `Removed ${r.deleted} phantom session${r.deleted === 1 ? '' : 's'}`);
    load();
  } catch(e){ toast('Failed to clean phantom sessions'); }
}

let lastETag = '';
let lastFetchedMonth = '';
let dashboardRefreshTimer = null;

async function fetchDashboardData({ recentBefore = null } = {}) {
  const monthKey = currentMonthStateKey();
  const headers = {};
  if (!recentBefore && lastETag && lastFetchedMonth === monthKey) {
    headers['If-None-Match'] = lastETag;
  }
  const params = new URLSearchParams({ month: monthKey });
  if (recentBefore) params.set('recent_before', String(recentBefore));
  const res = await fetch(`/api/data?${params.toString()}`, { headers });
  if (res.status === 304) {
    return null;
  }
  const newETag = res.headers.get('ETag');
  if (!recentBefore && newETag) {
    lastETag = newETag;
    lastFetchedMonth = monthKey;
  }
  const data = await res.json();
  return { data, monthKey };
}

async function load() {
  try {
    const result = await fetchDashboardData();
    if (!result) return;
    const { data } = result;
    if (data?.summary?.selected_month) {
      dashboardMonthState.selectedMonth = data.summary.selected_month;
    }
    latestDashboardData = data;
    render(data);
  } catch(e) {
    console.error(e);
  }
}

async function loadOlderRecent() {
  if (!latestDashboardData?.recent_has_more || !latestDashboardData?.recent_oldest_start_time) return;
  const button = document.getElementById('btnLoadOlderRecent');
  if (button) button.disabled = true;
  try {
    const result = await fetchDashboardData({ recentBefore: latestDashboardData.recent_oldest_start_time });
    if (!result?.data) return;
    latestDashboardData = {
      ...latestDashboardData,
      recent: [
        ...(latestDashboardData.recent || []),
        ...(result.data.recent || []),
      ],
      recent_has_more: result.data.recent_has_more,
      recent_oldest_start_time: result.data.recent_oldest_start_time,
    };
    render(latestDashboardData);
  } catch (error) {
    toast(error.message || 'Failed to load older entries');
  } finally {
    if (button) button.disabled = false;
  }
}

function lastMonthPace(yearDaily, today = new Date()) {
  if (!Array.isArray(yearDaily) || yearDaily.length === 0) return null;
  const totals = new Map();
  for (const row of yearDaily) {
    if (row && row.day) totals.set(row.day, row.total_seconds || 0);
  }
  const y = today.getFullYear();
  const m = today.getMonth();
  const dayOfMonth = monthKeyFromDate(today) === currentMonthKey()
    ? new Date().getDate()
    : new Date(y, m + 1, 0).getDate();
  const iso = (d) => {
    const yy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yy}-${mm}-${dd}`;
  };
  let thisSum = 0;
  for (let d = 1; d <= dayOfMonth; d++) thisSum += totals.get(iso(new Date(y, m, d))) || 0;
  const lastMonthEnd = new Date(y, m, 0).getDate();
  const cap = Math.min(dayOfMonth, lastMonthEnd);
  let lastSum = 0;
  for (let d = 1; d <= cap; d++) lastSum += totals.get(iso(new Date(y, m - 1, d))) || 0;
  if (lastSum <= 0) return null;
  const pct = ((thisSum - lastSum) / lastSum) * 100;
  return { pct: Math.round(pct), positive: pct >= 0 };
}

function longestStreak(yearDaily) {
  if (!Array.isArray(yearDaily) || yearDaily.length === 0) return 0;
  const sorted = yearDaily
    .filter(r => r && r.day && (r.total_seconds || 0) > 0)
    .map(r => r.day)
    .sort();
  if (sorted.length === 0) return 0;
  const dayMs = 86400000;
  let best = 1;
  let cur = 1;
  let prev = new Date(sorted[0] + 'T00:00:00');
  for (let i = 1; i < sorted.length; i++) {
    const d = new Date(sorted[i] + 'T00:00:00');
    const gap = Math.round((d - prev) / dayMs);
    if (gap === 1) cur += 1;
    else if (gap > 1) cur = 1;
    if (cur > best) best = cur;
    prev = d;
  }
  return best;
}

function projectStatusLabel(status) {
  const found = PROJECT_STATUS_OPTIONS.find(([value]) => value === (status || ''));
  return found ? found[1] : 'Unsorted';
}

function projectTypeLabel(type) {
  const found = PROJECT_TYPE_OPTIONS.find(([value]) => value === (type || ''));
  return found ? found[1] : '';
}

function plannerActiveProjects(projects) {
  return (projects || []).filter(project => !['finished', 'paused', 'abandoned'].includes(project.status || ''));
}

function plannerOpenTasks(projects) {
  return (projects || []).flatMap(project => {
    const tasks = Array.isArray(project.project_tasks) ? project.project_tasks : [];
    return tasks.filter(task => task.status !== 'done').map(task => ({ task, project }));
  });
}

function daysSinceTimestamp(timestamp) {
  const ts = Number(timestamp || 0);
  if (!Number.isFinite(ts) || ts <= 0) return null;
  return Math.max(0, Math.floor((Date.now() / 1000 - ts) / 86400));
}

function lastWorkedLabel(project) {
  const days = daysSinceTimestamp(project?.last_seen);
  if (days == null) return 'No sessions yet';
  if (days === 0) return 'Worked today';
  if (days === 1) return 'Worked yesterday';
  return `Worked ${days} days ago`;
}

function plannerReasonPills(reasons) {
  return `<div class="planner-reason-list">${reasons.map(reason => `
    <span class="planner-pill ${reason.className || ''}">${escapeHtml(reason.label)}</span>
  `).join('')}</div>`;
}

function plannerGoalReasonPills(goals, project) {
  const activeGoals = (goals || []).filter(goal => goal.active !== false);
  const matches = activeGoals.filter(goal => {
    const scopeType = goal.scope_type || 'all';
    const scopeValue = goal.scope_value || '';
    if (scopeType === 'project') return scopeValue === project.project_name;
    if (scopeType === 'project_type') return scopeValue === project.type;
    if (scopeType === 'category') return scopeValue === project.category_key;
    return scopeType === 'all';
  });
  return matches.slice(0, 2).map(goal => {
    const progress = goal.progress || {};
    const percent = Number(progress.percent || 0);
    const label = percent >= 100 ? 'Goal met' : `${plannerGoalNumber(progress.remaining_value)} ${progress.unit || 'left'} to goal`;
    return { label, className: 'is-goal' };
  });
}

function plannerAddSuggestion(items, seen, item) {
  const key = `${item.kind || 'project'}:${item.project?.project_name || ''}:${item.title}`;
  if (!item.project || seen.has(key)) return;
  seen.add(key);
  items.push(item);
}

function plannerFocusItems(projects, goals = []) {
  const activeProjects = plannerActiveProjects(projects);
  const items = [];
  const seen = new Set();
  plannerOpenTasks(activeProjects).forEach(({ task, project }) => {
    const deadlineReasons = projectDeadlineReasons(project);
    plannerAddSuggestion(items, seen, {
      project,
      kind: 'task',
      action: 'Next task',
      title: task.title,
      meta: project.project_name,
      score: 70 + (task.priority === 'high' ? 18 : task.priority === 'normal' ? 8 : 0) + (project.type === 'client' ? 18 : 0) + Math.max(0, 12 - (daysSinceTimestamp(project.last_seen) || 0)),
      reasons: [
        { label: `${priorityLabel(task.priority)} task`, className: 'is-task' },
        ...(project.type === 'client' ? [{ label: 'Client', className: 'is-client' }] : []),
        ...deadlineReasons.slice(0, 2),
        ...plannerGoalReasonPills(goals, project),
      ],
    });
  });
  activeProjects.forEach(project => {
    const openCount = projectTaskSummary(project).openCount;
    const days = daysSinceTimestamp(project.last_seen);
    const isRecentPersonal = project.type === 'personal' && days != null && days <= 3;
    const isClient = project.type === 'client';
    const isFinishing = project.status === 'finishing' || project.status === 'final_touches';
    const isFinishCandidate = project.type === 'personal' && ['in_progress', 'finishing', 'final_touches'].includes(project.status || '') && days != null && days <= 7;
    const deadlineReasons = projectDeadlineReasons(project);
    if (isFinishCandidate) {
      plannerAddSuggestion(items, seen, {
        project,
        kind: 'finish',
        action: isFinishing ? 'Push toward finish' : 'Keep momentum',
        title: project.project_name,
        meta: `${fmt.dur(project.total_seconds || 0)} tracked · ${lastWorkedLabel(project)}`,
        score: 68 + (isFinishing ? 24 : 10) + Math.max(0, 10 - days) + Math.min(openCount * 5, 18),
        reasons: [
          { label: project.status === 'final_touches' ? 'Final Touches' : isFinishing ? 'Finishing' : 'In progress', className: 'is-finishing' },
          { label: days === 0 ? 'Worked today' : `Worked ${days}d ago`, className: 'is-momentum' },
          ...(openCount ? [{ label: `${openCount} open task${openCount !== 1 ? 's' : ''}`, className: 'is-task' }] : []),
          ...plannerGoalReasonPills(goals, project),
        ],
      });
    }
    if (isClient || isFinishing || isRecentPersonal || openCount || deadlineReasons.length) {
      plannerAddSuggestion(items, seen, {
      project,
      kind: 'project',
      action: isClient ? 'Protect deadline' : isFinishing ? 'Finish candidate' : 'Project focus',
      title: project.project_name,
      meta: `${fmt.dur(project.total_seconds || 0)} tracked · ${lastWorkedLabel(project)}`,
      score: (isClient ? 48 : 0) + (isFinishing ? 34 : 0) + (isRecentPersonal ? 24 : 0) + Math.min(openCount * 6, 24) + (deadlineReasons.length ? 22 : 0),
      reasons: [
        ...(isClient ? [{ label: 'Client', className: 'is-client' }] : []),
        ...(isFinishing ? [{ label: project.status === 'final_touches' ? 'Final Touches' : 'Finishing', className: 'is-finishing' }] : []),
        ...(isRecentPersonal ? [{ label: 'Personal momentum', className: 'is-momentum' }] : []),
        ...(openCount ? [{ label: `${openCount} open task${openCount !== 1 ? 's' : ''}`, className: 'is-task' }] : []),
        ...deadlineReasons.slice(0, 2),
        ...plannerGoalReasonPills(goals, project),
      ],
      });
    }
  });
  return items
    .sort((a, b) => b.score - a.score || String(a.title).localeCompare(String(b.title)))
    .slice(0, 10);
}

function plannerDeadlineRank(project) {
  const state = project?.deadline_state || '';
  if (state === 'overdue') return 0;
  if (state === 'due_soon') return 1;
  if (state === 'upcoming') return 2;
  if (state === 'delivered') return 3;
  return 4;
}

function plannerDeadlineProjects(projects) {
  return (projects || [])
    .filter(project => project.type === 'client' || project.due_date || project.hard_deadline)
    .sort((a, b) => {
      const rank = plannerDeadlineRank(a) - plannerDeadlineRank(b);
      if (rank !== 0) return rank;
      const aDate = a.hard_deadline || a.due_date || '9999-12-31';
      const bDate = b.hard_deadline || b.due_date || '9999-12-31';
      return String(aDate).localeCompare(String(bDate)) || String(a.project_name).localeCompare(String(b.project_name));
    });
}

function compactDateLabel(value) {
  if (!value) return 'No date';
  const parts = String(value).split('-').map(Number);
  if (parts.length !== 3 || parts.some(part => !Number.isFinite(part))) return String(value);
  const date = new Date(parts[0], parts[1] - 1, parts[2]);
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function projectDueDate(project) {
  const value = project?.due_date || '';
  const parts = String(value).split('-').map(Number);
  if (parts.length !== 3 || parts.some(part => !Number.isFinite(part))) return null;
  const date = new Date(parts[0], parts[1] - 1, parts[2]);
  date.setHours(0, 0, 0, 0);
  return date;
}

function projectDueDelta(project) {
  const date = projectDueDate(project);
  if (!date) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((date - today) / 86400000);
}

function projectUrgencyLabel(project) {
  const delta = projectDueDelta(project);
  if (delta == null) return '';
  if (delta < 0) return 'Overdue';
  if (delta === 0) return 'Today';
  if (delta === 1) return 'Tomorrow';
  if (delta <= 7) {
    return projectDueDate(project).toLocaleDateString('en-US', { weekday: 'long' });
  }
  return 'Upcoming';
}

function projectDueDateText(project) {
  if (!project?.due_date) return 'No due date';
  const urgency = projectUrgencyLabel(project);
  const date = compactDateLabel(project.due_date);
  return urgency ? `${urgency} · ${date}` : date;
}

function projectDuePillClass(project) {
  const delta = projectDueDelta(project);
  if (delta == null) return '';
  if (delta < 0) return 'is-overdue';
  if (delta <= 7) return 'is-due-soon';
  return '';
}

function isTerminalProjectStatus(status) {
  return ['finished', 'paused', 'abandoned'].includes(status || '');
}

async function confirmTerminalProjectMove(projectName, status) {
  return confirmDialog({
    title: `Move to <em>${escapeHtml(projectStatusLabel(status))}</em>?`,
    body: `This removes <code>${escapeHtml(projectName)}</code> from Today's Focus until you move it back to an active status.`,
    confirmLabel: 'Move project',
  });
}

async function updateProjectPinned(project, pinned) {
  return postJson('/api/project-metadata', {
    project_name: project.project_name,
    status: project.status || '',
    type: project.type || '',
    priority: project.priority || '',
    due_date: project.due_date || '',
    pinned,
    project_note: project.project_note || '',
  });
}

function todayFocusProjects(projects) {
  const activeStatuses = new Set(['idea', 'needs_work', 'in_progress', 'finishing', 'final_touches']);
  const candidates = (projects || [])
    .filter(project => activeStatuses.has(project.status || ''))
    .filter(project => !!project.due_date);
  const sortFocusGroup = group => group.slice().sort((a, b) => {
    const aDelta = projectDueDelta(a);
    const bDelta = projectDueDelta(b);
    if ((aDelta ?? 99999) !== (bDelta ?? 99999)) return (aDelta ?? 99999) - (bDelta ?? 99999);
    const aRecent = daysSinceTimestamp(a.last_seen);
    const bRecent = daysSinceTimestamp(b.last_seen);
    if ((aRecent ?? 99999) !== (bRecent ?? 99999)) return (aRecent ?? 99999) - (bRecent ?? 99999);
    if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
    const statusWeight = { finishing: 0, final_touches: 0, in_progress: 1, needs_work: 2, idea: 3 };
    const aStatus = statusWeight[a.status || ''] ?? 9;
    const bStatus = statusWeight[b.status || ''] ?? 9;
    if (aStatus !== bStatus) return aStatus - bStatus;
    const aTasks = projectTaskSummary(a).openCount;
    const bTasks = projectTaskSummary(b).openCount;
    if (aTasks !== bTasks) return bTasks - aTasks;
    return String(a.project_name || '').localeCompare(String(b.project_name || ''));
  });
  const overdue = sortFocusGroup(candidates.filter(project => {
    const delta = projectDueDelta(project);
    return delta != null && delta < 0;
  }));
  if (overdue.length >= 3) return overdue.slice(0, 3);
  const selected = [...overdue];
  const selectedNames = new Set(selected.map(project => project.project_name));
  const pinned = sortFocusGroup(candidates.filter(project => project.pinned && !selectedNames.has(project.project_name)));
  pinned.forEach(project => {
    if (selected.length < 3) {
      selected.push(project);
      selectedNames.add(project.project_name);
    }
  });
  const remaining = sortFocusGroup(candidates.filter(project => !selectedNames.has(project.project_name)));
  remaining.forEach(project => {
    if (selected.length < 3) selected.push(project);
  });
  return selected;
}

function renderPlannerGoalForm() {
  return `
    <form class="planner-goal-form" id="plannerGoalCreateForm" data-planner-goal-form>
      <div class="planner-goal-field">
        <label for="plannerGoalType">Goal</label>
        <select class="planner-goal-input" id="plannerGoalType" data-goal-type>
          ${PLANNER_GOAL_TYPE_OPTIONS.map(([value, label]) => `<option value="${value}">${escapeHtml(label)}</option>`).join('')}
        </select>
      </div>
      <div class="planner-goal-field">
        <label for="plannerGoalTarget">Target</label>
        <input class="planner-goal-input" id="plannerGoalTarget" data-goal-target type="number" min="0.25" step="0.25" value="1">
      </div>
      <div class="planner-goal-field">
        <label for="plannerGoalPeriod">Period</label>
        <select class="planner-goal-input" id="plannerGoalPeriod" data-goal-period>
          ${PLANNER_GOAL_PERIOD_OPTIONS.map(([value, label]) => `<option value="${value}">${escapeHtml(label)}</option>`).join('')}
        </select>
      </div>
      <div class="planner-goal-field">
        <label for="plannerGoalScope">Scope</label>
        <select class="planner-goal-input" id="plannerGoalScope" data-goal-scope-type>
          ${PLANNER_GOAL_SCOPE_OPTIONS.map(([value, label]) => `<option value="${value}">${escapeHtml(label)}</option>`).join('')}
        </select>
      </div>
      <div class="planner-goal-field">
        <label>Scope Value</label>
        <span data-goal-scope-value-wrap>${renderPlannerGoalScopeValueControl('all')}</span>
      </div>
      <div class="planner-goal-field">
        <label for="plannerGoalActive">Active</label>
        <label class="planner-goal-muted">
          <input id="plannerGoalActive" data-goal-active type="checkbox" checked> Track
        </label>
      </div>
      <button class="btn small" type="submit">Add</button>
    </form>
  `;
}

function renderPlannerGoalRows(goals) {
  if (!Array.isArray(goals) || goals.length === 0) {
    return '<div class="planner-empty">No planner goals yet. Add one to start tracking direction alongside time.</div>';
  }
  return `<div class="planner-goal-list">${goals.map(goal => {
    const progress = goal.progress || {};
    const percent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
    const active = goal.active !== false;
    const reasons = [
      { label: `${plannerGoalPeriodLabel(goal.period)} goal` },
      { label: plannerGoalScopeLabel(goal) },
      { label: active ? 'Active' : 'Paused', className: active ? 'is-finishing' : '' },
    ];
    return `
      <div class="planner-goal-row ${active ? '' : 'is-paused'}">
        <div class="planner-goal-top">
          <div class="planner-row-main">
            <div class="planner-row-title">
              <span class="planner-row-name">${escapeHtml(plannerGoalTypeLabel(goal.goal_type))}</span>
            </div>
            <div class="planner-row-meta">${escapeHtml(progress.label || plannerGoalProgressSummary(goal))}</div>
            ${plannerReasonPills(reasons)}
          </div>
          <div class="planner-goal-actions">
            <button class="btn small" type="button" data-goal-toggle="${goal.id}" data-goal-active="${active ? 'true' : 'false'}">${active ? 'Pause' : 'Reactivate'}</button>
            <button class="btn small" type="button" data-goal-delete="${goal.id}">Delete</button>
          </div>
        </div>
        <div class="planner-goal-progress" aria-label="${escapeHtml(percent)} percent complete" style="--goal-progress:${percent}%"><span></span></div>
        <div class="planner-goal-muted">${escapeHtml(plannerGoalProgressSummary(goal))} · ${escapeHtml(plannerGoalNumber(percent))}% complete</div>
      </div>
    `;
  }).join('')}</div>`;
}

function renderPlanner(data) {
  const plannerEl = document.getElementById('appPlanner');
  if (!plannerEl) return;
  const projects = Array.isArray(data.projects) ? data.projects : [];
  const plannerGoals = Array.isArray(data.planner_goals) ? data.planner_goals : [];
  const activeProjects = plannerActiveProjects(projects);
  const openTasks = plannerOpenTasks(projects);
  const recentActive = activeProjects
    .filter(project => {
      const days = daysSinceTimestamp(project.last_seen);
      return days != null && days <= 7;
    })
    .sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
  const staleActive = activeProjects
    .filter(project => {
      const days = daysSinceTimestamp(project.last_seen);
      return days == null || days >= 7;
    })
    .sort((a, b) => (a.last_seen || 0) - (b.last_seen || 0));
  const focusProjects = todayFocusProjects(projects);
  const deadlineProjects = plannerDeadlineProjects(projects);

  const renderFocus = () => {
    const slots = [0, 1, 2].map(index => {
      const project = focusProjects[index];
      if (!project) {
        return `
          <div class="planner-focus-row focus-slot is-empty">
            <div class="planner-row-main">
              <div class="focus-rank">#${index + 1}</div>
              <div class="planner-row-title"><span class="planner-row-name">No project</span></div>
              <div class="planner-row-meta">Add a due date or pin a project</div>
            </div>
          </div>
        `;
      }
      const reasons = [
        { label: projectDueDateText(project), className: projectDuePillClass(project) },
        ...(project.pinned ? [{ label: 'Pinned', className: 'is-goal' }] : []),
        ...(projectTaskSummary(project).openCount ? [{ label: `${projectTaskSummary(project).openCount} open task${projectTaskSummary(project).openCount !== 1 ? 's' : ''}`, className: 'is-task' }] : []),
      ];
      return `
      <div class="planner-focus-row focus-slot" data-open-project="${encodeURIComponent(project.project_name || '')}">
        <div class="planner-row-main">
          <div class="focus-rank">#${index + 1}</div>
          <div class="planner-row-title">
            <span class="proj-dot" style="background:${projectColor(project, index)}"></span>
            <span class="planner-row-name" title="${escapeHtml(project.project_name)}">${escapeHtml(project.project_name)}</span>
          </div>
          <div class="planner-row-meta">${escapeHtml(projectStatusLabel(project.status) || 'No status')} · ${escapeHtml(lastWorkedLabel(project))}</div>
          ${plannerReasonPills(reasons)}
        </div>
      </div>
      `;
    });
    return `<div class="focus-slots">${slots.join('')}</div>`;
  };

  const renderDeadlineRadar = () => {
    if (deadlineProjects.length === 0) {
      return '<div class="planner-empty">No client projects or deadlines yet.</div>';
    }
    return `<div class="planner-deadline-list">${deadlineProjects.map((project, index) => {
      const dateValue = project.hard_deadline || project.due_date || '';
      const label = projectDeadlineLabel(project) || (project.type === 'client' ? 'Client project' : 'Deadline');
      const reasons = [
        { label, className: deadlinePillClass(project.deadline_state) },
        ...(project.priority ? [{ label: `${projectPriorityLabel(project.priority)} priority`, className: 'is-priority' }] : []),
        ...projectDeadlineReasons(project),
      ];
      return `
        <div class="planner-deadline-row">
          <div class="planner-row-main">
            <div class="planner-row-title">${projectBadge(project, index)}</div>
            <div class="planner-row-meta">${escapeHtml(projectTypeLabel(project.type) || 'No type')} · ${escapeHtml(projectStatusLabel(project.status))} · ${escapeHtml(lastWorkedLabel(project))}</div>
            ${plannerReasonPills(reasons)}
          </div>
          <div class="planner-deadline-date">
            <span>${project.hard_deadline ? 'Hard' : project.due_date ? 'Due' : 'Date'}</span>
            <strong>${escapeHtml(compactDateLabel(dateValue))}</strong>
            ${projectTaskButton(project)}
          </div>
        </div>
      `;
    }).join('')}</div>`;
  };

  const renderBoardProject = (project, index) => {
    const encodedProject = encodeURIComponent(project.project_name || '');
    const dueClass = projectDuePillClass(project);
    const terminalClass = project.deadline_state === 'overdue' || projectDueDelta(project) < 0
      ? 'is-overdue'
      : dueClass === 'is-due-soon' ? 'is-due-soon' : '';
    
    return `
      <div class="planner-board-project ${terminalClass}" draggable="true" data-open-project="${encodedProject}" data-project-name="${encodedProject}" data-current-status="${project.status || ''}">
        <div class="board-card-header">
          <div class="board-card-title-wrap">
            <span class="proj-dot" style="background:${projectColor(project, index)}"></span>
            <span class="board-card-title" title="${escapeHtml(project.project_name || '')}">${escapeHtml(project.project_name || 'Untitled')}</span>
          </div>
          <div class="board-card-meta">
            <div class="board-card-meta-left">
              <span class="board-card-due" title="Due date">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
                ${escapeHtml(projectDueDateText(project))}
              </span>
              ${categoryPill(project)}
            </div>
            <button class="board-pin ${project.pinned ? 'is-pinned' : ''}" type="button" data-pin-project="${encodedProject}" aria-label="${project.pinned ? 'Unpin project' : 'Pin project'}" title="${project.pinned ? 'Unpin' : 'Pin'}">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="${project.pinned ? 'currentColor' : 'none'}" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 17v5"/><path d="M5 17h14"/><path d="M7 10h10"/><path d="M9 10V4h6v6"/><path d="M8 17l2-7h4l2 7"/></svg>
            </button>
          </div>
        </div>
      </div>
    `;
  };

  const collapsedColumns = (() => {
    try {
      const stored = localStorage.getItem('boardCollapsedColumns');
      return stored ? JSON.parse(stored) : [];
    } catch(e) {
      return [];
    }
  })();
  const hiddenColumns = (() => {
    try {
      const stored = localStorage.getItem('boardHiddenColumns');
      return stored ? JSON.parse(stored) : ['finished', 'paused', 'abandoned'];
    } catch(e) {
      return ['finished', 'paused', 'abandoned'];
    }
  })();

  const renderBoardColumn = ({ value, label, projects: groupProjects }) => {
    const isCollapsed = collapsedColumns.includes(value);
    const canHide = isTerminalProjectStatus(value);
    
    let emptyHtml = '';
    if (value === '') {
      emptyHtml = '<div class="board-empty-hint"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M8 12h8M12 8v8"/></svg>Drag projects here or set status</div>';
    } else if (value === 'finishing' || value === 'final_touches') {
      emptyHtml = '<div class="board-empty-hint">Move projects here when they are close to done</div>';
    } else {
      emptyHtml = '<div class="board-empty-hint">No projects</div>';
    }

    return `
      <div class="planner-board-column ${isCollapsed ? 'is-collapsed' : ''}" data-planner-status="${value}">
        <div class="planner-board-head">
          <span>${escapeHtml(label)}</span>
          <div>
            <span class="planner-board-count">${groupProjects.length}</span>
            ${canHide ? `
              <button type="button" class="board-column-toggle" data-hide-column="${value}" aria-label="Hide ${escapeHtml(label)} lane">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3l18 18"/><path d="M10.6 10.6A2 2 0 0 0 13.4 13.4"/><path d="M9.9 4.2A11.3 11.3 0 0 1 12 4c5 0 9 4.5 10 8a12.8 12.8 0 0 1-2.1 3.7"/><path d="M6.2 6.2A12.9 12.9 0 0 0 2 12c1 3.5 5 8 10 8 1.5 0 2.9-.4 4.1-1"/></svg>
              </button>
            ` : ''}
            <button type="button" class="board-column-toggle" data-toggle-column="${value}" aria-label="Toggle column">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
            </button>
          </div>
        </div>
        <div class="planner-board-column-content">
          ${groupProjects.length ? groupProjects.map(renderBoardProject).join('') : emptyHtml}
        </div>
      </div>
    `;
  };

  let boardSort = localStorage.getItem('boardSortKey') || 'recent';

  let boardProjects = projects.filter(p => {
    if (boardSearchQuery && !(p.project_name || '').toLowerCase().includes(boardSearchQuery.toLowerCase())) return false;
    if (boardTypeFilter && p.type !== boardTypeFilter) return false;
    return true;
  });

  boardProjects.sort((a, b) => {
    if (boardSort === 'name') return String(a.project_name).localeCompare(String(b.project_name));
    if (boardSort === 'time') return (b.total_seconds || 0) - (a.total_seconds || 0);
    if (boardSort === 'priority') {
      const weights = { high: 3, normal: 2, low: 1, '': 0 };
      const wa = weights[a.priority || ''] || 0;
      const wb = weights[b.priority || ''] || 0;
      if (wa !== wb) return wb - wa;
    }
    // Default to recent
    return (b.last_seen || 0) - (a.last_seen || 0);
  });

  const statusGroups = PROJECT_STATUS_OPTIONS
    .filter(([value]) => value)
    .filter(([value]) => !hiddenColumns.includes(value))
    .map(([value, label]) => ({
      value,
      label,
      projects: boardProjects.filter(project => (project.status || '') === value),
    }));
  const unsortedProjects = boardProjects.filter(project => !(project.status || ''));
  const hiddenStatusOptions = PROJECT_STATUS_OPTIONS.filter(([value]) => hiddenColumns.includes(value));

  plannerEl.innerHTML = `
    <div class="planner-shell">
      <section class="planner-panel">
        <div class="planner-panel-head">
          <div>
            <div class="planner-panel-title">Today's Focus</div>
          </div>
        </div>
        ${renderFocus()}
      </section>

      <details class="planner-panel" open>
        <summary class="planner-panel-head">
          <div>
            <div class="planner-panel-title">Project Board</div>
          </div>
        </summary>
        <div class="board-toolbar">
          <input type="text" class="board-search" id="boardSearchInput" placeholder="Search projects…" value="${escapeHtml(boardSearchQuery)}">
          <div class="board-filters">
            ${PROJECT_TYPE_OPTIONS.filter(([val]) => val).map(([val, label]) => `
              <button type="button" class="board-filter-chip ${boardTypeFilter === val ? 'is-active' : ''}" data-filter-type="${val}">
                ${escapeHtml(label)}
              </button>
            `).join('')}
            ${boardTypeFilter ? `<button type="button" class="board-filter-chip" data-filter-type="">Clear</button>` : ''}
          </div>
          <select class="board-sort-control" id="boardSortControl">
            <option value="recent" ${boardSort === 'recent' ? 'selected' : ''}>Recent first</option>
            <option value="name" ${boardSort === 'name' ? 'selected' : ''}>Name A&rarr;Z</option>
            <option value="time" ${boardSort === 'time' ? 'selected' : ''}>Most time</option>
            <option value="priority" ${boardSort === 'priority' ? 'selected' : ''}>Priority</option>
          </select>
          ${hiddenStatusOptions.length ? `
            <details class="hidden-lanes-control">
              <summary>Hidden lanes</summary>
              <div class="hidden-lanes-menu">
                ${hiddenStatusOptions.map(([value, label]) => `<button class="hidden-lane-chip" type="button" data-show-hidden-lane="${value}">Show ${escapeHtml(label)}</button>`).join('')}
              </div>
            </details>
          ` : ''}
        </div>
        <div class="planner-board">
          ${unsortedProjects.length ? renderBoardColumn({ value: '', label: 'Unsorted', projects: unsortedProjects }) : ''}
          ${statusGroups.map(renderBoardColumn).join('')}
        </div>
      </details>
    </div>
  `;
  bindProjectMetaControls();
  bindProjectTaskTriggers();
  bindCategoryTriggers();
  bindPlannerGoals();
  bindProjectBoard(data);
}

function bindProjectBoard(data) {
  const searchInput = document.getElementById('boardSearchInput');
  const filterChips = document.querySelectorAll('[data-filter-type]');
  const sortControl = document.getElementById('boardSortControl');
  
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      boardSearchQuery = searchInput.value;
      renderPlanner(data);
      // Re-focus the input after re-render so typing isn't interrupted
      const newSearchInput = document.getElementById('boardSearchInput');
      if (newSearchInput) {
        newSearchInput.focus();
        const len = newSearchInput.value.length;
        newSearchInput.setSelectionRange(len, len);
      }
    });
  }
  
  filterChips.forEach(chip => {
    chip.addEventListener('click', () => {
      boardTypeFilter = chip.dataset.filterType;
      renderPlanner(data);
    });
  });

  if (sortControl) {
    sortControl.addEventListener('change', () => {
      localStorage.setItem('boardSortKey', sortControl.value);
      renderPlanner(data);
    });
  }

  document.querySelectorAll('[data-open-project]').forEach(trigger => {
    trigger.addEventListener('click', event => {
      const pinButton = event.target.closest('[data-pin-project]');
      const categoryButton = event.target.closest('[data-category-trigger]');
      if (pinButton || categoryButton) return;
      const projectName = decodeURIComponent(trigger.dataset.openProject || '');
      if (projectName) openProjectTasks(projectName);
    });
  });

  document.querySelectorAll('[data-pin-project]').forEach(button => {
    button.addEventListener('click', async event => {
      event.stopPropagation();
      const projectName = decodeURIComponent(button.dataset.pinProject || '');
      const project = (data.projects || []).find(p => p.project_name === projectName);
      if (!project) return;
      button.disabled = true;
      try {
        await updateProjectPinned(project, !project.pinned);
        toast(project.pinned ? `Unpinned ${projectName}` : `Pinned ${projectName}`);
        load();
      } catch (error) {
        toast(error.message || 'Failed to update pin');
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll('[data-show-hidden-lane]').forEach(button => {
    button.addEventListener('click', () => {
      const status = button.dataset.showHiddenLane;
      let hidden;
      try {
        hidden = JSON.parse(localStorage.getItem('boardHiddenColumns') || '["finished","paused","abandoned"]');
      } catch(err) {
        hidden = ['finished', 'paused', 'abandoned'];
      }
      hidden = hidden.filter(value => value !== status);
      localStorage.setItem('boardHiddenColumns', JSON.stringify(hidden));
      renderPlanner(data);
    });
  });

  document.querySelectorAll('[data-hide-column]').forEach(button => {
    button.addEventListener('click', event => {
      event.stopPropagation();
      const status = button.dataset.hideColumn;
      let hidden;
      try {
        hidden = JSON.parse(localStorage.getItem('boardHiddenColumns') || '["finished","paused","abandoned"]');
      } catch(err) {
        hidden = ['finished', 'paused', 'abandoned'];
      }
      if (!hidden.includes(status)) hidden.push(status);
      localStorage.setItem('boardHiddenColumns', JSON.stringify(hidden));
      renderPlanner(data);
    });
  });

  document.querySelectorAll('[data-toggle-column]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const col = btn.dataset.toggleColumn;
      let collapsed;
      try {
        const stored = localStorage.getItem('boardCollapsedColumns');
        collapsed = stored ? JSON.parse(stored) : [];
      } catch(err) {
        collapsed = [];
      }
      if (collapsed.includes(col)) {
        collapsed = collapsed.filter(c => c !== col);
      } else {
        collapsed.push(col);
      }
      localStorage.setItem('boardCollapsedColumns', JSON.stringify(collapsed));
      renderPlanner(data);
    });
  });

  let dragSrcEl = null;
  document.querySelectorAll('.planner-board-project[draggable]').forEach(card => {
    card.addEventListener('dragstart', (e) => {
      dragSrcEl = card;
      card.classList.add('is-dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', card.dataset.projectName);
    });
    card.addEventListener('dragend', () => {
      card.classList.remove('is-dragging');
      document.querySelectorAll('.planner-board-column').forEach(col => {
        col.classList.remove('drag-over');
      });
    });
  });

  document.querySelectorAll('.planner-board-column').forEach(col => {
    col.addEventListener('dragover', (e) => {
      if (e.preventDefault) { e.preventDefault(); }
      e.dataTransfer.dropEffect = 'move';
      col.classList.add('drag-over');
      return false;
    });
    col.addEventListener('dragenter', (e) => {
      col.classList.add('drag-over');
    });
    col.addEventListener('dragleave', (e) => {
      if (!col.contains(e.relatedTarget)) {
        col.classList.remove('drag-over');
      }
    });
    col.addEventListener('drop', async (e) => {
      if (e.stopPropagation) e.stopPropagation();
      col.classList.remove('drag-over');
      
      const projectName = e.dataTransfer.getData('text/plain');
      const newStatus = col.dataset.plannerStatus;
      
      if (dragSrcEl && projectName) {
        const currentStatus = dragSrcEl.dataset.currentStatus || '';
        if (currentStatus !== newStatus) {
          const project = (data.projects || []).find(p => p.project_name === decodeURIComponent(projectName));
          if (project) {
            try {
              if (isTerminalProjectStatus(newStatus)) {
                const ok = await confirmTerminalProjectMove(decodeURIComponent(projectName), newStatus);
                if (!ok) return false;
              }
              toast(`Moving ${decodeURIComponent(projectName)}...`);
              await postJson('/api/project-metadata', {
                project_name: decodeURIComponent(projectName),
                status: newStatus,
                type: project.type || '',
                priority: project.priority || '',
                due_date: project.due_date || '',
                pinned: !!project.pinned,
                project_note: project.project_note || '',
              });
              load();
            } catch (error) {
              toast(error.message || 'Failed to move project');
            }
          }
        }
      }
      return false;
    });
  });
}

function render(data) {
  if (detailModalAbort) {
    detailModalAbort.abort();
    detailModalAbort = null;
  }
  if (data.error) {
    const errHtml = `<div class="empty"><p>${escapeHtml(data.error)}</p><small>run start_tracker.command</small></div>`;
    const dashEl = document.getElementById('appDashboard');
    const plannerEl = document.getElementById('appPlanner');
    const setEl  = document.getElementById('appSettings');
    if (dashEl) dashEl.innerHTML = errHtml;
    if (plannerEl) plannerEl.innerHTML = errHtml;
    if (setEl)  { setEl.innerHTML = errHtml; setEl.dataset.rendered = 'true'; }
    return;
  }
  syncCategoryOptions(data.category_options);
  syncProjectOptions(data.project_status_options, data.project_type_options);
  const { summary, projects, year_daily = [], year_hourly = [], recent } = data;
  if (summary.week_start_weekday != null) {
    WEEK_START_BACKEND_DAY = summary.week_start_weekday;
  }
  updateMonthNav(summary);

  // New data invalidates any previously-rendered settings view.
  // It will be re-rendered next time the user switches to it.
  const setEl = document.getElementById('appSettings');
  if (setEl) setEl.dataset.rendered = '';

  // If user is currently on settings, refresh it now so the visible data stays fresh.
  if (activeView === 'settings') {
    renderSettings(data);
    // fall through and also refresh the (hidden) dashboard so the next switch is instant
  }
  if (activeView === 'planner') {
    renderPlanner(data);
  }

  updateSessionStatus(summary);

  if (summary.live_project) {
    document.getElementById('liveBadge').style.display = 'inline-flex';
    document.getElementById('liveProject').textContent = summary.live_project;
  } else {
    document.getElementById('liveBadge').style.display = 'none';
  }

  const unsavedCount = summary.unsaved_closed_count || 0;
  const closedSessionCount = summary.closed_session_count || 0;
  const phantomCount = summary.phantom_closed_count || 0;
  const todaySessionCount = summary.today_session_count || 0;
  const todayProjectCount = summary.today_project_count || 0;

  const selectedMonth = selectedMonthDate(summary);
  const monthName = selectedMonthLabel(summary);
  const monthSub = summary.selected_month_is_current ? 'this month' : monthName;
  const monthlyProjects = monthProjectRows(projects);
  const monthlyProjectCount = summary.month_project_count || monthlyProjects.length;
  const pace = lastMonthPace(year_daily, selectedMonth);
  let paceChip;
  if (!pace) {
    paceChip = `<div class="card-chip">— vs last month</div>`;
  } else {
    const sign = pace.pct > 0 ? '+' : '';
    const cls = pace.positive ? ' card-chip--good' : '';
    paceChip = `<div class="card-chip${cls}">${sign}${pace.pct}% vs last month</div>`;
  }

  const best = longestStreak(year_daily);
  const cur = summary.streak_days || 0;

  // Top project this month (uses month_seconds field added server-side)
  const topProject = monthlyProjects[0];
  const topProjectShare = topProject && summary.month_seconds > 0
    ? Math.round((topProject.month_seconds / summary.month_seconds) * 100)
    : 0;

  // ── Streak ──
  const isRecord = cur > 0 && cur >= best;
  const pbIconClass = isRecord ? 'pb-icon is-record' : 'pb-icon';
  const pbIconLabel = isRecord ? 'NEW' : 'PB';
  
  const streakTrackDays = [];
  const todayDate = new Date();
  todayDate.setHours(12, 0, 0, 0);
  const startTrack = addDays(todayDate, -6);
  for (let i = 0; i < 7; i++) {
    const d = addDays(startTrack, i);
    const key = localDateKey(d);
    const hasActivity = year_daily.some(x => x.day === key && (x.total_seconds || 0) > 0);
    const dayName = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()];
    streakTrackDays.push({ dayName, hasActivity });
  }

  const streakTrackHTML = `
    <div class="streak-mini-track">
      <div class="streak-mini-bars">
        ${streakTrackDays.map(d => `<div class="streak-mini-bar ${d.hasActivity ? 'is-active' : ''}"></div>`).join('')}
      </div>
      <div class="streak-mini-labels">
        ${streakTrackDays.map(d => `<span>${d.dayName}</span>`).join('')}
      </div>
    </div>
  `;

  document.getElementById('appDashboard').innerHTML = `
    <div class="cards">
      <div class="card card--streak" role="button" tabindex="0" data-detail="streak">
        <div class="streak-card-top">
          <div class="streak-card-main">
            <div class="card-label" style="padding-top: 0; margin-bottom: 4px;">Current Streak</div>
            <div class="card-value">${cur}<span class="unit">day${cur !== 1 ? 's' : ''}</span></div>
          </div>
          <div class="streak-card-pb">
            <div class="${pbIconClass}">${pbIconLabel}</div>
            <div class="pb-label">Personal Best</div>
            <div class="pb-value">${best} Day${best !== 1 ? 's' : ''}</div>
          </div>
        </div>
        ${streakTrackHTML}
      </div>
      <div class="card" role="button" tabindex="0" data-detail="top-project">
        ${topProject
          ? `<div class="card-value card-value--text" title="${escapeHtml(topProject.project_name)}">${escapeHtml(topProject.project_name)}</div>
             <div class="card-chip">${fmt.dur(topProject.month_seconds)} · ${topProjectShare}% of month</div>
             <div class="card-sub"><span class="proj-dot" style="background:${projectColor(topProject, 0)}"></span>${topProject.category_label || 'Uncategorized'}</div>`
          : `<div class="card-value">—</div>
             <div class="card-chip">No projects yet</div>
             <div class="card-sub">${monthSub}</div>`}
        <div class="card-label">Top Project</div>
      </div>
      <div class="card" role="button" tabindex="0" data-detail="this-month">
        <div class="card-value accent">${fmt.dur(summary.month_seconds)}</div>
        ${paceChip}
        <div class="card-sub">${monthlyProjectCount} project${monthlyProjectCount !== 1 ? 's' : ''} · ${monthSub}</div>
        <div class="card-label">${summary.selected_month_is_current ? 'This Month' : 'Month'}</div>
      </div>
    </div>

    <div class="rings-row">
      <div class="chart-card target-card pace-card" role="button" tabindex="0" data-detail="weekly-target" aria-haspopup="dialog" aria-controls="detailModal">
        <div class="section-head daily-target-head">
          <h3 class="section-title">Weekly <em>Pace</em></h3>
          <div class="daily-target-head-side">
            <div class="section-meta">
              <span id="weeklyTargetRangeLabel">Current Week</span>
            </div>
          </div>
        </div>
        <div class="chart-wrap"><div id="weeklyGoalCard" class="daily-target-panel"></div></div>
      </div>
      <div class="chart-card target-card pace-card" role="button" tabindex="0" data-detail="weekly-target" aria-haspopup="dialog" aria-controls="detailModal">
        <div class="section-head daily-target-head">
          <h3 class="section-title">Today's <em>Required</em></h3>
          <div class="daily-target-head-side">
            <div class="daily-target-nav" aria-label="Weekly target date navigation">
              <button class="btn small weekly-target-current-btn" id="weeklyTargetCurrentBtn" type="button" hidden>Current Week</button>
              <button class="daily-target-nav-btn" id="weeklyTargetPrev" type="button" aria-label="Show previous week">
                <svg viewBox="0 0 14 14" aria-hidden="true">
                  <path d="M8.75 2.5L4.25 7l4.5 4.5"></path>
                </svg>
              </button>
              <button class="daily-target-nav-btn" id="weeklyTargetNext" type="button" aria-label="Show next week">
                <svg viewBox="0 0 14 14" aria-hidden="true">
                  <path d="M5.25 2.5L9.75 7l-4.5 4.5"></path>
                </svg>
              </button>
            </div>
            <div class="section-meta">
              <span id="weeklyRequiredContext">This week</span>
            </div>
          </div>
        </div>
        <div class="chart-wrap"><div id="dailyGoalCard" class="daily-target-panel"></div></div>
      </div>
    </div>

    <div class="chart-card chart-card-wide">
      <div class="section-head">
        <h3 class="section-title">Activity</h3>
        <span class="section-meta" id="activityHeatmapWeekLabel2">Weekly rhythm</span>
      </div>
      <div class="chart-wrap"><div id="activityHeatmap"></div></div>
    </div>

    <div class="chart-card chart-card-wide">
      <div class="section-head">
        <h3 class="section-title">By <em>category</em></h3>
        <span class="section-meta">hours in ${escapeHtml(monthName)}</span>
      </div>
      <div class="chart-wrap"><div id="categoryChart"></div></div>
    </div>

    <div class="table-card">
      <div class="section-head">
        <h3 class="section-title">Projects</h3>
      </div>
      ${projects.length === 0
        ? '<div class="empty"><p>No projects yet</p><small>waiting for a live session</small></div>'
        : `<div class="table-scroll"><table>
            <thead><tr>
              <th style="width:40px">#</th>
              <th>Project</th>
              <th style="text-align:right">Total</th>
              <th style="text-align:right">Sessions</th>
            </tr></thead>
            <tbody>
              ${projects.map((p,i) => {
                return `
                <tr>
                  <td><span class="rank">${String(i+1).padStart(2,'0')}</span></td>
                  <td>
                    <div class="proj-badge-wrap">
                      ${projectBadge(p, i)}
                    </div>
                  </td>
                  <td class="dur">${fmt.dur(p.total_seconds)}</td>
                  <td class="num">${p.session_count}</td>
                </tr>`;
              }).join('')}
            </tbody>
          </table></div>
          ${data.recent_has_more ? '<div class="table-more"><button class="btn small" id="btnLoadOlderRecent" type="button">Load older entries</button></div>' : ''}`}
    </div>

    <div class="table-card">
      <div class="section-head">
        <h3 class="section-title">Recent <em>entries</em></h3>
        <span class="recent-stats-inline">${fmt.dur(summary.today_average_session_seconds || 0)} avg · ${todaySessionCount} session${todaySessionCount !== 1 ? 's' : ''} · ${todayProjectCount} project${todayProjectCount !== 1 ? 's' : ''} today</span>
      </div>
      ${recent.length === 0
        ? '<div class="empty"><p>No sessions logged</p><small>start Ableton to begin</small></div>'
        : `<div class="table-scroll"><table>
            <thead><tr>
              <th>Project</th><th>Started</th><th>Ended</th><th style="text-align:right">Duration</th><th aria-label="Delete"></th>
            </tr></thead>
            <tbody>
              ${recent.map(s => {
                const ids = (s.session_ids || []).join(',');
                const isLive = !s.end_time;
                const delTitle = isLive
                  ? 'Cannot delete the live session'
                  : 'Delete this entry';
                return `
                <tr>
                  <td>
                    <div class="proj-badge-wrap">
                      ${projectBadge(s)}
                      ${s.category_key ? categoryPill(s) : ''}
                    </div>
                  </td>
                  <td class="num" style="text-align:left">${fmt.datetime(s.start_time)}</td>
                  <td class="num" style="text-align:left">${s.end_time
                    ? fmt.datetime(s.end_time)
                    : '<span class="active-tag"><span class="dot"></span>active now</span>'}</td>
                  <td class="dur">${fmt.dur(s.active_seconds)}</td>
                  <td class="row-action">
                    ${(() => {
                      const notesData = JSON.stringify(s.session_notes || {});
                      const todosData = JSON.stringify(s.session_todos || {});
                      const timesData = JSON.stringify(s.session_start_times || {});
                      const endTimesData = JSON.stringify(s.session_end_times || {});
                      const lastSeenData = JSON.stringify(s.session_last_seen_times || {});
                      const activeSecondsData = JSON.stringify(s.session_active_seconds || {});
                      const hasNotes = Object.keys(s.session_notes || {}).length > 0;
                      return `<button class="row-note${hasNotes ? ' has-notes' : ''}" type="button"
                        data-session-ids="${ids}"
                        data-session-notes="${encodeURIComponent(notesData)}"
                        data-session-todos="${encodeURIComponent(todosData)}"
                        data-session-start-times="${encodeURIComponent(timesData)}"
                        data-session-end-times="${encodeURIComponent(endTimesData)}"
                        data-session-last-seen-times="${encodeURIComponent(lastSeenData)}"
                        data-session-active-seconds="${encodeURIComponent(activeSecondsData)}"
                        data-project-name="${encodeURIComponent(s.project_name || '')}"
                        data-project-note="${encodeURIComponent(s.project_note || '')}"
                        title="${hasNotes ? 'Edit notes' : 'Add notes'}">✎</button>`;
                    })()}
                    <button class="row-del" type="button"
                      data-session-ids="${ids}"
                      data-project-name="${encodeURIComponent(s.project_name || '')}"
                      title="${delTitle}"
                      ${isLive || !ids ? 'disabled' : ''}>×</button>
                  </td>
                </tr>`;
              }).join('')}
            </tbody>
          </table></div>`}
    </div>

    <div class="data-actions">
      <button class="btn small" id="btnClearUnsaved" ${unsavedCount === 0 ? 'disabled' : ''}>Clear unsaved${unsavedCount > 0 ? ` · ${unsavedCount}` : ''}</button>
      <button class="btn small" id="btnClearPhantoms" ${phantomCount === 0 ? 'disabled' : ''}>Clean phantom logs${phantomCount > 0 ? ` · ${phantomCount}` : ''}</button>
      <button class="btn small" id="btnClearRecent" ${closedSessionCount === 0 ? 'disabled' : ''}>Clear logs</button>
    </div>

    <div class="detail-modal-backdrop" id="detailModal" aria-hidden="true">
      <div class="modal detail-modal" id="detailModalPanel" role="dialog" aria-modal="true" aria-labelledby="detailModalTitle">
        <div class="detail-modal-head">
          <div>
            <h3 class="detail-modal-title" id="detailModalTitle"></h3>
            <div class="detail-modal-subtitle" id="detailModalSubtitle"></div>
          </div>
          <button class="detail-modal-close" id="detailModalClose" type="button" aria-label="Close detail view">×</button>
        </div>
        <div class="detail-modal-body" id="detailModalBody"></div>
      </div>
    </div>
  `;

  const br = document.getElementById('btnClearRecent');
  const bp = document.getElementById('btnClearPhantoms');
  const bu = document.getElementById('btnClearUnsaved');
  const bo = document.getElementById('btnLoadOlderRecent');
  if (br) br.addEventListener('click', clearRecent);
  if (bp) bp.addEventListener('click', clearPhantoms);
  if (bu) bu.addEventListener('click', clearUnsaved);
  if (bo) bo.addEventListener('click', loadOlderRecent);
  bindCategoryTriggers();
  bindProjectMetaControls();
  bindProjectTaskTriggers();
  bindRowDeleteTriggers();
  bindNotesTriggers();
  bindDetailCards({ summary, projects, year_daily });

  renderWeeklyRhythm(year_daily, year_hourly);
  renderWeeklyGoal(summary, year_daily);
  renderCategoryChart(projects, summary);
}

function updateSessionStatus(summary) {
  const status = document.getElementById('sessionStatus');
  const label = document.getElementById('sessionStatusText');
  const indicator = document.getElementById('sessionStatusIndicator');
  if (!status || !label || !indicator) return;

  let state = 'off';
  let text = 'Off';
  let indicatorHtml = '<span class="session-status__dot"></span>';

  if (summary?.ableton_has_project) {
    state = 'live';
    text = 'Live';
    indicator.className = 'session-status__indicator session-status__indicator--live';
    indicatorHtml = '<span class="session-status__dot"></span>';
  } else if (summary?.ableton_running) {
    state = 'booting';
    text = 'Booting up';
    indicator.className = 'session-status__indicator session-status__indicator--booting';
    indicatorHtml = `
      <span class="session-status__dot"></span>
      <span class="session-status__dot"></span>
      <span class="session-status__dot"></span>
    `;
  } else {
    state = 'off';
    text = 'Off';
    indicator.className = 'session-status__indicator session-status__indicator--off';
    indicatorHtml = '<span class="session-status__dot"></span>';
  }

  status.dataset.state = state;
  label.textContent = text;
  indicator.innerHTML = indicatorHtml;
}

function renderActivityHeatmap(yearDaily, yearHourly, targetMount = null) {
  const mount = targetMount || document.getElementById('activityHeatmap');
  if (!mount) return;

  const { activityByDay, activityByDayHour } = buildActivityMaps(yearDaily, yearHourly);

  const today = new Date();
  today.setHours(12, 0, 0, 0);
  const start = addDays(today, -364);
  const todayKey = localDateKey(today);
  const weeks = [];
  let cursor = new Date(startOfWeek(start));
  const lastWeekStart = startOfWeek(today);

  while (cursor <= lastWeekStart) {
    const days = [];
    for (let index = 0; index < 7; index++) {
      const day = addDays(cursor, index);
      const key = localDateKey(day);
      const inRange = day >= start && day <= today;
      const isUpcoming = day > today;
      const value = inRange ? Math.round((activityByDay[key] || 0) * 10) / 10 : 0;
      days.push({ index, key, date: day, inRange, isUpcoming, value, isToday: key === todayKey });
    }
    if (days.some(d => d.inRange)) {
      weeks.push({ startKey: localDateKey(days[0].date), endKey: localDateKey(days[6].date), days });
    }
    cursor = addDays(cursor, 7);
  }

  if (weeks.length === 0) {
    mount.innerHTML = `<div class="chart-empty">No ${weekStartDayName()}-to-${weekEndDayName()} weeks to show yet.</div>`;
    return;
  }

  let currentWeekIndex = weeks.length - 1;

  const hourLabel = h => {
    if (h === 0)  return '12am';
    if (h === 12) return '12pm';
    return h < 12 ? `${h}am` : `${h - 12}pm`;
  };

  // Y axis: 24 labels top-to-bottom (h=23 → h=0), text only every 3 hours
  const yAxisHTML = Array.from({ length: 24 }, (_, i) => {
    const h = 23 - i;
    const show = h % 3 === 0;
    return `<div class="heatmap-yaxis-label">${show ? hourLabel(h) : ''}</div>`;
  }).join('');

  mount.innerHTML = `
    <div class="heatmap-shell">
      <div class="heatmap-meta">
        <div class="heatmap-summary" id="activityHeatmapSummary"></div>
        <div class="heatmap-controls">
          <div class="heatmap-scale">
            <span>0 min</span>
            <div class="heatmap-scale-row">
              ${[0, 0.2, 0.45, 0.7, 1].map(level => `
                <span class="heatmap-swatch" style="background:${heatmapColor(level * 3600, 3600)}"></span>
              `).join('')}
            </div>
            <span>60 min</span>
          </div>
          <div class="heatmap-nav">
            <button class="heatmap-nav-btn" id="heatmapPrevWeek" type="button" aria-label="Show previous week">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M8.75 2.5L4.25 7l4.5 4.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
            <button class="heatmap-nav-btn" id="heatmapNextWeek" type="button" aria-label="Show next week">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M5.25 2.5L9.75 7l-4.5 4.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
          </div>
        </div>
      </div>
      <div class="heatmap-week-label" id="activityHeatmapWeekLabel"></div>
      <div class="heatmap-stage" id="activityHeatmapStage"></div>
      <div class="heatmap-foot" id="activityHeatmapFoot"></div>
    </div>
  `;

  const summaryEl  = mount.querySelector('#activityHeatmapSummary');
  const weekLabelEl = mount.querySelector('#activityHeatmapWeekLabel');
  const stageEl    = mount.querySelector('#activityHeatmapStage');
  const footEl     = mount.querySelector('#activityHeatmapFoot');
  const prevBtn    = mount.querySelector('#heatmapPrevWeek');
  const nextBtn    = mount.querySelector('#heatmapNextWeek');
  let heatmapResizeObserver = null;

  function renderWeek() {
    const week = weeks[currentWeekIndex];
    const visibleDays = week.days.filter(d => d.inRange);
    const activeDays  = visibleDays.filter(d => d.value > 0).length;
    const totalHours  = Math.round(visibleDays.reduce((s, d) => s + d.value, 0) * 10) / 10;
    const peakDay     = visibleDays.reduce((best, d) => (
      d.value > 0 && (!best || d.value > best.value) ? d : best
    ), null);

    const isCurrentWeek = currentWeekIndex === weeks.length - 1;
    const todayInWeek = week.days.find(d => d.isToday);
    const todayHasActivity = todayInWeek && todayInWeek.value > 0;

    let message = '';
    if (activeDays === 0) {
      message = '';
    } else if (activeDays === visibleDays.length) {
      message = '';
    } else if (isCurrentWeek && todayInWeek && !todayHasActivity) {
      message = '';
    } else {
      message = '';
    }

    const weekDotsHTML = visibleDays.map(d => {
      const active = d.value > 0 && !d.isUpcoming;
      const today = d.isToday;
      const cls = ['heatmap-week-dot', active ? 'is-active' : '', today ? 'is-today' : ''].filter(Boolean).join(' ');
      const title = d.isUpcoming ? 'Upcoming' : active ? `${formatHoursNumber(d.value)}h` : 'No activity';
      return `<div class="${cls}" title="${escapeHtml(title)}"></div>`;
    }).join('');

    summaryEl.innerHTML = `
      <strong>${activeDays} active day${activeDays === 1 ? '' : 's'}</strong>
      <div class="heatmap-week-dots">${weekDotsHTML}</div>
      <span>${formatHoursNumber(totalHours)}h this week</span>
      <span class="heatmap-message">${message}</span>
    `;
    weekLabelEl.textContent = `${shortRange(week.startKey, week.endKey)}${currentWeekIndex === weeks.length - 1 ? ' · current week' : ''}`;

    const headersHTML = week.days.map(day => {
      const cls = ['heatmap-day-head', day.isToday ? 'is-today' : ''].filter(Boolean).join(' ');
      const dailyTotal = day.isUpcoming ? '' : day.value > 0 ? `${formatHoursNumber(day.value)}h` : '';
      const todayIndicator = day.isToday ? '<span class="today-indicator"></span>' : '';
      return `
        <div class="${cls}">
          <strong>${day.date.toLocaleDateString('en-US', { weekday:'short' })}</strong>
          <span>${day.date.toLocaleDateString('en-US', { month:'short', day:'numeric' })}</span>
          ${todayIndicator}
          <span class="day-total">${dailyTotal}</span>
        </div>
      `;
    }).join('');

    // Rows: h=23 at top → h=0 at bottom (night at top, morning at bottom)
    const rowsHTML = Array.from({ length: 24 }, (_, i) => {
      const h = 23 - i;
      const cells = week.days.map(day => {
        if (!day.inRange) return `<div class="heatmap-cell"></div>`;
        const secs = Math.min(activityByDayHour[`${day.key}_${h}`] || 0, 3600);
        const mins = Math.round(secs / 60);
        const label = day.date.toLocaleDateString('en-US', { weekday:'short', month:'short', day:'numeric' });
        const title = secs > 0 ? `${label} ${hourLabel(h)}: ${mins}m active` : `${label} ${hourLabel(h)}: no activity`;
        return `<div class="heatmap-cell" title="${escapeHtml(title)}"></div>`;
      }).join('');
      return `<div class="heatmap-grid-row">${cells}</div>`;
    }).join('');

    stageEl.innerHTML = `
      <div class="heatmap-grid-wrap">
        <div class="heatmap-yaxis">${yAxisHTML}</div>
        <div class="heatmap-grid-body">
          <div class="heatmap-grid-headers">${headersHTML}</div>
          <div class="heatmap-grid-rows">
            <canvas class="heatmap-canvas"></canvas>
            ${rowsHTML}
          </div>
        </div>
      </div>
    `;

    const canvas = stageEl.querySelector('.heatmap-canvas');
    const gridRows = stageEl.querySelector('.heatmap-grid-rows');
    const paint = () => paintHeatmapCanvas(canvas, gridRows, week.days, activityByDayHour);
    const schedulePaint = debounce(() => requestAnimationFrame(paint), 120);
    requestAnimationFrame(paint);
    if (heatmapResizeObserver) heatmapResizeObserver.disconnect();
    heatmapResizeObserver = new ResizeObserver(schedulePaint);
    heatmapResizeObserver.observe(gridRows);

    let footHTML = '';
    if (peakDay) {
      footHTML = `Peak this week: <strong>${peakDay.date.toLocaleDateString('en-US', { weekday:'short', month:'short', day:'numeric' })}</strong> with ${formatHoursNumber(peakDay.value)}h.`;
      // Find most productive hour of the week
      let peakHour = -1, peakHourSecs = 0;
      for (let h = 0; h < 24; h++) {
        const hourTotal = week.days.reduce((sum, d) => sum + (activityByDayHour[`${d.key}_${h}`] || 0), 0);
        if (hourTotal > peakHourSecs) {
          peakHourSecs = hourTotal;
          peakHour = h;
        }
      }
      if (peakHour >= 0 && peakHourSecs > 0) {
        footHTML += ` Most productive time: <strong>${hourLabel(peakHour)}</strong>.`;
      }
    } else {
      footHTML = '';
    }
    footEl.innerHTML = footHTML;

    prevBtn.disabled = currentWeekIndex === 0;
    nextBtn.disabled = currentWeekIndex === weeks.length - 1;
  }

  prevBtn.addEventListener('click', () => {
    if (currentWeekIndex === 0) return;
    currentWeekIndex -= 1;
    renderWeek();
  });
  nextBtn.addEventListener('click', () => {
    if (currentWeekIndex === weeks.length - 1) return;
    currentWeekIndex += 1;
    renderWeek();
  });

  renderWeek();
}

function renderGoalCard({
  mountId,
  storageKey,
  fallbackGoalHours,
  completedSeconds,
  rangeLabel,
  goalLabel,
  helperLabel,
  remainingId,
  presets,
}) {
  const mount = document.getElementById(mountId);
  if (!mount) return;

  const completedHours = Math.round(((completedSeconds || 0) / 3600) * 10) / 10;
  const remainingMount = remainingId ? document.getElementById(remainingId) : null;
  const coldColor = '#a05050';
  const coolColor = '#c89430';
  const warmColor = '#7bc85a';
  const hotColor = '#30d158';

  function lerpColor(a, b, amount) {
    const parse = color => [1, 3, 5].map(start => parseInt(color.slice(start, start + 2), 16));
    const from = parse(a);
    const to = parse(b);
    return '#' + from.map((part, index) => {
      const value = Math.round(part + (to[index] - part) * amount);
      return value.toString(16).padStart(2, '0');
    }).join('');
  }

  function ringColorStops(progressClamped) {
    if (progressClamped <= 1 / 3) {
      const endColor = lerpColor(coldColor, coolColor, progressClamped * 3);
      return { cool: endColor, warm: endColor, hot: endColor };
    }
    if (progressClamped <= 2 / 3) {
      const endColor = lerpColor(coolColor, warmColor, (progressClamped - 1 / 3) * 3);
      return { cool: coolColor, warm: endColor, hot: endColor };
    }
    const endColor = lerpColor(warmColor, hotColor, (progressClamped - 2 / 3) * 3);
    return { cool: coolColor, warm: warmColor, hot: endColor };
  }

  function remainingGoalLabel(goalHours) {
    const goalSeconds = goalHours * 3600;
    const deltaSeconds = Math.round(goalSeconds - (completedSeconds || 0));
    if (deltaSeconds <= 0) {
      const overSeconds = Math.abs(deltaSeconds);
      return overSeconds > 0
        ? `Goal met · ${fmt.dur(overSeconds)} extra`
        : 'Goal met right on target';
    }
    return `${fmt.dur(deltaSeconds)} left to goal`;
  }

  function paint(goalHours) {
    const safeGoalHours = clamp(Math.round(goalHours * 10) / 10, 1, 100);
    const progress = completedHours / safeGoalHours;
    const progressClamped = clamp(progress, 0, 1);
    const progressDegrees = Math.round(progressClamped * 360);
    const progressMidDegrees = Math.min(progressDegrees, 120);
    const progressHotDegrees = Math.min(progressDegrees, 240);
    const ringStops = ringColorStops(progressClamped);
    const percent = Math.round(progress * 100);
    const percentLabel = `${Math.min(percent, 999)}%`;
    const helperMarkup = helperLabel ? `<span>${helperLabel}</span>` : '';
    if (remainingMount) {
      remainingMount.textContent = remainingGoalLabel(safeGoalHours);
    }

    mount.innerHTML = `
      <div class="goal-shell">
        <div class="goal-ring-wrap">
          <div class="goal-ring ${progress >= 1 ? 'is-complete' : ''}" style="--goal-progress:${progressDegrees}deg;--goal-progress-mid:${progressMidDegrees}deg;--goal-progress-hot:${progressHotDegrees}deg;--goal-cool:${ringStops.cool};--goal-warm:${ringStops.warm};--goal-hot:${ringStops.hot}" aria-label="${percentLabel} complete">
            <div class="goal-ring-core">
              <div class="goal-value">${percentLabel}</div>
            </div>
          </div>
        </div>
        <div class="goal-controls">
          <div class="goal-range">${rangeLabel}</div>
          <div class="goal-input-row">
            <label class="goal-label" for="${mountId}Input">
              <strong>${goalLabel}</strong>
              ${helperMarkup}
            </label>
            <div class="goal-input-wrap">
              <input class="goal-input" id="${mountId}Input" type="number" min="1" max="100" step="0.5" value="${safeGoalHours}">
              <span class="goal-unit">hours</span>
            </div>
          </div>
          <div class="goal-presets">
            ${presets.map(hours => `
              <button class="goal-chip" type="button" data-goal-hours="${hours}">${hours}h</button>
            `).join('')}
          </div>
        </div>
      </div>
    `;

    const input = mount.querySelector(`#${mountId}Input`);
    if (input) {
      const commit = () => {
        const nextValue = clamp(Number(input.value) || safeGoalHours, 1, 100);
        setStoredGoalHours(storageKey, nextValue);
        paint(nextValue);
      };
      input.addEventListener('change', commit);
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter') commit();
      });
    }

    mount.querySelectorAll('[data-goal-hours]').forEach(button => {
      button.addEventListener('click', () => {
        const nextValue = Number(button.dataset.goalHours);
        setStoredGoalHours(storageKey, nextValue);
        paint(nextValue);
      });
    });
  }

  paint(getStoredGoalHours(storageKey, fallbackGoalHours));
}

function currentWeekStartKey() {
  return localDateKey(startOfWeek(new Date()));
}

function updateWeeklyTargetMeta({ weekStart }) {
  const rangeLabel = document.getElementById('weeklyTargetRangeLabel');
  const currentBtn = document.getElementById('weeklyTargetCurrentBtn');
  if (rangeLabel) rangeLabel.textContent = weekStart === currentWeekStartKey() ? 'Current Week' : 'Selected Week';
  if (currentBtn) currentBtn.hidden = weekStart === currentWeekStartKey();
}

function renderWeeklyTargetCard(target) {
  const mount = document.getElementById('weeklyGoalCard');
  const requiredMount = document.getElementById('dailyGoalCard');
  if (!mount) return;

  const model = buildWeeklyTargetModel(target);
  const progressPct = model.hasGoal ? clamp(Math.round(model.progressRatio * 100), 0, 100) : 0;
  const progressLabel = model.hasGoal ? `${Math.min(model.percent, 999)}%` : 'No goal';
  const goalLabel = model.hasGoal ? `${formatHoursNumber(model.goalHours)}h` : 'Goal';
  const editGoalHours = model.hasGoal ? model.goalHours : 20;
  const projectedLabel = model.hasGoal ? fmt.dur(model.projectedSeconds) : '--';
  const remainingLabel = model.hasGoal ? fmt.dur(model.shortfallSeconds) : '--';
  const requiredLabel = model.hasGoal ? fmt.dur(model.requiredPerDaySeconds) : '--';
  const requiredContext = document.getElementById('weeklyRequiredContext');
  const requiredHeadline = !model.hasGoal
    ? 'Set a weekly goal'
    : model.isPastWeek
      ? 'Final result'
      : model.isFutureWeek
        ? 'Starts soon'
        : model.isMet
          ? '0m'
          : requiredLabel;
  const requiredCopy = !model.hasGoal
    ? 'Add a weekly target to turn progress into a daily pace.'
    : model.isPastWeek
      ? `${model.progressText} logged.`
      : model.isFutureWeek
        ? `Week starts ${shortDate(model.weekStart)}.`
        : model.isMet
          ? 'Weekly goal met. Extra time is optional.'
          : `needed per remaining day · ${model.remainingDays} day${model.remainingDays === 1 ? '' : 's'} left`;

  updateWeeklyTargetMeta({
    weekStart: model.weekStart,
    weekEnd: model.weekEnd,
  });
  if (requiredContext) {
    requiredContext.textContent = weekStartDayName();
  }

  mount.innerHTML = `
    <div class="pace-panel" style="--pace-accent:${model.status.accent};--target-accent:${model.status.accent};--pace-progress:${progressPct}%">
      <div class="pace-hero">
        <div class="pace-kicker">${model.hasGoal ? 'Logged / goal' : 'Logged this week'}</div>
        <div class="pace-value">${fmt.dur(model.progressSeconds)} <small>/ ${goalLabel}</small></div>
        <div class="pace-bar" aria-label="${escapeHtml(progressLabel)} complete">
          <div class="pace-bar-fill"></div>
        </div>
        <div class="pace-subline">
          <span class="pace-status">${escapeHtml(model.status.label)}</span>
          <span>${progressLabel}</span>
        </div>
      </div>
      <div class="pace-stats">
        <div class="pace-stat">
          <span>Projected</span>
          <strong>${projectedLabel}</strong>
        </div>
        <div class="pace-stat">
          <span>Remaining</span>
          <strong>${remainingLabel}</strong>
        </div>
      </div>
      <div class="pace-goal-edit">
        <div class="goal-input-row">
          <label class="goal-label" for="weeklyGoalCardInput">
            <strong>Weekly goal</strong>
          </label>
          <div class="goal-input-wrap">
            <input class="goal-input" id="weeklyGoalCardInput" type="number" min="1" max="100" step="0.5" value="${editGoalHours}">
            <span class="goal-unit">hours</span>
          </div>
        </div>
        <div class="goal-presets">
          ${[10, 20, 30, 40].map(hours => `
            <button class="goal-chip ${Math.abs(editGoalHours - hours) < 0.01 ? 'is-active' : ''}" type="button" data-weekly-goal-hours="${hours}">${hours}h</button>
          `).join('')}
        </div>
      </div>
    </div>
  `;

  if (requiredMount) {
    requiredMount.innerHTML = `
      <div class="pace-panel" style="--pace-accent:${model.status.accent};--target-accent:${model.status.accent}">
        <div class="pace-hero">
          <div class="pace-kicker">${model.isCurrentWeek ? 'Today' : 'Selected week'}</div>
          <div class="pace-value">${requiredHeadline}</div>
          <div class="required-note">${requiredCopy}</div>
        </div>
        ${renderWeekStrip(model)}
        <div class="pace-stats">
          <div class="pace-stat">
            <span>Goal</span>
            <strong>${model.hasGoal ? `${formatHoursNumber(model.goalHours)}h` : '--'}</strong>
          </div>
          <div class="pace-stat">
            <span>${model.isPastWeek ? 'Shortfall' : 'Left'}</span>
            <strong>${model.hasGoal ? fmt.dur(model.shortfallSeconds) : '--'}</strong>
          </div>
        </div>
      </div>
    `;
    requiredMount.classList.remove('is-fresh');
    void requiredMount.offsetWidth;
    requiredMount.classList.add('is-fresh');
  }

  mount.classList.remove('is-fresh');
  void mount.offsetWidth;
  mount.classList.add('is-fresh');

  const input = mount.querySelector('#weeklyGoalCardInput');
  const repaint = async nextValue => {
    const normalized = clamp(Math.round((Number(nextValue) || editGoalHours) * 10) / 10, 1, 100);
    mount.classList.add('is-loading');
    try {
      await postJson('/api/weekly-target', { goal_hours: normalized });
      const res = await fetch(`/api/weekly-target?date=${encodeURIComponent(weeklyTargetState.currentWeekStartDate || currentWeekStartKey())}`);
      const fresh = await res.json();
      renderWeeklyTargetCard(fresh);
    } catch (e) {
      toast(e.message || 'Failed to save weekly target');
      mount.classList.remove('is-loading');
    }
  };
  if (input) {
    input.addEventListener('change', () => repaint(input.value));
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') repaint(input.value);
    });
  }
  mount.querySelectorAll('[data-weekly-goal-hours]').forEach(button => {
    button.addEventListener('click', () => repaint(button.dataset.weeklyGoalHours));
  });
}

async function loadWeeklyTarget(summary = {}, yearDaily = []) {
  const mount = document.getElementById('weeklyGoalCard');
  const requiredMount = document.getElementById('dailyGoalCard');
  if (!mount) return;
  weeklyTargetState.dailyRows = Array.isArray(yearDaily) ? yearDaily : [];
  if (!weeklyTargetState.currentWeekStartDate) {
    weeklyTargetState.currentWeekStartDate = summary.goal_week_start || currentWeekStartKey();
  }
  const weekStart = weeklyTargetState.currentWeekStartDate;
  const weekEnd = localDateKey(addDays(dateFromKey(weekStart), 6));
  updateWeeklyTargetMeta({
    weekStart,
    weekEnd,
    progressSeconds: weekStart === (summary.goal_week_start || '') ? (summary.week_seconds || 0) : 0,
    note: 'Loading target…',
  });
  mount.classList.add('is-loading');
  if (requiredMount) requiredMount.classList.add('is-loading');
  mount.setAttribute('aria-busy', 'true');
  const token = ++weeklyTargetState.requestToken;
  try {
    const res = await fetch(`/api/weekly-target?date=${encodeURIComponent(weekStart)}`);
    const payload = await res.json();
    if (!res.ok || payload.error) throw new Error(payload.error || 'Failed to load weekly target');
    if (token !== weeklyTargetState.requestToken || weeklyTargetState.currentWeekStartDate !== weekStart) return;
    weeklyTargetState.currentWeekStartDate = payload.week_start;
    renderWeeklyTargetCard(payload);
  } catch (error) {
    if (token !== weeklyTargetState.requestToken || weeklyTargetState.currentWeekStartDate !== weekStart) return;
    updateWeeklyTargetMeta({
      weekStart,
      weekEnd,
      progressSeconds: 0,
      note: 'Could not load target',
    });
    mount.innerHTML = '<div class="chart-empty">Unable to load this weekly target right now.</div>';
    if (requiredMount) requiredMount.innerHTML = '<div class="chart-empty">Unable to load this weekly target right now.</div>';
    toast(error.message || 'Failed to load weekly target');
  } finally {
    if (token === weeklyTargetState.requestToken) {
      mount.classList.remove('is-loading');
      if (requiredMount) requiredMount.classList.remove('is-loading');
      mount.removeAttribute('aria-busy');
    }
  }
}

function renderWeeklyGoal(summary, yearDaily = []) {
  const prevBtn = document.getElementById('weeklyTargetPrev');
  const nextBtn = document.getElementById('weeklyTargetNext');
  const currentBtn = document.getElementById('weeklyTargetCurrentBtn');
  if (!prevBtn || !nextBtn || !currentBtn) return;
  weeklyTargetState.dailyRows = Array.isArray(yearDaily) ? yearDaily : [];

  if (!weeklyTargetState.currentWeekStartDate) {
    weeklyTargetState.currentWeekStartDate = summary.goal_week_start || currentWeekStartKey();
  }

  const moveByWeeks = amount => {
    weeklyTargetState.currentWeekStartDate = localDateKey(addDays(dateFromKey(weeklyTargetState.currentWeekStartDate), amount * 7));
    loadWeeklyTarget(summary, yearDaily);
  };

  prevBtn.onclick = () => moveByWeeks(-1);
  nextBtn.onclick = () => moveByWeeks(1);
  currentBtn.onclick = () => {
    weeklyTargetState.currentWeekStartDate = currentWeekStartKey();
    loadWeeklyTarget(summary, yearDaily);
  };

  loadWeeklyTarget(summary, yearDaily);
}

function updateDailyTargetMeta({ dateKey }) {
  const viewedLabel = document.getElementById('dailyTargetViewedLabel');
  const todayBtn = document.getElementById('dailyTargetTodayBtn');
  if (viewedLabel) viewedLabel.textContent = longDailyTargetLabel(dateKey);
  if (todayBtn) todayBtn.hidden = dateKey === localDateKey(new Date());
}

function renderDailyTargetCard(target) {
  const mount = document.getElementById('dailyGoalCard');
  if (!mount) return;

  const dateKey = target.date;
  const progressSeconds = Math.max(0, target.progress_seconds || 0);
  const progressHours = Math.round((progressSeconds / 3600) * 10) / 10;
  const hasGoal = Number.isFinite(target.goal_hours) && target.goal_hours > 0;
  const goalHours = hasGoal ? Math.round(target.goal_hours * 10) / 10 : null;

  if (hasGoal) {
    const progress = progressHours / goalHours;
    const percent = Math.round(progress * 100);
    const percentLabel = `${Math.min(percent, 999)}%`;
    const tone = targetTone(progress, true);
    const remainingLabel = goalRemainingText(goalHours * 3600, progressSeconds);
    updateDailyTargetMeta({ dateKey });
    mount.innerHTML = `
      <div class="goal-shell">
        <div class="goal-ring-wrap">
          ${renderGoalRing({
            progressRatio: progress,
            hasGoal: true,
            label: `${percentLabel} complete`,
          })}
        </div>
        <div class="goal-controls" style="--target-accent:${tone.accent}">
          <div class="goal-progress-copy">
            <span>Time: <strong>${fmt.dur(progressSeconds)}</strong></span>
            <span class="goal-left">Time left: ${remainingLabel}</span>
          </div>
          <div class="goal-input-row">
            <label class="goal-label" for="dailyGoalCardInput">
              <strong>Daily goal</strong>
            </label>
            <div class="goal-input-wrap">
              <input class="goal-input" id="dailyGoalCardInput" type="number" min="1" max="100" step="0.5" value="${goalHours}">
              <span class="goal-unit">hours</span>
            </div>
          </div>
          <div class="goal-presets">
            ${[1, 2, 3, 5].map(hours => `
              <button class="goal-chip ${Math.abs(goalHours - hours) < 0.01 ? 'is-active' : ''}" type="button" data-daily-goal-hours="${hours}">${hours}h</button>
            `).join('')}
          </div>
        </div>
      </div>
    `;
  } else {
    updateDailyTargetMeta({ dateKey });
    mount.innerHTML = `
      <div class="goal-shell">
        <div class="goal-ring-wrap">
          ${renderGoalRing({
            progressRatio: 0,
            hasGoal: false,
            label: `${fmt.dur(progressSeconds)} logged with no target set`,
            valueMarkup: `<span class="goal-value--stack"><span>${progressHours > 0 ? progressHours.toFixed(progressHours % 1 === 0 ? 0 : 1) : '0'}<small>h</small></span><span class="goal-value-caption">Worked</span></span>`,
          })}
        </div>
        <div class="goal-controls" style="--target-accent:var(--ink-4)">
          <div class="goal-progress-copy">
            <span>Time: <strong>${fmt.dur(progressSeconds)}</strong></span>
            <span class="goal-left">Time left: set a goal first</span>
          </div>
          <div class="goal-input-row">
            <label class="goal-label" for="dailyGoalCardInput">
              <strong>Daily goal</strong>
            </label>
            <div class="goal-input-wrap">
              <input class="goal-input" id="dailyGoalCardInput" type="number" min="1" max="100" step="0.5" value="3">
              <span class="goal-unit">hours</span>
            </div>
          </div>
          <div class="goal-presets" style="--target-accent:var(--accent)">
            ${[1, 2, 3, 5].map(hours => `
              <button class="goal-chip ${hours === 3 ? 'is-active' : ''}" type="button" data-daily-goal-hours="${hours}">${hours}h</button>
            `).join('')}
          </div>
        </div>
      </div>
    `;
  }

  mount.classList.remove('is-fresh');
  void mount.offsetWidth;
  mount.classList.add('is-fresh');
  animateGoalRings(mount);

  const input = mount.querySelector('#dailyGoalCardInput');
  const commit = async (nextValue) => {
    const normalized = clamp(Math.round((Number(nextValue) || 0) * 10) / 10, 1, 100);
    const token = ++dailyTargetState.requestToken;
    mount.classList.add('is-loading');
    try {
      const payload = await postJson('/api/daily-target', {
        date: dateKey,
        goal_hours: normalized,
      });
      if (token !== dailyTargetState.requestToken || dailyTargetState.viewedDate !== dateKey) return;
      renderDailyTargetCard(payload);
      toast(`Saved ${normalized}h for ${humanDailyTargetLabel(dateKey)}`);
    } catch (error) {
      toast(error.message || 'Failed to save daily target');
    } finally {
      if (token === dailyTargetState.requestToken) {
        mount.classList.remove('is-loading');
      }
    }
  };

  if (input) {
    const submitInput = () => commit(input.value);
    input.addEventListener('change', submitInput);
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') submitInput();
    });
  }

  mount.querySelectorAll('[data-daily-goal-hours]').forEach(button => {
    button.addEventListener('click', () => commit(button.dataset.dailyGoalHours));
  });
}

async function loadDailyTarget(summary) {
  const mount = document.getElementById('dailyGoalCard');
  if (!mount) return;
  const dateKey = dailyTargetState.viewedDate;
  updateDailyTargetMeta({
    dateKey,
    progressSeconds: dateKey === localDateKey(new Date()) ? (summary.today_seconds || 0) : 0,
    note: 'Loading target…',
  });
  mount.classList.add('is-loading');
  mount.setAttribute('aria-busy', 'true');
  const token = ++dailyTargetState.requestToken;
  try {
    const res = await fetch(`/api/daily-target?date=${encodeURIComponent(dateKey)}`);
    const payload = await res.json();
    if (!res.ok || payload.error) throw new Error(payload.error || 'Failed to load daily target');
    if (token !== dailyTargetState.requestToken || dailyTargetState.viewedDate !== dateKey) return;
    renderDailyTargetCard(payload);
  } catch (error) {
    if (token !== dailyTargetState.requestToken || dailyTargetState.viewedDate !== dateKey) return;
    updateDailyTargetMeta({
      dateKey,
      progressSeconds: 0,
      note: 'Could not load target',
    });
    mount.innerHTML = '<div class="chart-empty">Unable to load this target right now.</div>';
    toast(error.message || 'Failed to load daily target');
  } finally {
    if (token === dailyTargetState.requestToken) {
      mount.classList.remove('is-loading');
      mount.removeAttribute('aria-busy');
    }
  }
}

function renderDailyGoal(summary) {
  const prevBtn = document.getElementById('dailyTargetPrev');
  const nextBtn = document.getElementById('dailyTargetNext');
  const todayBtn = document.getElementById('dailyTargetTodayBtn');
  if (!prevBtn || !nextBtn || !todayBtn) return;

  const moveByDays = amount => {
    dailyTargetState.viewedDate = localDateKey(addDays(dateFromKey(dailyTargetState.viewedDate), amount));
    loadDailyTarget(summary);
  };

  prevBtn.addEventListener('click', () => moveByDays(-1));
  nextBtn.addEventListener('click', () => moveByDays(1));
  todayBtn.addEventListener('click', () => {
    dailyTargetState.viewedDate = localDateKey(new Date());
    loadDailyTarget(summary);
  });

  loadDailyTarget(summary);
}

function renderCategoryChart(projects, summary = {}) {
  const mount = document.getElementById('categoryChart');
  if (!mount) return;

  const buckets = new Map();
  projects.forEach(project => {
    const seconds = project.month_seconds || 0;
    if (seconds <= 0) return;
    const key = project.category_key || '__uncategorized';
    const meta = project.category_key ? CATEGORY_BY_KEY[project.category_key] : null;
    const label = meta?.label || project.category_label || 'Uncategorized';
    const color = meta?.color || project.category_color || '#8E8E93';
    if (!buckets.has(key)) {
      buckets.set(key, { key, label, color, total_seconds: 0 });
    }
    buckets.get(key).total_seconds += seconds;
  });

  let entries = [...buckets.values()]
    .filter(entry => entry.total_seconds > 0)
    .sort((a, b) => b.total_seconds - a.total_seconds);

  if (entries.length === 0) {
    mount.innerHTML = `<div class="chart-empty">No category data in ${escapeHtml(selectedMonthLabel(summary))}.</div>`;
    return;
  }

  if (entries.length > 6) {
    const topEntries = entries.slice(0, 5);
    const otherSeconds = entries.slice(5).reduce((sum, entry) => sum + entry.total_seconds, 0);
    if (otherSeconds > 0) {
      topEntries.push({
        key: '__other',
        label: 'Other',
        color: '#8E8E93',
        total_seconds: otherSeconds,
      });
    }
    entries = topEntries;
  }

  const totalSeconds = entries.reduce((sum, entry) => sum + entry.total_seconds, 0) || 1;
  const formatPercent = value => {
    const rounded = value < 10 ? value.toFixed(1) : value.toFixed(0);
    return rounded.replace(/\.0$/, '');
  };
  let startPercent = 0;
  const chartEntries = entries.map((entry, index) => {
    const percent = (entry.total_seconds / totalSeconds) * 100;
    const endPercent = index === entries.length - 1 ? 100 : startPercent + percent;
    const chartEntry = {
      ...entry,
      percent,
      start_percent: startPercent,
      end_percent: endPercent,
    };
    startPercent = endPercent;
    return chartEntry;
  });
  const totalHours = Math.round((totalSeconds / 3600) * 10) / 10;

  mount.innerHTML = `
    <div class="donut-layout">
      <div class="bar-column">
        <div class="bar-header">
          <div class="donut-total">${totalHours}h</div>
          <div class="donut-caption">${escapeHtml(selectedMonthLabel(summary))}</div>
        </div>
        <div id="donutChart" class="donut-chart">
          ${chartEntries.map((entry, index) => `
            <div class="bar-segment" data-segment-index="${index}" style="width:${entry.percent.toFixed(3)}%;background:${entry.color}"></div>
          `).join('')}
        </div>
      </div>
      <div class="donut-legend">
        ${chartEntries.map((entry, index) => {
          const hours = Math.round((entry.total_seconds / 3600) * 10) / 10;
          const barWidth = Math.max(1, Math.min(100, entry.percent));
          return `
            <div class="legend-item" data-legend-index="${index}" style="--legend-color:${entry.color};--legend-width:${barWidth.toFixed(3)}%">
              <span class="legend-bar"></span>
              <span class="legend-swatch" style="background:${entry.color}"></span>
              <span class="legend-name">${escapeHtml(entry.label)}</span>
              <span class="legend-value">${hours}h</span>
              <span class="legend-percent">${formatPercent(entry.percent)}%</span>
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `;

  const chart = mount.querySelector('#donutChart');
  const segments = [...mount.querySelectorAll('.bar-segment')];
  const legendItems = [...mount.querySelectorAll('.legend-item')];
  if (!chart || legendItems.length === 0) return;

  const setHighlight = activeIndex => {
    const hasActive = Number.isInteger(activeIndex) && activeIndex >= 0 && activeIndex < legendItems.length;
    legendItems.forEach((item, index) => {
      item.classList.toggle('is-active', hasActive && index === activeIndex);
      item.classList.toggle('is-muted', hasActive && index !== activeIndex);
    });
    segments.forEach((seg, index) => {
      seg.classList.toggle('is-muted', hasActive && index !== activeIndex);
    });
    chart.classList.toggle('is-hovered', hasActive);
  };

  const findSegmentIndex = event => {
    const rect = chart.getBoundingClientRect();
    const x = event.clientX - rect.left;
    if (x < 0 || x > rect.width) return -1;
    const percent = (x / rect.width) * 100;
    return chartEntries.findIndex(entry =>
      percent >= entry.start_percent &&
      (percent < entry.end_percent || entry.end_percent === 100)
    );
  };

  legendItems.forEach(item => {
    const index = Number(item.dataset.legendIndex);
    item.addEventListener('mouseenter', () => setHighlight(index));
    item.addEventListener('mouseleave', () => setHighlight(-1));
  });

  segments.forEach(seg => {
    const index = Number(seg.dataset.segmentIndex);
    seg.addEventListener('mouseenter', () => setHighlight(index));
    seg.addEventListener('mouseleave', () => setHighlight(-1));
  });

  chart.addEventListener('mousemove', event => {
    const index = findSegmentIndex(event);
    setHighlight(index);
  });
  chart.addEventListener('mouseleave', () => setHighlight(-1));
}

load();
if (dashboardRefreshTimer) {
  clearInterval(dashboardRefreshTimer);
}
dashboardRefreshTimer = setInterval(load, 60_000);
