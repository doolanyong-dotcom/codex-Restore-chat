from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from sync_backend import (
    disable_share_mode,
    enable_share_mode,
    get_share_mode_status,
    get_status,
    make_backup,
    resolve_paths,
    restore_backup,
    sync_share_once,
    sync_to_current_provider,
)


def write_config(codex_home, provider: str = "new_provider", model: str = "gpt-new") -> None:
    (codex_home / "config.toml").write_text(
        f'model_provider = "{provider}"\nmodel = "{model}"\n',
        encoding="utf-8",
    )


def create_threads_db(codex_home, *, with_model: bool = True) -> None:
    conn = sqlite3.connect(codex_home / "state_5.sqlite")
    if with_model:
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL, model TEXT)")
        conn.executemany(
            "INSERT INTO threads (id, model_provider, model) VALUES (?, ?, ?)",
            [
                ("old-provider-old-model", "old_provider", "gpt-old"),
                ("new-provider-old-model", "new_provider", "gpt-old"),
                ("already-current", "new_provider", "gpt-new"),
            ],
        )
    else:
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO threads (id, model_provider) VALUES (?, ?)",
            [
                ("old-provider", "old_provider"),
                ("already-current", "new_provider"),
            ],
        )
    conn.commit()
    conn.close()


def create_visibility_threads_db(codex_home) -> None:
    conn = sqlite3.connect(codex_home / "state_5.sqlite")
    conn.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            model_provider TEXT NOT NULL,
            model TEXT,
            cwd TEXT,
            has_user_event INTEGER NOT NULL,
            first_user_message TEXT,
            archived INTEGER NOT NULL,
            archived_at INTEGER,
            title TEXT,
            updated_at INTEGER
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO threads (
            id, model_provider, model, cwd, has_user_event, first_user_message,
            archived, archived_at, title, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "hidden-thread",
                "old_provider",
                "gpt-old",
                r"\\?\E:\Workspace\old",
                0,
                "hello",
                1,
                12345,
                "Hidden thread",
                1710000000,
            ),
            (
                "already-current",
                "new_provider",
                "gpt-new",
                r"E:\Workspace\codex-history-sync-tool",
                1,
                "hi",
                0,
                None,
                "Current thread",
                1710000100,
            ),
        ],
    )
    conn.commit()
    conn.close()


def write_session_meta(codex_home, folder: str, thread_id: str, provider: str, model: str) -> Path:
    session_dir = codex_home / folder / "2026" / "06"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"rollout-2026-06-05-{thread_id}.jsonl"
    path.write_text(
        (
            '{"type":"session_meta","payload":'
            f'{{"id":"{thread_id}","model_provider":"{provider}","model":"{model}"}}}}\n'
            '{"type":"user","payload":{"text":"hello"}}\n'
        ),
        encoding="utf-8",
        newline="",
    )
    return path


def write_invalid_session_meta(codex_home, thread_id: str) -> Path:
    session_dir = codex_home / "sessions" / "2026" / "06"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"rollout-2026-06-05-{thread_id}.jsonl"
    path.write_text(
        'not-json\n{"type":"user","payload":{"text":"hello"}}\n',
        encoding="utf-8",
        newline="",
    )
    return path


