import unittest

from agent_core import Checkpoint, FileCheckpointStore, InMemoryCheckpointStore


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_validates_and_detaches_state(self) -> None:
        state = {"nested": {"value": 1}}
        checkpoint = Checkpoint("run-1", "graph-v1", "next", state, 2, 3)
        state["nested"]["value"] = 9
        self.assertEqual(checkpoint.state["nested"]["value"], 1)

    def test_checkpoint_rejects_invalid_counters_and_identity(self) -> None:
        with self.assertRaises(ValueError):
            Checkpoint("../escape", "graph", None, {}, 0, 0)
        with self.assertRaises(ValueError):
            Checkpoint("run", "graph", None, {}, -1, 0)
        with self.assertRaises(ValueError):
            Checkpoint("run", "graph", None, {}, 0, -1)

    def test_memory_store_roundtrip_is_detached(self) -> None:
        store = InMemoryCheckpointStore()
        checkpoint = Checkpoint("run-1", "graph-v1", "next", {"value": 1}, 1, 2)
        store.save(checkpoint)
        loaded = store.load("run-1")
        self.assertEqual(loaded, checkpoint)
        self.assertIsNot(loaded, checkpoint)
        self.assertIsNone(store.load("missing"))

    def test_memory_store_delete_is_idempotent(self) -> None:
        store = InMemoryCheckpointStore()
        store.save(Checkpoint("run-1", "graph-v1", None, {}, 0, 0))
        store.delete("run-1")
        store.delete("run-1")
        self.assertIsNone(store.load("run-1"))

    def test_memory_store_rejects_older_checkpoint(self) -> None:
        store = InMemoryCheckpointStore()
        store.save(Checkpoint("run-1", "graph-v1", "next", {}, 2, 2))
        with self.assertRaisesRegex(ValueError, "cannot move backwards"):
            store.save(Checkpoint("run-1", "graph-v1", "old", {}, 1, 1))

    def test_memory_store_rejects_step_count_rollback(self) -> None:
        store = InMemoryCheckpointStore()
        store.save(Checkpoint("run-1", "graph-v1", "next", {}, 2, 3))
        with self.assertRaisesRegex(ValueError, "step_count cannot move backwards"):
            store.save(Checkpoint("run-1", "graph-v1", "old", {}, 2, 2))

    def test_memory_store_rejects_conflicting_checkpoint_without_progress(self) -> None:
        store = InMemoryCheckpointStore()
        store.save(Checkpoint("run-1", "graph-v1", "next", {"value": 1}, 2, 3))
        with self.assertRaisesRegex(ValueError, "cannot change without progress"):
            store.save(Checkpoint("run-1", "graph-v1", "other", {"value": 2}, 2, 3))

    def test_memory_store_checks_graph_compatibility(self) -> None:
        store = InMemoryCheckpointStore()
        store.save(Checkpoint("run-1", "graph-v1", "next", {}, 1, 1))
        self.assertIsNotNone(store.load_for("run-1", "graph-v1"))
        with self.assertRaisesRegex(ValueError, "graph_id does not match"):
            store.load_for("run-1", "graph-v2")
        with self.assertRaisesRegex(ValueError, "graph_id cannot change"):
            store.save(Checkpoint("run-1", "graph-v2", "next", {}, 2, 2))

    def test_checkpoint_json_shape_roundtrips_and_rejects_unknown_schema(self) -> None:
        checkpoint = Checkpoint("run-1", "graph-v1", "next", {"value": 1}, 2, 3)
        self.assertEqual(Checkpoint.from_dict(checkpoint.to_dict()), checkpoint)
        payload = checkpoint.to_dict()
        payload["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "unsupported checkpoint"):
            Checkpoint.from_dict(payload)

    def test_file_store_roundtrip_and_graph_check(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            store = FileCheckpointStore(Path(directory))
            checkpoint = Checkpoint("run-1", "graph-v1", "next", {"value": 1}, 2, 3)
            store.save(checkpoint)
            self.assertEqual(store.load("run-1"), checkpoint)
            self.assertEqual(store.load_for("run-1", "graph-v1"), checkpoint)
            with self.assertRaises(ValueError):
                store.load_for("run-1", "graph-v2")

    def test_file_store_rejects_older_checkpoint_and_bad_record(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            store = FileCheckpointStore(Path(directory))
            store.save(Checkpoint("run-1", "graph-v1", None, {}, 2, 2))
            with self.assertRaises(ValueError):
                store.save(Checkpoint("run-1", "graph-v1", None, {}, 1, 1))
            with self.assertRaisesRegex(ValueError, "step_count cannot move backwards"):
                store.save(Checkpoint("run-1", "graph-v1", None, {}, 2, 1))
            with self.assertRaisesRegex(ValueError, "graph_id cannot change"):
                store.save(Checkpoint("run-1", "graph-v2", None, {}, 3, 3))
            with self.assertRaisesRegex(ValueError, "cannot change without progress"):
                store.save(Checkpoint("run-1", "graph-v1", "other", {}, 2, 2))
            (Path(directory) / "broken.json").write_text("{bad", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid checkpoint"):
                store.load("broken")

    def test_file_store_cleans_temporary_file_when_replace_fails(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            store = FileCheckpointStore(Path(directory))
            checkpoint = Checkpoint("replace-failure", "graph-v1", "next", {}, 1, 1)
            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    store.save(checkpoint)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
