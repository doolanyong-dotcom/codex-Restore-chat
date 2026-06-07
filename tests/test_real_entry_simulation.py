from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "sync_backend.py"

HIDDEN_THREAD_ID = "11111111-1111-1111-1111-111111111111"
CURRENT_THREAD_ID = "22222222-2222-2222-2222-222222222222"
MODEL_ONLY_THREAD_ID = "33333333-3333-3333-3333-333333333333"
STALE_INDEX_ID = "44444444-4444-4444-4444-444444444444"


def run_backend(
    codex_home: Path,
    startup_dir: Path,
    *args: str,
    include_codex_home: bool = True,
    extra_env: dict[str, str] | None = None,
) -> dict[str, object]:
    command = [sys.executable, str(BACKEND)]
    if include_codex_home:
        command.extend(["--codex-home", str(codex_home)])
    command.extend(["--json", *args])

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["CODEX_HISTORY_SHARE_STARTUP_DIR"] = str(startup_dir)
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = result.stdout.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Backend did not return JSON.\nCommand: {command}\nExit: {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        ) from exc

    if result.returncode != 0 or not payload.get("ok"):
        raise AssertionError(
            f"Backend command failed.\nCommand: {command}\nExit: {result.returncode}\nPayload: {payload}\n"
            f"stderr:\n{result.stderr}"
        )
    return payload