class SyncBackendTests(unittest.TestCase):
    def test_sync_updates_provider_and_model_for_newer_codex_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir)
            write_config(codex_home)
            create_threads_db(codex_home, with_model=True)
            paths = resolve_paths(str(codex_home))

            status = get_status(paths)

            self.assertEqual(status["provider_movable_threads"], 1)
            self.assertEqual(status["model_movable_threads"], 2)
            self.assertEqual(status["movable_threads"], 2)

            result = sync_to_current_provider(paths)

            self.assertEqual(result["synced_fields"], ["model_provider", "model"])
            self.assertEqual(result["updated_rows"], 2)

            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn:
                rows = conn.execute(
                    "SELECT model_provider, model, COUNT(*) FROM threads GROUP BY model_provider, model"
                ).fetchall()

            self.assertEqual(rows, [("new_provider", "gpt-new", 3)])

    def test_sync_still_supports_legacy_schema_without_model_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir)
            write_config(codex_home)
            create_threads_db(codex_home, with_model=False)
            paths = resolve_paths(str(codex_home))

            status = get_status(paths)

            self.assertEqual(status["provider_movable_threads"], 1)
            self.assertIsNone(status["model_movable_threads"])
            self.assertEqual(status["movable_threads"], 1)

            result = sync_to_current_provider(paths)

            self.assertEqual(result["synced_fields"], ["model_provider"])
            self.assertEqual(result["updated_rows"], 1)

            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn:
                rows = conn.execute("SELECT model_provider, COUNT(*) FROM threads GROUP BY model_provider").fetchall()

            self.assertEqual(rows, [("new_provider", 2)])

    def test_restore_backup_restores_previous_database_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir)
            write_config(codex_home)
            create_threads_db(codex_home, with_model=True)
            paths = resolve_paths(str(codex_home))
            backup_path = make_backup(paths, "manual")

            sync_to_current_provider(paths)
            result = restore_backup(paths, str(backup_path))

            self.assertEqual(result["restored_from"], str(backup_path))
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn:
                rows = conn.execute(
                    "SELECT model_provider, model, COUNT(*) FROM threads GROUP BY model_provider, model ORDER BY model_provider, model"
                ).fetchall()

            self.assertEqual(
                rows,
                [
                    ("new_provider", "gpt-new", 1),
                    ("new_provider", "gpt-old", 1),
                    ("old_provider", "gpt-old", 1),
                ],
            )

    def test_sync_repairs_visibility_filters_in_one_restore_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir)
            write_config(codex_home)
            create_visibility_threads_db(codex_home)
            paths = resolve_paths(str(codex_home))

            status = get_status(paths)

            self.assertEqual(status["cwd_prefix_threads"], 1)
            self.assertEqual(status["missing_user_event_threads"], 1)
            self.assertEqual(status["archived_threads"], 1)
            self.assertEqual(status["visibility_movable_threads"], 1)
            self.assertEqual(status["movable_threads"], 1)

            result = sync_to_current_provider(paths)

            self.assertEqual(result["visibility_updates"]["normalized_cwd"], 1)
            self.assertEqual(result["visibility_updates"]["set_has_user_event"], 1)
            self.assertEqual(result["visibility_updates"]["unarchived"], 1)
            self.assertEqual(result["rewritten_index_entries"], 2)

            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as conn:
                row = conn.execute(
                    """
                    SELECT model_provider, model, cwd, has_user_event, archived, archived_at
                    FROM threads
                    WHERE id='hidden-thread'
                    """
                ).fetchone()

            self.assertEqual(row, ("new_provider", "gpt-new", r"E:\Workspace\old", 1, 0, None))
            self.assertIn('"id":"hidden-thread"', (codex_home / "session_index.jsonl").read_text(encoding="utf-8"))

    def test_share_once_updates_active_and_archived_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir)
            write_config(codex_home)
            create_threads_db(codex_home, with_model=True)
            active_path = write_session_meta(
                codex_home,
                "sessions",
                "11111111-1111-1111-1111-111111111111",
                "old_provider",
                "gpt-old",
            )
            archived_path = write_session_meta(
                codex_home,
                "archived_sessions",
                "22222222-2222-2222-2222-222222222222",
                "old_provider",
                "gpt-old",
            )
            paths = resolve_paths(str(codex_home))

            result = sync_share_once(paths)

            self.assertTrue(result["changed"])
            self.assertEqual(result["updated_session_files"], 2)
            self.assertIn('"model_provider":"new_provider"', active_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertIn('"model":"gpt-new"', archived_path.read_text(encoding="utf-8").splitlines()[0])

    def test_share_once_skips_invalid_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir)
            write_config(codex_home)
            create_threads_db(codex_home, with_model=True)
            invalid_path = write_invalid_session_meta(
                codex_home,
                "33333333-3333-3333-3333-333333333333",
            )
            valid_path = write_session_meta(
                codex_home,
                "sessions",
                "11111111-1111-1111-1111-111111111111",
                "old_provider",
                "gpt-old",
            )
            paths = resolve_paths(str(codex_home))

            result = sync_share_once(paths)

            self.assertTrue(result["changed"])
            self.assertEqual(result["updated_session_files"], 1)
            self.assertEqual(invalid_path.read_text(encoding="utf-8").splitlines()[0], "not-json")
            self.assertIn('"model_provider":"new_provider"', valid_path.read_text(encoding="utf-8").splitlines()[0])

    def test_share_mode_startup_entry_can_be_enabled_and_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "codex"
            startup_dir = Path(temp_dir) / "startup"
            codex_home.mkdir()
            write_config(codex_home)
            create_threads_db(codex_home, with_model=True)
            paths = resolve_paths(str(codex_home))

            enabled = enable_share_mode(paths, interval_seconds=7, startup_dir=startup_dir, start_process=False)
            status = get_share_mode_status(paths, startup_dir=startup_dir)

            self.assertTrue(enabled["enabled"])
            self.assertTrue(status["enabled"])
            self.assertFalse(status["running"])
            self.assertTrue(Path(enabled["startup_path"]).exists())
            self.assertIn("--interval 7", Path(enabled["startup_path"]).read_text(encoding="utf-8"))

            disabled = disable_share_mode(paths, startup_dir=startup_dir, stop_process=False)
            status_after = get_share_mode_status(paths, startup_dir=startup_dir)

            self.assertFalse(disabled["enabled"])
            self.assertFalse(status_after["enabled"])

    def test_resolve_paths_prefers_default_codex_home_when_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home_dir = Path(temp_dir) / "home"
            codex_home = home_dir / ".codex"
            codex_home.mkdir(parents=True)
            write_config(codex_home)
            create_threads_db(codex_home, with_model=True)

            paths = resolve_paths(None, home_dir=home_dir, environ={})

            self.assertEqual(paths.codex_home, codex_home)
            self.assertEqual(get_status(paths)["codex_home"], str(codex_home))

    def test_resolve_paths_falls_back_to_configured_codex_home_when_default_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home_dir = Path(temp_dir) / "home"
            codex_home = Path(temp_dir) / "custom-codex"
            codex_home.mkdir(parents=True)
            write_config(codex_home)
            create_threads_db(codex_home, with_model=True)

            paths = resolve_paths(None, home_dir=home_dir, environ={"CODEX_HOME": str(codex_home)})

            self.assertEqual(paths.codex_home, codex_home)
            self.assertEqual(get_status(paths)["codex_home"], str(codex_home))

    def test_resolve_paths_asks_for_manual_directory_when_no_candidate_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home_dir = Path(temp_dir) / "home"

            with self.assertRaisesRegex(RuntimeError, "请选择 Codex 数据目录"):
                resolve_paths(None, home_dir=home_dir, environ={})


if __name__ == "__main__":
    unittest.main()
