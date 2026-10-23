import unittest

from agent_core import Checkpoint, InMemoryCheckpointStore


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


if __name__ == "__main__":
    unittest.main()
