#!/usr/bin/env python3
"""Synchronize verified local project groups between two saved accounts."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

import recover

ATOMS = "electron-persisted-atom-state"
UNIFIED_ORDER = "unified-sidebar-project-order-v1"
PROJECT_PREFIX = "codex:project:"


def merge_order(existing, shared, sequence):
    """Replace shared slots in order and retain the order of other entries."""
    recover.check(isinstance(existing, list) and all(isinstance(x, str) for x in existing),
                  "Unsupported sidebar order.")
    values = iter(sequence)
    result = []
    for item in existing:
        if item not in shared:
            result.append(item)
        else:
            replacement = next(values, None)
            if replacement is not None:
                result.append(replacement)
    return [*result, *values]


def plan(state, host, database, include_chats):
    scope = state[recover.SCOPE]
    owner, inactive = scope["owner"], scope["inactive"]
    recover.check(isinstance(owner, str) and bool(owner) and isinstance(inactive, dict) and len(inactive) == 1,
                  "Exactly two saved Codex accounts are required.")
    [(other, saved)] = inactive.items()
    recover.check(other != owner and isinstance(saved["globals"], dict) and isinstance(saved["atoms"], dict),
                  "Invalid saved account scope.")
    saved_globals, saved_atoms = saved["globals"], saved["atoms"]
    active_atoms = state.get(ATOMS, {})
    recover.check(isinstance(active_atoms, dict), "Unsupported active sidebar preferences.")
    # For a saved account there is no live UI list. Only already existing local records
    # with a known creator, original explicit association and matching cwd can qualify.
    local_records = set(database[3]) if include_chats else None
    active_updates, into_active, active_skips = recover.plan_changes(state, host, other, local_records, database)
    reverse = {
        **saved_globals,
        recover.SCOPE: {"owner": other, "inactive": {owner: {"globals": state}}},
    }
    saved_updates, into_saved, saved_skips = recover.plan_changes(reverse, host, owner, local_records, database)
    active_merged = {**state, **active_updates}
    saved_merged = {**saved_globals, **saved_updates}

    # Only verified records with the same local ID and SQLite mapping participate in common order.
    active_projects = active_merged.get(recover.PROJECTS, {})
    saved_projects = saved_merged.get(recover.PROJECTS, {})
    active_map = active_merged.get(recover.MAPPING, {}).get(host, {})
    saved_map = saved_merged.get(recover.MAPPING, {}).get(host, {})
    projects, keys, roots, _ = database
    shared = {
        key for key, item in active_projects.items()
        if key in saved_projects and item == saved_projects[key]
        and not key.startswith("g-p-") and item.keys() == recover.LOCAL_FIELDS
        and key in active_map and active_map[key] == saved_map.get(key) == keys.get(key)
        and projects.get(active_map[key]) == (item["name"], {})
        and roots.get(active_map[key]) == item["rootPaths"]
        and all(Path(path).is_absolute() and Path(path).is_dir() for path in item["rootPaths"])
    }

    def synchronize_order(left, right, key, available):
        if not available:
            return left, right
        left_order, right_order = left.get(key, []), right.get(key, [])
        recover.check(all(isinstance(items, list) and all(isinstance(x, str) for x in items)
                          for items in (left_order, right_order)), "Unsupported sidebar order.")
        sequence = list(dict.fromkeys(item for item in [*left_order, *right_order, *sorted(available)]
                                      if item in available))
        return ({**left, key: merge_order(left_order, available, sequence)},
                {**right, key: merge_order(right_order, available, sequence)})

    active_merged, saved_merged = synchronize_order(active_merged, saved_merged, recover.ORDER, shared)
    active_atoms, saved_new_atoms = synchronize_order(
        active_atoms, saved_atoms, UNIFIED_ORDER, {PROJECT_PREFIX + key for key in shared}
    )
    if active_atoms != state.get(ATOMS, {}):
        active_merged[ATOMS] = active_atoms
    active_merged[recover.SCOPE] = {**scope, "inactive": {other: {
        **saved, "globals": saved_merged, "atoms": saved_new_atoms,
    }}}
    updates = {key: value for key, value in active_merged.items() if key not in state or value != state[key]}
    skipped = Counter()
    for item in (active_skips, saved_skips):
        for kind in ("projects", "chats"):
            skipped.update(reason for reason in item[kind].values() if reason != "already_present")
    summary = {
        "changed": bool(updates),
        "active_projects": into_active["projects_added"],
        "active_chats": into_active["chat_groups_added"],
        "saved_projects": into_saved["projects_added"],
        "saved_chats": into_saved["chat_groups_added"],
        "shared_projects": len(shared),
        "skipped": dict(skipped),
    }
    return updates, summary, {"into_active": active_skips, "into_saved": saved_skips}


def run(home, backup_parent, include_chats, apply):
    guard = lambda: recover.ensure_offline(home, check_switchers=False)
    guard()
    backup_parent = recover.private_backup_path(backup_parent, home)
    backup_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix="sync-", dir=backup_parent))
    recover.capture(home, backup)
    raw = (backup / recover.STATE).read_bytes()
    changes, result, exclusions = plan(
        recover.read_json(raw), f"local:{home.resolve()}", recover.load_database(backup), include_chats
    )
    recover.write_json(backup / "exclusions.json", exclusions)
    result["backup"] = str(backup)
    result["applied"] = False
    if changes:
        candidate = recover.patch_top_level(raw, changes)
        (backup / "candidate.json").write_bytes(candidate)
        recover.write_json(backup / "manifest.json", {
            "baseline": recover.digest(raw), "candidate": recover.digest(candidate),
            "home": recover.digest(os.fsencode(home.resolve())),
        })
        if apply:
            def final_check():
                guard()
                recover.verify_capture(home, backup)

            recover.atomic_replace(home / recover.STATE, raw, candidate, final_check)
            result["applied"] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--backup-parent", type=Path, required=True)
    parser.add_argument("--include-chats", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = run(args.codex_home.expanduser().absolute(), args.backup_parent.expanduser(),
                 args.include_chats, args.apply)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except recover.Stop as error:
        sys.exit(f"STOP: {error}")
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError,
            sqlite3.Error, subprocess.SubprocessError):
        sys.exit("STOP: A file, schema, or process check failed.")
