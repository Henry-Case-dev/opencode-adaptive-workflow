"""Run: python -m unittest tools/test_workflow_progress.py"""
from pathlib import Path
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import importlib.util

module_file = Path(__file__).with_name("workflow_progress.py")
spec = importlib.util.spec_from_file_location("workflow_progress", module_file)
progress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(progress)


class WorkflowProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_map(self, name, requirements):
        p = self.root / name
        rows = [dict(disposition="required", req_id=rid, slice=lane, status=state)
                for rid, lane, state in requirements]
        p.write_text(json.dumps({"schema": progress.SCHEMA, "items": rows,
                                 "additional_requirements": [{"disposition": "context"}]}), encoding="utf-8")
        return str(p)

    def source(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return str(path), hashlib.sha256(path.read_bytes()).hexdigest()

    def write_scope(self, rows):
        path = self.root / "progress_scope.json"
        path.write_text(json.dumps({"schema": progress.SCOPE_SCHEMA, "tasks": rows}, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_verified_task_cannot_hide_unverified_mapped_requirements(self):
        m = self.write_map("a.json", [("R1", "W1", "READY_FOR_LIVE")])
        src, fp = self.source("prompt.md", "готовая задача")
        scope = self.write_scope([{ "id":"T1", "status":"VERIFIED", "source_path":src,
                                   "source_sha256":fp, "maps":[m], "coverage_complete":True }])
        snap = progress.snapshot([], "весь проект", scope)
        self.assertFalse(snap["measurable"])
        self.assertIn("неподтверждённые", snap["stale_sources"][0])

    def test_new_prompt_counts_immediately_without_fake_requirements_denominator(self):
        a = self.write_map("a.json", [("R1", "S1", "VERIFIED")])
        src1, hash1 = self.source("initial.md", "первая задача")
        src2, hash2 = self.source("new.md", "новая задача")
        tasks = [
            {"id": "initial", "status": "VERIFIED", "source_path": src1, "source_sha256": hash1,
             "maps": [a], "coverage_complete": True},
            {"id": "new", "status": "PENDING", "source_path": src2, "source_sha256": hash2,
             "maps": [], "coverage_complete": False},
        ]
        snap = progress.snapshot([], "весь проект", self.write_scope(tasks))
        self.assertEqual(snap["tasks"], {"done": 1, "total": 2})
        self.assertFalse(snap["requirements_measurable"])
        output = progress.present(snap)
        self.assertIn("1/2 (50%)", output)
        self.assertNotIn("1/1 (100%)", output)
        self.assertIn("Общий процент по требованиям: не рассчитан", output)

    def test_same_slice_labels_in_different_tasks_are_not_merged(self):
        a = self.write_map("a.json", [("R1", "W1", "VERIFIED")])
        b = self.write_map("b.json", [("R2", "W1", "PENDING")])
        x, fx = self.source("x.md", "первое")
        y, fy = self.source("y.md", "второе")
        scope = self.write_scope([
            {"id": "A", "status": "VERIFIED", "source_path": x, "source_sha256": fx, "maps": [a], "coverage_complete": True},
            {"id": "B", "status": "PENDING", "source_path": y, "source_sha256": fy, "maps": [b], "coverage_complete": True},
        ])
        snap = progress.snapshot([], "проект", scope)
        self.assertEqual(snap["slices"], {"done": 1, "total": 2})

    def test_after_mapping_new_requirements_denominator_increases(self):
        a = self.write_map("a.json", [("R1", "S1", "VERIFIED")])
        b = self.write_map("b.json", [("R2", "S2", "PENDING"), ("R3", "S2", "PENDING")])
        src1, hash1 = self.source("initial.md", "первая задача")
        src2, hash2 = self.source("new.md", "новая задача")
        tasks = [
            {"id": "initial", "status": "VERIFIED", "source_path": src1, "source_sha256": hash1,
             "maps": [a], "coverage_complete": True},
            {"id": "new", "status": "PENDING", "source_path": src2, "source_sha256": hash2,
             "maps": [b], "coverage_complete": True},
        ]
        snap = progress.snapshot([], "весь проект", self.write_scope(tasks))
        self.assertTrue(snap["requirements_measurable"])
        self.assertEqual(snap["requirements"], {"done": 1, "total": 3})
        self.assertIn("1/3 (33%)", progress.present(snap))
        self.assertIn("1/2 (50%)", progress.present(snap))

    def test_changed_user_owned_source_suppresses_stale_percentage(self):
        a = self.write_map("a.json", [("R1", "S1", "VERIFIED")])
        src, fingerprint = self.source("current_task.md", "старая задача")
        scope = self.write_scope([{"id": "T1", "status": "VERIFIED", "source_path": src,
                                  "source_sha256": fingerprint, "maps": [a], "coverage_complete": True}])
        Path(src).write_text("старая задача\nновое требование", encoding="utf-8")
        snap = progress.snapshot([], "весь проект", scope)
        self.assertFalse(snap["measurable"])
        self.assertIn("исходное задание изменилось", progress.present(snap))
        self.assertNotIn("100%", progress.present(snap))

    def test_missing_source_fingerprint_is_stale(self):
        src, _ = self.source("prompt.md", "задача")
        scope = self.write_scope([{"id": "T1", "status": "PENDING", "source_path": src,
                                  "maps": [], "coverage_complete": False}])
        self.assertFalse(progress.snapshot([], "задача", scope)["measurable"])

    def test_scope_cli_json(self):
        src, fingerprint = self.source("prompt.md", "проверить")
        scope = self.write_scope([{"id": "T1", "status": "PENDING", "source_path": src,
                                  "source_sha256": fingerprint, "maps": [], "coverage_complete": False}])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(progress.main(["--scope-file", scope, "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["tasks"]["total"], 1)

    def test_unique_requirements_and_slice_all_verified(self):
        m = self.write_map("main.json", [("R1", "W1", "VERIFIED"), ("R2", "W1", "READY_FOR_LIVE"),
                                          ("R3", "W2", "VERIFIED"), ("R4", "W3", "FAILED")])
        snap = progress.snapshot([m], "эпик")
        self.assertEqual(snap["requirements"], {"done": 2, "total": 4})
        self.assertEqual(snap["slices"], {"done": 1, "total": 3})
        self.assertIn("1/3 (33%)", progress.present(snap))
        self.assertIn("2/4 (50%)", progress.present(snap))

    def test_two_maps_count_same_slice_once(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED"), ("M2", "W2", "VERIFIED")])
        b = self.write_map("amend.json", [("A1", "W1", "PENDING"), ("A2", "W3", "VERIFIED")])
        snap = progress.snapshot([a, b, a], "task")
        self.assertEqual(snap["requirements"], {"done": 3, "total": 4})
        self.assertEqual(snap["slices"], {"done": 2, "total": 3})
        self.assertEqual(len(snap["maps"]), 2)

    def test_duplicate_req_id_not_doubled(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED"), ("M1", "W1", "VERIFIED")])
        snap = progress.snapshot([a], "task")
        self.assertEqual(snap["requirements"]["total"], 1)
        self.assertEqual(snap["duplicates_collapsed"], 1)

    def test_contradictory_duplicate_is_error(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED"), ("M1", "W1", "PENDING")])
        with self.assertRaisesRegex(ValueError, "два статуса"):
            progress.snapshot([a], "task")

    def test_missing_map_returns_error_not_fake_zero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(progress.main(["--map", str(self.root / "missing.json")]), 2)

    def test_no_map_discloses_unknown_scope(self):
        snap = progress.snapshot([], "эпик")
        self.assertFalse(snap["measurable"])
        self.assertIn("не рассчитан", progress.present(snap))
        self.assertNotIn("0/0", progress.present(snap))

    def test_excluded_context_not_in_denominator(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED")])
        self.assertEqual(progress.snapshot([a], "epic")["requirements"]["total"], 1)

    def test_empty_required_is_unknown_not_100(self):
        a = self.write_map("main.json", [])
        with self.assertRaisesRegex(ValueError, "нет обязательных"):
            progress.snapshot([a], "epic")

    def test_unknown_status_rejected(self):
        a = self.write_map("main.json", [("M1", "W1", "PLANNED")])
        with self.assertRaisesRegex(ValueError, "неизвестный статус"):
            progress.snapshot([a], "epic")

    def test_owner_optional_is_separate_from_engineering_progress(self):
        policy = self.root / "owner_live_checks.md"
        policy.write_text("Personal checks optional", encoding="utf8")
        p = self.root / "map.json"
        data = {"schema": progress.SCHEMA, "items": [
            {"disposition":"required", "req_id":"R1", "slice":"S1", "status":"VERIFIED"},
            {"disposition":"required", "req_id":"O1", "slice":"S1", "status":"READY_FOR_LIVE",
             "live_required":False, "verification_tier":"owner_optional",
             "owner_policy_ref": str(policy), "owner_optional_reason":"Voluntary owner subjective client feedback",
             "engineering_req_ids":["R1"]}]}
        p.write_text(json.dumps(data), encoding="utf8")
        snap = progress.snapshot([str(p)], "проект")
        self.assertEqual(snap["requirements"], {"done":1,"total":1})
        self.assertEqual(snap["slices"], {"done":1,"total":1})
        self.assertEqual(snap["owner_optional_checks"], {"total":1,"pending":1,"verified":0})
        self.assertIn("100%", progress.present(snap))
        self.assertIn("Необязательная", progress.present(snap))
        src, fp = self.source("task.md", "Task")
        scope = self.write_scope([{"id":"T1", "status":"VERIFIED", "source_path":src,
                                   "source_sha256":fp, "maps":[str(p)], "coverage_complete":True}])
        self.assertTrue(progress.snapshot([], "проект", scope)["measurable"])

    def test_owner_optional_cannot_hide_failure_or_absent_peer(self):
        p = self.root / "policy.md"
        p.write_text("Personal checks optional", encoding="utf8")
        m = self.root / "map.json"
        data = {"schema": progress.SCHEMA, "items": [
            {"disposition":"required", "req_id":"O1", "slice":"S1", "status":"READY_FOR_LIVE",
             "live_required":False, "verification_tier":"owner_optional", "owner_policy_ref":str(p),
             "owner_optional_reason":"Voluntary owner subjective client feedback", "engineering_req_ids":["GHOST"]},
            {"disposition":"required", "req_id":"R1", "slice":"S1", "status":"VERIFIED"}]}
        m.write_text(json.dumps(data), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "без инженерной пары"):
            progress.snapshot([str(m)], "p")
        data["items"][0]["engineering_req_ids"] = ["R1"]
        data["items"][0]["status"] = "FAILED"
        m.write_text(json.dumps(data), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "проблема в необязательной проверке"):
            progress.snapshot([str(m)], "p")

    def test_full_verified_is_100_percent(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED"), ("M2", "W2", "VERIFIED")])
        lines = progress.present(progress.snapshot([a], "task"))
        self.assertIn("2/2 (100%) [####################]", lines)

    def test_json_cli(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(progress.main(["--map", a, "--json", "--label", "SOUL"]), 0)
        self.assertEqual(json.loads(out.getvalue())["scope"], "SOUL")


    def scope_with_inputs(self, tasks, inputs):
        file = self.root / "scope-with-inputs.json"
        file.write_text(json.dumps({"schema": progress.SCOPE_SCHEMA, "tasks": tasks,
                                    "owner_inputs": inputs}, ensure_ascii=False), encoding="utf-8")
        return str(file)

    def test_chat_correction_pending_does_not_silently_report_old_percentage(self):
        m = self.write_map("map.json", [("R1", "S", "VERIFIED")])
        initial, h = self.source("initial.md", "old task")
        change, hc = self.source("correction.md", "and please fix aliases")
        tasks = [{"id":"T", "status":"VERIFIED", "source_path":initial, "source_sha256":h,
                  "maps":[m], "coverage_complete":True}]
        inp = [{"id":"in2", "kind":"correction", "source_path":change,
                "source_sha256":hc, "target_task_ids":["T"], "reconciled":False}]
        snap = progress.snapshot([], "project", self.scope_with_inputs(tasks, inp))
        self.assertFalse(snap["measurable"])
        self.assertEqual(snap["tasks"], {"done":1,"total":1})
        self.assertIn("in2", progress.present(snap))

    def test_reconciled_short_amendment_does_not_create_second_task(self):
        m = self.write_map("map.json", [("R1", "S", "VERIFIED"),("R2", "S", "PENDING")])
        initial, h = self.source("initial.md", "original task")
        amend, ha = self.source("amend.md", "and check replies")
        tasks = [{"id":"T", "status":"BUILDING", "source_path":initial, "source_sha256":h,
                  "maps":[m], "coverage_complete":True}]
        inp = [{"id":"in2", "kind":"amendment", "source_path":amend,
                "source_sha256":ha, "target_task_ids":["T"], "reconciled":True}]
        snap = progress.snapshot([], "project", self.scope_with_inputs(tasks, inp))
        self.assertTrue(snap["measurable"])
        self.assertEqual(snap["tasks"]["total"], 1)
        self.assertEqual(snap["requirements"]["total"], 2)

    def test_unlinked_input_must_be_reconciled_before_progress(self):
        m = self.write_map("map.json", [("R1", "S", "VERIFIED")])
        original, h = self.source("original.md", "original")
        new, hn = self.source("new.md", "new independent project")
        tasks = [{"id":"T", "status":"VERIFIED", "source_path":original, "source_sha256":h,
                  "maps":[m], "coverage_complete":True}]
        inp = [{"id":"incoming", "kind":"new_task", "source_path":new,
                "source_sha256":hn, "target_task_ids":[], "reconciled":False}]
        snap = progress.snapshot([], "project", self.scope_with_inputs(tasks, inp))
        self.assertFalse(snap["measurable"])
        inp[0]["reconciled"] = True
        snap = progress.snapshot([], "project", self.scope_with_inputs(tasks, inp))
        self.assertFalse(snap["measurable"])
        self.assertIn("не связано с задачей", snap["stale_sources"][0])

    def test_missing_snapshot_and_duplicate_source_ids_are_rejected(self):
        original, h = self.source("task.md", "task")
        task = {"id":"T", "status":"PENDING", "source_path":original,
                "source_sha256":h, "maps":[], "coverage_complete":False}
        inp = {"id":"x", "kind":"correction", "source_path":str(self.root / "gone.md"),
               "source_sha256":h,"target_task_ids":["T"], "reconciled":False}
        snap = progress.snapshot([], "project", self.scope_with_inputs([task],[inp]))
        self.assertFalse(snap["measurable"])
        self.assertIn("потерян", snap["stale_sources"][0])
        with self.assertRaisesRegex(ValueError, "повторный ID входящего"):
            progress.snapshot([], "project", self.scope_with_inputs([task],[inp,inp]))

    def test_cancelled_task_excluded_from_active_denominator_only_with_owner_source(self):
        original,h = self.source("task.md", "initial")
        cancel,hc = self.source("cancel.md", "cancel second task")
        m = self.write_map("map.json",[("R1","S","VERIFIED")])
        a = {"id":"A", "status":"VERIFIED", "source_path":original,
             "source_sha256":h,"maps":[m],"coverage_complete":True}
        b = {"id":"B", "status":"CANCELLED", "source_path":original,
             "source_sha256":h,"maps":[],"coverage_complete":False,
             "cancelled_by_input":"cancel1"}
        inp = {"id":"cancel1", "kind":"cancellation", "source_path":cancel,
               "source_sha256":hc,"target_task_ids":["B"],"reconciled":True}
        snap = progress.snapshot([], "project", self.scope_with_inputs([a,b],[inp]))
        self.assertTrue(snap["measurable"])
        self.assertEqual(snap["tasks"], {"done":1,"total":1})
        b.pop("cancelled_by_input")
        snap = progress.snapshot([], "project", self.scope_with_inputs([a,b],[inp]))
        self.assertFalse(snap["measurable"])

    def test_saved_input_without_index_entry_is_detected(self):
        src, h = self.source("task.md", "initial")
        self.write_map("map.json", [("R1", "S", "VERIFIED")])
        inbox = self.root / "owner_inputs"
        inbox.mkdir()
        (inbox / "orphan.md").write_text("important correction", encoding="utf-8")
        scope = self.write_scope([{"id":"T", "status":"PENDING", "source_path":src,
                                  "source_sha256":h, "maps":[], "coverage_complete":False}])
        snap = progress.snapshot([], "project", scope)
        self.assertFalse(snap["measurable"])
        self.assertIn("неучтённое входящее", snap["stale_sources"][0])

    def test_legacy_scope_without_owner_inputs_remains_supported(self):
        m = self.write_map("map.json",[("R1","S","VERIFIED")])
        src,h = self.source("a.md","hello")
        scope = self.write_scope([{"id":"T","status":"VERIFIED","source_path":src,
                                  "source_sha256":h,"maps":[m],"coverage_complete":True}])
        snap = progress.snapshot([],"project",scope)
        self.assertTrue(snap["measurable"])
        self.assertEqual(snap["owner_inputs"]["registered"],0)



if __name__ == "__main__":
    unittest.main()
