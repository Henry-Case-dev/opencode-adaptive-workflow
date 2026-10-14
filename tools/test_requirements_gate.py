"""Standard-library tests for requirements_gate. Run python -m unittest tools/test_requirements_gate.py."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("requirements_gate.py")

class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.source = self.path / "task.md"
        self.source.write_text("# Project\n- Must fix real A flow\n- Must fix UI B flow\n", encoding="utf8")
        self.map = self.path / "map.json"
        self.run_gate("init", "--source", str(self.source), "--map", str(self.map), expected=0)
        self.data = json.loads(self.map.read_text(encoding="utf8"))

    def run_gate(self, *args, expected):
        p = subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True)
        self.assertEqual(p.returncode, expected, (p.stdout, p.stderr))
        return p.stdout

    def save(self):
        self.map.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf8")

    def configure(self):
        self.data["candidate_commit"] = "a"*40
        self.data["deployed_commit"] = "a"*40
        for i, row in enumerate(self.data["items"]):
            row.update(disposition="required", req_id=f"REQ-{i+1}", slice="A", acceptance="observable behavior succeeds", status="VERIFIED", live_required=i == 0,
                       live_when="postdeploy" if i==0 else "preprod",
                       evidence=[dict(type="live" if i == 0 else "integration", ref="logs/run-1", observation="passed", candidate_commit="a"*40)])
        self.save()

    def test_missing_mapping_blocks_plan(self):
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=1)

    def test_full_lifecycle(self):
        self.configure()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=0)
        self.run_gate("check", "--map", str(self.map), "--stage", "review", "--slice", "A", "--candidate", "a"*40, expected=0)
        self.run_gate("check", "--map", str(self.map), "--stage", "release", "--candidate", "a"*40, "--deployed-commit", "a"*40, expected=0)

    def test_source_hash_drift_blocks(self):
        self.configure()
        self.source.write_text(self.source.read_text(encoding="utf8") + "- Another requirement\n", encoding="utf8")
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=1)

    def test_missing_production_evidence_blocks(self):
        self.configure()
        self.data["items"][0]["evidence"] = [dict(type="unit", ref="tests/test.py", observation="passed", candidate_commit="a"*40)]
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "release", "--candidate", "a"*40, "--deployed-commit", "a"*40, expected=1)

    def test_missing_candidate_proof_blocks(self):
        self.configure()
        self.data["items"][1]["evidence"][0]["candidate_commit"] = "b"*40
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "review", "--slice", "A", "--candidate", "a"*40, expected=1)

    def test_postdeploy_canary_allowed_before_release_only(self):
        self.configure()
        self.data["items"][0]["status"] = "READY_FOR_LIVE"
        self.data["items"][0]["evidence"] = [dict(type="integration", ref="tests/flow.py", observation="predeploy passed", candidate_commit="a"*40)]
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "review", "--slice", "A", "--candidate", "a"*40, expected=0)
        self.run_gate("check", "--map", str(self.map), "--stage", "release", "--candidate", "a"*40, "--deployed-commit", "a"*40, expected=1)

    def test_unapproved_exclusion_rejected(self):
        self.configure()
        self.data["items"][0].update(disposition="user_excluded", reason="not needed per user")
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=1)

if __name__ == "__main__":
    unittest.main()
