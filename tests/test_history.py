import json
import tempfile
import unittest
from pathlib import Path

from bot.history import prepare_history


class HistoryMigrationTests(unittest.TestCase):
    def test_archives_retired_rows_and_keeps_v4_and_new_single_strategy(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            rows = [
                {"record_type": "decision", "slug": "old", "start": 100,
                 "recorded_ms": 1, "model_outputs": {"btc_twap_v2": {}}},
                {"record_type": "resolution", "slug": "old", "recorded_ms": 2},
                {"record_type": "decision", "slug": "kept", "start": 200,
                 "recorded_ms": 3, "fee_rate": .07, "components": {}, "quotes": {},
                 "model_version": "btc_twap_v2",
                 "model_outputs": {"btc_guard_v4": {"direction": "SKIP"}}},
                {"record_type": "resolution", "slug": "kept", "recorded_ms": 4},
                {"record_type": "decision", "slug": "single", "start": 300,
                 "recorded_ms": 5, "direction": "Up", "p_up": .95, "p_down": .05},
                {"record_type": "resolution", "slug": "single", "recorded_ms": 6},
            ]
            legacy = data / "stage4-round-history.jsonl"
            legacy.write_text("".join(json.dumps(row) + "\n" for row in rows))
            result = prepare_history(data)

            retained = [json.loads(line) for line in
                        (data / "round-history.jsonl").read_text().splitlines()]
            self.assertTrue(result["migrated"])
            self.assertEqual({row["slug"] for row in retained}, {"kept", "single"})
            kept = next(row for row in retained
                        if row["slug"] == "kept" and row["record_type"] == "decision")
            self.assertEqual(kept["direction"], "SKIP")
            self.assertNotIn("model_outputs", kept)
            self.assertNotIn("model_version", kept)
            self.assertTrue((data / "archive" / "legacy-history" /
                             "stage4-round-history.jsonl").exists())
            self.assertFalse(legacy.exists())

    def test_empty_install_creates_history_file(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            result = prepare_history(data)
            self.assertFalse(result["migrated"])
            self.assertTrue((data / "round-history.jsonl").exists())

    def test_post_migration_rows_are_merged_without_overwriting_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data / "round-history.jsonl").write_text(json.dumps({
                "record_type": "decision", "slug": "existing", "recorded_ms": 1,
                "direction": "SKIP",
            }) + "\n")
            archive = data / "archive" / "legacy-history"
            archive.mkdir(parents=True)
            original_archive = archive / "stage4-round-history.jsonl"
            original_archive.write_text("original archive\n")
            late = data / "stage4-round-history.jsonl"
            late.write_text(json.dumps({
                "record_type": "decision", "slug": "late", "recorded_ms": 2,
                "direction": "Up", "p_up": .95, "p_down": .05,
            }) + "\n")

            result = prepare_history(data)

            rows = [json.loads(line) for line in
                    (data / "round-history.jsonl").read_text().splitlines()]
            self.assertEqual({row["slug"] for row in rows}, {"existing", "late"})
            self.assertEqual(result["merged_post_migration_rows"], 1)
            self.assertEqual(original_archive.read_text(), "original archive\n")
            self.assertTrue((archive /
                             "post-migration-stage4-round-history.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
