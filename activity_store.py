"""Additive schema and interval-aware rollups shared by both database owners."""
import json
import sqlite3


def ensure_activity_schema(conn):
    columns={r[1] for r in conn.execute('PRAGMA table_info(sessions)')}
    if 'policy_version' not in columns:
        conn.execute('ALTER TABLE sessions ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 0')
    if 'start_token' not in columns:
        conn.execute('ALTER TABLE sessions ADD COLUMN start_token TEXT')
    conn.executescript('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_session_start_token ON sessions(start_token);
        CREATE TABLE IF NOT EXISTS activity_intervals (
            id TEXT PRIMARY KEY,
            session_id INTEGER NOT NULL,
            start_time REAL NOT NULL,
            end_time REAL NOT NULL,
            credited_seconds REAL NOT NULL DEFAULT 0 CHECK(credited_seconds >= 0),
            reason TEXT NOT NULL,
            evidence_source TEXT NOT NULL,
            policy_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_activity_session ON activity_intervals(session_id);
        CREATE INDEX IF NOT EXISTS idx_activity_time ON activity_intervals(start_time,end_time);
        CREATE TRIGGER IF NOT EXISTS delete_session_activity AFTER DELETE ON sessions
        BEGIN
            DELETE FROM activity_intervals WHERE session_id=OLD.id;
        END;
        CREATE TABLE IF NOT EXISTS tracker_status (
            id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL
        );
    ''')


def load_activity_rows(conn):
    """Use exact spans for covered rows and original estimates for historical rows.

    Callers must not include a covered parent alongside its intervals. No schema
    writes occur here; dashboards can safely use this within read transactions.
    """
    try:
        return conn.execute('''
            SELECT s.id,s.project_name,s.start_time,s.last_seen_time,s.end_time,s.active_seconds
            FROM sessions s WHERE s.policy_version=0 AND s.active_seconds>0
            UNION ALL
            SELECT s.id,s.project_name,a.start_time,a.end_time,a.end_time,a.credited_seconds
            FROM activity_intervals a JOIN sessions s ON s.id=a.session_id
            WHERE s.policy_version>0 AND a.credited_seconds>0
        ''').fetchall()
    except sqlite3.OperationalError as e:
        if 'no such column' not in str(e) and 'no such table' not in str(e): raise
        return conn.execute('SELECT id,project_name,start_time,last_seen_time,end_time,active_seconds FROM sessions WHERE active_seconds>0').fetchall()


def read_tracking_status(conn, now):
    try: row=conn.execute('SELECT payload FROM tracker_status WHERE id=1').fetchone()
    except sqlite3.OperationalError: return None
    if not row: return None
    try: status=json.loads(row[0])
    except (ValueError,TypeError): return None
    if status.get('state')=='tracking' and now>status.get('verified_until',0):
        status.update(state='idle_paused',reason='unverified',message='Paused: cannot verify activity')
    return status
