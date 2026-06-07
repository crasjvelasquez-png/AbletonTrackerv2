import os
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from unittest.mock import patch

import dashboard
import tracker


class DashboardCategoryTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_set_project_category_persists_and_is_returned_by_stats(self):
        created = dashboard.create_category("Production", "#00a6ff")

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Real Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

        result = dashboard.set_project_category("Real Song", created["category"]["key"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["category"]["label"], "Production")

        stats = dashboard.get_stats()
        self.assertEqual(stats["projects"][0]["category_key"], created["category"]["key"])
        self.assertEqual(stats["projects"][0]["category_label"], "Production")
        self.assertEqual(stats["projects"][0]["category_color"], "#00A6FF")
        self.assertEqual(stats["recent"][0]["category_key"], created["category"]["key"])

    def test_set_project_category_none_clears_existing_assignment(self):
        created = dashboard.create_category("Mixing", "#8b5a2b")

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Real Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

        dashboard.set_project_category("Real Song", created["category"]["key"])
        cleared = dashboard.set_project_category("Real Song", None)

        self.assertTrue(cleared["ok"])
        stats = dashboard.get_stats()
        self.assertIsNone(stats["projects"][0]["category_key"])
        self.assertIsNone(stats["recent"][0]["category_key"])

    def test_create_category_adds_custom_option_and_allows_assignment(self):
        created = dashboard.create_category("Sound Design", "#7c5cff")

        self.assertTrue(created["ok"])
        self.assertEqual(created["category"]["label"], "Sound Design")
        self.assertEqual(created["category"]["color"], "#7C5CFF")

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Custom Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

        assigned = dashboard.set_project_category("Custom Song", created["category"]["key"])
        self.assertTrue(assigned["ok"])

        stats = dashboard.get_stats()
        option = next(
            item for item in stats["category_options"]
            if item["key"] == created["category"]["key"]
        )
        self.assertEqual(option["label"], created["category"]["label"])
        self.assertEqual(option["color"], created["category"]["color"])
        self.assertEqual(option["assignment_count"], 1)
        self.assertEqual(stats["custom_category_count"], 1)
        self.assertEqual(stats["projects"][0]["category_label"], "Sound Design")
        self.assertEqual(stats["projects"][0]["category_color"], "#7C5CFF")

    def test_update_category_changes_name_and_color(self):
        created = dashboard.create_category("Sound Design", "#7c5cff")

        updated = dashboard.update_category(
            created["category"]["key"],
            "Vocal Production",
            "#11aa88",
        )

        self.assertTrue(updated["ok"])
        self.assertEqual(updated["category"]["label"], "Vocal Production")
        self.assertEqual(updated["category"]["color"], "#11AA88")

        stats = dashboard.get_stats()
        option = next(
            item for item in stats["category_options"]
            if item["key"] == created["category"]["key"]
        )
        self.assertEqual(option["label"], "Vocal Production")
        self.assertEqual(option["color"], "#11AA88")

    def test_delete_category_clears_project_assignments(self):
        created = dashboard.create_category("Sound Design", "#7c5cff")

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Custom Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

        dashboard.set_project_category("Custom Song", created["category"]["key"])
        deleted = dashboard.delete_category(created["category"]["key"])

        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["cleared_assignments"], 1)

        stats = dashboard.get_stats()
        self.assertFalse(
            any(item["key"] == created["category"]["key"] for item in stats["category_options"])
        )
        self.assertIsNone(stats["projects"][0]["category_key"])

    def test_legacy_seeded_categories_are_purged(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.ensure_category_definitions_table(conn)
            dashboard.ensure_project_category_table(conn)
            conn.execute(
                """
                INSERT INTO category_definitions (key, label, color, updated_at)
                VALUES (?, ?, ?, 0)
                """,
                ("production", "Production", "#00A6FF"),
            )
            conn.execute(
                """
                INSERT INTO project_categories (project_name, category_key, updated_at)
                VALUES (?, ?, 0)
                """,
                ("Legacy Song", "production"),
            )
            conn.commit()
            dashboard.purge_legacy_categories(conn)

        stats = dashboard.get_stats()
        self.assertFalse(any(item["key"] == "production" for item in stats["category_options"]))
        self.assertEqual(stats["custom_category_count"], 0)

    def test_category_table_migration_removes_is_default_column(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute("DROP TABLE IF EXISTS category_definitions")
            conn.execute(
                """
                CREATE TABLE category_definitions (
                    key         TEXT PRIMARY KEY,
                    label       TEXT NOT NULL,
                    color       TEXT NOT NULL,
                    is_default  INTEGER NOT NULL DEFAULT 0,
                    updated_at  INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO category_definitions (key, label, color, is_default, updated_at)
                VALUES (?, ?, ?, ?, 0)
                """,
                ("custom-mixing", "Mixing", "#11AA88", 0),
            )
            conn.commit()

            dashboard.ensure_category_definitions_table(conn)
            columns = [row[1] for row in conn.execute("PRAGMA table_info(category_definitions)").fetchall()]
            row = conn.execute(
                "SELECT key, label, color, updated_at FROM category_definitions WHERE key = ?",
                ("custom-mixing",),
            ).fetchone()

        self.assertEqual(columns, ["key", "label", "color", "updated_at"])
        self.assertEqual(tuple(row), ("custom-mixing", "Mixing", "#11AA88", 0))


class DashboardProjectMetadataTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def _insert_session(self, project_name="Planner Song"):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_name, 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()

    def test_set_project_metadata_persists_and_is_returned_by_stats(self):
        self._insert_session("Planner Song")

        result = dashboard.set_project_metadata(
            "Planner Song",
            "in_progress",
            "personal",
            "high",
            "2026-06-10",
            "2026-06-12",
            "",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["metadata"]["status"], "in_progress")
        self.assertEqual(result["metadata"]["status_label"], "In Progress")
        self.assertEqual(result["metadata"]["type"], "personal")
        self.assertEqual(result["metadata"]["type_label"], "Personal")
        self.assertEqual(result["metadata"]["priority"], "high")
        self.assertEqual(result["metadata"]["priority_label"], "High")
        self.assertEqual(result["metadata"]["due_date"], "2026-06-10")
        self.assertEqual(result["metadata"]["hard_deadline"], "2026-06-12")
        self.assertEqual(result["metadata"]["turn_in_date"], "")

        stats = dashboard.get_stats()
        self.assertEqual(stats["projects"][0]["status"], "in_progress")
        self.assertEqual(stats["projects"][0]["status_label"], "In Progress")
        self.assertEqual(stats["projects"][0]["type"], "personal")
        self.assertEqual(stats["projects"][0]["type_label"], "Personal")
        self.assertEqual(stats["projects"][0]["priority"], "high")
        self.assertEqual(stats["projects"][0]["priority_label"], "High")
        self.assertEqual(stats["projects"][0]["due_date"], "2026-06-10")
        self.assertEqual(stats["projects"][0]["hard_deadline"], "2026-06-12")
        self.assertEqual(stats["projects"][0]["turn_in_date"], "")
        self.assertEqual(stats["recent"][0]["status"], "in_progress")
        self.assertEqual(stats["recent"][0]["type"], "personal")
        self.assertEqual(stats["recent"][0]["priority"], "high")
        self.assertEqual(stats["recent"][0]["due_date"], "2026-06-10")

    def test_set_project_metadata_accepts_all_planner_statuses_and_types(self):
        statuses = {"idea", "in_progress", "finishing", "finished", "paused", "abandoned"}
        types = {"personal", "client", "other"}

        self.assertEqual(set(dashboard.PROJECT_STATUS_OPTIONS), statuses)
        self.assertEqual(set(dashboard.PROJECT_TYPE_OPTIONS), types)

        for index, status in enumerate(statuses):
            result = dashboard.set_project_metadata(f"Project {index}", status, "other")
            self.assertTrue(result["ok"])
            self.assertEqual(result["metadata"]["status"], status)

        for index, project_type in enumerate(types):
            result = dashboard.set_project_metadata(f"Type Project {index}", "idea", project_type)
            self.assertTrue(result["ok"])
            self.assertEqual(result["metadata"]["type"], project_type)

    def test_set_project_metadata_rejects_unknown_values(self):
        bad_status = dashboard.set_project_metadata("Planner Song", "active", "personal")
        bad_type = dashboard.set_project_metadata("Planner Song", "idea", "track")
        bad_priority = dashboard.set_project_metadata("Planner Song", "idea", "personal", "urgent")
        bad_date = dashboard.set_project_metadata(
            "Planner Song", "idea", "personal", "normal", "06/10/2026"
        )

        self.assertEqual(bad_status["error"], "Unknown project status.")
        self.assertEqual(bad_type["error"], "Unknown project type.")
        self.assertEqual(bad_priority["error"], "Unknown project priority.")
        self.assertEqual(bad_date["error"], "Project dates must be empty or YYYY-MM-DD.")

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM project_metadata").fetchone()[0]

        self.assertEqual(count, 0)

    def test_set_project_metadata_clears_existing_metadata(self):
        self._insert_session("Planner Song")
        dashboard.set_project_metadata("Planner Song", "finishing", "client")

        cleared = dashboard.set_project_metadata("Planner Song", "", "")

        self.assertTrue(cleared["ok"])
        self.assertEqual(cleared["metadata"]["status"], "")
        self.assertEqual(cleared["metadata"]["type"], "")
        stats = dashboard.get_stats()
        self.assertEqual(stats["projects"][0]["status"], "")
        self.assertEqual(stats["projects"][0]["status_label"], "")
        self.assertEqual(stats["projects"][0]["type"], "")
        self.assertEqual(stats["projects"][0]["type_label"], "")
        self.assertEqual(stats["projects"][0]["priority"], "")
        self.assertEqual(stats["projects"][0]["due_date"], "")
        self.assertEqual(stats["projects"][0]["hard_deadline"], "")
        self.assertEqual(stats["projects"][0]["turn_in_date"], "")
        self.assertEqual(stats["recent"][0]["status"], "")
        self.assertEqual(stats["recent"][0]["type"], "")

    def test_get_project_metadata_ignores_stale_unknown_values(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.row_factory = tracker.sqlite3.Row
            conn.execute(
                """
                INSERT INTO project_metadata (project_name, status, type, updated_at)
                VALUES (?, ?, ?, 0)
                """,
                ("Legacy Project", "active", "track"),
            )
            conn.commit()
            metadata = dashboard.get_project_metadata(conn)

        self.assertEqual(metadata["Legacy Project"]["status"], "")
        self.assertEqual(metadata["Legacy Project"]["status_label"], "")
        self.assertEqual(metadata["Legacy Project"]["type"], "")
        self.assertEqual(metadata["Legacy Project"]["type_label"], "")

    def test_project_metadata_migration_adds_deadline_columns(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute("DROP TABLE project_metadata")
            conn.execute(
                """
                CREATE TABLE project_metadata (
                    project_name TEXT PRIMARY KEY,
                    status       TEXT NOT NULL DEFAULT '',
                    type         TEXT NOT NULL DEFAULT '',
                    updated_at   INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO project_metadata (project_name, status, type, updated_at)
                VALUES (?, ?, ?, 0)
                """,
                ("Legacy Project", "in_progress", "client"),
            )
            dashboard.ensure_project_metadata_table(conn)
            conn.commit()
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(project_metadata)").fetchall()
            }
            row = conn.execute(
                """
                SELECT priority, due_date, hard_deadline, turn_in_date
                FROM project_metadata
                WHERE project_name = ?
                """,
                ("Legacy Project",),
            ).fetchone()

        self.assertIn("priority", columns)
        self.assertIn("due_date", columns)
        self.assertIn("hard_deadline", columns)
        self.assertIn("turn_in_date", columns)
        self.assertEqual(row, ("", "", "", ""))

    def test_project_deadline_states_are_exposed_in_stats(self):
        today = date.today()
        overdue = (today - timedelta(days=1)).isoformat()
        soon = (today + timedelta(days=2)).isoformat()
        upcoming = (today + timedelta(days=8)).isoformat()

        self._insert_session("Overdue Client")
        self._insert_session("Soon Client")
        self._insert_session("Upcoming Client")
        self._insert_session("Delivered Client")
        dashboard.set_project_metadata("Overdue Client", "in_progress", "client", "high", "", overdue, "")
        dashboard.set_project_metadata("Soon Client", "in_progress", "client", "normal", soon, "", "")
        dashboard.set_project_metadata("Upcoming Client", "in_progress", "client", "normal", upcoming, "", "")
        dashboard.set_project_metadata("Delivered Client", "finished", "client", "normal", overdue, overdue, today.isoformat())

        stats = dashboard.get_stats()
        by_name = {project["project_name"]: project for project in stats["projects"]}

        self.assertEqual(by_name["Overdue Client"]["deadline_state"], "overdue")
        self.assertEqual(by_name["Overdue Client"]["deadline_label"], "Overdue")
        self.assertIn(f"Hard deadline {overdue}", by_name["Overdue Client"]["deadline_reasons"])
        self.assertEqual(by_name["Soon Client"]["deadline_state"], "due_soon")
        self.assertEqual(by_name["Upcoming Client"]["deadline_state"], "upcoming")
        self.assertEqual(by_name["Delivered Client"]["deadline_state"], "delivered")

    def test_project_metadata_changes_invalidate_data_etag(self):
        self._insert_session("Planner Song")
        before = dashboard._compute_data_etag()

        dashboard.set_project_metadata(
            "Planner Song", "in_progress", "client", "normal", "2026-06-10", "", ""
        )
        after_due_date = dashboard._compute_data_etag()
        dashboard.set_project_metadata(
            "Planner Song", "in_progress", "client", "normal", "2026-06-11", "", ""
        )
        after_changed_due_date = dashboard._compute_data_etag()

        self.assertNotEqual(before, after_due_date)
        self.assertNotEqual(after_due_date, after_changed_due_date)

    def test_project_metadata_omitted_fields_preserve_existing_deadlines(self):
        self._insert_session("Planner Song")
        dashboard.set_project_metadata(
            "Planner Song", "in_progress", "client", "high", "2026-06-10", "2026-06-12", ""
        )

        updated = dashboard.set_project_metadata("Planner Song", "finishing", "client")

        self.assertTrue(updated["ok"])
        self.assertEqual(updated["metadata"]["status"], "finishing")
        self.assertEqual(updated["metadata"]["priority"], "high")
        self.assertEqual(updated["metadata"]["due_date"], "2026-06-10")
        self.assertEqual(updated["metadata"]["hard_deadline"], "2026-06-12")

    def test_project_metadata_supports_artist_id(self):
        self._insert_session("Collaboration Song")
        dashboard.set_project_metadata("Collaboration Song", "in_progress", "client", artist_id="artist_123")

        stats = dashboard.get_stats()
        self.assertEqual(stats["projects"][0]["artist_id"], "artist_123")
        self.assertEqual(stats["recent"][0]["artist_id"], "artist_123")


class DashboardArtistTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_create_and_get_artists(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.create_artist(conn, "a1", "Daft Punk", "info@daftpunk.com")
            dashboard.create_artist(conn, "a2", "Justice", "", "", "@justice")
        
        stats = dashboard.get_stats()
        artists = stats["artists"]
        self.assertEqual(len(artists), 2)
        self.assertEqual(artists[0]["name"], "Daft Punk")
        self.assertEqual(artists[0]["email"], "info@daftpunk.com")
        self.assertEqual(artists[1]["name"], "Justice")
        self.assertEqual(artists[1]["instagram"], "@justice")

    def test_update_artist(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.create_artist(conn, "a1", "Daft Punk")
            dashboard.update_artist(conn, "a1", "Daft Punk Updated", "dp@example.com", "555", "@dp")
            
        stats = dashboard.get_stats()
        artist = stats["artists"][0]
        self.assertEqual(artist["name"], "Daft Punk Updated")
        self.assertEqual(artist["email"], "dp@example.com")
        self.assertEqual(artist["phone"], "555")
        self.assertEqual(artist["instagram"], "@dp")

    def test_delete_artist_clears_project_artist_id(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.create_artist(conn, "a1", "Daft Punk")
            conn.execute(
                "INSERT INTO project_metadata (project_name, artist_id, updated_at) VALUES (?, ?, 0)",
                ("Collab Song", "a1")
            )
            conn.commit()

            dashboard.delete_artist(conn, "a1")
            
            row = conn.execute("SELECT artist_id FROM project_metadata WHERE project_name = 'Collab Song'").fetchone()
            self.assertEqual(row[0], "")

        stats = dashboard.get_stats()
        self.assertEqual(len(stats["artists"]), 0)


class DashboardProjectTaskTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_project_tasks_table_migration_adds_expected_columns(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(project_tasks)").fetchall()
            }

        self.assertEqual(
            columns,
            {
                "id",
                "project_name",
                "title",
                "status",
                "priority",
                "due_date",
                "completed_at",
                "sort_order",
                "created_at",
                "updated_at",
            },
        )

    def test_create_and_list_project_tasks(self):
        first = dashboard.create_project_task(
            "Planner Song", "Record vocals", "high", "2026-06-05", 2
        )
        second = dashboard.create_project_task("Planner Song", "Export rough", "normal", "", 1)

        self.assertTrue(first["ok"])
        self.assertEqual(first["task"]["project_name"], "Planner Song")
        self.assertEqual(first["task"]["title"], "Record vocals")
        self.assertEqual(first["task"]["status"], "open")
        self.assertEqual(first["task"]["priority"], "high")
        self.assertEqual(first["task"]["due_date"], "2026-06-05")
        self.assertIsNone(first["task"]["completed_at"])

        result = dashboard.get_project_tasks_response("Planner Song")
        self.assertTrue(result["ok"])
        self.assertEqual([task["title"] for task in result["tasks"]], ["Export rough", "Record vocals"])
        self.assertEqual(result["tasks"][0]["id"], second["task"]["id"])

    def test_update_project_task_fields(self):
        created = dashboard.create_project_task("Planner Song", "Record scratch", "normal")

        updated = dashboard.update_project_task(
            created["task"]["id"],
            {
                "title": "Record final vocal",
                "priority": "high",
                "due_date": "2026-06-06",
                "sort_order": 7,
            },
        )

        self.assertTrue(updated["ok"])
        self.assertEqual(updated["task"]["title"], "Record final vocal")
        self.assertEqual(updated["task"]["status"], "open")
        self.assertEqual(updated["task"]["priority"], "high")
        self.assertEqual(updated["task"]["due_date"], "2026-06-06")
        self.assertEqual(updated["task"]["sort_order"], 7)

    def test_complete_and_reopen_project_task(self):
        created = dashboard.create_project_task("Client Mix", "Send bounce")

        completed = dashboard.update_project_task(created["task"]["id"], {"status": "done"})
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["task"]["status"], "done")
        self.assertIsNotNone(completed["task"]["completed_at"])

        reopened = dashboard.update_project_task(created["task"]["id"], {"status": "open"})
        self.assertTrue(reopened["ok"])
        self.assertEqual(reopened["task"]["status"], "open")
        self.assertIsNone(reopened["task"]["completed_at"])

    def test_delete_project_task(self):
        created = dashboard.create_project_task("Planner Song", "Delete me")

        deleted = dashboard.delete_project_task(created["task"]["id"])

        self.assertTrue(deleted["ok"])
        self.assertEqual(deleted["deleted"], 1)
        result = dashboard.get_project_tasks_response("Planner Song")
        self.assertEqual(result["tasks"], [])

    def test_project_task_validation(self):
        missing_project = dashboard.create_project_task("", "Record vocals")
        missing_title = dashboard.create_project_task("Planner Song", "")
        bad_priority = dashboard.create_project_task("Planner Song", "Record vocals", "urgent")
        created = dashboard.create_project_task("Planner Song", "Record vocals")
        bad_status = dashboard.update_project_task(created["task"]["id"], {"status": "blocked"})
        missing_id = dashboard.update_project_task("nope", {"status": "done"})
        not_found = dashboard.delete_project_task(9999)

        self.assertEqual(missing_project["error"], "Project name is required.")
        self.assertEqual(missing_title["error"], "Task title is required.")
        self.assertEqual(bad_priority["error"], "Unknown task priority.")
        self.assertEqual(bad_status["error"], "Unknown task status.")
        self.assertEqual(missing_id["error"], "Task id is required.")
        self.assertEqual(not_found["error"], "Task not found.")

    def test_stats_project_rows_include_project_tasks(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Planner Song", 100.0, 200.0, 200.0, 100.0),
            )
            conn.commit()
        dashboard.create_project_task("Planner Song", "Arrange bridge")

        stats = dashboard.get_stats()

        self.assertEqual(len(stats["projects"][0]["project_tasks"]), 1)
        self.assertEqual(stats["projects"][0]["project_tasks"][0]["title"], "Arrange bridge")

    def test_project_tasks_preserve_session_todos(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, todos_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Planner Song", 100.0, 200.0, 200.0, 100.0,
                 '[{"text":"Session-only task","done":false}]'),
            )
            session_id = cur.lastrowid
            conn.commit()

        created = dashboard.create_project_task("Planner Song", "Project-level task")
        dashboard.update_project_task(created["task"]["id"], {"status": "done"})
        dashboard.delete_project_task(created["task"]["id"])

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT todos_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()

        self.assertIn("Session-only task", row[0])


class DashboardPlannerGoalTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def _insert_session(self, project_name, start_dt, seconds=3600):
        start_ts = start_dt.timestamp()
        end_ts = start_ts + seconds
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                (project_name, start_ts, end_ts, end_ts, seconds),
            )
            conn.commit()

    def test_planner_goals_table_migration_adds_expected_columns(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(planner_goals)").fetchall()
            }

        self.assertEqual(
            columns,
            {
                "id",
                "goal_type",
                "target_value",
                "period",
                "scope_type",
                "scope_value",
                "active",
                "created_at",
                "updated_at",
            },
        )

    def test_create_list_update_and_delete_planner_goal(self):
        created = dashboard.create_planner_goal(
            "sessions_per_week", 3, "week", "project_type", "client"
        )

        self.assertTrue(created["ok"])
        self.assertEqual(created["goal"]["goal_type"], "sessions_per_week")
        self.assertEqual(created["goal"]["target_value"], 3.0)
        self.assertEqual(created["goal"]["scope_type"], "project_type")
        self.assertEqual(created["goal"]["scope_value"], "client")
        self.assertTrue(created["goal"]["active"])

        listed = dashboard.get_planner_goals_response()
        self.assertTrue(listed["ok"])
        self.assertEqual(len(listed["goals"]), 1)

        updated = dashboard.update_planner_goal(
            created["goal"]["id"],
            {"target_value": 5, "scope_type": "all", "active": False},
        )
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["goal"]["target_value"], 5.0)
        self.assertEqual(updated["goal"]["scope_type"], "all")
        self.assertEqual(updated["goal"]["scope_value"], "")
        self.assertFalse(updated["goal"]["active"])

        deleted = dashboard.delete_planner_goal(created["goal"]["id"])
        self.assertTrue(deleted["ok"])
        self.assertEqual(dashboard.get_planner_goals_response()["goals"], [])

    def test_planner_goal_validation(self):
        bad_type = dashboard.create_planner_goal("daily_sessions", 3)
        bad_period = dashboard.create_planner_goal("sessions_per_week", 3, "day")
        bad_scope = dashboard.create_planner_goal("sessions_per_week", 3, "week", "artist")
        missing_scope = dashboard.create_planner_goal("sessions_per_week", 3, "week", "project")
        bad_target = dashboard.create_planner_goal("sessions_per_week", "many")
        zero_target = dashboard.create_planner_goal("sessions_per_week", 0)
        fractional_target = dashboard.create_planner_goal("sessions_per_week", 1.5)
        missing_id = dashboard.update_planner_goal("nope", {"target_value": 3})
        not_found = dashboard.delete_planner_goal(9999)

        self.assertEqual(bad_type["error"], "Unknown planner goal type.")
        self.assertEqual(bad_period["error"], "Unknown planner goal period.")
        self.assertEqual(bad_scope["error"], "Unknown planner goal scope.")
        self.assertEqual(missing_scope["error"], "Planner goal scope value is required.")
        self.assertEqual(bad_target["error"], "Planner goal target must be a number.")
        self.assertEqual(zero_target["error"], "Planner goal target must be greater than zero.")
        self.assertEqual(fractional_target["error"], "Planner goal target must be a whole number.")
        self.assertEqual(missing_id["error"], "Planner goal id is required.")
        self.assertEqual(not_found["error"], "Planner goal not found.")

    def test_sessions_and_hours_goal_progress_respects_scope(self):
        today = date.today()
        week_start, _week_end = dashboard.get_week_range(today)
        client_start = datetime.combine(week_start + timedelta(days=1), datetime.min.time())
        personal_start = datetime.combine(week_start + timedelta(days=2), datetime.min.time())
        self._insert_session("Client Mix", client_start, 7200)
        self._insert_session("Personal Song", personal_start, 3600)
        dashboard.set_project_metadata("Client Mix", "in_progress", "client")
        dashboard.set_project_metadata("Personal Song", "in_progress", "personal")

        sessions_goal = dashboard.create_planner_goal(
            "sessions_per_week", 2, "week", "project_type", "client"
        )
        hours_goal = dashboard.create_planner_goal(
            "hours_per_week", 3, "week", "project_type", "client"
        )

        self.assertEqual(sessions_goal["goal"]["progress"]["current_value"], 1.0)
        self.assertEqual(sessions_goal["goal"]["progress"]["unit"], "sessions")
        self.assertEqual(hours_goal["goal"]["progress"]["current_value"], 2.0)
        self.assertEqual(hours_goal["goal"]["progress"]["remaining_value"], 1.0)

    def test_finished_project_goal_counts_current_finished_without_finish_timestamp(self):
        self._insert_session("Finished Song", datetime.combine(date.today(), datetime.min.time()))
        self._insert_session("Active Song", datetime.combine(date.today(), datetime.min.time()))
        dashboard.set_project_metadata("Finished Song", "finished", "personal")
        dashboard.set_project_metadata("Active Song", "in_progress", "personal")

        goal = dashboard.create_planner_goal(
            "projects_finished_per_period", 1, "month", "project_type", "personal"
        )

        self.assertEqual(goal["goal"]["progress"]["current_value"], 1.0)
        self.assertEqual(goal["goal"]["progress"]["label"], "Finished projects this period")

    def test_finished_project_goal_uses_turn_in_date_when_available(self):
        today = date.today()
        previous_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        self._insert_session("Old Client", datetime.combine(today, datetime.min.time()))
        self._insert_session("New Client", datetime.combine(today, datetime.min.time()))
        dashboard.set_project_metadata("Old Client", "finished", "client", "normal", "", "", previous_month.isoformat())
        dashboard.set_project_metadata("New Client", "finished", "client", "normal", "", "", today.isoformat())

        goal = dashboard.create_planner_goal(
            "projects_finished_per_period", 2, "month", "project_type", "client"
        )

        self.assertEqual(goal["goal"]["progress"]["current_value"], 1.0)

    def test_touch_active_project_goal_counts_recently_touched_active_projects(self):
        today = date.today()
        recent = datetime.combine(today - timedelta(days=1), datetime.min.time())
        stale = datetime.combine(today - timedelta(days=8), datetime.min.time())
        self._insert_session("Recent Active", recent, 1800)
        self._insert_session("Stale Active", stale, 1800)
        self._insert_session("Finished Project", recent, 1800)
        dashboard.set_project_metadata("Recent Active", "in_progress", "personal")
        dashboard.set_project_metadata("Stale Active", "finishing", "personal")
        dashboard.set_project_metadata("Finished Project", "finished", "personal")

        goal = dashboard.create_planner_goal(
            "touch_active_project_every_n_days", 3, "week", "project_type", "personal"
        )

        progress = goal["goal"]["progress"]
        self.assertEqual(progress["current_value"], 1.0)
        self.assertEqual(progress["target_value"], 2.0)
        self.assertEqual(progress["total_active_projects"], 2)

    def test_stats_include_planner_goals_and_goal_changes_invalidate_etag(self):
        before = dashboard._compute_data_etag()
        created = dashboard.create_planner_goal("hours_per_week", 4, "week", "all")
        after_create = dashboard._compute_data_etag()
        dashboard.update_planner_goal(created["goal"]["id"], {"target_value": 5})
        after_update = dashboard._compute_data_etag()

        stats = dashboard.get_stats()

        self.assertNotEqual(before, after_create)
        self.assertNotEqual(after_create, after_update)
        self.assertEqual(len(stats["planner_goals"]), 1)
        self.assertEqual(stats["planner_goals"][0]["goal_type"], "hours_per_week")
        self.assertIn("progress", stats["planner_goals"][0])


class DashboardWeeklyTargetTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_friday_week_range_uses_previous_friday_through_thursday(self):
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 24), week_start_weekday=4),
            (date(2026, 4, 24), date(2026, 4, 30)),
        )
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 27), week_start_weekday=4),
            (date(2026, 4, 24), date(2026, 4, 30)),
        )
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 30), week_start_weekday=4),
            (date(2026, 4, 24), date(2026, 4, 30)),
        )
        self.assertEqual(
            dashboard.get_week_range(date(2026, 5, 1), week_start_weekday=4),
            (date(2026, 5, 1), date(2026, 5, 7)),
        )

    def test_weekly_target_aggregates_friday_to_thursday_progress(self):
        friday_start = datetime(2026, 4, 24, 10, 0).timestamp()
        friday_end = datetime(2026, 4, 24, 12, 0).timestamp()
        thursday_start = datetime(2026, 4, 30, 13, 0).timestamp()
        thursday_end = datetime(2026, 4, 30, 14, 0).timestamp()
        next_friday_start = datetime(2026, 5, 1, 10, 0).timestamp()
        next_friday_end = datetime(2026, 5, 1, 12, 0).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("Friday", friday_start, friday_end, friday_end, 7200.0),
                    ("Thursday", thursday_start, thursday_end, thursday_end, 3600.0),
                    ("Next Friday", next_friday_start, next_friday_end, next_friday_end, 7200.0),
                ],
            )
            conn.commit()

        target = dashboard.get_weekly_target("2026-04-27")

        self.assertEqual(target["week_start"], "2026-04-24")
        self.assertEqual(target["week_end"], "2026-04-30")
        self.assertEqual(target["weekly_start_date"], "2026-04-24")
        self.assertEqual(target["weekly_end_date"], "2026-04-30")
        self.assertEqual(target["progress_seconds"], 10800)
        self.assertEqual(target["reset_at"], "2026-05-01T00:00:00")
        self.assertGreaterEqual(target["seconds_until_reset"], 0)

    def test_weekly_target_is_independent_from_daily_goals(self):
        friday_start = datetime(2026, 4, 24, 10, 0).timestamp()
        friday_end = datetime(2026, 4, 24, 12, 0).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Friday", friday_start, friday_end, friday_end, 7200.0),
            )
            conn.commit()

        baseline = dashboard.get_weekly_target("2026-04-27")

        dashboard.set_daily_target("2026-04-24", 2.5)
        dashboard.set_daily_target("2026-04-30", 3.5)
        dashboard.set_daily_target("2026-04-27", 8.0)

        after_daily_changes = dashboard.get_weekly_target("2026-04-27")

        self.assertEqual(after_daily_changes["progress_seconds"], baseline["progress_seconds"])
        self.assertEqual(after_daily_changes["goal_hours"], baseline["goal_hours"])
        self.assertFalse(after_daily_changes["has_target"])
        self.assertNotIn("goal_day_count", after_daily_changes)

    # ── configurable week start ───────────────────────────────────

    def test_week_range_defaults_to_friday(self):
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 27)),
            (date(2026, 4, 24), date(2026, 4, 30)),
        )

    def test_week_range_monday_start(self):
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 27), week_start_weekday=0),
            (date(2026, 4, 27), date(2026, 5, 3)),
        )

    def test_week_range_sunday_start(self):
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 27), week_start_weekday=6),
            (date(2026, 4, 26), date(2026, 5, 2)),
        )

    def test_week_range_saturday_start(self):
        self.assertEqual(
            dashboard.get_week_range(date(2026, 4, 27), week_start_weekday=5),
            (date(2026, 4, 25), date(2026, 5, 1)),
        )

    def test_app_settings_set_and_get(self):
        dashboard.set_app_setting("week_start_weekday", "2")
        self.assertEqual(dashboard.get_app_setting("week_start_weekday"), "2")

    def test_app_settings_default_when_missing(self):
        self.assertEqual(dashboard.get_app_setting("nonexistent", "pancakes"), "pancakes")

    def test_app_settings_none_default(self):
        self.assertIsNone(dashboard.get_app_setting("never_set"))

    def test_get_all_app_settings_returns_dict(self):
        dashboard.set_app_setting("a", "1")
        dashboard.set_app_setting("b", "2")
        settings = dashboard.get_all_app_settings()
        self.assertEqual(settings.get("a"), "1")
        self.assertEqual(settings.get("b"), "2")

    def test_session_notes_migration_adds_todo_notes_column(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
        self.assertIn("notes", columns)
        self.assertIn("todo_notes", columns)

    def test_set_session_notes_saves_worked_on_and_todos(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Notes Project", 100.0, 200.0, 200.0, 100.0),
            )
            session_id = cur.lastrowid
            conn.commit()

        result = dashboard.set_session_notes(session_id, "Built drums", "Bounce stems")

        self.assertTrue(result["ok"])
        self.assertEqual(result["notes"], "Built drums")
        self.assertEqual(result["todo_notes"], "Bounce stems")
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT notes, todo_notes FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "Built drums")
        self.assertEqual(row[1], "Bounce stems")

    def test_clear_session_notes_preserves_sessions(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todo_notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Notes Project",
                    100.0,
                    200.0,
                    200.0,
                    100.0,
                    "Built drums",
                    "Bounce stems",
                    '[{"text":"Bounce stems","done":false}]',
                ),
            )
            session_id = cur.lastrowid
            conn.commit()

        result = dashboard.clear_session_notes()

        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], 1)
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT project_name, active_seconds, notes, todo_notes, todos_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "Notes Project")
        self.assertEqual(row[1], 100.0)
        self.assertEqual(row[2], "")
        self.assertEqual(row[3], "")
        self.assertEqual(row[4], "[]")

    def test_recent_payload_includes_session_todo_notes(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todo_notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("Notes Project", 100.0, 200.0, 200.0, 100.0, "Built drums", "Bounce stems"),
            )
            session_id = cur.lastrowid
            conn.commit()

        stats = dashboard.get_stats()
        recent = stats["recent"][0]

        self.assertEqual(recent["session_notes"][str(session_id)], "Built drums")
        self.assertEqual(recent["session_todo_notes"][str(session_id)], "Bounce stems")

    def test_todos_json_migration_adds_todos_json_column(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
        self.assertIn("todos_json", columns)

    def test_set_session_notes_saves_structured_todos(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Notes Project", 100.0, 200.0, 200.0, 100.0),
            )
            session_id = cur.lastrowid
            conn.commit()

        result = dashboard.set_session_notes(
            session_id, "Built drums", "",
            [{"text": "Bounce stems", "done": False}, {"text": "Export mix", "done": True}]
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["todo_notes"], "Bounce stems | Export mix")
        self.assertEqual(len(result["todos"]), 2)
        self.assertEqual(result["todos"][0]["text"], "Bounce stems")
        self.assertFalse(result["todos"][0]["done"])
        self.assertTrue(result["todos"][1]["done"])
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            row = conn.execute(
                "SELECT notes, todo_notes, todos_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "Built drums")
        self.assertEqual(row[1], "Bounce stems | Export mix")
        self.assertIn("Bounce stems", row[2])

    def test_get_last_session_todos_returns_todos_for_project(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, todos_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Alpha", 100.0, 200.0, 200.0, 100.0,
                 '[{"text":"Fix bass","done":false}]'),
            )
            conn.commit()

        result = dashboard.get_last_session_todos("Alpha")
        self.assertEqual(len(result["todos"]), 1)
        self.assertEqual(result["todos"][0]["text"], "Fix bass")
        self.assertEqual(result["project_name"], "Alpha")

    def test_get_last_session_todos_empty_when_none(self):
        result = dashboard.get_last_session_todos("Nonexistent")
        self.assertEqual(result["todos"], [])

    def test_get_last_session_todos_filters_by_project(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, todos_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Alpha", 100.0, 200.0, 200.0, 100.0,
                 '[{"text":"Alpha task","done":false}]'),
            )
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, todos_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Beta", 150.0, 250.0, 250.0, 100.0,
                 '[{"text":"Beta task","done":false}]'),
            )
            conn.commit()

        alpha = dashboard.get_last_session_todos("Alpha")
        beta = dashboard.get_last_session_todos("Beta")
        self.assertEqual(alpha["todos"][0]["text"], "Alpha task")
        self.assertEqual(beta["todos"][0]["text"], "Beta task")

    def test_get_session_notes_entry_returns_project_neighbors(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            first = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("Alpha", 100.0, 160.0, 160.0, 60.0, "First", '[{"text":"First task","done":false}]'),
            ).lastrowid
            second = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("Alpha", 200.0, 260.0, 260.0, 60.0, "Second", "[]"),
            ).lastrowid
            third = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("Alpha", 300.0, 360.0, 360.0, 60.0, "Third", "[]"),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, notes, todos_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("Beta", 400.0, 460.0, 460.0, 60.0, "Beta", "[]"),
            )
            conn.commit()

        result = dashboard.get_session_notes_entry(second, "Alpha")

        self.assertTrue(result["ok"])
        self.assertEqual(result["session"]["id"], second)
        self.assertEqual(result["session"]["notes"], "Second")
        self.assertEqual(result["previous_session_id"], first)
        self.assertEqual(result["next_session_id"], third)

    def test_recent_payload_includes_session_todos(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds, todos_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("Notes Project", 100.0, 200.0, 200.0, 100.0,
                 '[{"text":"Mix drums","done":true}]'),
            )
            session_id = cur.lastrowid
            conn.commit()

        stats = dashboard.get_stats()
        recent = stats["recent"][0]

        self.assertIn(str(session_id), recent["session_todos"])
        self.assertEqual(recent["session_todos"][str(session_id)][0]["text"], "Mix drums")
        self.assertTrue(recent["session_todos"][str(session_id)][0]["done"])

    def test_weekly_target_includes_week_start_weekday(self):
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Test", datetime(2026, 4, 24, 10, 0).timestamp(),
                 datetime(2026, 4, 24, 12, 0).timestamp(),
                 datetime(2026, 4, 24, 12, 0).timestamp(), 7200.0),
            )
            conn.commit()

        target = dashboard.get_weekly_target("2026-04-27")
        self.assertEqual(target["week_start_weekday"], 4)
        self.assertEqual(target["week_start_weekday_name"], "Friday")

    def test_weekly_target_respects_custom_week_start(self):
        dashboard.set_app_setting("week_start_weekday", "0")  # Monday
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Test", datetime(2026, 4, 27, 10, 0).timestamp(),
                 datetime(2026, 4, 27, 12, 0).timestamp(),
                 datetime(2026, 4, 27, 12, 0).timestamp(), 7200.0),
            )
            conn.commit()

        target = dashboard.get_weekly_target("2026-04-27")
        self.assertEqual(target["week_start"], "2026-04-27")
        self.assertEqual(target["week_end"], "2026-05-03")
        self.assertEqual(target["week_start_weekday"], 0)
        self.assertEqual(target["week_start_weekday_name"], "Monday")


