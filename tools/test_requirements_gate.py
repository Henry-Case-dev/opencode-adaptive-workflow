"""Standard-library tests for requirements_gate. Run python -m unittest tools/test_requirements_gate.py."""
import json
import hashlib
import shutil
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

    def test_optional_owner_feedback_does_not_block_release_if_engineering_verified(self):
        self.configure()
        (self.path / "owner_live_checks.md").write_text("Owner made client testing voluntary", encoding="utf8")
        row = self.data["items"][1]
        row.update(verification_tier="owner_optional", live_required=False,
                   owner_policy_ref="owner_live_checks.md",
                   owner_optional_reason="Voluntary personal Telegram observation only",
                   engineering_req_ids=[self.data["items"][0]["req_id"]],
                   status="READY_FOR_LIVE", evidence=[])
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", "--repo", str(self.path), expected=0)
        self.run_gate("check", "--map", str(self.map), "--stage", "review", "--slice", "A", "--candidate", "a"*40,
                      "--repo", str(self.path), expected=0)
        self.run_gate("check", "--map", str(self.map), "--stage", "release", "--candidate", "a"*40,
                      "--deployed-commit", "a"*40, "--repo", str(self.path), expected=0)
        # The engineering row is still mandatory; do not mistake optional feedback for a waiver.
        self.data["items"][0]["status"] = "READY_FOR_LIVE"
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "release", "--candidate", "a"*40,
                      "--deployed-commit", "a"*40, "--repo", str(self.path), expected=1)

    def test_owner_optional_without_owner_policy_or_engineering_peer_is_rejected(self):
        self.configure()
        row = self.data["items"][1]
        row.update(verification_tier="owner_optional", live_required=False, status="READY_FOR_LIVE",
                   owner_optional_reason="Voluntary personal Telegram observation only",
                   engineering_req_ids=["GHOST"], owner_policy_ref="absent.md")
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", "--repo", str(self.path), expected=1)

    def test_failed_optional_observation_is_never_ignored(self):
        self.configure()
        (self.path / "owner_live_checks.md").write_text("Owner made client testing voluntary", encoding="utf8")
        self.data["items"][1].update(verification_tier="owner_optional", live_required=False,
                   owner_policy_ref="owner_live_checks.md",
                   owner_optional_reason="Voluntary personal Telegram observation only",
                   engineering_req_ids=[self.data["items"][0]["req_id"]], status="FAILED")
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "release", "--candidate", "a"*40,
                      "--deployed-commit", "a"*40, "--repo", str(self.path), expected=1)

    def test_unapproved_exclusion_rejected(self):
        self.configure()
        self.data["items"][0].update(disposition="user_excluded", reason="not needed per user")
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=1)


    def test_manual_source_hash_is_now_checked(self):
        self.configure()
        row = dict(self.data["items"][0])
        row.update(source_id="M00002", source_line=2,
                   source_excerpt="- Must fix real A flow",
                   source_text_sha256="0" * 64, req_id="MANUAL-1", slice="A")
        self.data["additional_requirements"] = [row]
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=1)
        row["source_text_sha256"] = hashlib.sha256(b"- Must fix real A flow").hexdigest()
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=0)

    def test_conflicting_duplicate_requirement_is_rejected(self):
        self.configure()
        self.data["items"][1]["req_id"] = self.data["items"][0]["req_id"]
        self.data["items"][1]["acceptance"] = "contradictory expected behavior"
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=1)

    def test_graph_rejects_cycles_and_unsafe_parallel_writers(self):
        self.configure()
        self.data["items"][1]["slice"] = "B"
        self.save()
        graph = self.path / "slices.json"
        nodes = [dict(id="A", depends_on=["B"], write_scope=["services/shared.py"], acceptance="observable A is complete"),
                 dict(id="B", depends_on=["A"], write_scope=["services/shared.py"], acceptance="observable B is complete")]
        graph.write_text(json.dumps(dict(schema="openworkflow-slices-v1", slices=nodes)))
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", "--graph", str(graph), expected=1)
        nodes[0]["depends_on"] = []
        nodes[1]["depends_on"] = []
        graph.write_text(json.dumps(dict(schema="openworkflow-slices-v1", slices=nodes)))
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", "--graph", str(graph), expected=1)
        nodes[1]["depends_on"] = ["A"]
        graph.write_text(json.dumps(dict(schema="openworkflow-slices-v1", slices=nodes)))
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", "--graph", str(graph), expected=0)

    def test_strict_evidence_checks_real_commit_and_artifact(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        self.configure()
        repo = self.path / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  text=True, capture_output=True).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Gate Test")
        (repo / "proof.txt").write_text("verified observation", encoding="utf8")
        git("add", "proof.txt")
        git("commit", "-qm", "proof")
        real_sha = git("rev-parse", "HEAD")
        # Even with a file present, the fake candidate cannot pass.
        for row in self.data["items"]:
            row["evidence"][0]["artifact_path"] = "proof.txt"
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "review", "--slice", "A", "--candidate", "a"*40,
                      "--repo", str(repo), "--strict-evidence", expected=1)
        # Real candidate, absent artifact: still blocked.
        self.data["candidate_commit"] = real_sha
        for row in self.data["items"]:
            row["evidence"][0].update(candidate_commit=real_sha, artifact_path="ghost.txt")
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "review", "--slice", "A", "--candidate", real_sha,
                      "--repo", str(repo), "--strict-evidence", expected=1)
        for row in self.data["items"]:
            row["evidence"][0]["artifact_path"] = "proof.txt"
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "review", "--slice", "A", "--candidate", real_sha,
                      "--repo", str(repo), "--strict-evidence", expected=0)



    def test_graph_declared_on_map_is_automatically_checked(self):
        self.configure()
        graph = self.path / "slice-graph.json"
        graph.write_text(json.dumps({"schema": "openworkflow-slices-v1", "slices": [
            {"id": "A", "depends_on": ["MISSING"], "write_scope": ["a.py"],
             "acceptance": "observable behavior A succeeds"}]}))
        self.data["slice_graph_path"] = "slice-graph.json"
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=1)
        doc = json.loads(graph.read_text())
        doc["slices"][0]["depends_on"] = []
        graph.write_text(json.dumps(doc))
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=0)

    def test_manual_shared_source_pointer_allowed(self):
        self.configure()
        line = "- Must fix real A flow"
        pair = [dict(self.data["items"][0], source_id="M00002", source_line=2,
                     source_text_sha256=hashlib.sha256(line.encode()).hexdigest(), source_excerpt=line,
                     req_id=f"MANUAL-{n}") for n in (1, 2)]
        self.data["additional_requirements"] = pair
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=0)
        pair[1]["source_line"] = 3
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=1)


if __name__ == "__main__":
    unittest.main()
