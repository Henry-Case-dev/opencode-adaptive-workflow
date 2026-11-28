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

    def approval(self, row, verdict="Approved", candidate="a"*40, wait=None):
        (self.path / "review.md").write_text("Independent review artifact", encoding="utf8")
        entry = dict(type="review", reviewer_role="Reviewer", verdict=verdict,
                     ref="review.md", artifact_path="review.md", observation="Independent acceptance",
                     candidate_commit=candidate)
        if wait is not None:
            entry["approved_postrelease_wait"] = wait
        row["evidence"].append(entry)

    def stage(self, name, expected, slice=None):
        args = ["check", "--map", str(self.map), "--stage", name,
                "--candidate", "a"*40, "--repo", str(self.path)]
        if slice:
            args += ["--slice", slice]
        if name in ("release", "delivery"):
            args += ["--deployed-commit", "a"*40]
        return self.run_gate(*args, expected=expected)

    def test_review_can_pass_before_deployment_but_not_close_release(self):
        self.configure()
        row = self.data["items"][1]
        row["status"] = "REVIEW"
        self.data["deployed_commit"] = ""
        self.save()
        self.stage("review", 1, "A")
        self.approval(row)
        self.approval(self.data["items"][0])
        self.save()
        self.stage("review", 0, "A")
        self.stage("deploy", 0)
        self.stage("release", 1)
        self.data["deployed_commit"] = "a"*40
        row["status"] = "VERIFIED"
        self.save()
        self.stage("release", 0)

    def test_review_rejects_unapproved_stale_or_prose_only(self):
        self.configure()
        row = self.data["items"][1]
        row["status"] = "REVIEW"
        self.approval(row, verdict="Needs Fixes")
        self.save()
        self.stage("review", 1, "A")
        row["evidence"][-1]["verdict"] = "Approved"
        row["evidence"][-1]["candidate_commit"] = "b"*40
        self.save()
        self.stage("review", 1, "A")
        row["evidence"][-1]["candidate_commit"] = "a"*40
        row["evidence"][-1]["artifact_path"] = "missing.md"
        self.save()
        self.stage("review", 1, "A")
        row["evidence"][-1]["artifact_path"] = "review.md"
        row["evidence"] = row["evidence"][-1:]
        self.save()
        self.stage("review", 1, "A")

    def test_review_still_requires_executable_preprod_live(self):
        self.configure()
        row = self.data["items"][1]
        row.update(status="REVIEW", live_required=True, live_when="preprod")
        self.approval(row)
        self.save()
        self.stage("review", 1, "A")
        row["evidence"][0]["type"] = "ui"
        self.save()
        self.stage("review", 0, "A")

    def test_delivery_parks_only_explicit_approved_future_evidence(self):
        self.configure()
        row = self.data["items"][0]
        row.update(status="READY_FOR_LIVE", evidence=[dict(type="integration", ref="test", observation="passed", candidate_commit="a"*40)])
        wait = dict(nonblocking=True, reason="natural event cannot be triggered safely", trigger="first natural event",
                    next_check="on event", owner="DevOps", contract_ref="REQ-1 frozen acceptance")
        row["postrelease_wait"] = wait
        self.approval(row, verdict="Approved for controlled deployment", wait=wait.copy())
        self.approval(self.data["items"][1])
        (self.path / "smoke.md").write_text("Deployed SHA and smoke", encoding="utf8")
        self.data["delivery_evidence"] = [dict(type="production", ref="smoke.md", artifact_path="smoke.md",
            observation="actual deployment and smoke passed", candidate_commit="a"*40, deployed_commit="a"*40)]
        self.save()
        self.stage("review", 0, "A")
        self.stage("deploy", 0)
        self.assertIn('deferred_req_ids=["REQ-1"]', self.stage("delivery", 0))
        self.stage("release", 1)
        # A different event/contract cannot be deferred using stale approval.
        row["postrelease_wait"]["trigger"] = "different event"
        self.save()
        self.stage("delivery", 1)
        row["postrelease_wait"]["trigger"] = "first natural event"
        self.data["delivery_evidence"][0]["deployed_commit"] = "b"*40
        self.save()
        self.stage("delivery", 1)
        self.data["delivery_evidence"][0]["deployed_commit"] = "a"*40
        row["status"] = "FAILED"
        self.save()
        self.stage("delivery", 1)
        row["status"] = "READY_FOR_LIVE"
        row["evidence"].append(dict(type="production", ref="event", observation="actual event observed", candidate_commit="a"*40))
        row["status"] = "VERIFIED"
        self.save()
        self.stage("release", 0)

    def test_delivery_does_not_waive_immediate_canary(self):
        self.configure()
        row = self.data["items"][0]
        row["status"] = "READY_FOR_LIVE"
        self.save()
        self.stage("delivery", 1)
        row["postrelease_wait"] = {"nonblocking": True}
        self.save()
        self.stage("delivery", 1)

    def test_missing_mapping_blocks_plan(self):
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=1)

    def test_full_lifecycle(self):
        self.configure()
        self.run_gate("check", "--map", str(self.map), "--stage", "plan", expected=0)
        self.run_gate("check", "--map", str(self.map), "--stage", "review", "--slice", "A", "--candidate", "a"*40, expected=0)
        self.run_gate("check", "--map", str(self.map), "--stage", "release", "--candidate", "a"*40, "--deployed-commit", "a"*40, expected=0)

    def _configure_incremental_slices(self):
        self.configure()
        self.assertGreaterEqual(len(self.data["items"]), 2)
        # A is a reviewed/releasable feature; B is unrelated new work.
        self.data["items"][-1]["slice"] = "B"
        self.data["items"][-1]["status"] = "PENDING"
        self.data["items"][-1]["evidence"] = []
        self.graph = self.path / "slice-graph.json"
        self.graph_data = {
            "schema": "openworkflow-slices-v1",
            "slices": [
                {"id": "A", "depends_on": [], "write_scope": ["a/**"],
                 "acceptance": "A independently works as specified"},
                {"id": "B", "depends_on": [], "write_scope": ["b/**"],
                 "acceptance": "B independently works as specified"},
            ],
        }
        self.data["slice_graph_path"] = "slice-graph.json"
        self.graph.write_text(json.dumps(self.graph_data), encoding="utf8")
        self.save()

    def test_scoped_release_does_not_wait_for_unrelated_open_slice(self):
        self._configure_incremental_slices()
        self.run_gate("check", "--map", str(self.map), "--stage", "release",
                      "--candidate", "a"*40, "--deployed-commit", "a"*40, expected=1)
        self.run_gate("check", "--map", str(self.map), "--stage", "release",
                      "--slice", "A", "--candidate", "a"*40,
                      "--deployed-commit", "a"*40, expected=0)
        self.run_gate("check", "--map", str(self.map), "--stage", "release",
                      "--slice", "B", "--candidate", "a"*40,
                      "--deployed-commit", "a"*40, expected=1)

    def test_scoped_release_requires_verified_dependency_and_graph(self):
        self._configure_incremental_slices()
        self.graph_data["slices"][0]["depends_on"] = ["B"]
        self.graph.write_text(json.dumps(self.graph_data), encoding="utf8")
        self.run_gate("check", "--map", str(self.map), "--stage", "release",
                      "--slice", "A", "--candidate", "a"*40,
                      "--deployed-commit", "a"*40, expected=1)
        self.data["items"][-1]["status"] = "VERIFIED"
        self.data["items"][-1]["evidence"] = [{
            "type": "integration", "ref": "historical/integration-result",
            "observation": "previously verified predecessor", "candidate_commit": "b"*40
        }]
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "release",
                      "--slice", "A", "--candidate", "a"*40,
                      "--deployed-commit", "a"*40, expected=0)
        self.data["slice_graph_path"] = ""
        self.save()
        self.run_gate("check", "--map", str(self.map), "--stage", "release",
                      "--slice", "A", "--candidate", "a"*40,
                      "--deployed-commit", "a"*40, expected=1)

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



    def test_metadata_reuse_preserves_proof_but_rejects_product_and_contract_change(self):
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
        (repo / "app.py").write_text("value = 1\n")
        git("add", ".")
        git("commit", "-qm", "code")
        old = git("rev-parse", "HEAD")
        proof = repo / "plans/features/x/evidence/review.md"
        proof.parent.mkdir(parents=True)
        proof.write_text("Independent recorded review and actual test result")
        digest = hashlib.sha256(proof.read_bytes()).hexdigest()
        for row in self.data["items"]:
            row.update(status="REVIEW", live_required=False, live_when="preprod")
            row["evidence"] = []
        self.save()
        for row in self.data["items"]:
            contract = self.run_gate("contract-digest", "--map", str(self.map), "--req-id", row["req_id"], expected=0).strip()
            base = dict(candidate_commit=old, ref="review", artifact_path=proof.relative_to(repo).as_posix(),
                        artifact_sha256=digest, contract_sha256=contract, reuse_on_metadata=True,
                        observation="checked original candidate")
            row["evidence"] = [dict(base, type="integration"), dict(base, type="review", reviewer_role="Reviewer", verdict="Approved")]
        git("add", ".")
        git("commit", "-qm", "evidence only")
        current = git("rev-parse", "HEAD")
        def check(candidate, expected):
            self.data["candidate_commit"] = candidate
            self.save()
            self.run_gate("check", "--map", str(self.map), "--stage", "review", "--slice", "A",
                          "--candidate", candidate, "--repo", str(repo), "--strict-evidence", expected=expected)
        check(current, 0)
        row = self.data["items"][0]
        row["acceptance"] = "A materially different observable requirement"
        check(current, 1)
        row["acceptance"] = "observable behavior succeeds"
        check(current, 0)
        proof.write_text("tampered proof")
        check(current, 1)
        proof.write_text("Independent recorded review and actual test result")
        (repo / "app.py").write_text("value = 2\n")
        git("add", "app.py")
        git("commit", "-qm", "code changed")
        check(git("rev-parse", "HEAD"), 1)
        # Production/live evidence cannot be carried by the offline exception.
        for r in self.data["items"]:
            r["evidence"][0]["type"] = "production"
        check(current, 1)

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

    def test_strict_delivery_receipt_end_to_end_and_stale_evidence(self):
        """Use actual Git and actual gate output, not a prearranged PASS string."""
        if not shutil.which("git"):
            self.skipTest("git not available")
        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location("workflow_progress", SCRIPT.with_name("workflow_progress.py"))
        progress = module_from_spec(spec); spec.loader.exec_module(progress)
        def git(*args):
            return subprocess.run(["git", "-C", str(self.path), *args], check=True,
                                  text=True, capture_output=True).stdout.strip()
        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Gate Test")
        (self.path / "product.py").write_text("def feature(): return 42\n", encoding="utf8")
        git("add", "product.py")
        git("commit", "-qm", "candidate")
        candidate = git("rev-parse", "HEAD")
        self.configure()
        self.data.update(candidate_commit=candidate, deployed_commit=candidate)
        for name in ("test.md", "review.md", "smoke.md"):
            (self.path / name).write_text("Synthetic test fixture: " + name, encoding="utf8")
        for row in self.data["items"]:
            row["evidence"][0].update(candidate_commit=candidate, artifact_path="test.md")
            self.approval(row, candidate=candidate)
        wait = dict(nonblocking=True, reason="requires an external event", trigger="event", next_check="on event", owner="DevOps", contract_ref="REQ-1")
        first = self.data["items"][0]
        first.update(status="READY_FOR_LIVE", postrelease_wait=wait)
        first["evidence"][0]["type"] = "integration"
        first["evidence"][-1]["approved_postrelease_wait"] = wait.copy()
        self.data["delivery_evidence"] = [dict(type="production", ref="smoke.md", artifact_path="smoke.md", observation="fixture deployment evidence", candidate_commit=candidate, deployed_commit=candidate)]
        self.save()
        args = ("check", "--map", str(self.map), "--stage", "delivery", "--candidate", candidate, "--deployed-commit", candidate, "--repo", str(self.path), "--strict-evidence")
        output = self.run_gate(*args, expected=0)
        self.assertIn("delivery_receipt_v1=", output)
        receipt = self.path / "delivery.log"
        receipt.write_text(output, encoding="utf8")
        task = {"id":"T1", "status":"WAITING_EXTERNAL", "execution_state":"delivered_awaiting_evidence", "deferred_req_ids":["REQ-1"], "delivery_gate_ref":"delivery.log", "source_path":str(self.source), "source_sha256":self.data["source_sha256"], "maps":[str(self.map)], "coverage_complete":True, "queue_position":1, "priority":"P0", "blocked_by":[]}
        scope = self.path / "progress_scope.json"
        def snapshot():
            scope.write_text(json.dumps({"schema":progress.SCOPE_SCHEMA,"tasks":[task]}),encoding="utf8")
            return progress.legacy_snapshot([], "project", str(scope))
        result = snapshot()
        self.assertEqual(result["engineering_delivery"]["done"], 1)
        self.assertEqual(result["tasks"]["done"], 0)
        self.assertEqual(result["engineering_queue"]["task_ids"], [])
        self.assertEqual(result["engineering_delivery"]["external_evidence_task_ids"], ["T1"])
        # A stale or edited artifact must not continue certifying delivery.
        smoke = self.path / "smoke.md"
        original = smoke.read_bytes()
        smoke.write_text("Changed evidence",encoding="utf8")
        result = snapshot()
        self.assertEqual(result["engineering_delivery"]["done"], 0)
        self.assertEqual(result["engineering_delivery"]["reconcile_task_ids"], ["T1"])
        self.assertEqual(result["engineering_queue"]["task_ids"], ["T1"])
        smoke.write_bytes(original)
        # Old plain PASS or fabricated closure timestamps are not receipts.
        receipt.write_text('engineering_delivery=PASS\nacceptance=PENDING_FUTURE_EVIDENCE\ndeferred_req_ids=["REQ-1"]\n',encoding="utf8")
        task["engineering_closed_at"] = "2026-11-27"
        self.assertEqual(snapshot()["engineering_delivery"]["done"],0)
        receipt.write_text(output,encoding="utf8")
        # A second map cannot be silently omitted from a whole-task receipt.
        other = self.path / "second.json"
        other.write_bytes(self.map.read_bytes()); task["maps"].append(str(other))
        self.assertEqual(snapshot()["engineering_delivery"]["done"],0)
        task["maps"].pop()
        # Material map changes invalidate prior receipt and failed requirements cannot be parked.
        first["status"] = "FAILED"; self.save()
        self.assertEqual(snapshot()["engineering_delivery"]["done"],0)
        self.assertNotIn("delivery_receipt_v1=", self.run_gate(*args,expected=1))

    def test_non_deployment_receipt_requires_strict_review_and_no_live_claim(self):
        if not shutil.which("git"):
            self.skipTest("git not available")
        from importlib.util import spec_from_file_location, module_from_spec
        spec = spec_from_file_location("workflow_progress", SCRIPT.with_name("workflow_progress.py"))
        progress = module_from_spec(spec); spec.loader.exec_module(progress)
        def git(*args):
            return subprocess.run(["git","-C",str(self.path),*args],check=True,text=True,capture_output=True).stdout.strip()
        git("init","-q");git("config","user.email","test@example.invalid");git("config","user.name","Gate Test")
        git("add","task.md");git("commit","-qm","candidate");candidate=git("rev-parse","HEAD")
        self.configure(); self.data.update(candidate_commit=candidate,deployed_commit="",delivery_kind="non_deployment")
        (self.path/"proof.md").write_text("Synthetic fixture: actual output checked",encoding="utf8")
        for row in self.data["items"]:
            row.update(live_required=False,live_when="preprod")
            row["evidence"][0].update(type="inspection",candidate_commit=candidate,artifact_path="proof.md")
            self.approval(row,candidate=candidate)
        self.save()
        args=("check","--stage","delivery","--map",str(self.map),"--candidate",candidate,"--repo",str(self.path),"--non-deployment","--strict-evidence")
        output=self.run_gate(*args,expected=0)
        (self.path/"delivery.log").write_text(output,encoding="utf8")
        task=dict(id="T1",status="VERIFIED",maps=[str(self.map)],coverage_complete=True,delivery_gate_ref="delivery.log")
        result=progress.audit_delivery_receipts([task],self.path,progress.SnapshotReads(),[])
        self.assertEqual(result["certified_task_ids"],["T1"])
        self.data["items"][0]["live_required"]=True;self.save()
        self.assertNotIn("delivery_receipt_v1=",self.run_gate(*args,expected=1))
        self.data["items"][0]["live_required"]=False;self.data["items"][0]["evidence"].pop();self.save()
        self.assertNotIn("delivery_receipt_v1=",self.run_gate(*args,expected=1))


if __name__ == "__main__":
    unittest.main()