class DashboardRolloverTests(unittest.TestCase):
    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db_path = tracker.Path(path)
        self.addCleanup(self._cleanup_db)

        tracker.DB_PATH = self.db_path
        dashboard.DB_PATH = self.db_path
        tracker.setup_db()
        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            dashboard.run_schema_migrations(conn)

    def _cleanup_db(self):
        for suffix in ("", "-shm", "-wal"):
            try:
                (tracker.Path(str(self.db_path) + suffix)).unlink()
            except FileNotFoundError:
                pass

    def test_cross_midnight_session_counts_toward_new_day_and_hour(self):
        start_ts = datetime(2026, 4, 24, 23, 59).timestamp()
        last_seen_ts = datetime(2026, 4, 25, 0, 3).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Late Session", start_ts, last_seen_ts, None, 240.0),
            )
            conn.commit()

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 4, 25)

        with patch.object(dashboard, "date", FrozenDate):
            stats = dashboard.get_stats()

        self.assertEqual(stats["summary"]["today_seconds"], 180.0)
        self.assertEqual(stats["summary"]["week_seconds"], 240.0)

        daily = {row["day"]: row["total_seconds"] for row in stats["year_daily"]}
        self.assertEqual(daily["2026-04-24"], 60.0)
        self.assertEqual(daily["2026-04-25"], 180.0)

        hourly = {
            (row["day"], row["hour"]): row["active_seconds"]
            for row in stats["year_hourly"]
        }
        self.assertEqual(hourly[("2026-04-24", 23)], 60.0)
        self.assertEqual(hourly[("2026-04-25", 0)], 180.0)
        self.assertEqual(stats["summary"]["streak_days"], 2)

    def test_streak_does_not_reset_right_after_midnight_without_activity_yet(self):
        session_1_start_ts = datetime(2026, 4, 23, 12, 0).timestamp()
        session_1_end_ts = datetime(2026, 4, 23, 12, 10).timestamp()
        session_2_start_ts = datetime(2026, 4, 24, 12, 0).timestamp()
        session_2_end_ts = datetime(2026, 4, 24, 12, 10).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Day 1", session_1_start_ts, session_1_end_ts, session_1_end_ts, 600.0),
            )
            conn.execute(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Day 2", session_2_start_ts, session_2_end_ts, session_2_end_ts, 600.0),
            )
            conn.commit()

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 4, 25)

        with patch.object(dashboard, "date", FrozenDate):
            stats = dashboard.get_stats()

        self.assertEqual(stats["summary"]["streak_days"], 2)

    def test_today_reflection_stats_use_only_todays_allocated_time(self):
        late_start_ts = datetime(2026, 4, 24, 23, 50).timestamp()
        late_end_ts = datetime(2026, 4, 25, 0, 10).timestamp()
        today_1_start_ts = datetime(2026, 4, 25, 10, 0).timestamp()
        today_1_end_ts = datetime(2026, 4, 25, 10, 20).timestamp()
        today_2_start_ts = datetime(2026, 4, 25, 13, 0).timestamp()
        today_2_end_ts = datetime(2026, 4, 25, 13, 15).timestamp()
        yesterday_start_ts = datetime(2026, 4, 24, 14, 0).timestamp()
        yesterday_end_ts = datetime(2026, 4, 24, 14, 30).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("Song A", late_start_ts, late_end_ts, late_end_ts, 1200.0),
                    ("Song A", today_1_start_ts, today_1_end_ts, today_1_end_ts, 1200.0),
                    ("Song B", today_2_start_ts, today_2_end_ts, today_2_end_ts, 900.0),
                    ("Yesterday", yesterday_start_ts, yesterday_end_ts, yesterday_end_ts, 1800.0),
                ],
            )
            conn.commit()

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 4, 25)

        with patch.object(dashboard, "date", FrozenDate):
            stats = dashboard.get_stats()

        self.assertEqual(stats["summary"]["today_seconds"], 2700.0)
        self.assertEqual(stats["summary"]["today_session_count"], 3)
        self.assertEqual(stats["summary"]["today_project_count"], 2)
        self.assertEqual(stats["summary"]["today_average_session_seconds"], 900.0)

    def test_selected_month_uses_allocated_time_within_that_month(self):
        april_start_ts = datetime(2026, 4, 15, 10, 0).timestamp()
        april_end_ts = datetime(2026, 4, 15, 12, 0).timestamp()
        crossing_start_ts = datetime(2026, 4, 30, 23, 30).timestamp()
        crossing_end_ts = datetime(2026, 5, 1, 0, 30).timestamp()
        may_start_ts = datetime(2026, 5, 3, 9, 0).timestamp()
        may_end_ts = datetime(2026, 5, 3, 10, 0).timestamp()

        with closing(tracker.sqlite3.connect(tracker.DB_PATH)) as conn:
            conn.executemany(
                """
                INSERT INTO sessions (project_name, start_time, last_seen_time, end_time, active_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("April Song", april_start_ts, april_end_ts, april_end_ts, 7200.0),
                    ("Boundary Song", crossing_start_ts, crossing_end_ts, crossing_end_ts, 3600.0),
                    ("May Song", may_start_ts, may_end_ts, may_end_ts, 3600.0),
                ],
            )
            conn.commit()

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 5, 10)

        with patch.object(dashboard, "date", FrozenDate):
            april_stats = dashboard.get_stats("2026-04")
            may_stats = dashboard.get_stats("2026-05")

        april_projects = {row["project_name"]: row["month_seconds"] for row in april_stats["projects"]}
        may_projects = {row["project_name"]: row["month_seconds"] for row in may_stats["projects"]}

        self.assertEqual(april_stats["summary"]["selected_month"], "2026-04")
        self.assertEqual(april_stats["summary"]["month_seconds"], 9000.0)
        self.assertEqual(april_stats["summary"]["month_project_count"], 2)
        self.assertEqual(april_projects["April Song"], 7200.0)
        self.assertEqual(april_projects["Boundary Song"], 1800.0)
        self.assertEqual(may_stats["summary"]["selected_month"], "2026-05")
        self.assertEqual(may_stats["summary"]["month_seconds"], 5400.0)
        self.assertEqual(may_projects["Boundary Song"], 1800.0)
        self.assertEqual(may_projects["May Song"], 3600.0)


if __name__ == "__main__":
    unittest.main()
