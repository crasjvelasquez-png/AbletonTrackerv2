# AbletonTracker — Engineering Improvements Handoff

**Prepared:** 2026-04-24  
**Context:** The tracker uses a single-table SQLite database at `~/.ableton_tracker/sessions.db`. A full audit identified three improvement areas: database hygiene in the core daemon, data export in the dashboard, and a portable backup utility. Each section below maps to one file and can be assigned independently.

---

## File 1 — `tracker.py`

**Owner:** one engineer  
**Risk:** low — all changes are additive or confined to `setup_db()`  
**Estimated effort:** 2–3 hours

### Background
`setup_db()` is the single entry point for all schema creation and migration. It currently uses a manual `PRAGMA table_info` column check as a one-off migration. There are no indexes on any column, and no formal record of schema version. These gaps are harmless today but create compounding maintenance debt as the schema evolves.

### To-Dos

**1. Add a `schema_version` table**

Inside `setup_db()`, before the `sessions` table creation, add:

```python
conn.execute("""
    CREATE TABLE IF NOT EXISTS schema_version (
        version   INTEGER PRIMARY KEY,
        applied_at REAL NOT NULL
    )
""")
```

This gives every future migration a numbered anchor instead of ad-hoc column checks.

**2. Convert the existing `last_seen_time` column check into a versioned migration**

Replace the current block:

```python
columns = {
    row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
}
if "last_seen_time" not in columns:
    conn.execute("ALTER TABLE sessions ADD COLUMN last_seen_time REAL")
conn.execute("""
    UPDATE sessions
    SET last_seen_time = COALESCE(last_seen_time, end_time, start_time)
    WHERE last_seen_time IS NULL
""")
```

With a pattern like:

```python
def _applied(conn, version):
    return conn.execute(
        "SELECT 1 FROM schema_version WHERE version = ?", (version,)
    ).fetchone() is not None

def _mark(conn, version):
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
        (version, time.time())
    )

# Migration 1 — add last_seen_time
if not _applied(conn, 1):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "last_seen_time" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_seen_time REAL")
    conn.execute("""
        UPDATE sessions
        SET last_seen_time = COALESCE(last_seen_time, end_time, start_time)
        WHERE last_seen_time IS NULL
    """)
    _mark(conn, 1)
```

All future schema changes should follow this same numbered pattern.

**3. Add indexes inside `setup_db()`**

After the `sessions` table `CREATE TABLE` statement, add:

```python
conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_sessions_start_time
    ON sessions(start_time)
""")
conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_sessions_project_name
    ON sessions(project_name)
""")
conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_sessions_end_time
    ON sessions(end_time)
""")
```

These cover the three columns the dashboard queries filter and group on most heavily (`start_time` in date-range filters, `project_name` in aggregations, `end_time` in open-session checks). `CREATE INDEX IF NOT EXISTS` is idempotent so it is safe to run on existing databases.

---

## File 2 — `dashboard.py`

**Owner:** one engineer  
**Risk:** low — additive only; no existing endpoints are modified  
**Estimated effort:** 2–4 hours

### Background
There is currently no way to get your data out of the tracker other than querying the SQLite file directly. This means moving devices or sharing data with another tool requires knowing where the file lives and having a SQLite client. A CSV export endpoint solves this cleanly from within the existing local web server.

### To-Dos

**1. Add a `GET /export/csv` HTTP endpoint**

In the `do_GET` handler of the `BaseHTTPRequestHandler` subclass (wherever `/api/stats` is routed), add a branch for `/export/csv`:

```python
elif self.path == "/export/csv":
    payload = export_csv()
    self.send_response(200)
    self.send_header("Content-Type", "text/csv; charset=utf-8")
    self.send_header(
        "Content-Disposition",
        f'attachment; filename="ableton_sessions_{date.today().isoformat()}.csv"'
    )
    self.end_headers()
    self.wfile.write(payload.encode("utf-8"))
```

