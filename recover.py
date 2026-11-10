#!/usr/bin/env python3
"""Prepare or apply a narrowly scoped macOS Codex sidebar recovery."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from contextlib import closing

STATE = ".codex-global-state.json"
DATABASE = "state_5.sqlite"
SCOPE = "electron-account-renderer-state-v1"
PROJECTS = "local-projects"
MAPPING = "app-server-project-id-by-legacy-project-id-by-host"
ASSIGNMENTS = "thread-project-assignments"
HOSTS = "thread-project-membership-host-ids"
PROJECTLESS = "projectless-thread-ids"
ORDER = "project-order"
LOCAL_FIELDS = {"id", "name", "rootPaths", "createdAt", "updatedAt"}
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


class Stop(Exception):
    """A precondition failed."""


def check(condition, message):
    if not condition:
        raise Stop(message)


def unique(entries):
    result = dict(entries)
    check(len(result) == len(entries), "Duplicate JSON keys.")
    return result


def read_json(data):
    value = json.loads(data, object_pairs_hook=unique)
    check(isinstance(value, dict), "Expected a JSON object.")
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def principal(user, account):
    if user and account:
        value = json.dumps([user, account], ensure_ascii=False, separators=(",", ":"))
        return digest(value.encode("utf-8"))
    return None


def require_file(path):
    info = path.lstat()
    check(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1,
          "A state file has an unexpected owner or file type.")
    return info


def ensure_offline(home):
    check(sys.platform == "darwin", "This recovery workflow supports macOS only.")
    check(home.is_dir() and not home.is_symlink(), "Invalid Codex home.")
    processes = subprocess.run(["/bin/ps", "-A", "-o", "comm="], check=True,
                               capture_output=True, text=True).stdout
    apps = re.compile(r"(?:^|/)(ChatGPT|Codex|CC Switch|codexpanel|Visual Studio Code[^/]*)\.app/", re.I)
    names = {"chatgpt", "codex", "codex-cli", "cc-switch", "codexpanel", "code", "code-insiders"}
    for command in processes.splitlines():
        executable = Path(command.strip()).name
        if executable != "browser_crashpad_handler" and (apps.search(command) or executable.casefold() in names):
            raise Stop("Quit Codex/ChatGPT, VS Code and any account switchers before continuing.")
    paths = [home / filename for filename in (STATE, DATABASE, DATABASE + "-wal", DATABASE + "-shm")]
    probe = subprocess.run(["/usr/sbin/lsof", "-nP", "-t", "--", *[str(p) for p in paths if p.exists()]],
                           capture_output=True, text=True)
    check(probe.returncode in (0, 1) and not probe.stdout.strip() and not probe.stderr.strip(),
          "Codex state files may still be in use.")


def private_backup_path(path, home):
    resolved = path.resolve()
    check(not resolved.is_relative_to(home.resolve()), "Choose a backup directory outside Codex home.")
    check(not any((parent / ".git").exists() for parent in (resolved, *resolved.parents)),
          "Choose a backup directory outside Git repositories.")
    return resolved


def capture(home, backup):
    files = (STATE, DATABASE, DATABASE + "-wal")
    for name in files:
        source = home / name
        if name.endswith("-wal") and not source.exists() and not source.is_symlink():
            continue
        require_file(source)
        shutil.copyfile(source, backup / name)
    verify_capture(home, backup)


def verify_capture(home, backup):
    for name in (STATE, DATABASE, DATABASE + "-wal"):
        live, copy = home / name, backup / name
        check(live.exists() == copy.exists(), "Live state changed after capture.")
        if live.exists():
            require_file(live)
            check(digest(live.read_bytes()) == digest(copy.read_bytes()), "Live state changed after capture.")


def load_database(backup):
    # SQLite can rebuild WAL shared memory in this disposable copy.
    with tempfile.TemporaryDirectory(dir=backup) as scratch:
        folder = Path(scratch)
        for name in (DATABASE, DATABASE + "-wal"):
            if (backup / name).exists():
                shutil.copyfile(backup / name, folder / name)
        with closing(sqlite3.connect((folder / DATABASE).as_uri() + "?mode=ro")) as db:
            db.execute("PRAGMA query_only=ON")
            check(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
                  and not db.execute("PRAGMA foreign_key_check").fetchall(), "SQLite integrity check failed.")
            projects = {key: (label, read_json(metadata)) for key, label, metadata in db.execute(
                "SELECT id, name, metadata FROM projects"
            )}
            keys = dict(db.execute("SELECT key, project_id FROM project_idempotency_keys"))
            roots = defaultdict(list)
            for key, path in db.execute("SELECT project_id, path FROM project_roots ORDER BY position"):
                roots[key].append(path)
            threads = {row[0]: row[1:] for row in db.execute(
                "SELECT id, creator_user_id, creator_account_id, project_id, archived, cwd FROM threads"
            )}
    return projects, keys, roots, threads


def directory_set(paths):
    return frozenset(Path(path).resolve() for path in paths)


def plan_changes(state, host, source_key, visible, database):
    scope = state[SCOPE]
    owner, inactive = scope["owner"], scope["inactive"]
    check(isinstance(owner, str) and bool(owner) and isinstance(inactive, dict),
          "No active account was found.")
    if source_key is None:
        check(len(inactive) == 1, "Select one inactive source with --source-key.")
        source_key = next(iter(inactive))
    check(source_key in inactive and source_key != owner, "Invalid inactive source account.")
    source = inactive[source_key]["globals"]
    old_projects, target_projects = source[PROJECTS], state.get(PROJECTS, {})
    mappings = state.get(MAPPING, {})
    old_map, target_map = source[MAPPING][host], mappings.get(host, {})
    old_links, target_links = source.get(ASSIGNMENTS, {}), state.get(ASSIGNMENTS, {})
    check(all(isinstance(section, dict) for section in (
        old_projects, target_projects, old_map, target_map, old_links, target_links
    )), "Unsupported sidebar structure.")
    check(len(set(target_map.values())) == len(target_map), "Destination project mapping is ambiguous.")
    projects, keys, roots, threads = database
    old_roots = Counter(directory_set(project["rootPaths"]) for project in old_projects.values())
    used_roots = {directory_set(project["rootPaths"]) for project in target_projects.values()}
    permitted, added_projects, added_map, added_links = {}, {}, {}, {}
    project_skips, chat_skips = {}, {}
    for key, record in old_projects.items():
        server = old_map.get(key)
        folders = record["rootPaths"]
        folder_set = directory_set(folders)
        if key.startswith("g-p-") or record.keys() != LOCAL_FIELDS:
            reason = "nonlocal_or_unverified_metadata"
        elif (record["id"] != key or not folders or keys.get(key) != server
              or projects.get(server) != (record["name"], {}) or roots.get(server) != folders
              or not all(Path(p).is_absolute() and Path(p).is_dir() for p in folders)):
            reason = "database_or_directory_mismatch"
        elif old_roots[folder_set] != 1 or sum(v == server for v in old_map.values()) != 1:
            reason = "ambiguous_source_project"
        elif key in target_projects:
            reason = "already_present" if target_projects[key] == record and target_map.get(key) == server else "target_conflict"
        elif key in target_map or server in target_map.values() or folder_set in used_roots:
            reason = "target_conflict"
        else:
            reason = None

        if reason is not None:
            project_skips[key] = reason
        if reason in (None, "already_present"):
            permitted[key] = server
        if reason is None:
            added_projects[key], added_map[key] = record, server

    if visible is not None:
        for thread, record in old_links.items():
            project = record.get("projectId")
            row = threads.get(thread)
            if thread in target_links:
                reason = "already_present" if target_links[thread] == record else "target_conflict"
            elif thread in state.get(PROJECTLESS, []):
                reason = "explicitly_projectless"
            elif record.keys() != {"projectKind", "projectId"} or record["projectKind"] != "local":
                reason = "nonlocal_or_unverified_association"
            elif project not in permitted:
                reason = "project_excluded"
            elif row is None:
                reason = "absent_from_sqlite"
            elif principal(row[0], row[1]) not in (owner, source_key):
                reason = "creator_unverified"
            elif (thread not in visible or row[3] or source.get(HOSTS, {}).get(thread, "local") != "local"
                  or state.get(HOSTS, {}).get(thread, "local") != "local"):
                reason = "not_verified_visible_local"
            elif row[2] not in (None, permitted[project]):
                reason = "sqlite_project_conflict"
            elif not Path(row[4]).is_absolute() or not any(
                Path(row[4]).resolve().is_relative_to(Path(root).resolve()) for root in roots[permitted[project]]
            ):
                reason = "outside_project_directory"
            else:
                added_links[thread] = record
                continue
            chat_skips[thread] = reason

    changes = {}
    if added_projects:
        changes[PROJECTS] = {**target_projects, **added_projects}
        changes[MAPPING] = {**mappings, host: {**target_map, **added_map}}
        old_order, target_order = source.get(ORDER, []), state.get(ORDER, [])
        check(isinstance(old_order, list) and isinstance(target_order, list), "Unsupported project order.")
        tail = list(dict.fromkeys(key for key in old_order if key in added_projects and key not in target_order))
        if tail:
            changes[ORDER] = target_order + tail
    if added_links:
        changes[ASSIGNMENTS] = {**target_links, **added_links}
    summary = {
        "projects_added": len(added_projects), "chat_groups_added": len(added_links),
        "project_skips": dict(Counter(project_skips.values())),
        "chat_skips": dict(Counter(chat_skips.values())),
    }
    return changes, summary, {"projects": project_skips, "chats": chat_skips}


def patch_top_level(data, updates):
    # Keep the original text of every untouched value.
    text = data.decode("utf-8")
    decoder = json.JSONDecoder(object_pairs_hook=unique)
    whitespace = re.compile(r"\s*").match
    pos = whitespace(text, 0).end() + 1
    spans = {}
    seen = set()
    while True:
        pos = whitespace(text, pos).end()
        if text[pos] == "}":
            break
        key, end = decoder.raw_decode(text, pos)
        colon = whitespace(text, end).end()
        start = whitespace(text, colon + 1).end()
        _, end = decoder.raw_decode(text, start)
        seen.add(key)
        if key in updates:
            spans[key] = (start, end)
        pos = whitespace(text, end).end()
        if text[pos] == "}":
            break
        pos += 1

    def dump(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    missing = {k: v for k, v in updates.items() if k not in seen}
    if missing:
        text = text[:pos] + ("," if seen else "") + dump(missing)[1:-1] + text[pos:]
    for key, (start, end) in sorted(spans.items(), key=lambda item: item[1], reverse=True):
        text = text[:start] + dump(updates[key]) + text[end:]
    result = text.encode("utf-8")
    check(read_json(result) == {**read_json(data), **updates}, "Candidate changed unexpected fields.")
    return result


def atomic_replace(path, expected, result, guard):
    original = require_file(path)
    check(path.read_bytes() == expected, "Current sidebar hash differs. No write made.")
    fd, temp_name = tempfile.mkstemp(prefix=".codex-sidebar-recovery-", dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            if temporary.stat().st_gid != original.st_gid:
                os.fchown(stream.fileno(), -1, original.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(original.st_mode))
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        guard()
        latest = require_file(path)
        check((original.st_dev, original.st_ino, original.st_mode, original.st_gid)
              == (latest.st_dev, latest.st_ino, latest.st_mode, latest.st_gid)
              and path.read_bytes() == expected, "Current sidebar changed. No write made.")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    check(path.read_bytes() == result, "Read-back differed after replacement. Keep the private backup.")


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "apply", "rollback"):
        sub = actions.add_parser(action)
        sub.add_argument("--backup-dir", type=Path, required=True)
        if action == "prepare":
            sub.add_argument("--source-key")
            sub.add_argument("--visible-threads", type=Path, help="Optional file of verified active local chat IDs.")
    args = parser.parse_args()
    home = args.codex_home.expanduser().absolute()
    backup = private_backup_path(args.backup_dir.expanduser(), home)
    ensure_offline(home)
    if args.action == "prepare":
        backup.mkdir(mode=0o700)
        capture(home, backup)
        raw = (backup / STATE).read_bytes()
        visible = None
        if args.visible_threads:
            visible = set(args.visible_threads.read_text().split())
            check(visible and all(UUID.fullmatch(x) for x in visible), "Expected only chat IDs in the allowlist.")
        changes, summary, exclusions = plan_changes(
            read_json(raw), f"local:{home.resolve()}", args.source_key, visible, load_database(backup)
        )
        write_json(backup / "exclusions.json", exclusions)
        print(json.dumps(summary))
        check(changes, "No verified changes to apply.")
        candidate = patch_top_level(raw, changes)
        ensure_offline(home)
        verify_capture(home, backup)
        (backup / "candidate.json").write_bytes(candidate)
        write_json(backup / "manifest.json", {"baseline": digest(raw), "candidate": digest(candidate),
                                              "home": digest(os.fsencode(home.resolve()))})
        print("PREPARE OK")
        return

    check(backup.stat().st_uid == os.getuid() and stat.S_IMODE(backup.stat().st_mode) == 0o700,
          "The backup directory must be private (0700).")
    raw = (backup / STATE).read_bytes()
    candidate = (backup / "candidate.json").read_bytes()
    manifest = read_json((backup / "manifest.json").read_bytes())
    check(manifest == {"baseline": digest(raw), "candidate": digest(candidate),
                       "home": digest(os.fsencode(home.resolve()))}, "Backup or candidate hash mismatch.")

    if args.action == "apply":
        def guard():
            ensure_offline(home)
            verify_capture(home, backup)

        atomic_replace(home / STATE, raw, candidate, guard)
        print("APPLY OK")
    else:
        atomic_replace(home / STATE, candidate, raw, lambda: ensure_offline(home))
        print("ROLLBACK OK")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except Stop as error:
        sys.exit(f"STOP: {error}")
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, sqlite3.Error, subprocess.SubprocessError):
        sys.exit("STOP: A file, schema, or process check failed.")
