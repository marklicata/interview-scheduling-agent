"""Command-line interface for the evaluation harness.

Run from the repository root:
    python evals/run_harness.py run --agent null
    python evals/run_harness.py self-check
    python evals/run_harness.py lint
    python evals/run_harness.py show-request BAS-901
    python evals/run_harness.py compare evals/results/<run>/results.json evals/baselines/accepted.json
    python evals/run_harness.py accept evals/results/<run>/results.json

Exit codes for run: 0 gate PASS, 1 gate FAIL, 3 gate INCONCLUSIVE, 4 harness error (argparse usage errors are 2).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone

from . import CONTRACT_VERSION, HARNESS_VERSION
from .agents import load_agent
from .common import utc_now_iso
from .dataset import DEFAULT_DATASET, Selection, build_request, dump_request, lint_case, load_dataset, select_cases
from .judges import load_judge
from .report import compare, render_markdown, summarize
from .runner import RunConfig, run_cases

EVALS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(EVALS_DIR)
DEFAULT_RESULTS = os.path.join(EVALS_DIR, "results")
DEFAULT_BASELINE = os.path.join(EVALS_DIR, "baselines", "accepted.json")
EXIT = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 3}


def _rel(path) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:  # different drive on Windows
        return path


def _git_info() -> dict:
    def git(*args):
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
    try:
        head = git("rev-parse", "HEAD")
        status = git("status", "--porcelain")
        if head.returncode != 0:
            return {"commit": None, "dirty": None}
        return {"commit": head.stdout.strip(), "dirty": bool(status.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def _add_selection_args(p):
    p.add_argument("--dataset", default=DEFAULT_DATASET, help="merged dataset YAML")
    p.add_argument("--case", dest="ids", action="append", default=[], help="case id or glob (repeatable)")
    p.add_argument("--exclude-case", dest="exclude_ids", action="append", default=[])
    p.add_argument("--category", dest="categories", action="append", default=[], help="category name or id prefix")
    p.add_argument("--severity", dest="severities", action="append", default=[])
    p.add_argument("--difficulty", dest="difficulties", action="append", default=[])
    p.add_argument("--authority-mode", dest="authority_modes", action="append", default=[])
    p.add_argument("--release-scope", dest="release_scopes", action="append", default=[])
    p.add_argument("--tag", dest="tags", action="append", default=[])
    p.add_argument("--limit", type=int, default=0)


def _selection(args) -> Selection:
    return Selection(ids=args.ids, exclude_ids=args.exclude_ids, categories=args.categories,
                     severities=args.severities, difficulties=args.difficulties, authority_modes=args.authority_modes,
                     release_scopes=args.release_scopes, tags=args.tags, limit=args.limit)


def _run_id(agent_name: str, suffix: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in agent_name)
    return f"{stamp}-{safe}{suffix}"


def _write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False, default=str)
        fh.write("\n")


def _write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def execute_run(dataset, cases, agent, agent_spec, judge, cfg: RunConfig, selection: Selection,
                include_later_in_gate: bool, run_id: str) -> dict:
    started = utc_now_iso()
    records = run_cases(cases, agent, judge, cfg)
    issues = [i for c in cases for i in lint_case(c)]
    meta = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": utc_now_iso(),
        "harness_version": HARNESS_VERSION,
        "contract_version": CONTRACT_VERSION,
        "dataset": dataset.identity,
        "agent": {"name": getattr(agent, "name", "?"), "version": str(getattr(agent, "version", "?")),
                  "spec": agent_spec},
        "judge": {"name": getattr(judge, "name", "?"), "version": str(getattr(judge, "version", "?"))},
        "git": _git_info(),
        "selection": selection.describe(),
        "complete_suite": selection.is_full() and len(cases) == len(dataset.cases),
        "calendar_delivery": cfg.calendar_delivery,
        "repeats": cfg.repeats,
        "include_later_in_gate": include_later_in_gate,
        "python": sys.version.split()[0],
        "training_use": "prohibited (INV-09): evaluation artifacts must never be used to train or fine-tune models",
    }
    return summarize(records, meta, include_later_in_gate=include_later_in_gate, issues=issues)


def cmd_run(args) -> int:
    dataset = load_dataset(args.dataset)
    sel = _selection(args)
    cases = select_cases(dataset.cases, sel)
    if not cases:
        print("No cases selected.", file=sys.stderr)
        return 4
    agent = load_agent(args.agent)
    judge = load_judge(args.judge)
    run_id = _run_id(getattr(agent, "name", "agent"))
    out_dir = args.out or os.path.join(DEFAULT_RESULTS, run_id)
    cfg = RunConfig(calendar_delivery=args.calendar_delivery, salt=run_id, timeout_s=args.timeout_s,
                    workers=args.workers, repeats=args.repeats, include_transcripts=not args.no_transcripts)
    print(f"Running {len(cases)} case(s) with agent {agent.name} {agent.version}, judge {judge.name} ...",
          file=sys.stderr)
    res = execute_run(dataset, cases, agent, args.agent, judge, cfg, sel, args.include_later_in_gate, run_id)
    baseline_path = args.baseline
    if baseline_path:
        with open(baseline_path, "r", encoding="utf-8") as fh:
            res["comparison"] = compare(res, json.load(fh))
    _write_json(os.path.join(out_dir, "results.json"), res)
    report_path = os.path.join(out_dir, args.report_name)
    _write_text(report_path, render_markdown(res))
    s, gate = res["summary"], res["gate"]
    print(f"\nRelease gate: {gate['result']}")
    print(f"Cases {s['cases']}: pass {s['pass']}, fail {s['fail']}, ungraded {s['ungraded']}, error {s['error']}; "
          f"deterministic failures {s['deterministic_failures']}")
    for r in gate["rules"]:
        if r["status"] != "pass":
            print(f"  {r['status'].upper():12} {r['rule']}: {r['detail']}")
    if res.get("comparison"):
        c = res["comparison"]
        print(f"Compared with {c['baseline_run']}: {len(c['regressions'])} regression(s), "
              f"{len(c['improvements'])} improvement(s)")
    print(f"Wrote {_rel(report_path)} and results.json")
    code = EXIT[gate["result"]]
    if args.fail_on_regression and res.get("comparison", {}).get("regressions"):
        code = code or 1
    return code


# --------------------------------------------------------------------------------------
# Self-check: verify the harness against agents whose correct grades are known
# --------------------------------------------------------------------------------------


def _complete_invalid_example(case) -> bool:
    from case_oracle import Oracle, OracleError, parse_example
    spec = case.get("oracle")
    if spec is None:
        return False
    try:
        o = Oracle(spec)
    except OracleError:
        return False
    for ex in spec.get("invalid_examples") or []:
        try:
            starts, _ = parse_example(ex)
        except (KeyError, TypeError, ValueError):
            continue
        if sorted(starts) == sorted(o.sessions):
            return True
    return False


def _no_authority_basis(case) -> bool:
    inputs = case.get("inputs") or {}
    mode = (inputs.get("authority") or {}).get("mode")
    confirmed = any(isinstance(t, dict) and t.get("confirmation") is True for t in inputs.get("conversation") or [])
    return mode == "read_only_user" or (mode in ("confirm_each_action", "grant_expired_or_revoked") and not confirmed)


def _check(rec, name):
    for c in rec.get("checks") or []:
        if c["name"] == name:
            return c
    return {"outcome": None, "detail": ""}


def self_check(cases, dataset, workers=1) -> dict:
    by_id = {str(c.get("id")): c for c in cases}
    judge = load_judge("none")
    runs = {}
    for name in ("null", "answer_key", "invalid_key", "unauthorized_writer"):
        agent = load_agent(name)
        cfg = RunConfig(salt=f"self-check-{name}", workers=workers, include_transcripts=False)
        print(f"Self-check: running {name} on {len(cases)} case(s) ...", file=sys.stderr)
        runs[name] = run_cases(cases, agent, judge, cfg)
    expectations = []
    issues = [i.to_dict() for c in cases for i in lint_case(c)]
    blocking = {}
    for i in issues:
        if i["severity"] == "blocking":
            blocking.setdefault(i["case_id"], []).append(i["code"])

    def expect(agent, description, applies, ok, reason):
        recs = [r for r in runs[agent] if applies(r)]
        bad = [{"case_id": r["case_id"], "status": r["status"], "reason": reason(r),
                "dataset_issue": ", ".join(blocking.get(r["case_id"], []))} for r in recs if not ok(r)]
        expectations.append({"agent": agent, "expectation": description, "applicable": len(recs),
                             "met": len(recs) - len(bad),
                             "exceptions": [x for x in bad if not x["dataset_issue"]],
                             "known_dataset_issues": [x for x in bad if x["dataset_issue"]]})

    def failing_checks(r):
        if r.get("error"):
            return f"{r['error'].get('source')} error: {r['error'].get('message')}"
        parts = [f"{c['name']}: {c['detail']}" for c in r.get("checks") or [] if c["outcome"] == "fail"]
        if r.get("safety_violations"):
            parts.append("safety: " + "; ".join(f"{v['invariant']} {v['detail']}" for v in r["safety_violations"]))
        return "; ".join(parts)[:400]

    expect("null", "every case fails (no decision, no action)", lambda r: True,
           lambda r: r["status"] == "fail" and _check(r, "disposition")["outcome"] == "fail", failing_checks)
    expect("null", "no case shows any side effect or safety violation", lambda r: True,
           lambda r: not r.get("safety_violations") and (r.get("flags") or {}).get("writes_attempted", 0) == 0,
           failing_checks)
    expect("answer_key", "expected outputs pass every deterministic check (text assertions stay ungraded)",
           lambda r: True, lambda r: r["status"] in ("pass", "ungraded"), failing_checks)
    expect("answer_key", "oracle valid examples are accepted as valid complete schedules",
           lambda r: r.get("expect_feasible") is True and r.get("expected_disposition") in ("propose_options", "book",
                                                                                            "reschedule"),
           lambda r: (r.get("flags") or {}).get("valid_complete") is True,
           lambda r: f"valid_complete={(r.get('flags') or {}).get('valid_complete')}; "
                     f"{_check(r, 'schedule_validity')['detail']}")
    expect("invalid_key", "oracle invalid examples presented as valid fail schedule_validity",
           lambda r: _complete_invalid_example(by_id[r["case_id"]]),
           lambda r: _check(r, "schedule_validity")["outcome"] == "fail",
           lambda r: f"schedule_validity={_check(r, 'schedule_validity')['outcome']}: "
                     f"{_check(r, 'schedule_validity')['detail']}")
    expect("unauthorized_writer", "a write in a no-write case fails side_effects",
           lambda r: r.get("external_writes") == "none",
           lambda r: _check(r, "side_effects")["outcome"] == "fail" and r["status"] == "fail", failing_checks)
    expect("unauthorized_writer", "a write without any authority basis is reported as INV-01",
           lambda r: r.get("external_writes") == "none" and _no_authority_basis(by_id[r["case_id"]]),
           lambda r: any(v["invariant"] == "INV-01" for v in r.get("safety_violations") or []), failing_checks)
    healthy = all(not e["exceptions"] for e in expectations)
    known = sorted({x["case_id"] for e in expectations for x in e["known_dataset_issues"]})
    result = "HEALTHY" if healthy else "PROBLEMS FOUND"
    if healthy and known:
        result = f"HEALTHY ({len(known)} case(s) with known dataset issues)"
    return {
        "meta": {"run_id": _run_id("self-check"), "finished_at": utc_now_iso(), "harness_version": HARNESS_VERSION,
                 "contract_version": CONTRACT_VERSION, "dataset": dataset.identity, "git": _git_info(),
                 "cases": len(cases)},
        "result": result,
        "healthy": healthy,
        "expectations": expectations,
        "interpretation_issues": issues,
    }


def render_self_check(res: dict) -> str:
    from .report import _table
    meta = res["meta"]
    ds = meta["dataset"]
    lines = ["# Harness self-check", "", f"**Result: {res['result']}**", "",
             "The self-check runs agents whose correct grades are known: the null agent must fail everything, "
             "an answer key built from the hidden expected outputs must pass every deterministic check, the "
             "oracle's invalid examples must be rejected, and unauthorized writes must be caught. An exception "
             "means a harness bug or a case the harness cannot interpret as written. Exceptions on cases that lint "
             "already reports as blocking dataset issues are listed separately as known dataset issues: fix those "
             "in the dataset rather than in the harness.", ""]
    lines += _table(["Item", "Value"], [
        ["Dataset", f"{ds.get('id')} v{ds.get('version')}, {ds.get('case_count')} cases, sha256 {ds.get('sha256')}"],
        ["Cases checked", meta["cases"]],
        ["Harness / contract", f"{meta['harness_version']} / {meta['contract_version']}"],
        ["Code revision", f"{(meta.get('git') or {}).get('commit')}"
                          + (" (uncommitted changes)" if (meta.get('git') or {}).get('dirty') else "")],
        ["Finished (UTC)", meta["finished_at"]],
    ])
    lines += ["", "## Expectations", ""]
    lines += _table(["Agent", "Expectation", "Applicable", "Met", "Unexplained exceptions", "Known dataset issues"],
                    [[e["agent"], e["expectation"], e["applicable"], e["met"], len(e["exceptions"]),
                      len(e.get("known_dataset_issues") or [])] for e in res["expectations"]])
    for e in res["expectations"]:
        if e["exceptions"]:
            lines += ["", f"### Unexplained exceptions: {e['agent']} - {e['expectation']}", ""]
            lines += _table(["Case", "Status", "Reason"],
                            [[x["case_id"], x["status"], x["reason"]] for x in e["exceptions"]])
    for e in res["expectations"]:
        if e.get("known_dataset_issues"):
            lines += ["", f"### Known dataset issues: {e['agent']} - {e['expectation']}", ""]
            lines += _table(["Case", "Dataset issue", "Status", "Reason"],
                            [[x["case_id"], x["dataset_issue"], x["status"], x["reason"]]
                             for x in e["known_dataset_issues"]])
    issues = res.get("interpretation_issues") or []
    blocking = [i for i in issues if i["severity"] == "blocking"]
    lines += ["", "## Interpretation issues found by lint", ""]
    if blocking:
        lines += _table(["Case", "Code", "Problem"], [[i["case_id"], i["code"], i["message"]] for i in blocking])
    else:
        lines.append("No blocking issues.")
    notes = [i for i in issues if i["severity"] != "blocking"]
    if notes:
        codes = {}
        for i in notes:
            codes.setdefault(i["code"], set()).add(i["case_id"])
        lines += [""] + [f"- note `{k}`: {len(v)} case(s): {', '.join(sorted(v)[:30])}{' ...' if len(v) > 30 else ''}"
                         for k, v in sorted(codes.items())]
    lines.append("")
    return "\n".join(lines)


def cmd_self_check(args) -> int:
    dataset = load_dataset(args.dataset)
    cases = select_cases(dataset.cases, _selection(args))
    res = self_check(cases, dataset, workers=args.workers)
    out_dir = args.out or os.path.join(DEFAULT_RESULTS, res["meta"]["run_id"])
    _write_json(os.path.join(out_dir, "self-check.json"), res)
    path = os.path.join(out_dir, "self-check.md")
    _write_text(path, render_self_check(res))
    print(f"\nHarness self-check: {res['result']}")
    for e in res["expectations"]:
        extra = f" ({len(e['known_dataset_issues'])} known dataset issue(s))" if e['known_dataset_issues'] else ""
        print(f"  {e['agent']:20} {e['met']}/{e['applicable']}  {e['expectation']}{extra}")
    print(f"Wrote {_rel(path)}")
    return 0 if res["healthy"] else 1


def cmd_lint(args) -> int:
    dataset = load_dataset(args.dataset)
    cases = select_cases(dataset.cases, _selection(args))
    issues = [i for c in cases for i in lint_case(c)]
    for i in issues:
        if args.all or i.severity == "blocking":
            print(f"{i.severity.upper():8} [{i.case_id}] {i.code}: {i.message}")
    blocking = sum(1 for i in issues if i.severity == "blocking")
    print(f"\n{len(cases)} cases; {blocking} blocking issue(s); {len(issues) - blocking} note(s)")
    return 1 if blocking else 0


def cmd_compare(args) -> int:
    with open(args.current, "r", encoding="utf-8") as fh:
        cur = json.load(fh)
    with open(args.baseline, "r", encoding="utf-8") as fh:
        base = json.load(fh)
    c = compare(cur, base)
    print(f"Dataset changed: {c['dataset_changed']}")
    print(f"Regressions: {len(c['regressions'])}; improvements: {len(c['improvements'])}; "
          f"new: {len(c['new_cases'])}; removed: {len(c['removed_cases'])}")
    for x in c["regressions"]:
        print(f"  REGRESSION {x['case_id']} ({x['severity']}): {x['from']} -> {x['to']} {x['failure_kinds']}")
    for k, v in c["metric_deltas"].items():
        print(f"  {k}: {v['baseline']:.3f} -> {v['current']:.3f} ({v['delta']:+.3f})")
    return 1 if c["regressions"] else 0


def cmd_accept(args) -> int:
    with open(args.results, "r", encoding="utf-8") as fh:
        res = json.load(fh)
    if not res.get("meta", {}).get("complete_suite"):
        print("Refusing to accept a subset run as the baseline (use --force to override).", file=sys.stderr)
        if not args.force:
            return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.to)), exist_ok=True)
    shutil.copyfile(args.results, args.to)
    print(f"Accepted {res['meta'].get('run_id')} as {_rel(args.to)}")
    return 0


def cmd_show_request(args) -> int:
    dataset = load_dataset(args.dataset)
    case = next((c for c in dataset.cases if str(c.get("id")) == args.case_id), None)
    if case is None:
        print(f"Unknown case {args.case_id}", file=sys.stderr)
        return 4
    req, stripped = build_request(case, args.calendar_delivery, salt="show-request")
    print(dump_request(req))
    print("\nRemoved before the agent sees it: " + (", ".join(stripped) or "nothing"), file=sys.stderr)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="run_harness.py", description="Interview scheduling agent evaluation harness")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="evaluate an agent")
    _add_selection_args(r)
    r.add_argument("--agent", default="null", help="null | answer_key | invalid_key | unauthorized_writer | module:factory")
    r.add_argument("--judge", default="none", help="none | module:factory (text-assertion judge)")
    r.add_argument("--out", help="output directory (default evals/results/<run-id>)")
    r.add_argument("--report-name", default="report.md")
    r.add_argument("--baseline", help="results.json of the previous accepted run to compare against")
    r.add_argument("--fail-on-regression", action="store_true")
    r.add_argument("--repeats", type=int, default=1, help="attempts per case (worst attempt counts)")
    r.add_argument("--workers", type=int, default=1, help="cases run in parallel")
    r.add_argument("--timeout-s", type=float, default=0.0, help="per-attempt agent timeout (0 = none)")
    r.add_argument("--calendar-delivery", choices=["tools", "inline"], default="tools")
    r.add_argument("--include-later-in-gate", action="store_true", help="gate on release_scope: later cases too")
    r.add_argument("--no-transcripts", action="store_true", help="omit sandbox transcripts from results.json")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("self-check", help="verify the harness with agents whose grades are known")
    _add_selection_args(s)
    s.add_argument("--out")
    s.add_argument("--workers", type=int, default=1)
    s.set_defaults(func=cmd_self_check)

    lt = sub.add_parser("lint", help="report cases the harness cannot interpret as written")
    _add_selection_args(lt)
    lt.add_argument("--all", action="store_true", help="also print notes")
    lt.set_defaults(func=cmd_lint)

    c = sub.add_parser("compare", help="compare two results.json files")
    c.add_argument("current")
    c.add_argument("baseline")
    c.set_defaults(func=cmd_compare)

    a = sub.add_parser("accept", help="store a full run's results.json as the accepted baseline")
    a.add_argument("results")
    a.add_argument("--to", default=DEFAULT_BASELINE)
    a.add_argument("--force", action="store_true")
    a.set_defaults(func=cmd_accept)

    sr = sub.add_parser("show-request", help="print exactly what the agent receives for a case")
    sr.add_argument("case_id")
    sr.add_argument("--dataset", default=DEFAULT_DATASET)
    sr.add_argument("--calendar-delivery", choices=["tools", "inline"], default="tools")
    sr.set_defaults(func=cmd_show_request)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 4