def run_backend_expect_failure(
    startup_dir: Path,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> dict[str, object]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["CODEX_HISTORY_SHARE_STARTUP_DIR"] = str(startup_dir)
    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [sys.executable, str(BACKEND), "--json", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = result.stdout.strip()
    payload = json.loads(output)
    if result.returncode == 0 or payload.get("ok"):
        raise AssertionError(f"Backend command unexpectedly succeeded: {payload}")
    return payload


def write_config(codex_home: Path, provider: str = "new_provider", model: str = "gpt-new") -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        f'model_provider = "{provider}"\nmodel = "{model}"\n',
        encoding="utf-8",
    )


def create_realistic_threads_db(codex_home: Path) -> None:
    conn = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
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
                    HIDDEN_THREAD_ID,
                    "old_provider",
                    "gpt-old",
                    r"\\?\E:\Workspace\hidden",
                    0,
                    "restore this thread",
                    1,
                    1710000000,
                    "Hidden old thread",
                    1710000000,
                ),
                (
                    CURRENT_THREAD_ID,
                    "new_provider",
                    "gpt-new",
                    r"E:\Workspace\current",
                    1,
                    "already current",
                    0,
                    None,
                    "Current thread",
                    1710000100,
                ),
                (
                    MODEL_ONLY_THREAD_ID,
                    "new_provider",
                    "gpt-old",
                    r"E:\Workspace\model-only",
                    1,
                    "provider current but model old",
                    0,
                    None,
                    "Model changed thread",
                    1710000200,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def session_path(codex_home: Path, folder: str, thread_id: str) -> Path:
    return codex_home / folder / "2026" / "06" / f"rollout-2026-06-05-{thread_id}.jsonl"


def write_session_file(codex_home: Path, folder: str, thread_id: str, provider: str, model: str) -> Path:
    path = session_path(codex_home, folder, thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    first_line = json.dumps(
        {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": "2026-06-05T12:00:00Z",
                "cwd": r"E:\Workspace\codex-history-sync-tool",
                "model_provider": provider,
                "model": model,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    path.write_text(
        first_line + '\n{"type":"user","payload":{"text":"hello"}}\n',
        encoding="utf-8",
        newline="",
    )
    return path


def write_stale_session_index(codex_home: Path) -> None:
    entries = [
        {
            "id": MODEL_ONLY_THREAD_ID,
            "thread_name": "Existing model-only entry",
            "updated_at": "2026-06-05T12:00:00Z",
        },
        {
            "id": STALE_INDEX_ID,
            "thread_name": "Index only preserved entry",
            "updated_at": "2026-06-05T12:01:00Z",
        },
    ]
    (codex_home / "session_index.jsonl").write_text(
        "".join(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n" for entry in entries),
        encoding="utf-8",
        newline="",
    )


def create_broken_codex_home(codex_home: Path) -> dict[str, str]:
    write_config(codex_home)
    create_realistic_threads_db(codex_home)
    active_hidden = write_session_file(codex_home, "sessions", HIDDEN_THREAD_ID, "old_provider", "gpt-old")
    active_current = write_session_file(codex_home, "sessions", CURRENT_THREAD_ID, "new_provider", "gpt-new")
    archived_model = write_session_file(
        codex_home,
        "archived_sessions",
        MODEL_ONLY_THREAD_ID,
        "old_provider",
        "gpt-old",
    )
    write_stale_session_index(codex_home)
    return {
        "hidden_first_line": active_hidden.read_text(encoding="utf-8").splitlines()[0],
        "current_first_line": active_current.read_text(encoding="utf-8").splitlines()[0],
        "model_only_first_line": archived_model.read_text(encoding="utf-8").splitlines()[0],
    }


def thread_rows(codex_home: Path) -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        conn.row_factory = sqlite3.Row
        return {
            str(row["id"]): row
            for row in conn.execute(
                """
                SELECT id, model_provider, model, cwd, has_user_event, archived, archived_at
                FROM threads
                ORDER BY id
                """
            )
        }
    finally:
        conn.close()


def session_meta(codex_home: Path, folder: str, thread_id: str) -> dict[str, object]:
    first_line = session_path(codex_home, folder, thread_id).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(first_line)["payload"]


def session_index_ids(codex_home: Path) -> set[str]:
    return {
        json.loads(line)["id"]
        for line in (codex_home / "session_index.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def assert_fully_aligned(testcase: unittest.TestCase, codex_home: Path) -> None:
    rows = thread_rows(codex_home)
    testcase.assertEqual(set(rows), {HIDDEN_THREAD_ID, CURRENT_THREAD_ID, MODEL_ONLY_THREAD_ID})
    for row in rows.values():
        testcase.assertEqual(row["model_provider"], "new_provider")
        testcase.assertEqual(row["model"], "gpt-new")
        testcase.assertFalse(str(row["cwd"]).startswith("\\\\?\\"))
        testcase.assertEqual(row["has_user_event"], 1)
        testcase.assertEqual(row["archived"], 0)
        testcase.assertIsNone(row["archived_at"])

    for folder, thread_id in (
        ("sessions", HIDDEN_THREAD_ID),
        ("sessions", CURRENT_THREAD_ID),
        ("archived_sessions", MODEL_ONLY_THREAD_ID),
    ):
        meta = session_meta(codex_home, folder, thread_id)
        testcase.assertEqual(meta["model_provider"], "new_provider")
        testcase.assertEqual(meta["model"], "gpt-new")

    testcase.assertGreaterEqual(
        session_index_ids(codex_home),
        {HIDDEN_THREAD_ID, CURRENT_THREAD_ID, MODEL_ONLY_THREAD_ID, STALE_INDEX_ID},
    )


class RealEntrySimulationTests(unittest.TestCase):
    def test_restore_mode_entry_recovers_real_files_and_manual_backup_can_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            startup_dir = root / "startup"
            original_meta = create_broken_codex_home(codex_home)

            status = run_backend(codex_home, startup_dir, "status")
            self.assertEqual(status["current_provider"], "new_provider")
            self.assertEqual(status["current_model"], "gpt-new")
            self.assertGreaterEqual(int(status["movable_threads"]), 1)
            self.assertEqual(status["cwd_prefix_threads"], 1)
            self.assertEqual(status["missing_user_event_threads"], 1)
            self.assertEqual(status["archived_threads"], 1)

            backup = run_backend(codex_home, startup_dir, "backup")
            backup_path = Path(str(backup["backup_path"]))
            self.assertTrue(backup_path.exists())
            self.assertTrue(backup_path.with_name(f"{backup_path.name}.session_index.jsonl").exists())
            self.assertTrue(backup_path.with_name(f"{backup_path.name}.session_meta.json").exists())

            sync = run_backend(codex_home, startup_dir, "sync", "--passes", "3")
            self.assertTrue(sync["changed"])
            self.assertEqual(sync["action"], "sync")
            self.assertEqual(sync["updated_rows"], 2)
            self.assertEqual(sync["updated_session_files"], 2)
            self.assertEqual(sync["visibility_updates"]["normalized_cwd"], 1)
            self.assertEqual(sync["visibility_updates"]["set_has_user_event"], 1)
            self.assertEqual(sync["visibility_updates"]["unarchived"], 1)
            self.assertGreaterEqual(sync["missing_session_index_entries_before"], 2)
            self.assertTrue(Path(str(sync["backup_path"])).exists())
            assert_fully_aligned(self, codex_home)

            restore = run_backend(codex_home, startup_dir, "restore", "--backup", str(backup_path))
            self.assertEqual(restore["action"], "restore")
            self.assertEqual(restore["restored_from"], str(backup_path))
            self.assertTrue(Path(str(restore["safety_backup"])).exists())
            self.assertGreaterEqual(restore["metadata_restore"]["session_files_restored"], 3)

            rows = thread_rows(codex_home)
            hidden = rows[HIDDEN_THREAD_ID]
            self.assertEqual(hidden["model_provider"], "old_provider")
            self.assertEqual(hidden["model"], "gpt-old")
            self.assertEqual(hidden["cwd"], r"\\?\E:\Workspace\hidden")
            self.assertEqual(hidden["has_user_event"], 0)
            self.assertEqual(hidden["archived"], 1)
            self.assertEqual(hidden["archived_at"], 1710000000)

            self.assertEqual(
                session_path(codex_home, "sessions", HIDDEN_THREAD_ID).read_text(encoding="utf-8").splitlines()[0],
                original_meta["hidden_first_line"],
            )
            self.assertEqual(
                session_path(codex_home, "sessions", CURRENT_THREAD_ID).read_text(encoding="utf-8").splitlines()[0],
                original_meta["current_first_line"],
            )
            self.assertEqual(
                session_path(codex_home, "archived_sessions", MODEL_ONLY_THREAD_ID)
                .read_text(encoding="utf-8")
                .splitlines()[0],
                original_meta["model_only_first_line"],
            )

    def test_share_mode_entry_syncs_real_files_and_sandbox_startup_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            startup_dir = root / "sandbox-startup"
            create_broken_codex_home(codex_home)

            try:
                share_once = run_backend(codex_home, startup_dir, "share-once")
                self.assertTrue(share_once["changed"])
                self.assertEqual(share_once["action"], "share_once")
                self.assertEqual(share_once["updated_rows"], 2)
                self.assertEqual(share_once["updated_session_files"], 2)
                self.assertTrue(Path(str(share_once["backup_path"])).exists())
                assert_fully_aligned(self, codex_home)

                enabled = run_backend(codex_home, startup_dir, "share-enable", "--interval", "7")
                self.assertTrue(enabled["enabled"])
                startup_path = Path(str(enabled["startup_path"]))
                self.assertEqual(startup_path.parent, startup_dir)
                self.assertTrue(startup_path.exists())
                startup_text = startup_path.read_text(encoding="utf-8")
                self.assertIn(str(BACKEND), startup_text)
                self.assertIn(f'--codex-home "{codex_home}"', startup_text)
                self.assertIn("watch --interval 7 --quiet", startup_text)

                state_path = codex_home / "history_share_mode" / "state.json"
                self.assertTrue(state_path.exists())
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertIn("pid", state)

                status = run_backend(codex_home, startup_dir, "share-status")
                self.assertTrue(status["enabled"])
                self.assertEqual(Path(str(status["startup_path"])), startup_path)
            finally:
                disabled = run_backend(codex_home, startup_dir, "share-disable")
                self.assertFalse(disabled["enabled"])
                self.assertFalse(Path(str(disabled["startup_path"])).exists())
                self.assertFalse(disabled["running"])

    def test_real_file_directory_discovery_uses_default_then_manual_prompt_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            startup_dir = root / "startup"
            fake_home = root / "user-home"
            default_codex_home = fake_home / ".codex"
            create_broken_codex_home(default_codex_home)

            env = {
                "USERPROFILE": str(fake_home),
                "HOME": str(fake_home),
                "LOCALAPPDATA": str(root / "local-appdata"),
                "APPDATA": str(root / "appdata"),
            }
            status = run_backend(
                default_codex_home,
                startup_dir,
                "status",
                include_codex_home=False,
                extra_env=env,
            )
            self.assertEqual(Path(str(status["codex_home"])), default_codex_home)

            missing_home = root / "missing-home"
            failure = run_backend_expect_failure(
                startup_dir,
                "status",
                extra_env={
                    "USERPROFILE": str(missing_home),
                    "HOME": str(missing_home),
                    "LOCALAPPDATA": str(root / "missing-local"),
                    "APPDATA": str(root / "missing-appdata"),
                    "CODEX_HOME": str(root / "also-missing"),
                },
            )
            self.assertIn("请选择 Codex 数据目录", str(failure["error"]))
            self.assertIn("config.toml", str(failure["error"]))
            self.assertIn("state_5.sqlite", str(failure["error"]))


if __name__ == "__main__":
    unittest.main()
