"""Tests of no-write reconciliation across real git branches and worktrees."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import workflow_lineage as wl


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.g("init", "-q", "-b", "main")
        self.g("config", "user.email", "test@example.test")
        self.g("config", "user.name", "Test")
        self.current_scope = {"schema": "openworkflow-progress-scope-v1", "tasks": [self.task("A", "PENDING")],
                              "owner_inputs": [self.inp("in1", "A")]}
        self.write_scope(self.repo, self.current_scope)
        self.write_map(self.repo, status="PENDING")
        self.g("add", ".")
        self.g("commit", "-qm", "old")
        self.old = self.g("rev-parse", "HEAD").strip()

    def g(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise AssertionError(f"git {args}: {result.stderr}")
        return result.stdout

    @staticmethod
    def task(ident, state):
        return {"id": ident, "status": state, "coverage_complete": True,
                "maps": ["plans/maps/a.json"]}

    @staticmethod
    def inp(ident, target):
        return {"id": ident, "kind": "new_task", "target_task_ids": [target],
                "reconciled": False}

    @staticmethod
    def write_scope(root, obj):
        path = root / "plans" / "progress_scope.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf8")

    @staticmethod
    def write_map(root, status="PENDING"):
        path = root / "plans/maps/a.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema":"openworkflow-coverage-v1", "items":[
            {"req_id":"R1", "status":status, "slice":"B", "disposition":"required"}]}), encoding="utf8")

    def test_missing_in_current_is_flagged_and_never_modified(self):
        old = self.old
        later = dict(self.current_scope, tasks=[self.task("A", "VERIFIED"),self.task("B", "PENDING")],
                     owner_inputs=[self.inp("in1", "A"),self.inp("in2", "B")])
        self.write_scope(self.repo, later)
        self.write_map(self.repo, "VERIFIED")
        self.g("add", "."); self.g("commit", "-qm", "release newer")
        ref = self.g("rev-parse", "HEAD").strip()
        self.g("checkout", "-q", old)
        raw = (self.repo / "plans/progress_scope.json").read_bytes()
        result = wl.lineage(self.repo, [ref], [])[0]
        self.assertEqual(result["only_reference_task_ids"], ["B"])
        self.assertEqual(result["only_reference_input_ids"], ["in2"])
        self.assertEqual(result["task_conflicts"][0]["id"], "A")
        self.assertTrue(result["map_conflicts"])
        self.assertTrue(result["needs_reconciliation"])
        self.assertEqual(raw, (self.repo / "plans/progress_scope.json").read_bytes())

    def test_new_task_in_current_is_not_automatically_a_defect(self):
        later = dict(self.current_scope, tasks=[self.task("A", "PENDING"),self.task("B", "PENDING")],
                     owner_inputs=[self.inp("in1", "A"),self.inp("in2", "B")])
        self.write_scope(self.repo, later)
        res = wl.lineage(self.repo, [self.old], [])[0]
        self.assertEqual(res["only_current_task_ids"], ["B"])
        self.assertTrue(res["informational_additions"])
        self.assertFalse(res["needs_reconciliation"])

    def test_changed_status_is_not_assumed_to_be_completed(self):
        changed = dict(self.current_scope, tasks=[self.task("A", "VERIFIED")])
        self.write_scope(self.repo, changed)
        res = wl.lineage(self.repo, [self.old], [])[0]
        self.assertTrue(res["needs_reconciliation"])
        self.assertEqual(res["task_conflicts"][0]["fields"], ["status"])
        self.assertEqual(res["task_conflicts"][0]["current_status"], "VERIFIED")

    def test_queue_position_priority_and_blockers_do_not_disappear_between_sessions(self):
        old_task = dict(self.task("A", "PENDING"), queue_position=5, priority="P0",
                        blocked_by=["PAY-ARCH"], blocker_sources={"PAY-ARCH":"plans/maps/a.json"})
        self.write_scope(self.repo, dict(self.current_scope, tasks=[old_task]))
        self.g("add", ".")
        self.g("commit", "-qm", "queued")
        queued = self.g("rev-parse", "HEAD").strip()
        newer = dict(old_task)
        newer.pop("queue_position")
        newer["priority"] = "P3"
        newer["blocked_by"] = []
        self.write_scope(self.repo, dict(self.current_scope, tasks=[newer]))
        result = wl.lineage(self.repo, [queued], [])[0]
        self.assertTrue(result["needs_reconciliation"])
        self.assertEqual(result["task_conflicts"][0]["fields"],
                         ["queue_position", "priority", "blocked_by"])

    def test_delivery_wait_metadata_survives_lineage_comparison(self):
        old_task = dict(self.task("A", "WAITING_EXTERNAL"), execution_state="delivered_awaiting_evidence",
                        deferred_req_ids=["R1"], delivery_gate_ref="plans/delivery.log")
        self.write_scope(self.repo, dict(self.current_scope, tasks=[old_task]))
        self.g("add", ".")
        self.g("commit", "-qm", "delivered")
        delivered = self.g("rev-parse", "HEAD").strip()
        newer = self.task("A", "WAITING_EXTERNAL")
        self.write_scope(self.repo, dict(self.current_scope, tasks=[newer]))
        result = wl.lineage(self.repo, [delivered], [])[0]
        self.assertEqual(result["task_conflicts"][0]["fields"],
                         ["execution_state", "deferred_req_ids", "delivery_gate_ref"])

    def test_worktree_detected_even_if_branch_not_checked_out(self):
        self.g("branch", "develop", self.old)
        wt = Path(self.tmp.name) / "other"
        self.g("worktree", "add", "-q", str(wt), "develop")
        self.write_scope(wt, dict(self.current_scope, tasks=[self.task("A", "PENDING"), self.task("B", "PENDING")]))
        res = wl.lineage(self.repo, [], [str(wt)])[0]
        self.assertEqual(res["only_reference_task_ids"], ["B"])
        self.assertTrue(res["needs_reconciliation"])
        self.assertTrue(res["reference"]["dirty_plans"])

    def test_unrelated_repo_is_refused(self):
        another = Path(self.tmp.name) / "unrelated"
        another.mkdir()
        subprocess.run(["git", "-C", str(another), "init", "-q"], check=True)
        with self.assertRaises(wl.LineageError):
            wl.lineage(self.repo, [], [str(another)])

    def test_prevents_map_path_leaking_outside_repo(self):
        changed = dict(self.current_scope, tasks=[dict(self.task("A", "PENDING"), maps=["../../private.json"])])
        self.write_scope(self.repo, changed)
        res = wl.lineage(self.repo, [self.old], [])[0]
        self.assertTrue(res["needs_reconciliation"])
        self.assertIn("вне репозитория", res["task_conflicts"][0]["error"])

    def test_progress_snapshot_is_uncertified_when_release_differs(self):
        import hashlib
        src = self.repo / "plans/task.md"
        src.write_text("original requirement", encoding="utf8")
        scope = {"schema":"openworkflow-progress-scope-v1", "tasks":[{
            "id":"A", "status":"PENDING", "coverage_complete":True,
            "source_path":"plans/task.md", "source_sha256":hashlib.sha256(src.read_bytes()).hexdigest(),
            "maps":["plans/maps/a.json"]}]}
        self.write_scope(self.repo, scope)
        self.g("add", "."); self.g("commit", "-qm", "baseline recorded")
        baseline = self.g("rev-parse", "HEAD").strip()
        scope["tasks"][0]["status"] = "VERIFIED"
        self.write_scope(self.repo, scope)
        self.write_map(self.repo, "VERIFIED")
        plain = subprocess.run([sys.executable, str(TOOLS / "workflow_progress.py"),
                                "--scope-file", str(self.repo / "plans/progress_scope.json"), "--json"],
                               capture_output=True, text=True)
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertTrue(json.loads(plain.stdout)["measurable"])
        checked = subprocess.run([sys.executable, str(TOOLS / "workflow_progress.py"),
                                  "--scope-file", str(self.repo / "plans/progress_scope.json"),
                                  "--compare-ref", baseline, "--json"],
                                 capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        data = json.loads(checked.stdout)
        self.assertTrue(data["lineage_checked"])
        self.assertFalse(data["measurable"])
        self.assertEqual(data["lineage"][0]["task_conflicts"][0]["id"], "A")

    def test_cli_exit_status_and_json(self):
        self.write_scope(self.repo, dict(self.current_scope, tasks=[self.task("A", "VERIFIED")]))
        p = subprocess.run([sys.executable, str(TOOLS / 'workflow_lineage.py'),
                            '--repo', str(self.repo), '--compare-ref', self.old, '--json'],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertTrue(json.loads(p.stdout)["needs_reconciliation"])


if __name__ == '__main__':
    unittest.main()
