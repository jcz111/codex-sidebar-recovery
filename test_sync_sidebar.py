import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import recover
import sync_sidebar
import test_recover


class SharedSidebarTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_recover.RecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.home = self.fixture.home
        self.source = self.fixture.source
        self.host = self.fixture.host

    def test_empty_destination_and_idempotent_second_run(self):
        private = self.fixture.folder / "backups"
        with patch.object(recover, "ensure_offline"):
            first = sync_sidebar.run(self.home, private, True, True)
            current = (self.home / recover.STATE).read_bytes()
            second = sync_sidebar.run(self.home, private, True, True)
        self.assertTrue(first["applied"])
        self.assertEqual(first["active_projects"], 1)
        self.assertFalse(second["applied"])
        self.assertEqual(current, (self.home / recover.STATE).read_bytes())
        result = json.loads(current)
        self.assertEqual(result[recover.ASSIGNMENTS][self.fixture.good], self.fixture.link)
        self.assertNotIn(self.fixture.projectless, result[recover.ASSIGNMENTS])
        self.assertNotIn(self.fixture.unverified, result[recover.ASSIGNMENTS])
        self.assertEqual(result[recover.PROJECTLESS], [self.fixture.projectless])
        self.assertEqual(result["remote-project-order"], ["remote-demo"])

    def test_projects_and_known_chats_flow_in_both_directions(self):
        root = self.fixture.folder / "second-project"
        root.mkdir()
        project = {"id": "new-local-project", "name": "Second", "rootPaths": [str(root)],
                   "createdAt": 2, "updatedAt": 2}
        chat = "20000000-0000-0000-0000-000000000001"
        link = {"projectKind": "local", "projectId": project["id"]}
        self.state[recover.PROJECTS] = {project["id"]: project}
        self.state[recover.MAPPING] = {self.host: {project["id"]: "second-sqlite-project"}}
        self.state[recover.ASSIGNMENTS] = {chat: link}
        self.state[recover.ORDER] = [project["id"]]
        with closing(sqlite3.connect(self.home / recover.DATABASE)) as db:
            db.execute("INSERT INTO projects VALUES ('second-sqlite-project', 'Second', '{}')")
            db.execute("INSERT INTO project_idempotency_keys VALUES ('new-local-project', 'second-sqlite-project')")
            db.execute("INSERT INTO project_roots VALUES ('second-sqlite-project', 0, ?)", (str(root),))
            db.execute("INSERT INTO threads VALUES (?, 'target-user', 'target-account', NULL, 0, ?)",
                       (chat, str(root)))
            db.commit()

        database = recover.load_database(self.home)
        changes, stats, _ = sync_sidebar.plan(self.state, self.host, database, True)
        merged = {**self.state, **changes}
        other = merged[recover.SCOPE]["inactive"][self.source]
        self.assertEqual(stats["active_projects"], 1)
        self.assertEqual(stats["saved_projects"], 1)
        self.assertEqual(other["globals"][recover.ASSIGNMENTS][chat], link)
        self.assertEqual(merged[recover.ASSIGNMENTS][self.fixture.good], self.fixture.link)
        self.assertEqual(merged[recover.ORDER], other["globals"][recover.ORDER])
        self.assertEqual(other["atoms"][sync_sidebar.UNIFIED_ORDER],
                         merged[sync_sidebar.ATOMS][sync_sidebar.UNIFIED_ORDER])


if __name__ == "__main__":
    unittest.main()
