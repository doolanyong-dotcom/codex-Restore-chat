from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "sync_backend.py"
INDEX_ONLY_ID = "99999999-9999-9999-9999-999999999999"
WINDOWS_LONG_PATH_PREFIX = "\\\\?\\"


PROFILES = [
    ("chatgpt-account-a", "openai", "gpt-5"),
    ("api-key-main", "cpamc", "gpt-5-cpamc"),
    ("api-key-openai", "openai-api", "gpt-4.1"),
    ("local-lab", "local-api", "qwen3-coder"),
    ("router-team", "openrouter", "claude-sonnet"),
    ("chatgpt-account-b", "chatgpt-login", "gpt-5"),
    ("api-key-backup", "cpamc", "o3"),
    ("chatgpt-account-a", "openai", "gpt-5-mini"),
]


def run_backend(codex_home: Path, startup_dir: Path, *args: str) -> dict[str, object]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["CODEX_HISTORY_SHARE_STARTUP_DIR"] = str(startup_dir)
    result = subprocess.run(
        [sys.executable, str(BACKEND), "--codex-home", str(codex_home), "--json", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Backend did not return JSON.\nExit: {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        ) from exc
    if result.returncode != 0 or not payload.get("ok"):
        raise AssertionError(
            f"Backend command failed.\nArgs: {args}\nExit: {result.returncode}\n"
            f"payload:\n{payload}\nstderr:\n{result.stderr}"
        )
    return payload


def write_config(codex_home: Path, account: str, provider: str, model: str) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        "\n".join(
            [
                f'active_account = "{account}"',
                f'model_provider = "{provider}"',
                f'model = "{model}"',
                f'auth_session_id = "{account}-{provider}"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def read_active_config(codex_home: Path) -> tuple[str, str]:
    text = (codex_home / "config.toml").read_text(encoding="utf-8")
    provider = re.search(r'(?m)^\s*model_provider\s*=\s*"([^"]+)"', text)
    model = re.search(r'(?m)^\s*model\s*=\s*"([^"]+)"', text)
    if not provider or not model:
        raise AssertionError("config.toml does not contain model_provider and model.")
    return provider.group(1), model.group(1)


def create_threads_db(codex_home: Path) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
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
        conn.commit()
    finally:
        conn.close()


def thread_id_for_round(round_index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"codex-history-switch-round-{round_index}"))


def session_path(codex_home: Path, thread_id: str) -> Path:
    return codex_home / "sessions" / "2026" / "06" / f"rollout-2026-06-05-{thread_id}.jsonl"


def write_chat_session(
    codex_home: Path,
    thread_id: str,
    provider: str,
    model: str,
    user_text: str,
    assistant_text: str,
) -> None:
    path = session_path(codex_home, thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    first_line = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "timestamp": "2026-06-05T12:00:00Z",
            "cwd": r"E:\Workspace\codex-history-sync-tool",
            "model_provider": provider,
            "model": model,
        },
    }
    lines = [
        first_line,
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": {"content": [{"type": "input_text", "text": user_text}]},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "assistant_message",
                "message": {"content": [{"type": "output_text", "text": assistant_text}]},
            },
        },
    ]
    path.write_text(
        "".join(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n" for line in lines),
        encoding="utf-8",
        newline="",
    )


def insert_thread(
    codex_home: Path,
    thread_id: str,
    provider: str,
    model: str,
    user_text: str,
    round_index: int,
) -> None:
    conn = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        conn.execute(
            """
            INSERT INTO threads (
                id, model_provider, model, cwd, has_user_event, first_user_message,
                archived, archived_at, title, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, 0, NULL, ?, ?)
            """,
            (
                thread_id,
                provider,
                model,
                rf"E:\Workspace\switch-round-{round_index}",
                user_text,
                f"Switch round {round_index}",
                1780671600 + round_index,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def hide_previous_threads(codex_home: Path, thread_ids: list[str], round_index: int) -> None:
    if not thread_ids:
        return
    conn = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        conn.executemany(
            """
            UPDATE threads
            SET cwd = ?,
                has_user_event = 0,
                archived = 1,
                archived_at = ?
            WHERE id = ?
            """,
            [
                (rf"\\?\E:\Workspace\hidden-after-switch-{round_index}", 1780672000 + round_index, thread_id)
                for thread_id in thread_ids
            ],
        )
        conn.commit()
    finally:
        conn.close()


def write_stale_index(codex_home: Path, visible_ids: list[str]) -> None:
    entries = [
        {
            "id": thread_id,
            "thread_name": f"Visible before repair {thread_id}",
            "updated_at": "2026-06-05T12:00:00Z",
        }
        for thread_id in visible_ids
    ]
    entries.append(
        {
            "id": INDEX_ONLY_ID,
            "thread_name": "Preserved index-only entry",
            "updated_at": "2026-06-05T12:01:00Z",
        }
    )
    (codex_home / "session_index.jsonl").write_text(
        "".join(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n" for entry in entries),
        encoding="utf-8",
        newline="",
    )


def session_index_ids(codex_home: Path) -> set[str]:
    path = codex_home / "session_index.jsonl"
    if not path.exists():
        return set()
    return {json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def read_thread_rows(codex_home: Path) -> dict[str, sqlite3.Row]:
    conn = sqlite3.connect(codex_home / "state_5.sqlite")
    try:
        conn.row_factory = sqlite3.Row
        return {
            str(row["id"]): row
            for row in conn.execute(
                """
                SELECT id, model_provider, model, cwd, has_user_event, first_user_message,
                       archived, archived_at, title, updated_at
                FROM threads
                ORDER BY id
                """
            )
        }
    finally:
        conn.close()


def read_session_meta(codex_home: Path, thread_id: str) -> dict[str, object]:
    first_line = session_path(codex_home, thread_id).read_text(encoding="utf-8").splitlines()[0]
    return json.loads(first_line)["payload"]


def visible_chat_ids_from_backend_state(codex_home: Path) -> set[str]:
    provider, model = read_active_config(codex_home)
    rows = read_thread_rows(codex_home)
    index_ids = session_index_ids(codex_home)
    visible: set[str] = set()
    for thread_id, row in rows.items():
        if row["model_provider"] != provider or row["model"] != model:
            continue
        if int(row["archived"]) != 0 or int(row["has_user_event"]) != 1:
            continue
        if str(row["cwd"] or "").startswith(WINDOWS_LONG_PATH_PREFIX):
            continue
        if thread_id not in index_ids:
            continue
        path = session_path(codex_home, thread_id)
        if not path.exists():
            continue
        meta = read_session_meta(codex_home, thread_id)
        if meta.get("model_provider") != provider or meta.get("model") != model:
            continue
        visible.add(thread_id)
    return visible


def assert_chats_visible_and_usable(
    testcase: unittest.TestCase,
    codex_home: Path,
    expected_contents: dict[str, tuple[str, str]],
) -> None:
    provider, model = read_active_config(codex_home)
    rows = read_thread_rows(codex_home)
    expected_ids = set(expected_contents)
    testcase.assertEqual(visible_chat_ids_from_backend_state(codex_home), expected_ids)
    testcase.assertGreaterEqual(session_index_ids(codex_home), expected_ids | {INDEX_ONLY_ID})

    for thread_id, (user_text, assistant_text) in expected_contents.items():
        row = rows[thread_id]
        testcase.assertEqual(row["model_provider"], provider)
        testcase.assertEqual(row["model"], model)
        testcase.assertEqual(row["has_user_event"], 1)
        testcase.assertEqual(row["archived"], 0)
        testcase.assertIsNone(row["archived_at"])
        testcase.assertFalse(str(row["cwd"]).startswith(WINDOWS_LONG_PATH_PREFIX))

        meta = read_session_meta(codex_home, thread_id)
        testcase.assertEqual(meta["model_provider"], provider)
        testcase.assertEqual(meta["model"], model)

        session_text = session_path(codex_home, thread_id).read_text(encoding="utf-8")
        testcase.assertIn(user_text, session_text)
        testcase.assertIn(assistant_text, session_text)


class SwitchingScenarioTests(unittest.TestCase):
    def test_restore_and_share_entries_preserve_chats_across_account_and_model_switches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            startup_dir = root / "startup"
            create_threads_db(codex_home)

            expected_contents: dict[str, tuple[str, str]] = {}
            created_ids: list[str] = []

            for round_index, (account, provider, model) in enumerate(PROFILES, start=1):
                write_config(codex_home, account, provider, model)
                new_thread_id = thread_id_for_round(round_index)
                user_text = f"user-message-round-{round_index}-{account}-{provider}-{model}"
                assistant_text = f"assistant-reply-round-{round_index}-{account}-{provider}-{model}"

                insert_thread(codex_home, new_thread_id, provider, model, user_text, round_index)
                write_chat_session(codex_home, new_thread_id, provider, model, user_text, assistant_text)
                hide_previous_threads(codex_home, created_ids, round_index)
                write_stale_index(codex_home, [new_thread_id] if round_index % 2 == 0 else [])

                expected_contents[new_thread_id] = (user_text, assistant_text)
                before_visible = visible_chat_ids_from_backend_state(codex_home)
                self.assertNotEqual(before_visible, set(expected_contents))

                if round_index % 2 == 1:
                    payload = run_backend(codex_home, startup_dir, "sync", "--passes", "3")
                    self.assertEqual(payload["action"], "sync")
                else:
                    payload = run_backend(codex_home, startup_dir, "share-once")
                    self.assertEqual(payload["action"], "share_once")

                self.assertTrue(payload["changed"])
                self.assertEqual(payload["current_provider"], provider)
                self.assertEqual(payload["current_model"], model)
                self.assertEqual(payload["status"]["movable_threads"], 0)
                assert_chats_visible_and_usable(self, codex_home, expected_contents)

                created_ids.append(new_thread_id)

    def test_backup_restore_then_current_provider_repair_keeps_restored_chats_usable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex"
            startup_dir = root / "startup"
            create_threads_db(codex_home)

            account, provider, model = PROFILES[0]
            write_config(codex_home, account, provider, model)
            expected_contents: dict[str, tuple[str, str]] = {}
            created_ids: list[str] = []

            for round_index in range(1, 4):
                thread_id = thread_id_for_round(100 + round_index)
                user_text = f"backup-user-message-{round_index}"
                assistant_text = f"backup-assistant-reply-{round_index}"
                insert_thread(codex_home, thread_id, provider, model, user_text, round_index)
                write_chat_session(codex_home, thread_id, provider, model, user_text, assistant_text)
                expected_contents[thread_id] = (user_text, assistant_text)
                created_ids.append(thread_id)

            write_stale_index(codex_home, [])
            run_backend(codex_home, startup_dir, "sync", "--passes", "3")
            assert_chats_visible_and_usable(self, codex_home, expected_contents)

            backup = run_backend(codex_home, startup_dir, "backup")
            backup_path = Path(str(backup["backup_path"]))
            self.assertTrue(backup_path.exists())

            switched_account, switched_provider, switched_model = PROFILES[4]
            write_config(codex_home, switched_account, switched_provider, switched_model)
            hide_previous_threads(codex_home, created_ids, 50)
            write_stale_index(codex_home, [])
            self.assertEqual(visible_chat_ids_from_backend_state(codex_home), set())

            run_backend(codex_home, startup_dir, "sync", "--passes", "3")
            assert_chats_visible_and_usable(self, codex_home, expected_contents)

            restore = run_backend(codex_home, startup_dir, "restore", "--backup", str(backup_path))
            self.assertEqual(restore["action"], "restore")
            self.assertEqual(restore["restored_from"], str(backup_path))
            for thread_id, (user_text, assistant_text) in expected_contents.items():
                meta = read_session_meta(codex_home, thread_id)
                self.assertEqual(meta["model_provider"], provider)
                self.assertEqual(meta["model"], model)
                session_text = session_path(codex_home, thread_id).read_text(encoding="utf-8")
                self.assertIn(user_text, session_text)
                self.assertIn(assistant_text, session_text)

            write_config(codex_home, switched_account, switched_provider, switched_model)
            repair = run_backend(codex_home, startup_dir, "sync", "--passes", "3")
            self.assertTrue(repair["changed"])
            self.assertEqual(repair["status"]["movable_threads"], 0)
            assert_chats_visible_and_usable(self, codex_home, expected_contents)


if __name__ == "__main__":
    unittest.main()
