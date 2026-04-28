# AbletonTracker: Project Intelligence & Token Rules

## ⚠️ CRITICAL: TOKEN SAVING RULES
- **Large File Handling:** `dashboard.py` and `dashboard.html` are >4,000 lines. **NEVER** `read_file` these in their entirety. 
- **Targeted Reading:** Use `grep -n` to find line numbers first, then use `sed` or targeted tool calls to read only the necessary blocks (max 200 lines at a time).
- **No Redundant Verification:** Do not run `ls` or `cat` to verify a file exists after you have just edited it. Trust your internal state.
- **Concise Responses:** Skip preambles and "I have updated the file" summaries. Only provide the code or the confirmation.
- **Testing:** Only run `build_app.py` or specific tracking tests when logic changes. Do not run for CSS/UI-only tweaks.

## Project Overview
A macOS menu bar app (Python/PyObjC) that tracks Ableton Live activity and serves a local web dashboard (SQLite/Vanilla JS).

## Technical Map

### 1. The Monoliths (Handle with care)
- `templates/dashboard.html` (~4,227 lines): 
    - Contains ALL CSS (top), HTML structure, and Vanilla JS logic (bottom).
    - **Views:** `#dashboard` and `#settings`.
    - **Key CSS:** `.settings-shell`, `.settings-grid`, `.category-library`.
- `dashboard.py` (~4,211 lines): 
    - Core HTTP Server (Port 7421) and API Handlers.
    - **Key Logic:** `create_category`, `update_category`, `delete_category`.

### 2. Supporting Logic
- `tracker.py` (~977 lines): The background daemon monitoring Ableton.
- `menubar.py` (~221 lines): The macOS system tray interface.
- `build_app.py`: Packaging script.

## API & Data Reference (Do not search for these)
### Endpoints (POST/GET)
- `/api/category-options` (Create/Update/Delete)
- `/api/project-category` (Mapping)
- `/api/data` (Global Stats)
- `/api/daily-target` (User Goals)

### Database: `~/.ableton_tracker/sessions.db`
- `project_categories`: (project_name, category_key)
- `category_definitions`: (key, label, color, custom)

## Architecture Patterns
- **Frontend:** SPA style using hash-based navigation (#settings). CSS is embedded to ensure the app remains a single-file portable dashboard.
- **Backend:** `Handler` class in `dashboard.py` routes API requests via `do_GET` and `do_POST`.
- **Normalization:** Category keys are generated from labels via `normalize_category_key()`.

## Commands
- **Build:** `python3 build_app.py`
- **Run Local:** `python3 menubar.py`