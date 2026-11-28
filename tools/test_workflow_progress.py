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
        snap = progress.legacy_snapshot([], "весь проект", scope)
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
        snap = progress.legacy_snapshot([], "весь проект", self.write_scope(tasks))
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
        snap = progress.legacy_snapshot([], "проект", scope)
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
        snap = progress.legacy_snapshot([], "весь проект", self.write_scope(tasks))
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
        snap = progress.legacy_snapshot([], "весь проект", scope)
        self.assertFalse(snap["measurable"])
        self.assertIn("исходное задание изменилось", progress.present(snap))
        self.assertNotIn("100%", progress.present(snap))

    def test_missing_source_fingerprint_is_stale(self):
        src, _ = self.source("prompt.md", "задача")
        scope = self.write_scope([{"id": "T1", "status": "PENDING", "source_path": src,
                                  "maps": [], "coverage_complete": False}])
        self.assertFalse(progress.legacy_snapshot([], "задача", scope)["measurable"])

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
        snap = progress.legacy_snapshot([m], "эпик")
        self.assertEqual(snap["requirements"], {"done": 2, "total": 4})
        self.assertEqual(snap["slices"], {"done": 1, "total": 3})
        self.assertIn("1/3 (33%)", progress.present(snap))
        self.assertIn("2/4 (50%)", progress.present(snap))

    def test_two_maps_count_same_slice_once(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED"), ("M2", "W2", "VERIFIED")])
        b = self.write_map("amend.json", [("A1", "W1", "PENDING"), ("A2", "W3", "VERIFIED")])
        snap = progress.legacy_snapshot([a, b, a], "task")
        self.assertEqual(snap["requirements"], {"done": 3, "total": 4})
        self.assertEqual(snap["slices"], {"done": 2, "total": 3})
        self.assertEqual(len(snap["maps"]), 2)

    def test_duplicate_req_id_not_doubled(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED"), ("M1", "W1", "VERIFIED")])
        snap = progress.legacy_snapshot([a], "task")
        self.assertEqual(snap["requirements"]["total"], 1)
        self.assertEqual(snap["duplicates_collapsed"], 1)

    def test_contradictory_duplicate_is_error(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED"), ("M1", "W1", "PENDING")])
        with self.assertRaisesRegex(ValueError, "два статуса"):
            progress.legacy_snapshot([a], "task")

    def test_missing_map_returns_error_not_fake_zero(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(progress.main(["--map", str(self.root / "missing.json")]), 2)

    def test_no_map_discloses_unknown_scope(self):
        snap = progress.legacy_snapshot([], "эпик")
        self.assertFalse(snap["measurable"])
        self.assertIn("не рассчитан", progress.present(snap))
        self.assertNotIn("0/0", progress.present(snap))

    def test_excluded_context_not_in_denominator(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED")])
        self.assertEqual(progress.legacy_snapshot([a], "epic")["requirements"]["total"], 1)

    def test_empty_required_is_unknown_not_100(self):
        a = self.write_map("main.json", [])
        with self.assertRaisesRegex(ValueError, "нет обязательных"):
            progress.legacy_snapshot([a], "epic")

    def test_unknown_status_rejected(self):
        a = self.write_map("main.json", [("M1", "W1", "PLANNED")])
        with self.assertRaisesRegex(ValueError, "неизвестный статус"):
            progress.legacy_snapshot([a], "epic")

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
        snap = progress.legacy_snapshot([str(p)], "проект")
        self.assertEqual(snap["requirements"], {"done":1,"total":1})
        self.assertEqual(snap["slices"], {"done":1,"total":1})
        self.assertEqual(snap["owner_optional_checks"], {"total":1,"pending":1,"verified":0})
        self.assertIn("100%", progress.present(snap))
        self.assertIn("Необязательная", progress.present(snap))
        src, fp = self.source("task.md", "Task")
        scope = self.write_scope([{"id":"T1", "status":"VERIFIED", "source_path":src,
                                   "source_sha256":fp, "maps":[str(p)], "coverage_complete":True}])
        self.assertTrue(progress.legacy_snapshot([], "проект", scope)["measurable"])

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
            progress.legacy_snapshot([str(m)], "p")
        data["items"][0]["engineering_req_ids"] = ["R1"]
        data["items"][0]["status"] = "FAILED"
        m.write_text(json.dumps(data), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "проблема в необязательной проверке"):
            progress.legacy_snapshot([str(m)], "p")

    def test_full_verified_is_100_percent(self):
        a = self.write_map("main.json", [("M1", "W1", "VERIFIED"), ("M2", "W2", "VERIFIED")])
        lines = progress.present(progress.legacy_snapshot([a], "task"))
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
        snap = progress.legacy_snapshot([], "project", self.scope_with_inputs(tasks, inp))
        self.assertFalse(snap["measurable"])
        self.assertEqual(snap["tasks"], {"done":1,"total":1})
        self.assertIn("in2", progress.present(snap))
        self.assertFalse(snap["requirements_measurable"])
        self.assertNotIn("requirements", snap)
        self.assertEqual(snap["registered_scope_progress"]["requirements"], {"done":1,"total":1})
        self.assertEqual(snap["registered_scope_progress"]["slices"], {"done":1,"total":1})
        # Source drift invalidates even the partial display.
        Path(initial).write_text("changed", encoding="utf-8")
        drift = progress.legacy_snapshot([], "project", self.scope_with_inputs(tasks, inp))
        self.assertFalse(drift["measurable"])
        self.assertNotIn("registered_scope_progress", drift)

    def test_reconciled_short_amendment_does_not_create_second_task(self):
        m = self.write_map("map.json", [("R1", "S", "VERIFIED"),("R2", "S", "PENDING")])
        initial, h = self.source("initial.md", "original task")
        amend, ha = self.source("amend.md", "and check replies")
        tasks = [{"id":"T", "status":"BUILDING", "source_path":initial, "source_sha256":h,
                  "maps":[m], "coverage_complete":True}]
        inp = [{"id":"in2", "kind":"amendment", "source_path":amend,
                "source_sha256":ha, "target_task_ids":["T"], "reconciled":True,
                "coverage_refs":[{"task_id":"T", "map":m, "req_ids":["R2"]}]}]
        snap = progress.legacy_snapshot([], "project", self.scope_with_inputs(tasks, inp))
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
        snap = progress.legacy_snapshot([], "project", self.scope_with_inputs(tasks, inp))
        self.assertFalse(snap["measurable"])
        inp[0]["reconciled"] = True
        snap = progress.legacy_snapshot([], "project", self.scope_with_inputs(tasks, inp))
        self.assertFalse(snap["measurable"])
        self.assertIn("не связано с задачей", snap["stale_sources"][0])

    def test_missing_snapshot_and_duplicate_source_ids_are_rejected(self):
        original, h = self.source("task.md", "task")
        task = {"id":"T", "status":"PENDING", "source_path":original,
                "source_sha256":h, "maps":[], "coverage_complete":False}
        inp = {"id":"x", "kind":"correction", "source_path":str(self.root / "gone.md"),
               "source_sha256":h,"target_task_ids":["T"], "reconciled":False}
        snap = progress.legacy_snapshot([], "project", self.scope_with_inputs([task],[inp]))
        self.assertFalse(snap["measurable"])
        self.assertIn("потерян", snap["stale_sources"][0])
        with self.assertRaisesRegex(ValueError, "повторный ID входящего"):
            progress.legacy_snapshot([], "project", self.scope_with_inputs([task],[inp,inp]))

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
        snap = progress.legacy_snapshot([], "project", self.scope_with_inputs([a,b],[inp]))
        self.assertTrue(snap["measurable"])
        self.assertEqual(snap["tasks"], {"done":1,"total":1})
        b.pop("cancelled_by_input")
        snap = progress.legacy_snapshot([], "project", self.scope_with_inputs([a,b],[inp]))
        self.assertFalse(snap["measurable"])

    def test_saved_input_without_index_entry_is_detected(self):
        src, h = self.source("task.md", "initial")
        self.write_map("map.json", [("R1", "S", "VERIFIED")])
        inbox = self.root / "owner_inputs"
        inbox.mkdir()
        (inbox / "orphan.md").write_text("important correction", encoding="utf-8")
        scope = self.write_scope([{"id":"T", "status":"PENDING", "source_path":src,
                                  "source_sha256":h, "maps":[], "coverage_complete":False}])
        snap = progress.legacy_snapshot([], "project", scope)
        self.assertFalse(snap["measurable"])
        self.assertIn("неучтённое входящее", snap["stale_sources"][0])

    def test_legacy_scope_without_owner_inputs_remains_supported(self):
        m = self.write_map("map.json",[("R1","S","VERIFIED")])
        src,h = self.source("a.md","hello")
        scope = self.write_scope([{"id":"T","status":"VERIFIED","source_path":src,
                                  "source_sha256":h,"maps":[m],"coverage_complete":True}])
        snap = progress.legacy_snapshot([],"project",scope)
        self.assertTrue(snap["measurable"])
        self.assertEqual(snap["owner_inputs"]["registered"],0)



    def test_current_task_sections_not_in_registry_block_global_percentage(self):
        folder = self.root / "plans"
        folder.mkdir()
        (folder / "current_task.md").write_text("# Epic A\nFix routing\n# Epic B\nFix replies\n", encoding="utf-8")
        m = self.write_map("map.json", [("R1", "S", "VERIFIED")])
        src, sha = self.source("task.md", "some earlier plan")
        scope = folder / "progress_scope.json"
        scope.write_text(json.dumps({"schema": progress.SCOPE_SCHEMA, "tasks":[{
            "id": "T", "status": "VERIFIED", "source_path":src,"source_sha256":sha,
            "maps": [m], "coverage_complete": True
        }]}), encoding="utf-8")
        snap = progress.legacy_snapshot([], "global", str(scope))
        self.assertFalse(snap["measurable"])
        self.assertIn("source_inventory", " ".join(snap["stale_sources"]))
        template = progress.inventory_template(folder / "current_task.md", self.root)
        self.assertEqual(len(template["sections"]), 2)
        self.assertEqual(template["sections"][0]["disposition"], "unreviewed")

    def test_partial_source_inventory_cannot_hide_unmapped_heading(self):
        folder = self.root / "plans"
        folder.mkdir()
        src_file = folder / "current_task.md"
        src_file.write_text("# A\nDo a\n# B\nDo b\n", encoding="utf-8")
        m = self.write_map("map.json", [("R1", "S", "PENDING")])
        initial, sha = self.source("task.md", "initial")
        t = {"id":"T", "status":"PENDING", "source_path":initial,
             "source_sha256":sha, "maps":[m],"coverage_complete":True}
        manifest = progress.inventory_template(src_file, self.root)
        manifest["sections"] = manifest["sections"][:1]
        manifest["sections"][0].update(disposition="mapped", target_task_ids=["T"], coverage_refs=[m])
        scope = folder / "progress_scope.json"
        scope.write_text(json.dumps({"schema":progress.SCOPE_SCHEMA, "tasks":[t],"source_inventory":manifest}),encoding="utf8")
        snap = progress.legacy_snapshot([], "global", str(scope))
        self.assertFalse(snap["measurable"])
        self.assertIn("не учтён раздел current_task", " ".join(snap["stale_sources"]))

    def test_valid_source_inventory_and_input_links_allow_calculation(self):
        folder = self.root / "plans"
        folder.mkdir()
        src_file = folder / "current_task.md"
        src_file.write_text("# Epic A\nDo a\n# Epic B\nDo b\n", encoding="utf-8")
        m = self.write_map("map.json", [("R1", "S", "VERIFIED"), ("R2", "S", "PENDING")])
        src, sha = self.source("task.md", "original")
        amend, hm = self.source("amend.md", "new details")
        tasks = [{"id":"T","status":"BUILDING","source_path":src,"source_sha256":sha,
                  "maps":[m],"coverage_complete":True}]
        inp = {"id":"in1", "kind":"amendment", "source_path":amend,
               "source_sha256":hm, "target_task_ids":["T"],"reconciled":True,
               "coverage_refs":[{"task_id":"T", "map":m,"req_ids":["R2"]}]}
        manifest = progress.inventory_template(src_file, self.root)
        for entry in manifest["sections"]:
            entry.update(disposition="mapped", target_task_ids=["T"], coverage_refs=[m])
        scope = folder / "progress_scope.json"
        scope.write_text(json.dumps({"schema":progress.SCOPE_SCHEMA,"tasks":tasks,
                                     "owner_inputs":[inp],"source_inventory":manifest}), encoding="utf8")
        snap = progress.legacy_snapshot([], "global", str(scope))
        self.assertTrue(snap["measurable"], snap["stale_sources"])
        self.assertEqual(snap["source_coverage"]["accounted"], 2)
        self.assertEqual(snap["requirements"]["total"], 2)
        self.assertEqual(snap["owner_inputs"]["registered"], 1)

        # A newly appended owner section must reopen scope coverage.
        src_file.write_text(src_file.read_text(encoding="utf8")+"# Epic C\nDo c\n",encoding="utf8")
        drift = progress.legacy_snapshot([], "global", str(scope))
        self.assertFalse(drift["measurable"])
        self.assertTrue(any("Epic C" in item for item in drift["stale_sources"]))

    def test_reconciled_input_with_nonexistent_requirement_is_unverified(self):
        m = self.write_map("map.json", [("R1", "S", "PENDING")])
        src,h = self.source("task.md", "original")
        change,hc = self.source("delta.md", "new feature")
        tasks=[{"id":"T","status":"PENDING","source_path":src,"source_sha256":h,
                "maps":[m],"coverage_complete":True}]
        inp={"id":"delta","kind":"correction","source_path":change,"source_sha256":hc,
             "target_task_ids":["T"],"reconciled":True,
             "coverage_refs":[{"task_id":"T","map":m,"req_ids":["NONEXISTENT"]}]}
        snap=progress.legacy_snapshot([],"global",self.scope_with_inputs(tasks,[inp]))
        self.assertFalse(snap["measurable"])
        self.assertIn("NONEXISTENT"," ".join(snap["stale_sources"]))

    def test_reconciled_input_without_refs_does_not_pass_anymore(self):
        m = self.write_map("map.json", [("R1", "S", "VERIFIED")])
        src,h = self.source("task.md", "original")
        change,hc = self.source("delta.md", "important correction")
        tasks=[{"id":"T","status":"VERIFIED","source_path":src,"source_sha256":h,
                "maps":[m],"coverage_complete":True}]
        inp={"id":"delta","kind":"correction","source_path":change,"source_sha256":hc,
             "target_task_ids":["T"],"reconciled":True}
        snap=progress.legacy_snapshot([],"global",self.scope_with_inputs(tasks,[inp]))
        self.assertFalse(snap["measurable"])
        self.assertIn("reconciled=true", " ".join(snap["stale_sources"]))

    def test_source_change_requires_reconciliation_even_if_heading_unchanged(self):
        folder=self.root/"plans"
        folder.mkdir()
        source=folder/"current_task.md"
        source.write_text("# Hello\nBefore\n",encoding="utf8")
        manifest=progress.inventory_template(source,self.root)
        source.write_text("# Hello\nAfter\n",encoding="utf8")
        issues=[]
        audit=progress.audit_source_inventory({"source_inventory":manifest},folder/"progress_scope.json",{}, {}, issues)
        self.assertEqual(audit["accounted"],0)
        self.assertIn("изменён", " ".join(issues))

    def test_ignored_headings_in_code_fences(self):
        src,_=self.source("headings.md", "# One\n```md\n# Not a task\n```\n# Two\n")
        depth, sections = progress.source_sections(Path(src))
        self.assertEqual(depth,1)
        self.assertEqual(len(sections),2)

    def test_source_inventory_cli_is_read_only(self):
        folder=self.root/"plans"
        folder.mkdir()
        source=folder/"current_task.md"
        source.write_text("# One\nDo one\n# Two\nDo two\n", encoding="utf8")
        scope=folder/"progress_scope.json"
        scope.write_text("{}",encoding="utf8")
        result=io.StringIO()
        with contextlib.redirect_stdout(result):
            self.assertEqual(progress.main(["--scope-file",str(scope),"--inventory-template"]),0)
        self.assertEqual(json.loads(result.getvalue())["sections"][0]["disposition"],"unreviewed")
        self.assertEqual(scope.read_text(encoding="utf8"),"{}")


class CheckoutLocationIntegrityTests(unittest.TestCase):
    """A new OpenCode session may have a different process cwd."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        (self.project / 'plans/maps').mkdir(parents=True)
        (self.root / 'other').mkdir()
        src = self.project / 'plans/task.md'
        src.write_text('owner requirement', encoding='utf8')
        m = self.project / 'plans/maps/m.json'
        m.write_text(json.dumps({'schema':progress.SCHEMA,'items':[{'req_id':'R1',
            'slice':'W1','status':'PENDING','disposition':'required'}]}),encoding='utf8')
        self.scope = self.project / 'plans/progress_scope.json'
        self.scope.write_text(json.dumps({'schema':progress.SCOPE_SCHEMA,'tasks':[{
            'id':'T1','status':'PENDING','source_path':'plans/task.md',
            'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),
            'maps':['plans/maps/m.json'],'coverage_complete':True}]}),encoding='utf8')

    def test_relative_sources_and_maps_resolve_at_project_not_cwd(self):
        import os
        initial = Path.cwd()
        try:
            os.chdir(self.root / 'other')
            snap = progress.legacy_snapshot([], 'all', str(self.scope))
            self.assertTrue(snap['measurable'], snap['stale_sources'])
            self.assertTrue(snap['requirements_measurable'])
            self.assertEqual(snap['requirements']['total'], 1)
        finally:
            os.chdir(initial)




class TestDurableQueueRegistration(unittest.TestCase):
    def test_unqueued_task_is_visible_and_strictly_blocks_certification(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = Path(directory) / "progress_scope.json"
            source = Path(directory) / "input.md"
            source.write_text("queued epic", encoding="utf8")
            scope.write_text(json.dumps({"schema": progress.SCOPE_SCHEMA,
                "tasks": [{"id": "EPIC-X", "status": "PENDING",
                           "source_path": str(source),
                           "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                           "maps": [], "coverage_complete": False}]}), encoding="utf8")
            snap = progress.legacy_snapshot([], "project", str(scope), require_queue=True)
            self.assertFalse(snap["queue_registration_complete"])
            self.assertFalse(snap["measurable"])
            self.assertTrue(any("queue_position" in x for x in snap["execution_queue"]["issues"]))
            self.assertTrue(any("blocked_by" in x for x in snap["execution_queue"]["issues"]))

    def test_delivered_epic_parks_only_future_evidence_not_next_epic(self):
        row = {"id": "delivered", "status": "WAITING_EXTERNAL", "queue_position": 1,
               "priority": "P0", "blocked_by": [], "execution_state": "delivered_awaiting_evidence",
               "deferred_req_ids": ["REQ-1"], "delivery_gate_ref": "delivery.log"}
        next_row = {"id": "next", "status": "PENDING", "queue_position": 2,
                    "priority": "P0", "blocked_by": []}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "delivery.log").write_text('engineering_delivery=PASS\nacceptance=PENDING_FUTURE_EVIDENCE\ndeferred_req_ids=["REQ-1"]\n', encoding="utf8")
        mp = root / "delivery.json"
        row["maps"] = [str(mp)]
        data = {"schema": progress.SCHEMA, "items": [dict(disposition="required", req_id="REQ-1", slice="A", status="READY_FOR_LIVE")]}
        data["items"][0].update(live_required=True, live_when="postdeploy", postrelease_wait=dict(nonblocking=True, reason="future only", trigger="event", next_check="on event", owner="DevOps", contract_ref="REQ-1"))
        Path(mp).write_text(json.dumps(data))
        result = progress.audit_execution_queue([row, next_row], project=root)
        self.assertTrue(result["complete"])
        self.assertEqual(result["parked_delivery_tasks"], ["delivered"])
        self.assertEqual(result["entries"][0]["dispatch_disposition"], "parked_evidence")
        self.assertEqual(result["entries"][1]["dispatch_disposition"], "assess_runnable")
        data["items"][0]["status"] = "FAILED"
        Path(mp).write_text(json.dumps(data))
        result = progress.audit_execution_queue([row, next_row], project=root)
        self.assertFalse(result["complete"])
        self.assertEqual(result["parked_delivery_tasks"], [])

    def test_waiting_for_architect_still_has_queue_position(self):
        scope = [{"id": "EPIC-X", "status": "PENDING", "queue_position": 7,
                  "priority": "P0", "blocked_by": ["PAY-ARCH"],
                  "blocker_sources": {"PAY-ARCH": "plans/features/epic-x/requirements-map.json"}}]
        result = progress.audit_execution_queue(scope)
        self.assertTrue(result["complete"], result["issues"])
        self.assertEqual(result["entries"][0]["blocked_by"], ["PAY-ARCH"])

    def test_duplicate_positions_and_dependency_cycle_are_rejected(self):
        scope = [{"id": "A", "status": "BUILDING", "priority": "P0",
                  "queue_position": 1, "blocked_by": ["B"]},
                 {"id": "B", "status": "PENDING", "priority": "P1",
                  "queue_position": 1, "blocked_by": ["A"]}]
        result = progress.audit_execution_queue(scope)
        self.assertFalse(result["complete"])
        self.assertTrue(any("занят" in x for x in result["issues"]))
        self.assertTrue(any("цикл" in x for x in result["issues"]))

    def test_finished_and_cancelled_entries_are_not_required_in_active_queue(self):
        scope = [{"id": "done", "status": "VERIFIED"}, {"id": "cancelled", "status": "CANCELLED"},
                 {"id": "active", "status": "PENDING", "queue_position": 2,
                  "priority": "P1", "blocked_by": []}]
        result = progress.audit_execution_queue(scope)
        self.assertTrue(result["complete"])
        self.assertEqual(result["open_tasks"], 1)

    def test_untraced_nontask_blocker_rejected(self):
        result = progress.audit_execution_queue([{"id": "epic", "status": "PENDING",
                       "queue_position": 3, "priority": "P0", "blocked_by": ["PAY-ARCH"]}])
        self.assertFalse(result["complete"])
        self.assertTrue(any("blocker_sources" in x for x in result["issues"]))

    def test_bool_position_and_unrecognized_priority_rejected(self):
        result = progress.audit_execution_queue([{"id": "epic", "status": "PENDING",
                       "queue_position": True, "priority": "HIGH", "blocked_by": []}])
        self.assertFalse(result["complete"])
        self.assertGreaterEqual(len(result["issues"]), 2)


class TestQueueSourceEvidence(unittest.TestCase):
    def test_non_task_blocker_provenance_is_an_actual_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            row = {"id":"EPIC", "status":"PENDING", "queue_position":1,
                   "priority":"P0", "blocked_by":["PAY-ARCH"],
                   "blocker_sources":{"PAY-ARCH":"plans/architecture.md"}}
            result = progress.audit_execution_queue([row], project=root)
            self.assertFalse(result["complete"])
            (root / "plans").mkdir()
            (root / "plans/architecture.md").write_text("Architecture needed",encoding="utf8")
            result = progress.audit_execution_queue([row], project=root)
            self.assertTrue(result["complete"])


class TestQueueCliGate(unittest.TestCase):
    def test_missing_queue_returns_nonzero_with_structured_diagnostic(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "request.md"
            source.write_text("Task", encoding="utf8")
            scope = root / "progress_scope.json"
            scope.write_text(json.dumps({"schema":progress.SCOPE_SCHEMA,"tasks":[{
                "id":"X","status":"PENDING","source_path":str(source),
                "source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
                "maps":[],"coverage_complete":False}]}),encoding="utf8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = progress.main(["--scope-file",str(scope),"--legacy-audit","--require-queue","--json"])
            self.assertEqual(code,3)
            result = json.loads(out.getvalue())
            self.assertFalse(result["queue_registration_complete"])
            self.assertFalse(result["measurable"])

if __name__ == "__main__":
    unittest.main()
