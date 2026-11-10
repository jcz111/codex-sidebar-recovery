import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import recover


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.home = self.folder / "codex-state"
        self.home.mkdir()
        self.project_root = self.folder / "synthetic-project"
        self.project_root.mkdir()
        self.host = f"local:{self.home.resolve()}"
        self.source = recover.principal("source-user", "source-account")
        self.target = recover.principal("target-user", "target-account")
        self.good = "10000000-0000-0000-0000-000000000001"
        self.projectless = "10000000-0000-0000-0000-000000000002"
        self.unverified = "10000000-0000-0000-0000-000000000003"
        self.hidden = "10000000-0000-0000-0000-000000000004"
        self.project = {"id": "old-local-project", "name": "Demo", "rootPaths": [str(self.project_root)],
                        "createdAt": 1, "updatedAt": 1}
        self.link = {"projectKind": "local", "projectId": self.project["id"]}
        self.state = {
            recover.PROJECTLESS: [self.projectless],
            "remote-project-order": ["remote-demo"],
            recover.SCOPE: {"owner": self.target, "inactive": {self.source: {"globals": {
                recover.PROJECTS: {self.project["id"]: self.project},
                recover.MAPPING: {self.host: {self.project["id"]: "sqlite-project"}},
                recover.ORDER: [self.project["id"]],
                recover.ASSIGNMENTS: {t: self.link for t in (self.good, self.projectless, self.unverified, self.hidden)}
            }, "atoms": {}}}},
        }
        (self.home / recover.STATE).write_text(json.dumps(self.state), encoding="utf-8")
        with closing(sqlite3.connect(self.home / recover.DATABASE)) as db:
            db.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, metadata TEXT);
                CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT);
                CREATE TABLE project_idempotency_keys (key TEXT PRIMARY KEY, project_id TEXT);
                CREATE TABLE threads (id TEXT PRIMARY KEY, creator_user_id TEXT,
                    creator_account_id TEXT, project_id TEXT, archived INTEGER, cwd TEXT);
                INSERT INTO projects VALUES ('sqlite-project', 'Demo', '{}');
                INSERT INTO project_idempotency_keys VALUES ('old-local-project', 'sqlite-project');
            """)
            db.execute("INSERT INTO project_roots VALUES ('sqlite-project', 0, ?)", (str(self.project_root),))
            db.executemany("INSERT INTO threads VALUES (?, ?, ?, NULL, 0, ?)", [
                (self.good, "source-user", "source-account", str(self.project_root)),
                (self.projectless, "source-user", "source-account", str(self.project_root)),
                (self.unverified, None, None, str(self.project_root)),
                (self.hidden, "source-user", "source-account", str(self.project_root)),
            ])
            db.commit()

    def cli(self, backup, *args):
        argv = ["recover.py", "--codex-home", str(self.home), *args, "--backup-dir", str(backup)]
        with patch("sys.argv", argv), patch.object(recover, "ensure_offline"):
            recover.main()

    def test_prepare_apply_and_rollback(self):
        backup = self.folder / "private"
        allowlist = self.folder / "synthetic-ids.txt"
        allowlist.write_text("\n".join((self.good, self.projectless, self.unverified)))
        state_file = self.home / recover.STATE
        original = state_file.read_bytes()
        database = (self.home / recover.DATABASE).read_bytes()
        self.cli(backup, "prepare", "--visible-threads", str(allowlist))
        self.assertEqual(state_file.read_bytes(), original)
        self.cli(backup, "apply")
        applied = json.loads(state_file.read_bytes())
        self.assertEqual(applied[recover.ASSIGNMENTS], {self.good: self.link})
        self.assertEqual(applied[recover.SCOPE], self.state[recover.SCOPE])
        self.assertEqual(applied[recover.PROJECTLESS], [self.projectless])
        self.assertEqual(applied["remote-project-order"], ["remote-demo"])
        self.assertEqual((self.home / recover.DATABASE).read_bytes(), database)
        self.cli(backup, "rollback")
        self.assertEqual(state_file.read_bytes(), original)

    def test_stale_json_stops_apply(self):
        backup = self.folder / "private"
        self.cli(backup, "prepare")
        live = self.home / recover.STATE
        changed = live.read_bytes() + b"\n"
        live.write_bytes(changed)
        with self.assertRaisesRegex(recover.Stop, "hash differs"):
            self.cli(backup, "apply")
        self.assertEqual(live.read_bytes(), changed)

    def test_conflicting_destination_project_stops_import(self):
        self.state[recover.PROJECTS] = {"different-id": {**self.project, "id": "different-id"}}
        changes, summary, exclusions = recover.plan_changes(
            self.state, self.host, self.source, {self.good}, recover.load_database(self.home)
        )
        self.assertEqual(changes, {})
        self.assertEqual(summary["project_skips"], {"target_conflict": 1})
        self.assertEqual(exclusions["chats"][self.good], "project_excluded")


if __name__ == "__main__":
    unittest.main()
