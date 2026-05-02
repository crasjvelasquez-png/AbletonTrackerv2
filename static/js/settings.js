/* Ableton Tracker — Settings module
 *
 * Exposes window.Settings with:
 *   init()            — wire up the partial after it has been injected into the DOM.
 *   render(data)      — repaint dynamic regions from a /api/data response.
 *   refreshGoals()    — re-fetch daily + weekly target endpoints and repaint goal cards.
 *
 * Depends on globals from dashboard.html:
 *   toast, confirmDialog, postJson, postAction, escapeHtml, load,
 *   bindColorField, renderColorField, normalizeHexColor,
 *   THEME_STORAGE_KEY, WEEKLY_GOAL_STORAGE_KEY, applyStoredTheme,
 *   localDateKey
 */
(function () {
  'use strict';

  const CUSTOM_CATEGORY_LIMIT_FALLBACK = 12;
  const DEFAULT_NEW_COLOR = '#7C5CFF';
  const WEEK_DAYS = [
    { name: 'Monday',    abbrev: 'Mon', jsDay: 1 },
    { name: 'Tuesday',   abbrev: 'Tue', jsDay: 2 },
    { name: 'Wednesday', abbrev: 'Wed', jsDay: 3 },
    { name: 'Thursday',  abbrev: 'Thu', jsDay: 4 },
    { name: 'Friday',    abbrev: 'Fri', jsDay: 5 },
    { name: 'Saturday',  abbrev: 'Sat', jsDay: 6 },
    { name: 'Sunday',    abbrev: 'Sun', jsDay: 0 },
  ];
  let currentWeekStartDay = null;

  let inited = false;
  let lastData = null;
  let mountedAt = 0;

  // ── helpers ─────────────────────────────────────────────────────────
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

  function fmtHours(seconds) {
    const h = (seconds || 0) / 3600;
    if (h >= 10) return `${h.toFixed(0)}h`;
    return `${h.toFixed(1)}h`;
  }

  function todayIso() {
    if (typeof window.localDateKey === 'function') {
      return window.localDateKey(new Date());
    }
    const d = new Date();
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  }

  function setFeedback(el, message, kind /* 'success' | 'error' */) {
    if (!el) return;
    if (!message) {
      el.hidden = true;
      el.textContent = '';
      el.removeAttribute('data-kind');
      return;
    }
    el.textContent = message;
    el.dataset.kind = kind || 'success';
    el.hidden = false;
  }

  // ── categories ──────────────────────────────────────────────────────
  function renderCategories(data) {
    const root = $('[data-settings-root]');
    if (!root) return;

    const options = (data && data.category_options) || [];
    const limit = (data && data.custom_category_limit) || CUSTOM_CATEGORY_LIMIT_FALLBACK;
    const count = (data && data.custom_category_count) || 0;
    const remaining = Math.max(limit - count, 0);

    // Header / KPI numbers
    const meta = $('[data-categories-meta]', root);
    if (meta) meta.textContent = `${count}/${limit} used`;

    const copy = $('[data-categories-copy]', root);
    if (copy) {
      copy.innerHTML =
        `Create up to ${limit} personal categories with your own color palette. ` +
        `Anything you add here becomes available in the project category picker right away.`;
    }

    const createMeta = $('[data-create-meta]', root);
    if (createMeta) createMeta.textContent = `${remaining} slot${remaining === 1 ? '' : 's'} left`;

    const kpiCat = $('[data-kpi-categories]', root);
    const kpiAvail = $('[data-kpi-available]', root);
    const kpiLimit = $('[data-kpi-limit]', root);
    if (kpiCat) kpiCat.textContent = String(count);
    if (kpiAvail) kpiAvail.textContent = String(remaining);
    if (kpiLimit) kpiLimit.textContent = String(limit);

    // Create form: enable/disable based on remaining slots
    const createForm = $('[data-category-create-form]', root);
    const nameInput = $('[data-category-name-input]', root);
    const submitBtn = $('[data-category-submit]', root);
    const colorMount = $('[data-category-color-mount]', root);

    if (colorMount && colorMount.dataset.bound !== 'true') {
      colorMount.innerHTML = window.renderColorField({
        inputId: 'categorySettingsColorInput',
        value: DEFAULT_NEW_COLOR,
        disabled: remaining === 0,
      });
      window.bindColorField(colorMount);
      colorMount.dataset.bound = 'true';
    } else if (colorMount) {
      const colorInput = colorMount.querySelector('input[name="color"]');
      if (colorInput) colorInput.disabled = remaining === 0;
      colorMount.querySelectorAll('[data-color-preset]').forEach(b => { b.disabled = remaining === 0; });
    }

    if (nameInput) nameInput.disabled = remaining === 0;
    if (submitBtn) submitBtn.disabled = remaining === 0;
    createForm?.classList.toggle('is-disabled', remaining === 0);

    // Library grid
    const grid = $('[data-category-library-grid]', root);
    const libMeta = $('[data-library-meta]', root);
    if (libMeta) libMeta.textContent = `${options.length} total`;
    if (!grid) return;

    if (options.length === 0) {
      grid.innerHTML = `<div class="empty-mini">No categories yet — create one above.</div>`;
      return;
    }

    grid.innerHTML = options.map(option => {
      const labelEsc = window.escapeHtml(option.label);
      const assignments = option.assignment_count || 0;
      return `
        <div class="category-library-card" data-category-card="${option.key}">
          <div class="category-library-head">
            <div class="category-library-title">
              <span class="category-library-swatch" style="--category-color:${option.color}"></span>
              <span class="category-library-name">${labelEsc}</span>
            </div>
          </div>
          <div class="category-library-meta">
            <span>${assignments} assigned project${assignments === 1 ? '' : 's'}</span>
            <div class="category-library-actions">
              <button class="btn subtle small" type="button" data-category-edit="${option.key}">Edit</button>
              <button class="btn danger small" type="button"
                      data-category-delete="${option.key}"
                      data-category-label="${labelEsc}"
                      data-category-assignments="${assignments}">Delete</button>
            </div>
          </div>
          <form class="category-editor" data-category-editor="${option.key}" hidden>
            <div class="category-editor-head">
              <span class="category-editor-title">Edit category</span>
              <button class="btn subtle small" type="button" data-category-cancel="${option.key}">Cancel</button>
            </div>
            <div class="field-grid">
              <label class="field">
                <span class="field-label">Category name</span>
                <input class="text-input" name="label" type="text" maxlength="32"
                       value="${labelEsc}" required>
              </label>
              <label class="field">
                <span class="field-label">Color</span>
                ${window.renderColorField({ value: option.color, showPresets: false })}
              </label>
            </div>
            <div class="category-editor-actions">
              <button class="btn small" type="submit">Save changes</button>
            </div>
          </form>
        </div>
      `;
    }).join('');

    // Bind color fields inside editors
    $all('[data-category-editor]', grid).forEach(editor => window.bindColorField(editor));
  }

  // ── week refresh day ──────────────────────────────────────────────
  function renderWeekStartPicker() {
    const root = $('[data-settings-root]');
    const picker = $('[data-weekday-picker]', root);
    if (!picker) return;

    picker.innerHTML = WEEK_DAYS.map(day => {
      const active = currentWeekStartDay === String(day.jsDay);
      return `
        <button class="weekday-option${active ? ' is-active' : ''}"
                type="button" role="radio"
                data-weekday-value="${day.jsDay}"
                aria-checked="${active}">
          <span class="weekday-option-abbrev">${day.abbrev}</span>
          <span class="weekday-option-day">${day.name}</span>
        </button>
      `;
    }).join('');

    picker.querySelectorAll('[data-weekday-value]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const value = btn.dataset.weekdayValue;
        try {
          await window.postJson('/api/app-settings', {
            key: 'week_start_weekday',
            value: value,
          });
          currentWeekStartDay = String(value);
          syncWeekStartPicker();
          syncWeekRefreshMeta();
          window.toast(`Week starts on ${WEEK_DAYS.find(d => String(d.jsDay) === String(value)).name}`);
          window.load?.();
        } catch (e) {
          window.toast('Failed to save week start day');
        }
      });
    });
  }

  function syncWeekStartPicker() {
    const root = $('[data-settings-root]');
    if (!root) return;
    $all('[data-weekday-value]', root).forEach(btn => {
      const active = btn.dataset.weekdayValue === currentWeekStartDay;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-checked', String(active));
    });
  }

  function syncWeekRefreshMeta() {
    const root = $('[data-settings-root]');
    const meta = $('[data-week-refresh-meta]', root);
    if (!meta) return;
    const day = WEEK_DAYS.find(d => String(d.jsDay) === currentWeekStartDay);
    if (day) {
      meta.textContent = `resets ${day.name} morning`;
    } else {
      meta.textContent = 'not set';
    }
  }

  async function loadWeekStartDay() {
    try {
      const res = await fetch('/api/app-settings');
      const data = await res.json();
      currentWeekStartDay = (data && data.week_start_weekday != null) ? String(data.week_start_weekday) : '5'; // Friday=5 default
    } catch (_) {
      currentWeekStartDay = '5';
    }
    renderWeekStartPicker();
    syncWeekRefreshMeta();
  }

  function bindCategoryDelegation(root) {
    // Create-form submit
    root.addEventListener('submit', async event => {
      const target = event.target;
      if (target.matches('[data-category-create-form]')) {
        event.preventDefault();
        await handleCreateSubmit(target);
        return;
      }
      if (target.matches('[data-category-editor]')) {
        event.preventDefault();
        await handleEditorSubmit(target);
        return;
      }
      if (target.matches('[data-goal-form]')) {
        event.preventDefault();
        await handleGoalSubmit(target);
        return;
      }
    });

    // Click delegation: edit / cancel / delete / cleanup actions / theme
    root.addEventListener('click', async event => {
      const editBtn = event.target.closest('[data-category-edit]');
      if (editBtn) {
        const key = editBtn.dataset.categoryEdit;
        const editor = root.querySelector(`[data-category-editor="${CSS.escape(key)}"]`);
        const shouldOpen = !editor || editor.hidden;
        toggleEditor(root, key, shouldOpen);
        if (shouldOpen) editor?.querySelector('input[name="label"]')?.focus();
        return;
      }
      const cancelBtn = event.target.closest('[data-category-cancel]');
      if (cancelBtn) {
        toggleEditor(root, cancelBtn.dataset.categoryCancel, false);
        return;
      }
      const deleteBtn = event.target.closest('[data-category-delete]');
      if (deleteBtn) {
        await handleDelete(deleteBtn);
        return;
      }
      const actionBtn = event.target.closest('[data-action]');
      if (actionBtn) {
        await handleDataAction(actionBtn);
        return;
      }
      const themeBtn = event.target.closest('[data-theme-option]');
      if (themeBtn) {
        applyTheme(themeBtn.dataset.themeOption);
        return;
      }
    });
  }

  function toggleEditor(root, key, open) {
    $all('[data-category-editor]', root).forEach(editor => {
      const isOpen = editor.dataset.categoryEditor === key && open;
      editor.hidden = !isOpen;
      editor.closest('[data-category-card]')?.classList.toggle('is-editing', isOpen);
    });
    $all('[data-category-edit]', root).forEach(button => {
      const isOpen = button.dataset.categoryEdit === key && open;
      button.setAttribute('aria-expanded', String(isOpen));
      button.textContent = isOpen ? 'Close' : 'Edit';
    });
  }

  async function handleCreateSubmit(form) {
    const nameInput = form.querySelector('[data-category-name-input]');
    const colorInput = form.querySelector('input[name="color"]');
    const submitBtn = form.querySelector('[data-category-submit]');
    const label = (nameInput?.value || '').trim();
    const color = colorInput?.value || DEFAULT_NEW_COLOR;
    if (!label) {
      window.toast('Add a category name first');
      nameInput?.focus();
      return;
    }
    submitBtn.disabled = true;
    try {
      const result = await window.postJson('/api/category-options', { label, color });
      window.toast(`Created ${result.category.label}`);
      if (nameInput) nameInput.value = '';
      if (colorInput) {
        colorInput.value = DEFAULT_NEW_COLOR;
        // re-sync color preview
        colorInput.dispatchEvent(new Event('input', { bubbles: true }));
      }
      await window.load();
    } catch (error) {
      window.toast(error.message || 'Failed to create category');
      submitBtn.disabled = false;
    }
    // Note: re-enable handled by next render() based on fresh data.
  }

  async function handleEditorSubmit(editor) {
    const key = editor.dataset.categoryEditor;
    const submitBtn = editor.querySelector('button[type="submit"]');
    const label = editor.querySelector('input[name="label"]').value.trim();
    const color = editor.querySelector('input[name="color"]').value;
    if (!label) {
      window.toast('Category name cannot be empty');
      return;
    }
    submitBtn.disabled = true;
    try {
      const result = await window.postJson('/api/category-options/update', { key, label, color });
      window.toast(`Updated ${result.category.label}`);
      await window.load();
    } catch (error) {
      window.toast(error.message || 'Failed to update category');
      submitBtn.disabled = false;
    }
  }

  async function handleDelete(button) {
    const key = button.dataset.categoryDelete;
    const label = button.dataset.categoryLabel || 'this category';
    const assignments = Number(button.dataset.categoryAssignments || '0');
    const ok = await window.confirmDialog({
      title: 'Delete custom <em>category</em>?',
      body: assignments > 0
        ? `Deletes <code>${label}</code> and clears it from ${assignments} assigned project${assignments === 1 ? '' : 's'}.`
        : `Deletes <code>${label}</code> from your category library.`,
      confirmLabel: 'Delete category',
    });
    if (!ok) return;
    button.disabled = true;
    try {
      const result = await window.postJson('/api/category-options/delete', { key });
      window.toast(
        result.cleared_assignments > 0
          ? `Deleted ${result.category.label} and cleared ${result.cleared_assignments} project${result.cleared_assignments === 1 ? '' : 's'}`
          : `Deleted ${result.category.label}`
      );
      await window.load();
    } catch (error) {
      window.toast(error.message || 'Failed to delete category');
      button.disabled = false;
    }
  }

  // ── goals ───────────────────────────────────────────────────────────
  async function refreshGoals() {
    await Promise.all([refreshDaily(), refreshWeekly()]);
    updateGoalsMeta();
  }

  async function refreshDaily() {
    const root = $('[data-settings-root]');
    if (!root) return;
    const input = $('[data-daily-input]', root);
    const progress = $('[data-daily-progress]', root);
    try {
      const res = await fetch(`/api/daily-target?date=${todayIso()}`);
      const data = await res.json();
      if (input && document.activeElement !== input) {
        input.value = data.has_target ? data.goal_hours : '';
      }
      if (progress) {
        if (data.has_target) {
          progress.textContent = `${fmtHours(data.progress_seconds)} of ${data.goal_hours}h today`;
        } else {
          progress.textContent = `${fmtHours(data.progress_seconds)} logged today · no target set`;
        }
      }
    } catch (e) {
      if (progress) progress.textContent = 'Could not load today\'s progress';
    }
  }

  async function refreshWeekly() {
    const root = $('[data-settings-root]');
    if (!root) return;
    const input = $('[data-weekly-input]', root);
    const progress = $('[data-weekly-progress]', root);
    try {
      const res = await fetch(`/api/weekly-target?date=${todayIso()}`);
      const data = await res.json();
      const goalHours = (data.has_target && Number.isFinite(data.goal_hours) && data.goal_hours > 0) ? data.goal_hours : null;
      if (input && document.activeElement !== input) {
        input.value = goalHours != null ? goalHours : '';
      }
      if (progress) {
        const range = formatWeekRange(data.week_start, data.week_end);
        if (goalHours != null) {
          progress.textContent = `${fmtHours(data.progress_seconds)} of ${goalHours}h · ${range}`;
        } else {
          progress.textContent = `${fmtHours(data.progress_seconds)} logged · ${range}`;
        }
      }
    } catch (e) {
      if (progress) progress.textContent = 'Could not load weekly progress';
    }
  }

  function formatWeekRange(startIso, endIso) {
    if (!startIso || !endIso) return 'this week';
    const start = new Date(startIso + 'T00:00:00');
    const end = new Date(endIso + 'T00:00:00');
    const opts = { month: 'short', day: 'numeric' };
    return `${start.toLocaleDateString(undefined, opts)} – ${end.toLocaleDateString(undefined, opts)}`;
  }

  function updateGoalsMeta() {
    const root = $('[data-settings-root]');
    const meta = $('[data-goals-meta]', root);
    if (!meta) return;
    const dailyInput = $('[data-daily-input]', root);
    const weeklyInput = $('[data-weekly-input]', root);
    const hasDaily = dailyInput?.value && Number(dailyInput.value) > 0;
    const hasWeekly = weeklyInput?.value && Number(weeklyInput.value) > 0;
    if (hasDaily && hasWeekly) meta.textContent = 'daily & weekly set';
    else if (hasDaily) meta.textContent = 'daily set';
    else if (hasWeekly) meta.textContent = 'weekly set';
    else meta.textContent = 'no targets set';
  }

  async function handleGoalSubmit(form) {
    const kind = form.dataset.goalForm;
    if (kind === 'daily') return handleDailySubmit(form);
    if (kind === 'weekly') return handleWeeklySubmit(form);
  }

  async function handleDailySubmit(form) {
    const input = form.querySelector('[data-daily-input]');
    const feedback = form.querySelector('[data-daily-feedback]');
    const submit = form.querySelector('button[type="submit"]');
    const value = Number(input.value);
    if (!Number.isFinite(value) || value <= 0 || value > 100) {
      setFeedback(feedback, 'Enter a number between 0.1 and 100.', 'error');
      input.focus();
      return;
    }
    submit.disabled = true;
    setFeedback(feedback, '');
    try {
      const result = await window.postJson('/api/daily-target', {
        date: todayIso(),
        goal_hours: value,
      });
      setFeedback(feedback, `Saved · ${fmtHours(result.progress_seconds)} of ${result.goal_hours}h today`, 'success');
      await refreshDaily();
      updateGoalsMeta();
      window.load?.();
    } catch (error) {
      setFeedback(feedback, error.message || 'Failed to save daily target', 'error');
    } finally {
      submit.disabled = false;
    }
  }

  async function handleWeeklySubmit(form) {
    const input = form.querySelector('[data-weekly-input]');
    const feedback = form.querySelector('[data-weekly-feedback]');
    const submit = form.querySelector('button[type="submit"]');
    const value = Number(input.value);
    if (!Number.isFinite(value) || value <= 0 || value > 168) {
      setFeedback(feedback, 'Enter a number between 0.5 and 168.', 'error');
      input.focus();
      return;
    }
    submit.disabled = true;
    setFeedback(feedback, '');
    try {
      const normalized = Math.round(value * 10) / 10;
      await window.postJson('/api/weekly-target', { goal_hours: normalized });
      setFeedback(feedback, `Saved · target is ${normalized}h per week`, 'success');
      await refreshWeekly();
      updateGoalsMeta();
      window.load?.();
    } catch (error) {
      setFeedback(feedback, error.message || 'Failed to save weekly target', 'error');
    } finally {
      submit.disabled = false;
    }
  }

  // ── data management ────────────────────────────────────────────────
  const ACTIONS = {
    'clear-recent': {
      title: 'Clear <em>all logs?</em>',
      body: 'Permanently deletes closed sessions from your history. If Ableton is recording right now, the live session is preserved. This cannot be undone.',
      confirmLabel: 'Clear logs',
      endpoint: '/api/clear-recent',
      success: r => `Cleared ${r.deleted} closed session${r.deleted === 1 ? '' : 's'}`,
      empty: 'No sessions to clear',
      failure: 'Failed to clear logs',
    },
    'clear-unsaved': {
      title: 'Remove <em>unsaved</em> projects?',
      body: 'Deletes closed sessions logged against <code>Untitled</code> or <code>Untitled Project</code>.',
      confirmLabel: 'Remove drafts',
      endpoint: '/api/clear-unsaved',
      success: r => `Removed ${r.deleted} unsaved session${r.deleted === 1 ? '' : 's'}`,
      empty: 'No unsaved sessions found',
      failure: 'Failed to remove drafts',
    },
    'clear-phantoms': {
      title: 'Remove phantom <em>sessions</em>?',
      body: 'Deletes closed rows that were captured from export dialogs or plugin windows instead of real Live sets.',
      confirmLabel: 'Clean phantoms',
      endpoint: '/api/clear-phantoms',
      success: r => `Removed ${r.deleted} phantom session${r.deleted === 1 ? '' : 's'}`,
      empty: 'No phantom sessions found',
      failure: 'Failed to clean phantom sessions',
    },
  };

  async function handleDataAction(button) {
    const key = button.dataset.action;
    const spec = ACTIONS[key];
    if (!spec) return;
    const ok = await window.confirmDialog({
      title: spec.title,
      body: spec.body,
      confirmLabel: spec.confirmLabel,
    });
    if (!ok) return;
    button.disabled = true;
    try {
      const r = await window.postAction(spec.endpoint);
      window.toast(r && r.deleted === 0 ? spec.empty : spec.success(r));
      await window.load();
    } catch (e) {
      window.toast(spec.failure);
    } finally {
      button.disabled = false;
    }
  }

  // ── appearance ─────────────────────────────────────────────────────
  function currentThemeChoice() {
    const stored = localStorage.getItem(window.THEME_STORAGE_KEY);
    if (stored === 'light' || stored === 'dark') return stored;
    return 'system';
  }

  function syncThemePicker() {
    const root = $('[data-settings-root]');
    if (!root) return;
    const choice = currentThemeChoice();
    $all('[data-theme-option]', root).forEach(btn => {
      const isActive = btn.dataset.themeOption === choice;
      btn.classList.toggle('is-active', isActive);
      btn.setAttribute('aria-checked', String(isActive));
    });
    const meta = $('[data-theme-meta]', root);
    if (meta) meta.textContent = choice;
  }

  function applyTheme(choice) {
    if (choice === 'light' || choice === 'dark') {
      localStorage.setItem(window.THEME_STORAGE_KEY, choice);
    } else {
      localStorage.removeItem(window.THEME_STORAGE_KEY);
    }
    if (typeof window.applyStoredTheme === 'function') window.applyStoredTheme();
    syncThemePicker();
  }

  // ── lifecycle ──────────────────────────────────────────────────────
  function init() {
    if (inited) return;
    const root = $('[data-settings-root]');
    if (!root) return;
    inited = true;
    mountedAt = Date.now();
    bindCategoryDelegation(root);
    syncThemePicker();
    refreshGoals();
    loadWeekStartDay();
  }

  function render(data) {
    if (data) lastData = data;
    const root = $('[data-settings-root]');
    if (!root) return;
    if (!inited) init();
    if (lastData) renderCategories(lastData);
    syncThemePicker();
    // Don't refetch goals on every dashboard data change — they update when the user
    // saves a target or when settings is first opened. We do refresh meta in case the
    // dashboard updated daily progress.
    refreshDaily();
    refreshWeekly().then(updateGoalsMeta);
  }

  window.Settings = { init, render, refreshGoals };
})();