**2. Implement the `export_csv()` function**

Add this function in the "Data layer" section, alongside `get_stats()`:

```python
def export_csv() -> str:
    import csv, io
    if not DB_PATH.exists():
        return "id,project_name,start_time,last_seen_time,end_time,active_seconds\n"
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, project_name, start_time, last_seen_time, end_time, active_seconds
            FROM sessions
            ORDER BY start_time ASC
        """).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "project_name", "start_time", "last_seen_time", "end_time", "active_seconds"])
    for r in rows:
        writer.writerow([
            r["id"],
            r["project_name"],
            r["start_time"],
            r["last_seen_time"],
            r["end_time"],
            r["active_seconds"],
        ])
    return buf.getvalue()
```

The timestamps are exported as raw Unix epoch values. If human-readable dates are preferred, wrap them with `datetime.fromtimestamp(r["start_time"]).isoformat()` etc.

**3. Add an "Export CSV" button to the HTML dashboard**

In the `HTML` string, find the header or the intro section and add a download link. The simplest approach is an anchor that hits the new endpoint:

```html
<a href="/export/csv"
   style="/* match the existing button styles in the template */">
  Export CSV
</a>
```

Place it near the existing action buttons (Cleanup Phantom Sessions, etc.) so it is discoverable but not prominent. The browser will prompt a file download automatically due to the `Content-Disposition` header set in step 1.

---

## File 3 — `install.command`

**Owner:** one engineer  
**Risk:** low — this is a standalone utility script; nothing in the existing runtime depends on it  
**Estimated effort:** 1–2 hours

### Background
The database is the only persistent artifact the tracker produces. There is no backup mechanism. A drive failure or accidental deletion after months of tracking would be a total data loss. The fix has two parts: verify Time Machine is covering the data directory, and provide a one-command manual backup for when the user is migrating devices.

### To-Dos

**1. Add a Time Machine exclusion check during install**

At the end of `install.command`, after the tracker and launch agent are set up, add a check that warns if the data directory is excluded from Time Machine:

```bash
DATA_DIR="$HOME/.ableton_tracker"

# Check if Time Machine is excluding the data directory
TM_EXCLUSIONS=$(defaults read /Library/Preferences/com.apple.TimeMachine.plist SkipPaths 2>/dev/null || echo "")
if echo "$TM_EXCLUSIONS" | grep -q "$DATA_DIR"; then
    echo ""
    echo "WARNING: $DATA_DIR appears to be excluded from Time Machine."
    echo "Your session history will not be backed up automatically."
    echo "Run the following to re-include it:"
    echo "  sudo tmutil addexclusion -p \"$DATA_DIR\""
fi
```

This is informational only — it does not modify Time Machine settings without explicit user action.

**2. Create a `backup.command` script in the project root**

Add a new file `backup.command` alongside the existing `.command` files. It should:

- Create a timestamped copy of `sessions.db` in a user-chosen or default location
- Print the destination path clearly so the user knows where to find it when migrating devices

```bash
#!/bin/bash
set -euo pipefail

SRC="$HOME/.ableton_tracker/sessions.db"
DEST_DIR="$HOME/Desktop"
TIMESTAMP=$(date +"%Y-%m-%d_%H%M%S")
DEST="$DEST_DIR/ableton_sessions_backup_${TIMESTAMP}.db"

if [ ! -f "$SRC" ]; then
    echo "No database found at $SRC — nothing to back up."
    exit 1
fi

cp "$SRC" "$DEST"
echo ""
echo "Backup saved to:"
echo "  $DEST"
echo ""
echo "To restore on a new machine, copy this file to:"
echo "  ~/.ableton_tracker/sessions.db"
echo "  (create the folder first if it does not exist)"
echo ""
read -p "Press Return to close..."
```

Make it executable:

```bash
chmod +x backup.command
```

This gives any user a one-double-click path to a portable backup before switching machines, without needing to know where the hidden directory lives.
