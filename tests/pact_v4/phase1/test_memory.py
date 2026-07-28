import unittest
import os
import shutil
import tempfile
from pact_v4.phase1.memory import MemoryManager, atomic_write, load_json

class TestMemoryManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mm = MemoryManager(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_atomic_write_and_load(self):
        path = os.path.join(self.test_dir, "test.json")
        data = {"key": "value"}
        atomic_write(path, data)
        loaded = load_json(path)
        self.assertEqual(data, loaded)

    def test_snapshot_creation(self):
        atomic_write(self.mm.glossary_path, {"term": "translation"})
        self.mm.create_snapshot()
        snapshot = load_json(self.mm.chapter_memory_path)
        self.assertEqual(snapshot["glossary"]["term"], "translation")
        
        obs = load_json(self.mm.observations_path)
        self.assertEqual(obs, {"glossary": {}, "book_memory": {}})

    def test_promotion_on_complete(self):
        atomic_write(self.mm.glossary_path, {"old": "val"})
        self.mm.add_observation("glossary", "new", "val2")
        
        self.mm.promote("complete")
        
        glossary = load_json(self.mm.glossary_path)
        self.assertIn("new", glossary)
        self.assertEqual(glossary["new"], "val2")
        
        obs = load_json(self.mm.observations_path)
        self.assertEqual(obs["glossary"], {})

    def test_isolation_on_failed(self):
        atomic_write(self.mm.glossary_path, {"old": "val"})
        self.mm.add_observation("glossary", "new", "val2")
        
        self.mm.promote("failed")
        
        glossary = load_json(self.mm.glossary_path)
        self.assertNotIn("new", glossary)
        
        obs = load_json(self.mm.observations_path)
        self.assertEqual(obs["glossary"]["new"], "val2")

    def test_conflict_resolution_locked(self):
        atomic_write(self.mm.glossary_path, {
            "locked_term": {"val": "stable", "status": "locked"},
            "open_term": {"val": "draft"}
        })
        
        self.mm.add_observation("glossary", "locked_term", {"val": "new_attempt"})
        self.mm.add_observation("glossary", "open_term", {"val": "updated"})
        
        self.mm.promote("complete")
        
        glossary = load_json(self.mm.glossary_path)
        # Should NOT overwrite locked
        self.assertEqual(glossary["locked_term"]["val"], "stable")
        # Should overwrite draft/open
        self.assertEqual(glossary["open_term"]["val"], "updated")

    def test_rollback(self):
        atomic_write(self.mm.glossary_path, {"v": 1})
        self.mm.create_snapshot()
        
        atomic_write(self.mm.glossary_path, {"v": 2})
        self.mm.rollback()
        
        glossary = load_json(self.mm.glossary_path)
        self.assertEqual(glossary["v"], 1)

if __name__ == "__main__":
    unittest.main()
