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


if __name__ == "__main__":
    unittest.main()
