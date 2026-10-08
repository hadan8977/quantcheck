import _test_env  # noqa: F401  -- must stay first: isolates QUANTCHECK_HOME from the real install
import json
import unittest

from quantcheck.state import atomic_write_json, load_json, prune_old_files


class StateTests(unittest.TestCase):
    def test_atomic_write_json_round_trips(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, {"symbol": "ABC"})

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"symbol": "ABC"})
            self.assertEqual(load_json(path), {"symbol": "ABC"})

    def test_prune_old_files_keeps_newest(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for index in range(3):
                path = tmp_path / f"raw_{index}.json"
                path.write_text("{}", encoding="utf-8")

            prune_old_files(tmp_path, "raw_*.json", keep=2)

            self.assertEqual(len(list(tmp_path.glob("raw_*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
