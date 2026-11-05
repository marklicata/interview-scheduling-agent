import os
import tempfile
import unittest

from harness_fixtures import EXEMPLARS

from harness.cli import execute_run, main, render_self_check, self_check
from harness.common import wilson
from harness.dataset import Selection, load_dataset, select_cases
from harness.judges import NoJudge
from harness.agents import load_agent
from harness.report import compare, evaluate_gate, rate_metric, render_markdown, summarize
from harness.runner import RunConfig


def rec(cid, status, severity="high", **flags):
    base = {"oracle": False, "presented_count": 0, "all_presented_valid": None, "valid_complete": None,
            "disposition_match": status == "pass", "deterministic_failure": status == "fail",
            "side_effects_ok": True, "assertions_ungraded": 0}
    base.update(flags)
    return {"case_id": cid, "status": status, "severity": severity, "category": "basic_scheduling",
            "difficulty": "basic", "release_scope": "mvp", "authority_mode": "confirm_each_action",
            "expected_disposition": "propose_options", "external_writes": "none", "expect_feasible": None,
            "flags": base, "safety_violations": [], "failure_kinds": [], "checks": [], "assertions": [],
            "duration_s": 0.01, "usage": {}}


class MetricTests(unittest.TestCase):
    def test_wilson(self):
        self.assertIsNone(wilson(0, 0))
        lo, hi = wilson(5, 10)
        self.assertTrue(0 < lo < 0.5 < hi < 1)

    def test_bounds_with_undetermined(self):
        rows = [rec(f"C{i}", "pass") for i in range(9)] + [rec("C9", "ungraded")]
        out = lambda r: {"pass": True, "fail": False}.get(r["status"])  # noqa: E731
        self.assertEqual(rate_metric("m", rows, lambda r: True, out, 0.9)["status"], "pass")
        self.assertEqual(rate_metric("m", rows, lambda r: True, out, 0.95)["status"], "inconclusive")
        self.assertEqual(rate_metric("m", [rec("C", "fail")], lambda r: True, out, 0.95)["status"], "fail")

    def test_empty_denominator_is_na(self):
        m = rate_metric("m", [], lambda r: True, lambda r: True, 0.99)
        self.assertEqual(m["status"], "n/a")
        self.assertIsNone(m["value"])

    def test_critical_failure_fails_gate(self):
        rows = [rec("A", "pass", "critical")] + [rec(f"B{i}", "pass") for i in range(50)] + [rec("C", "fail", "critical")]
        res = summarize(rows, {"complete_suite": True, "judge": {"name": "stub"}})
        self.assertEqual(res["gate"]["result"], "FAIL")
        rule = next(r for r in res["gate"]["rules"] if r["rule"] == "no_critical_case_failures")
        self.assertEqual(rule["status"], "fail")

    def test_subset_and_na_are_inconclusive(self):
        rows = [rec("A", "pass", "critical")]
        res = summarize(rows, {"complete_suite": False, "judge": {"name": "stub"}})
        self.assertEqual(res["gate"]["result"], "INCONCLUSIVE")

    def test_compare_finds_regressions(self):
        base = {"cases": [rec("A", "pass"), rec("B", "fail")], "metrics": {}, "meta": {}}
        cur = {"cases": [rec("A", "fail"), rec("B", "pass"), rec("C", "pass")], "metrics": {}, "meta": {}}
        c = compare(cur, base)
        self.assertEqual([x["case_id"] for x in c["regressions"]], ["A"])
        self.assertEqual([x["case_id"] for x in c["improvements"]], ["B"])
        self.assertEqual(c["new_cases"], ["C"])


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(EXEMPLARS)
        cls.cases = select_cases(cls.dataset.cases, Selection())

    def run_agent(self, name):
        agent = load_agent(name)
        cfg = RunConfig(salt="test", progress=False)
        return execute_run(self.dataset, self.cases, agent, name, NoJudge(), cfg, Selection(), False, "test-run")

    def test_null_agent_fails_everything(self):
        res = self.run_agent("null")
        self.assertEqual(res["gate"]["result"], "FAIL")
        self.assertEqual(res["summary"]["fail"], len(self.cases))
        self.assertEqual(res["metrics"]["valid_proposal_precision"]["status"], "n/a")
        self.assertEqual(res["metrics"]["feasible_case_completion"]["value"], 0.0)
        md = render_markdown(res)
        self.assertIn("Release gate: FAIL", md)
        self.assertIn("Baseline with no agent", md)

    def test_answer_key_has_no_deterministic_failures(self):
        res = self.run_agent("answer_key")
        bad = {r["case_id"]: [c for c in r["checks"] if c["outcome"] == "fail"] for r in res["cases"]
               if r["status"] in ("fail", "error")}
        self.assertEqual(bad, {})
        self.assertEqual(res["gate"]["result"], "INCONCLUSIVE")

    def test_self_check_is_healthy_on_exemplars(self):
        res = self_check(self.cases, self.dataset)
        problems = {e["expectation"]: e["exceptions"] for e in res["expectations"] if e["exceptions"]}
        self.assertEqual(problems, {})
        self.assertTrue(res["healthy"])
        self.assertEqual(res["result"], "HEALTHY")
        self.assertIn("Harness self-check", render_self_check(res))

    def test_cli_run_writes_report_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(["run", "--dataset", EXEMPLARS, "--agent", "null", "--out", tmp])
            self.assertEqual(code, 1)
            self.assertTrue(os.path.exists(os.path.join(tmp, "report.md")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "results.json")))
            code = main(["compare", os.path.join(tmp, "results.json"), os.path.join(tmp, "results.json")])
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
