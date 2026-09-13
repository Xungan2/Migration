import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from porter.artifacts import ARTIFACTS, PREPARE_ARTIFACTS, locate


class TestArtifacts(unittest.TestCase):
    def test_prepare_can_index_only_markdown_plan(self):
        with TemporaryDirectory() as raw:
            ws = Path(raw)
            (ws / "plan" / "migration-plan.md").parent.mkdir()
            (ws / "plan" / "migration-plan.md").write_text("# plan\n", encoding="utf-8")
            self.assertEqual(set(locate(ws, required=PREPARE_ARTIFACTS)),
                             {"migration-plan.md"})

    def test_recursive_discovery_and_state_index(self):
        with TemporaryDirectory() as raw:
            ws = Path(raw)
            folder = ws / "nested" / "plans"
            folder.mkdir(parents=True)
            for name in ARTIFACTS:
                (folder / name).write_text("{}\n", encoding="utf-8")
            paths = locate(ws)
            state = json.loads((ws / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["artifacts"]["migration-plan.json"]["path"],
                             "nested/plans/migration-plan.json")
            (folder / "migration-plan.json").unlink()
            replacement = ws / "other" / "migration-plan.json"
            replacement.parent.mkdir()
            replacement.write_text("{}\n", encoding="utf-8")
            self.assertEqual(locate(ws)["migration-plan.json"], replacement.resolve())


if __name__ == "__main__":
    unittest.main()
