from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SESSION_FILENAME_PATTERN = re.compile(
    r"rollout-.*-(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)
UTC = timezone.utc
DEFAULT_DB_TIMEOUT_SECONDS = 30.0
WRITE_OPERATION_TIMEOUT_SECONDS = 0.5
WRITE_LOCK_RETRY_LIMIT = 40
WRITE_LOCK_RETRY_DELAY_SECONDS = 0.25
FILE_REPLACE_RETRY_LIMIT = 20
FILE_REPLACE_RETRY_DELAY_SECONDS = 0.1
SYNC_CHECKPOINT_MODE = "PASSIVE"


def default_codex_home(home_dir: Path | None = None) -> Path:
    return (home_dir or Path.home()) / ".codex"


def normalize_candidate_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def is_codex_home(path: Path) -> bool:
    return (path / "config.toml").exists() and (path / "state_5.sqlite").exists()


def codex_home_candidates(
    *,
    home_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Path]:
    env = os.environ if environ is None else environ
    raw_candidates: list[Path] = [default_codex_home(home_dir)]

    env_home = env.get("CODEX_HOME")
    if env_home:
        raw_candidates.append(Path(env_home))

    user_profile = env.get("USERPROFILE")
    if user_profile:
        raw_candidates.append(Path(user_profile) / ".codex")

    for key in ("LOCALAPPDATA", "APPDATA"):
        value = env.get(key)
        if value:
            raw_candidates.append(Path(value) / "Codex" / ".codex")

    candidates: list[Path] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        normalized = normalize_candidate_path(candidate)
        key = str(normalized).casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(normalized)
    return candidates


def resolve_codex_home(
    codex_home: str | None,
    *,
    home_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    if codex_home:
        return normalize_candidate_path(codex_home)

    candidates = codex_home_candidates(home_dir=home_dir, environ=environ)
    for candidate in candidates:
        if is_codex_home(candidate):
            return candidate

    checked_paths = "\n".join(f"- {candidate}" for candidate in candidates)
    raise RuntimeError(
        "找不到 Codex 数据目录。请选择 Codex 数据目录，"
        "目录里需要包含 config.toml 和 state_5.sqlite。\n\n"
        f"已检查:\n{checked_paths}"
    )


@dataclass
class Paths:
    codex_home: Path
    config_path: Path
    db_path: Path
    backup_dir: Path
    session_index_path: Path
    sessions_dir: Path
    archived_sessions_dir: Path
    share_mode_dir: Path
    share_state_path: Path
    share_log_path: Path


@dataclass
class SessionRecord:
    thread_id: str
    path: Path
    model_provider: str
    model: str | None


def resolve_paths(
    codex_home: str | None,
    *,
    home_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Paths:
    home = resolve_codex_home(codex_home, home_dir=home_dir, environ=environ)
    return Paths(
        codex_home=home,
        config_path=home / "config.toml",
        db_path=home / "state_5.sqlite",
        backup_dir=home / "history_sync_backups",
        session_index_path=home / "session_index.jsonl",
        sessions_dir=home / "sessions",
        archived_sessions_dir=home / "archived_sessions",
        share_mode_dir=home / "history_share_mode",
        share_state_path=home / "history_share_mode" / "state.json",
        share_log_path=home / "history_share_mode" / "share_mode.log",
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_text_exact(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def replace_file_with_retry(source_path: Path, target_path: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(FILE_REPLACE_RETRY_LIMIT):
        try:
            # 用原子替换避免写到一半被 Codex 读到半成品文件。
            source_path.replace(target_path)
            return
        except PermissionError as exc:
            last_error = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (5, 32):
                raise
            last_error = exc

        if attempt < FILE_REPLACE_RETRY_LIMIT - 1:
            time.sleep(FILE_REPLACE_RETRY_DELAY_SECONDS)

    raise RuntimeError(f"File is busy and could not be replaced: {target_path}") from last_error


def write_text_exact(path: Path, text: str) -> None:
    temp_path = path.with_name(f".{path.name}.codex-sync-{time.time_ns()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        replace_file_with_retry(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def parse_current_provider(config_text: str) -> str:
    match = re.search(r'(?m)^\s*model_provider\s*=\s*"([^"]+)"', config_text)
    if not match:
        raise RuntimeError("Could not find model_provider in config.toml.")
    return match.group(1)


def parse_current_model(config_text: str) -> str | None:
    match = re.search(r'(?m)^\s*model\s*=\s*"([^"]+)"', config_text)
    return match.group(1) if match else None


@contextmanager
def connect_db(
    path: Path,
    readonly: bool = False,
    timeout_seconds: float = DEFAULT_DB_TIMEOUT_SECONDS,
    busy_timeout_ms: int | None = None,
) -> Iterator[sqlite3.Connection]:
    if busy_timeout_ms is None:
        busy_timeout_ms = max(1, int(timeout_seconds * 1000))

    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout_seconds)
    else:
        conn = sqlite3.connect(str(path), timeout=timeout_seconds)

    try:
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def ensure_environment(paths: Paths) -> None:
    if not paths.config_path.exists():
        raise RuntimeError(f"Missing config file: {paths.config_path}")
    if not paths.db_path.exists():
        raise RuntimeError(f"Missing database file: {paths.db_path}")


def get_thread_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row["name"]) for row in conn.execute("PRAGMA table_info(threads)")}


def counts_to_rows(counts: OrderedDict[str, int]) -> list[dict[str, object]]:
    return [{"provider": key, "count": value} for key, value in counts.items()]


def model_counts_to_rows(counts: OrderedDict[str, int]) -> list[dict[str, object]]:
    return [{"model": key, "count": value} for key, value in counts.items()]


def ordered_counts(values: list[str]) -> OrderedDict[str, int]:
    raw_counts: dict[str, int] = {}
    for value in values:
        key = value or "(empty)"
        raw_counts[key] = raw_counts.get(key, 0) + 1

    counts = OrderedDict()
    for key, value in sorted(raw_counts.items(), key=lambda item: (-item[1], item[0])):
        counts[key] = value
    return counts


def elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def query_provider_counts(conn: sqlite3.Connection) -> OrderedDict[str, int]:
    counts = OrderedDict()
    for provider, count in conn.execute(
        """
        SELECT model_provider, COUNT(*)
        FROM threads
        GROUP BY model_provider
        ORDER BY COUNT(*) DESC, model_provider ASC
        """
    ):
        counts[str(provider or "(empty)")] = int(count)
    return counts


def query_model_counts(conn: sqlite3.Connection) -> OrderedDict[str, int]:
    counts = OrderedDict()
    for model, count in conn.execute(
        """
        SELECT model, COUNT(*)
        FROM threads
        GROUP BY model
        ORDER BY COUNT(*) DESC, model ASC
        """
    ):
        counts[str(model or "(empty)")] = int(count)
    return counts


def query_provider_model_counts(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = []
    for provider, model, count in conn.execute(
        """
        SELECT model_provider, model, COUNT(*)
        FROM threads
        GROUP BY model_provider, model
        ORDER BY COUNT(*) DESC, model_provider ASC, model ASC
        """
    ):
        rows.append(
            {
                "provider": str(provider or "(empty)"),
                "model": str(model or "(empty)"),
                "count": int(count),
            }
        )
    return rows


def query_cwd_counts(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, object]]:
    rows = []
    for cwd, count in conn.execute(
        """
        SELECT cwd, COUNT(*)
        FROM threads
        GROUP BY cwd
        ORDER BY COUNT(*) DESC, cwd ASC
        LIMIT ?
        """,
        (limit,),
    ):
        rows.append({"cwd": str(cwd or "(empty)"), "count": int(count)})
    return rows


def count_mismatched(conn: sqlite3.Connection, column: str, expected: str | None) -> int | None:
    if expected is None:
        return None
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM threads WHERE {column} IS NULL OR {column} <> ?",
            (expected,),
        ).fetchone()[0]
    )


def query_id_set(conn: sqlite3.Connection, query: str, params: tuple[object, ...] = ()) -> set[str]:
    return {str(row["id"]) for row in conn.execute(query, params)}


def query_visibility_candidates(
    conn: sqlite3.Connection,
    columns: set[str],
) -> tuple[set[str], dict[str, int]]:
    ids: set[str] = set()
    counts = {
        "cwd_prefix_threads": 0,
        "missing_user_event_threads": 0,
        "archived_threads": 0,
    }

    if "cwd" in columns:
        cwd_ids = query_id_set(conn, "SELECT id FROM threads WHERE cwd LIKE '\\\\?\\%'")
        counts["cwd_prefix_threads"] = len(cwd_ids)
        ids |= cwd_ids

    if {"has_user_event", "first_user_message"}.issubset(columns):
        user_event_ids = query_id_set(
            conn,
            """
            SELECT id
            FROM threads
            WHERE has_user_event=0
              AND COALESCE(TRIM(first_user_message), '') <> ''
            """,
        )
        counts["missing_user_event_threads"] = len(user_event_ids)
        ids |= user_event_ids

    if "archived" in columns:
        archived_ids = query_id_set(conn, "SELECT id FROM threads WHERE archived<>0")
        counts["archived_threads"] = len(archived_ids)
        ids |= archived_ids

    return ids, counts


def list_backups(paths: Paths, limit: int = 20) -> list[dict[str, str]]:
    if not paths.backup_dir.exists():
        return []
    files = sorted(
        paths.backup_dir.glob("state_5.sqlite.*.bak"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    output = []
    for item in files[:limit]:
        output.append(
            {
                "name": item.name,
                "path": str(item),
                "modified_at": datetime.fromtimestamp(item.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return output


def split_first_line(text: str) -> tuple[str, str, str]:
    for ending in ("\r\n", "\n", "\r"):
        index = text.find(ending)
        if index >= 0:
            return text[:index], ending, text[index + len(ending) :]
    return text, "", ""


def replace_first_line(path: Path, first_line: str) -> None:
    text = read_text_exact(path)
    _, ending, remainder = split_first_line(text)
    if ending:
        new_text = first_line + ending + remainder
    elif text:
        new_text = first_line
    else:
        new_text = first_line + "\n"
    write_text_exact(path, new_text)


def session_index_backup_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.session_index.jsonl")


def session_meta_backup_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.session_meta.json")


def iter_session_paths(paths: Paths) -> list[Path]:
    output: list[Path] = []
    for directory in (paths.sessions_dir, paths.archived_sessions_dir):
        if directory.exists():
            output.extend(directory.rglob("rollout-*.jsonl"))
    return sorted(output)


def parse_session_record(path: Path) -> SessionRecord | None:
    if not SESSION_FILENAME_PATTERN.search(path.name):
        return None

    with path.open("r", encoding="utf-8", newline="") as handle:
        first_line = handle.readline()

    if not first_line:
        return None

    try:
        item = json.loads(first_line.rstrip("\r\n"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(item, dict):
        return None
    if item.get("type") != "session_meta":
        return None

    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None

    thread_id = str(payload.get("id") or "").strip()
    if not thread_id:
        return None

    model_provider = str(payload.get("model_provider") or "")
    raw_model = payload.get("model")
    model = str(raw_model) if raw_model else None
    return SessionRecord(thread_id=thread_id, path=path, model_provider=model_provider, model=model)


def scan_session_records(paths: Paths) -> list[SessionRecord]:
    records: list[SessionRecord] = []
    for path in iter_session_paths(paths):
        record = parse_session_record(path)
        if record:
            records.append(record)
    return records


def read_session_index(paths: Paths) -> OrderedDict[str, dict[str, str]]:
    entries: OrderedDict[str, dict[str, str]] = OrderedDict()
    if not paths.session_index_path.exists():
        return entries

    for line in read_text(paths.session_index_path).splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        thread_id = str(entry.get("id") or "").strip()
        if not thread_id:
            continue
        entries[thread_id] = {
            "id": thread_id,
            "thread_name": str(entry.get("thread_name") or thread_id),
            "updated_at": str(entry.get("updated_at") or ""),
        }
    return entries


def write_session_index(paths: Paths, entries: list[dict[str, str]]) -> None:
    lines = [json.dumps(entry, ensure_ascii=False, separators=(",", ":")) for entry in entries]
    content = "\n".join(lines)
    if content:
        content += "\n"
    write_text_exact(paths.session_index_path, content)


def iso_utc_from_unix(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def parse_index_timestamp(value: str) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=UTC)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def snapshot_metadata(paths: Paths, backup_path: Path) -> None:
    if paths.session_index_path.exists():
        write_text_exact(session_index_backup_path(backup_path), read_text_exact(paths.session_index_path))

    items: list[dict[str, str]] = []
    for path in iter_session_paths(paths):
        with path.open("r", encoding="utf-8", newline="") as handle:
            first_line = handle.readline().rstrip("\r\n")
        if not first_line:
            continue

        try:
            relative_path = path.relative_to(paths.codex_home)
        except ValueError:
            relative_path = path

        items.append({"path": str(relative_path), "first_line": first_line})

    write_text_exact(
        session_meta_backup_path(backup_path),
        json.dumps(items, ensure_ascii=False, indent=2) + "\n",
    )


def restore_metadata(paths: Paths, backup_path: Path) -> dict[str, object]:
    started_at = time.monotonic()
    session_index_restored = False
    session_files_restored = 0

    index_backup = session_index_backup_path(backup_path)
    if index_backup.exists():
        write_text_exact(paths.session_index_path, read_text_exact(index_backup))
        session_index_restored = True

    meta_backup = session_meta_backup_path(backup_path)
    if meta_backup.exists():
        for item in json.loads(read_text(meta_backup)):
            raw_path = Path(item["path"])
            path = raw_path if raw_path.is_absolute() else paths.codex_home / raw_path
            if not path.exists():
                continue
            # 只恢复首行 session_meta，后面的对话内容保持原文件不动。
            replace_first_line(path, str(item["first_line"]))
            session_files_restored += 1

    return {
        "session_index_restored": session_index_restored,
        "session_files_restored": session_files_restored,
        "duration_ms": elapsed_ms(started_at),
    }


def rebuild_session_index(paths: Paths, conn: sqlite3.Connection) -> dict[str, int]:
    started_at = time.monotonic()
    existing_entries = read_session_index(paths)
    columns = get_thread_columns(conn)
    select_parts = ["id"]
    if "title" in columns:
        select_parts.append("title")
    if "updated_at" in columns:
        select_parts.append("updated_at")
    where_sql = "WHERE archived = 0" if "archived" in columns else ""
    db_rows = conn.execute(
        f"""
        SELECT {", ".join(select_parts)}
        FROM threads
        {where_sql}
        ORDER BY id ASC
        """
    ).fetchall()
    db_ids = {str(row["id"]) for row in db_rows}
    existing_ids = set(existing_entries)

    merged: list[dict[str, str]] = []
    for row in db_rows:
        thread_id = str(row["id"])
        existing_entry = existing_entries.get(thread_id)
        title = str(row["title"]) if "title" in columns and row["title"] else thread_id
        updated_at = int(row["updated_at"]) if "updated_at" in columns and row["updated_at"] else 0
        merged.append(
            {
                "id": thread_id,
                "thread_name": str((existing_entry or {}).get("thread_name") or title),
                "updated_at": iso_utc_from_unix(updated_at),
            }
        )

    for thread_id, entry in existing_entries.items():
        if thread_id not in db_ids:
            merged.append(entry)

    merged.sort(key=lambda item: (parse_index_timestamp(item["updated_at"]), item["id"]))
    write_session_index(paths, merged)

    return {
        "rewritten_index_entries": len(merged),
        "missing_session_index_entries_before": len(db_ids - existing_ids),
        "preserved_index_only_entries": len(existing_ids - db_ids),
        "duration_ms": elapsed_ms(started_at),
    }


def sync_session_records(
    paths: Paths,
    current_provider: str,
    current_model: str | None,
    include_model: bool = True,
) -> dict[str, object]:
    started_at = time.monotonic()
    before_records = scan_session_records(paths)
    updated_session_files = 0

    for record in before_records:
        model_matches = not include_model or current_model is None or record.model == current_model
        if record.model_provider == current_provider and model_matches:
            continue

        text = read_text_exact(record.path)
        first_line, ending, remainder = split_first_line(text)
        item = json.loads(first_line)
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue

        payload["model_provider"] = current_provider
        if include_model and current_model:
            payload["model"] = current_model
        new_first_line = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if ending:
            new_text = new_first_line + ending + remainder
        else:
            new_text = new_first_line
        write_text_exact(record.path, new_text)
        updated_session_files += 1

    after_records = scan_session_records(paths)
    return {
        "updated_session_files": updated_session_files,
        "session_before_counts": counts_to_rows(
            ordered_counts([record.model_provider for record in before_records])
        ),
        "session_after_counts": counts_to_rows(
            ordered_counts([record.model_provider for record in after_records])
        ),
        "session_before_model_counts": model_counts_to_rows(
            ordered_counts([record.model or "(empty)" for record in before_records])
        ),
        "session_after_model_counts": model_counts_to_rows(
            ordered_counts([record.model or "(empty)" for record in after_records])
        ),
        "duration_ms": elapsed_ms(started_at),
    }


def is_locked_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return (
        "database is locked" in message
        or "database table is locked" in message
        or "database is busy" in message
        or "destination database is in use" in message
    )


def checkpoint(conn: sqlite3.Connection, mode: str = SYNC_CHECKPOINT_MODE) -> tuple[int, int, int]:
    row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def update_provider_assignments(
    paths: Paths,
    current_provider: str,
    current_model: str | None,
    include_model: bool = True,
    ensure_visible: bool = True,
) -> dict[str, object]:
    started_at = time.monotonic()
    last_error: sqlite3.OperationalError | None = None

    for attempt in range(1, WRITE_LOCK_RETRY_LIMIT + 1):
        try:
            with connect_db(
                paths.db_path,
                readonly=False,
                timeout_seconds=WRITE_OPERATION_TIMEOUT_SECONDS,
            ) as conn:
                # 显式拿写锁，把等待控制在我们自己的重试节奏里。
                conn.execute("BEGIN IMMEDIATE")
                columns = get_thread_columns(conn)
                before_counts = query_provider_counts(conn)
                before_model_counts = query_model_counts(conn) if "model" in columns else OrderedDict()
                set_parts = ["model_provider = ?"]
                set_params = [current_provider]
                where_parts = ["model_provider IS NULL OR model_provider <> ?"]
                where_params = [current_provider]
                synced_fields = ["model_provider"]
                visibility_updates = {
                    "normalized_cwd": 0,
                    "set_has_user_event": 0,
                    "unarchived": 0,
                }

                if include_model and "model" in columns and current_model:
                    set_parts.append("model = ?")
                    set_params.append(current_model)
                    where_parts.append("model IS NULL OR model <> ?")
                    where_params.append(current_model)
                    synced_fields.append("model")

                set_sql = ", ".join(set_parts)
                where_sql = " OR ".join(f"({part})" for part in where_parts)
                updated_rows = conn.execute(
                    f"UPDATE threads SET {set_sql} WHERE {where_sql}",
                    (*set_params, *where_params),
                ).rowcount

                if ensure_visible:
                    if "cwd" in columns:
                        cursor = conn.execute("UPDATE threads SET cwd = substr(cwd, 5) WHERE cwd LIKE '\\\\?\\%'")
                        visibility_updates["normalized_cwd"] = int(cursor.rowcount)

                    if {"has_user_event", "first_user_message"}.issubset(columns):
                        cursor = conn.execute(
                            """
                            UPDATE threads
                            SET has_user_event=1
                            WHERE has_user_event=0
                              AND COALESCE(TRIM(first_user_message), '') <> ''
                            """
                        )
                        visibility_updates["set_has_user_event"] = int(cursor.rowcount)

                    if "archived" in columns:
                        if "archived_at" in columns:
                            cursor = conn.execute("UPDATE threads SET archived=0, archived_at=NULL WHERE archived<>0")
                        else:
                            cursor = conn.execute("UPDATE threads SET archived=0 WHERE archived<>0")
                        visibility_updates["unarchived"] = int(cursor.rowcount)

                conn.commit()
                after_counts = query_provider_counts(conn)
                after_model_counts = query_model_counts(conn) if "model" in columns else OrderedDict()
                checkpoint_result = checkpoint(conn)

            return {
                "attempts": attempt,
                "lock_wait_ms": elapsed_ms(started_at),
                "synced_fields": synced_fields,
                "updated_rows": updated_rows,
                "visibility_updates": visibility_updates,
                "before_counts": counts_to_rows(before_counts),
                "after_counts": counts_to_rows(after_counts),
                "before_model_counts": model_counts_to_rows(before_model_counts),
                "after_model_counts": model_counts_to_rows(after_model_counts),
                "checkpoint": {
                    "mode": SYNC_CHECKPOINT_MODE,
                    "busy": checkpoint_result[0],
                    "log_frames": checkpoint_result[1],
                    "checkpointed_frames": checkpoint_result[2],
                },
            }
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc):
                raise
            last_error = exc
            if attempt >= WRITE_LOCK_RETRY_LIMIT:
                waited_seconds = (time.monotonic() - started_at)
                raise RuntimeError(
                    "Codex 当前正在写入本地历史数据库，"
                    f"已等待 {waited_seconds:.1f} 秒仍未拿到写锁。"
                    "保持 Codex 开着也可以同步，但请等当前回复、工具调用或自动保存结束后再试一次。"
                ) from exc
            time.sleep(WRITE_LOCK_RETRY_DELAY_SECONDS)

    raise RuntimeError("Database write lock retry loop ended unexpectedly.") from last_error


def restore_database_with_retry(paths: Paths, chosen_backup: Path) -> dict[str, object]:
    started_at = time.monotonic()
    last_error: sqlite3.OperationalError | None = None

    for attempt in range(1, WRITE_LOCK_RETRY_LIMIT + 1):
        try:
            with connect_db(chosen_backup, readonly=True) as source, connect_db(
                paths.db_path,
                readonly=False,
                timeout_seconds=WRITE_OPERATION_TIMEOUT_SECONDS,
            ) as target:
                # SQLite 在整库 backup 到目标库时会自己申请所需锁；
                # 这里直接尝试 restore，失败后统一按“数据库正忙”重试即可。
                source.backup(target)
                checkpoint_result = checkpoint(target)

            return {
                "attempts": attempt,
                "lock_wait_ms": elapsed_ms(started_at),
                "checkpoint": {
                    "mode": SYNC_CHECKPOINT_MODE,
                    "busy": checkpoint_result[0],
                    "log_frames": checkpoint_result[1],
                    "checkpointed_frames": checkpoint_result[2],
                },
            }
        except sqlite3.OperationalError as exc:
            if not is_locked_error(exc):
                raise
            last_error = exc
            if attempt >= WRITE_LOCK_RETRY_LIMIT:
                waited_seconds = (time.monotonic() - started_at)
                raise RuntimeError(
                    "Codex 当前正在写入本地历史数据库，"
                    f"已等待 {waited_seconds:.1f} 秒仍无法完成还原。"
                    "请等当前回复、工具调用或自动保存结束后再试一次。"
                ) from exc
            time.sleep(WRITE_LOCK_RETRY_DELAY_SECONDS)

    raise RuntimeError("Database restore retry loop ended unexpectedly.") from last_error


def get_status(paths: Paths) -> dict[str, object]:
    ensure_environment(paths)
    config_text = read_text(paths.config_path)
    current_provider = parse_current_provider(config_text)
    current_model = parse_current_model(config_text)
    session_records = scan_session_records(paths)
    session_provider_counts = ordered_counts([record.model_provider for record in session_records])
    session_model_counts = ordered_counts([record.model or "(empty)" for record in session_records])
    session_movable_ids = {
        record.thread_id
        for record in session_records
        if record.model_provider != current_provider
        or (current_model is not None and record.model != current_model)
    }
    should_check_index = paths.session_index_path.exists() or paths.sessions_dir.exists()
    index_entries = read_session_index(paths)

    with connect_db(paths.db_path, readonly=True) as conn:
        columns = get_thread_columns(conn)
        counts = query_provider_counts(conn)
        model_counts = query_model_counts(conn) if "model" in columns else OrderedDict()
        provider_model_counts = query_provider_model_counts(conn) if "model" in columns else []
        cwd_counts = query_cwd_counts(conn) if "cwd" in columns else []
        total_threads = int(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
        provider_movable = count_mismatched(conn, "model_provider", current_provider)
        model_movable = count_mismatched(conn, "model", current_model) if "model" in columns else None
        where_parts = ["model_provider IS NULL OR model_provider <> ?"]
        params: list[str] = [current_provider]
        if "model" in columns and current_model:
            where_parts.append("model IS NULL OR model <> ?")
            params.append(current_model)
        where_sql = " OR ".join(f"({part})" for part in where_parts)
        db_movable_ids = {str(row["id"]) for row in conn.execute(f"SELECT id FROM threads WHERE {where_sql}", params)}
        db_thread_query = "SELECT id FROM threads WHERE archived = 0" if "archived" in columns else "SELECT id FROM threads"
        db_thread_ids = {str(row["id"]) for row in conn.execute(db_thread_query)}
        missing_index_ids = db_thread_ids - set(index_entries) if should_check_index else set()
        visibility_ids, visibility_counts = query_visibility_candidates(conn, columns)
        sync_candidate_ids = db_movable_ids | session_movable_ids | missing_index_ids | visibility_ids

    return {
        "codex_home": str(paths.codex_home),
        "config_path": str(paths.config_path),
        "db_path": str(paths.db_path),
        "session_index_path": str(paths.session_index_path),
        "sessions_dir": str(paths.sessions_dir),
        "archived_sessions_dir": str(paths.archived_sessions_dir),
        "backup_dir": str(paths.backup_dir),
        "share_mode_dir": str(paths.share_mode_dir),
        "share_log_path": str(paths.share_log_path),
        "current_provider": current_provider,
        "current_model": current_model,
        "total_threads": total_threads,
        "movable_threads": len(sync_candidate_ids),
        "provider_movable_threads": provider_movable,
        "model_movable_threads": model_movable,
        "movable_database_threads": len(db_movable_ids),
        "movable_session_threads": len(session_movable_ids),
        "missing_session_index_entries": len(missing_index_ids),
        "cwd_prefix_threads": visibility_counts["cwd_prefix_threads"],
        "missing_user_event_threads": visibility_counts["missing_user_event_threads"],
        "archived_threads": visibility_counts["archived_threads"],
        "visibility_movable_threads": len(visibility_ids),
        "indexed_threads": len(index_entries),
        "session_file_count": len(session_records),
        "provider_counts": counts_to_rows(counts),
        "model_counts": model_counts_to_rows(model_counts),
        "provider_model_counts": provider_model_counts,
        "cwd_counts": cwd_counts,
        "session_provider_counts": counts_to_rows(session_provider_counts),
        "session_model_counts": model_counts_to_rows(session_model_counts),
        "backups": list_backups(paths),
    }


def make_backup(paths: Paths, label: str) -> Path:
    ensure_environment(paths)
    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = paths.backup_dir / f"state_5.sqlite.{label}.{timestamp}.bak"
    with connect_db(paths.db_path, readonly=True) as source, connect_db(backup_path, readonly=False) as target:
        source.backup(target)
    snapshot_metadata(paths, backup_path)
    backup_path.touch()
    return backup_path


def add_visibility_updates(
    target: dict[str, int],
    source: dict[str, object],
) -> None:
    for key in ("normalized_cwd", "set_has_user_event", "unarchived"):
        target[key] = target.get(key, 0) + int(source.get(key, 0) or 0)


def sync_to_current_provider(
    paths: Paths,
    include_model: bool = True,
    max_passes: int = 3,
    backup_label: str = "pre-sync",
    action: str = "sync",
    backup_if_needed: bool = False,
) -> dict[str, object]:
    total_started_at = time.monotonic()
    status_before = get_status(paths)

    if backup_if_needed and int(status_before["movable_threads"]) <= 0:
        return {
            "action": action,
            "changed": False,
            "current_provider": str(status_before["current_provider"]),
            "current_model": status_before.get("current_model"),
            "synced_fields": ["model_provider", "model"] if include_model and status_before.get("current_model") else ["model_provider"],
            "updated_rows": 0,
            "updated_session_files": 0,
            "visibility_updates": {
                "normalized_cwd": 0,
                "set_has_user_event": 0,
                "unarchived": 0,
            },
            "provider_movable_threads": status_before["provider_movable_threads"],
            "model_movable_threads": status_before["model_movable_threads"],
            "backup_path": None,
            "before_counts": status_before["provider_counts"],
            "after_counts": status_before["provider_counts"],
            "before_model_counts": status_before["model_counts"],
            "after_model_counts": status_before["model_counts"],
            "session_before_counts": status_before["session_provider_counts"],
            "session_after_counts": status_before["session_provider_counts"],
            "session_before_model_counts": status_before["session_model_counts"],
            "session_after_model_counts": status_before["session_model_counts"],
            "checkpoint": None,
            "lock_wait_ms": 0,
            "lock_attempts": 0,
            "rewritten_index_entries": status_before["indexed_threads"],
            "missing_session_index_entries_before": 0,
            "preserved_index_only_entries": 0,
            "passes": 0,
            "timing": {"total_ms": elapsed_ms(total_started_at)},
            "status": status_before,
        }

    backup_started_at = time.monotonic()
    backup_path = make_backup(paths, backup_label)
    backup_duration_ms = elapsed_ms(backup_started_at)

    pass_summaries: list[dict[str, object]] = []
    total_updated_rows = 0
    total_updated_session_files = 0
    total_lock_wait_ms = 0
    total_lock_attempts = 0
    total_missing_index_before = 0
    visibility_updates = {
        "normalized_cwd": 0,
        "set_has_user_event": 0,
        "unarchived": 0,
    }
    status_after = status_before

    for pass_index in range(1, max(1, max_passes) + 1):
        target_status = status_before if pass_index == 1 else get_status(paths)
        current_provider = str(target_status["current_provider"])
        raw_current_model = target_status.get("current_model")
        current_model = str(raw_current_model) if raw_current_model else None

        db_summary = update_provider_assignments(
            paths,
            current_provider,
            current_model,
            include_model=include_model,
            ensure_visible=True,
        )
        session_summary = sync_session_records(
            paths,
            current_provider,
            current_model,
            include_model=include_model,
        )

        with connect_db(paths.db_path, readonly=True) as conn:
            index_summary = rebuild_session_index(paths, conn)

        status_after = get_status(paths)
        total_updated_rows += int(db_summary["updated_rows"])
        total_updated_session_files += int(session_summary["updated_session_files"])
        total_lock_wait_ms += int(db_summary["lock_wait_ms"])
        total_lock_attempts += int(db_summary["attempts"])
        total_missing_index_before += int(index_summary["missing_session_index_entries_before"])
        add_visibility_updates(visibility_updates, db_summary["visibility_updates"])
        pass_summaries.append(
            {
                "target_provider": current_provider,
                "target_model": current_model,
                "db": db_summary,
                "session": session_summary,
                "index": index_summary,
                "remaining": status_after["movable_threads"],
            }
        )

        if int(status_after["movable_threads"]) <= 0:
            break

    first_pass = pass_summaries[0]
    last_pass = pass_summaries[-1]
    first_db_summary = first_pass["db"]
    last_db_summary = last_pass["db"]
    first_session_summary = first_pass["session"]
    last_session_summary = last_pass["session"]
    last_index_summary = last_pass["index"]

    return {
        "action": action,
        "changed": bool(
            total_updated_rows
            or total_updated_session_files
            or any(visibility_updates.values())
            or total_missing_index_before
        ),
        "current_provider": status_after["current_provider"],
        "current_model": status_after["current_model"],
        "synced_fields": last_db_summary["synced_fields"],
        "updated_rows": total_updated_rows,
        "updated_session_files": total_updated_session_files,
        "visibility_updates": visibility_updates,
        "provider_movable_threads": status_before["provider_movable_threads"],
        "model_movable_threads": status_before["model_movable_threads"],
        "backup_path": str(backup_path),
        "before_counts": first_db_summary["before_counts"],
        "after_counts": last_db_summary["after_counts"],
        "before_model_counts": first_db_summary["before_model_counts"],
        "after_model_counts": last_db_summary["after_model_counts"],
        "session_before_counts": first_session_summary["session_before_counts"],
        "session_after_counts": last_session_summary["session_after_counts"],
        "session_before_model_counts": first_session_summary["session_before_model_counts"],
        "session_after_model_counts": last_session_summary["session_after_model_counts"],
        "checkpoint": last_db_summary["checkpoint"],
        "lock_wait_ms": total_lock_wait_ms,
        "lock_attempts": total_lock_attempts,
        "rewritten_index_entries": last_index_summary["rewritten_index_entries"],
        "missing_session_index_entries_before": total_missing_index_before,
        "preserved_index_only_entries": last_index_summary["preserved_index_only_entries"],
        "passes": len(pass_summaries),
        "timing": {
            "backup_ms": backup_duration_ms,
            "database_ms": total_lock_wait_ms,
            "session_ms": sum(int(item["session"]["duration_ms"]) for item in pass_summaries),
            "index_ms": sum(int(item["index"]["duration_ms"]) for item in pass_summaries),
            "total_ms": elapsed_ms(total_started_at),
        },
        "status": status_after,
    }


def sync_share_once(paths: Paths) -> dict[str, object]:
    return sync_to_current_provider(
        paths,
        include_model=True,
        max_passes=3,
        backup_label="pre-share",
        action="share_once",
        backup_if_needed=True,
    )


def resolve_backup(paths: Paths, requested_path: str | None) -> Path:
    if requested_path:
        backup = Path(requested_path).expanduser()
    else:
        backups = list_backups(paths, limit=1)
        if not backups:
            raise RuntimeError("No backup files were found.")
        backup = Path(backups[0]["path"])
    if not backup.exists():
        raise RuntimeError(f"Backup file does not exist: {backup}")
    return backup


def restore_backup(paths: Paths, backup_path: str | None) -> dict[str, object]:
    total_started_at = time.monotonic()
    ensure_environment(paths)
    chosen_backup = resolve_backup(paths, backup_path)

    backup_started_at = time.monotonic()
    restore_snapshot = make_backup(paths, "pre-restore")
    backup_duration_ms = elapsed_ms(backup_started_at)

    restore_db_started_at = time.monotonic()
    restore_db_summary = restore_database_with_retry(paths, chosen_backup)
    restore_db_duration_ms = elapsed_ms(restore_db_started_at)

    restore_summary = restore_metadata(paths, chosen_backup)
    # 恢复后统一重建索引，让数据库与侧边栏索引重新对齐。
    with connect_db(paths.db_path, readonly=True) as conn:
        index_summary = rebuild_session_index(paths, conn)

    status_after = get_status(paths)
    return {
        "action": "restore",
        "restored_from": str(chosen_backup),
        "safety_backup": str(restore_snapshot),
        "metadata_restore": restore_summary,
        "checkpoint": restore_db_summary["checkpoint"],
        "lock_wait_ms": restore_db_summary["lock_wait_ms"],
        "lock_attempts": restore_db_summary["attempts"],
        "rewritten_index_entries": index_summary["rewritten_index_entries"],
        "timing": {
            "backup_ms": backup_duration_ms,
            "database_ms": restore_db_duration_ms,
            "metadata_ms": restore_summary["duration_ms"],
            "index_ms": index_summary["duration_ms"],
            "total_ms": elapsed_ms(total_started_at),
        },
        "status": status_after,
    }


SHARE_STARTUP_FILENAME = "CodexHistoryShareMode.cmd"


def default_startup_dir() -> Path:
    override = os.environ.get("CODEX_HISTORY_SHARE_STARTUP_DIR")
    if override:
        return Path(override).expanduser()

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def share_startup_path(startup_dir: Path | None = None) -> Path:
    return (startup_dir or default_startup_dir()) / SHARE_STARTUP_FILENAME


def cmd_quote(value: Path | str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def backend_script_path() -> Path:
    return Path(__file__).resolve()


def build_share_startup_script(paths: Paths, interval_seconds: float) -> str:
    script_path = backend_script_path()
    interval_text = f"{interval_seconds:g}"
    return "\n".join(
        [
            "@echo off",
            "setlocal",
            f"if not exist {cmd_quote(paths.share_mode_dir)} mkdir {cmd_quote(paths.share_mode_dir)}",
            (
                f"py -3 {cmd_quote(script_path)} --codex-home {cmd_quote(paths.codex_home)} "
                f"watch --interval {interval_text} --quiet >> {cmd_quote(paths.share_log_path)} 2>&1"
            ),
            "",
        ]
    )


def read_share_state(paths: Paths) -> dict[str, object]:
    if not paths.share_state_path.exists():
        return {}
    try:
        return json.loads(read_text(paths.share_state_path))
    except json.JSONDecodeError:
        return {}


def write_share_state(paths: Paths, state: dict[str, object]) -> None:
    paths.share_mode_dir.mkdir(parents=True, exist_ok=True)
    write_text_exact(paths.share_state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def is_process_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False

    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        return str(pid) in result.stdout

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def get_share_mode_status(paths: Paths, startup_dir: Path | None = None) -> dict[str, object]:
    startup = share_startup_path(startup_dir)
    state = read_share_state(paths)
    raw_pid = state.get("pid")
    pid = int(raw_pid) if isinstance(raw_pid, int | str) and str(raw_pid).isdigit() else None
    running = is_process_running(pid)
    return {
        "action": "share-status",
        "enabled": startup.exists(),
        "running": running,
        "pid": pid if running else None,
        "startup_path": str(startup),
        "log_path": str(paths.share_log_path),
        "state_path": str(paths.share_state_path),
        "last_started_at": state.get("started_at"),
    }


def share_process_args(paths: Paths, interval_seconds: float) -> list[str]:
    return [
        sys.executable,
        str(backend_script_path()),
        "--codex-home",
        str(paths.codex_home),
        "watch",
        "--interval",
        f"{interval_seconds:g}",
        "--quiet",
    ]


def enable_share_mode(
    paths: Paths,
    interval_seconds: float = 2.0,
    startup_dir: Path | None = None,
    start_process: bool = True,
) -> dict[str, object]:
    paths.share_mode_dir.mkdir(parents=True, exist_ok=True)
    startup = share_startup_path(startup_dir)
    startup.parent.mkdir(parents=True, exist_ok=True)
    write_text_exact(startup, build_share_startup_script(paths, interval_seconds))

    status_before = get_share_mode_status(paths, startup_dir=startup_dir)
    pid: int | None = int(status_before["pid"]) if status_before["running"] and status_before["pid"] else None

    if start_process and not status_before["running"]:
        log_handle = paths.share_log_path.open("a", encoding="utf-8")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            share_process_args(paths, interval_seconds),
            cwd=str(backend_script_path().parent),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        log_handle.close()
        pid = int(process.pid)

    if pid:
        write_share_state(
            paths,
            {
                "pid": pid,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "interval_seconds": interval_seconds,
                "startup_path": str(startup),
                "log_path": str(paths.share_log_path),
            },
        )

    status = get_share_mode_status(paths, startup_dir=startup_dir)
    status["action"] = "share-enable"
    return status


def stop_recorded_share_process(paths: Paths) -> bool:
    state = read_share_state(paths)
    raw_pid = state.get("pid")
    pid = int(raw_pid) if isinstance(raw_pid, int | str) and str(raw_pid).isdigit() else None
    if not is_process_running(pid):
        return False

    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        os.kill(int(pid), 15)
    return True


def disable_share_mode(
    paths: Paths,
    startup_dir: Path | None = None,
    stop_process: bool = True,
) -> dict[str, object]:
    startup = share_startup_path(startup_dir)
    if startup.exists():
        startup.unlink()

    stopped = stop_recorded_share_process(paths) if stop_process else False
    state = read_share_state(paths)
    state["stopped_at"] = datetime.now().isoformat(timespec="seconds")
    state["stopped_process"] = stopped
    write_share_state(paths, state)

    status = get_share_mode_status(paths, startup_dir=startup_dir)
    status["action"] = "share-disable"
    status["stopped_process"] = stopped
    return status


def watch_share_mode(paths: Paths, interval_seconds: float, quiet: bool) -> int:
    while True:
        try:
            payload = sync_share_once(paths)
            if (not quiet) or payload.get("changed"):
                print(to_json(payload), flush=True)
        except Exception as exc:
            print(to_json({"ok": False, "error": str(exc)}), flush=True)
        time.sleep(max(0.5, interval_seconds))


def to_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex history sync helper")
    parser.add_argument("--codex-home", help="Override Codex home directory")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Show current provider/thread status")
    sync_parser = subparsers.add_parser("sync", help="Move all thread providers to the current provider")
    sync_parser.add_argument("--passes", type=int, default=3, help="Maximum stabilization passes")
    restore_parser = subparsers.add_parser("restore", help="Restore from a backup")
    restore_parser.add_argument("--backup", help="Backup file path; newest backup is used when omitted")
    subparsers.add_parser("backup", help="Create a manual backup")
    subparsers.add_parser("share-once", help="Run one share-mode synchronization pass")
    subparsers.add_parser("share-status", help="Show background share-mode status")
    share_enable_parser = subparsers.add_parser("share-enable", help="Enable background share mode")
    share_enable_parser.add_argument("--interval", type=float, default=2.0, help="Watch interval in seconds")
    subparsers.add_parser("share-disable", help="Disable background share mode")
    watch_parser = subparsers.add_parser("watch", help="Continuously keep local history aligned")
    watch_parser.add_argument("--interval", type=float, default=2.0, help="Watch interval in seconds")
    watch_parser.add_argument("--quiet", action="store_true", help="Only log changes and errors")

    args = parser.parse_args()

    try:
        paths = resolve_paths(args.codex_home)
        if args.command == "status":
            payload = get_status(paths)
        elif args.command == "sync":
            payload = sync_to_current_provider(paths, max_passes=args.passes)
        elif args.command == "restore":
            payload = restore_backup(paths, args.backup)
        elif args.command == "backup":
            ensure_environment(paths)
            backup_started_at = time.monotonic()
            payload = {
                "action": "backup",
                "backup_path": str(make_backup(paths, "manual")),
                "timing": {"total_ms": elapsed_ms(backup_started_at)},
            }
        elif args.command == "share-once":
            payload = sync_share_once(paths)
        elif args.command == "share-status":
            payload = get_share_mode_status(paths)
        elif args.command == "share-enable":
            payload = enable_share_mode(paths, interval_seconds=args.interval)
        elif args.command == "share-disable":
            payload = disable_share_mode(paths)
        elif args.command == "watch":
            return watch_share_mode(paths, args.interval, args.quiet)
        else:
            raise RuntimeError(f"Unsupported command: {args.command}")
    except Exception as exc:
        error_payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(to_json(error_payload))
        else:
            print(error_payload["error"])
        return 1

    if isinstance(payload, dict):
        payload["ok"] = True

    if args.json:
        print(to_json(payload))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
