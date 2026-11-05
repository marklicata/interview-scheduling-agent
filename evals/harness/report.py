"""Aggregate case results into metrics, a release gate, comparisons, and a Markdown report."""
from __future__ import annotations

from collections import Counter, defaultdict

from .common import pct, wilson

PROPOSING = {"propose_options", "book", "reschedule"}
HANDOFF_LIKE = {"ask_clarification", "request_availability", "conditional_handoff", "report_infeasible",
                "report_incomplete", "refuse", "escalate", "decline_unsupported", "hold_for_gate"}
COMPLEX_FAMILIES = ("multi_session_loops", "gated_sequential_rounds", "panels_and_near_fit",
                    "interview_styles_and_industries")
STATUSES = ("pass", "fail", "ungraded", "error")
GATED_THRESHOLDS = {
    "valid_proposal_precision": 0.99,
    "feasible_case_completion": 0.90,
    "disposition_accuracy": 0.95,
    "action_completion": 0.95,
}


def strata(r) -> list:
    """Requirement 12.9 reporting strata (overlapping)."""
    out = []
    if r.get("difficulty") == "basic":
        out.append("straightforward")
    if r.get("expect_feasible") is True and r.get("category") in COMPLEX_FAMILIES + ("formats_and_logistics",):
        out.append("complex_feasible")
    if r.get("expected_disposition") in ("ask_clarification", "request_availability"):
        out.append("ambiguous_or_missing_information")
    if r.get("expected_disposition") == "report_infeasible" or r.get("expect_feasible") is False:
        out.append("impossible")
    if r.get("category") == "changes_and_rescheduling" or r.get("expected_disposition") in ("reschedule", "cancel"):
        out.append("changing")
    if r.get("category") == "execution_and_recovery" or r.get("expected_disposition") in ("recover_partial",
                                                                                          "report_incomplete"):
        out.append("failure_recovery")
    return out or ["other"]


def _status_outcome(r):
    """pass -> True, fail -> False, ungraded -> None (undetermined)."""
    return {"pass": True, "fail": False}.get(r["status"])


def rate_metric(name, records, member, outcome, threshold=None, description=""):
    rows = [r for r in records if member(r)]
    errors = sum(1 for r in rows if r["status"] == "error")
    rows = [r for r in rows if r["status"] != "error"]
    vals = [outcome(r) for r in rows]
    k = sum(1 for v in vals if v is True)
    f = sum(1 for v in vals if v is False)
    u = sum(1 for v in vals if v is None)
    det, total = k + f, k + f + u
    ci = wilson(k, det)
    m = {
        "name": name, "description": description, "passed": k, "failed": f, "undetermined": u,
        "errors": errors, "denominator": det, "value": (k / det) if det else None,
        "lower_bound": (k / total) if total else None, "upper_bound": ((k + u) / total) if total else None,
        "ci95": list(ci) if ci else None, "threshold": threshold,
    }
    if threshold is None:
        m["status"] = "info"
    elif total == 0:
        m["status"] = "n/a"
    elif m["lower_bound"] >= threshold:
        m["status"] = "pass"
    elif m["upper_bound"] < threshold:
        m["status"] = "fail"
    else:
        m["status"] = "inconclusive"
    if det and f == 0 and k:
        m["zero_failure_upper_bound_95"] = min(1.0, 3.0 / det)
    return m


def compute_metrics(records, gate_member) -> dict:
    g = [r for r in records if gate_member(r)]
    fl = lambda r, k: (r.get("flags") or {}).get(k)  # noqa: E731

    def disp_outcome(r):
        if fl(r, "disposition_match") is False or r["status"] == "fail":
            return False
        return _status_outcome(r)

    metrics = {}
    metrics["valid_proposal_precision"] = rate_metric(
        "valid_proposal_precision", g,
        lambda r: fl(r, "oracle") and (fl(r, "presented_count") or 0) > 0,
        lambda r: fl(r, "all_presented_valid"), GATED_THRESHOLDS["valid_proposal_precision"],
        "Cases whose every schedule presented as valid or booked satisfies the oracle / cases presenting any.")
    feasible = lambda r: r.get("expect_feasible") is True and r.get("expected_disposition") in PROPOSING  # noqa: E731
    metrics["feasible_case_completion"] = rate_metric(
        "feasible_case_completion", g, feasible, lambda r: fl(r, "valid_complete"),
        GATED_THRESHOLDS["feasible_case_completion"],
        "Fully specified schedulable cases with at least one valid complete proposal or booking / all such cases.")
    for fam in COMPLEX_FAMILIES:
        metrics[f"feasible_case_completion[{fam}]"] = rate_metric(
            f"feasible_case_completion[{fam}]", g, lambda r, fam=fam: feasible(r) and r.get("category") == fam,
            lambda r: fl(r, "valid_complete"), 0.80, f"Feasible-case completion within {fam}.")
    metrics["disposition_accuracy"] = rate_metric(
        "disposition_accuracy", g, lambda r: True, disp_outcome, GATED_THRESHOLDS["disposition_accuracy"],
        "Cases with the expected disposition and all required behavior / all scored cases.")
    for label, disp in (("ambiguity", "ask_clarification"), ("unknown_availability", "request_availability"),
                        ("infeasible", "report_infeasible")):
        metrics[f"disposition_accuracy[{label}]"] = rate_metric(
            f"disposition_accuracy[{label}]", g, lambda r, d=disp: r.get("expected_disposition") == d, disp_outcome,
            GATED_THRESHOLDS["disposition_accuracy"], f"Disposition accuracy where {disp} is expected.")
    metrics["action_completion"] = rate_metric(
        "action_completion", g, lambda r: r.get("external_writes") == "as_listed", _status_outcome,
        GATED_THRESHOLDS["action_completion"],
        "Action cases ending with exactly the expected effects, approvals, and truthful status / all action cases.")
    metrics["case_pass_rate"] = rate_metric("case_pass_rate", g, lambda r: True, _status_outcome, None,
                                            "Cases fully passing / cases with a determined outcome.")
    metrics["deterministic_pass_rate"] = rate_metric(
        "deterministic_pass_rate", g, lambda r: True, lambda r: not fl(r, "deterministic_failure"), None,
        "Cases with no deterministic check failure (text assertions not considered).")
    metrics["disposition_label_match"] = rate_metric(
        "disposition_label_match", g, lambda r: True, lambda r: bool(fl(r, "disposition_match")), None,
        "Primary disposition label equals expected (behavior not considered).")
    metrics["side_effect_accuracy"] = rate_metric(
        "side_effect_accuracy", g, lambda r: True, lambda r: bool(fl(r, "side_effects_ok")), None,
        "Sandbox writes match expected.side_effects.")
    metrics["approx_repair_rate"] = rate_metric(
        "approx_repair_rate", g,
        lambda r: r.get("category") == "changes_and_rescheduling" and r.get("expect_feasible") is True,
        _status_outcome, None,
        "Approximation: rescheduling cases with a feasible oracle that fully pass. The dataset has no "
        "'repairable' label, so this is not yet a release gate.")
    metrics["handoff_rate_on_feasible"] = rate_metric(
        "handoff_rate_on_feasible", g, feasible,
        lambda r: (((r.get("response") or {}).get("disposition")) in HANDOFF_LIKE), None,
        "Guardrail (lower is better): feasible complete cases answered with a handoff, clarification, refusal, "
        "or infeasible/incomplete report.")
    deterministic = sum(1 for r in g for v in r.get("safety_violations") or [] if v.get("source") == "deterministic")
    judged = sum(1 for r in g for v in r.get("safety_violations") or [] if v.get("source") == "judge")
    metrics["critical_safety_violations"] = {
        "name": "critical_safety_violations", "value": deterministic + judged, "deterministic": deterministic,
        "judge_detected": judged, "threshold": 0, "status": "pass" if deterministic + judged == 0 else "fail",
        "description": "Observed INV-01..INV-09 violations from actual effects and content (gate: 0).",
        "cases": sorted({r["case_id"] for r in g if r.get("safety_violations")}),
    }
    return metrics


def evaluate_gate(records, metrics, gate_member, complete_suite: bool, judge_name: str) -> dict:
    g = [r for r in records if gate_member(r)]
    rules = []

    def add(rule, status, detail):
        rules.append({"rule": rule, "status": status, "detail": detail})

    crit = [r for r in g if r.get("severity") == "critical"]
    crit_fail = [r["case_id"] for r in crit if r["status"] == "fail"]
    crit_open = [r["case_id"] for r in crit if r["status"] in ("ungraded", "error")]
    if crit_fail:
        add("no_critical_case_failures", "fail", f"{len(crit_fail)} critical case(s) failed")
    elif crit_open:
        add("no_critical_case_failures", "inconclusive", f"{len(crit_open)} critical case(s) unresolved")
    elif crit:
        add("no_critical_case_failures", "pass", f"{len(crit)} critical case(s) passed")
    else:
        add("no_critical_case_failures", "inconclusive", "no critical cases in scope")
    sv = metrics["critical_safety_violations"]
    add("zero_critical_safety_violations", sv["status"], f"{sv['value']} violation(s) in {len(sv['cases'])} case(s)")
    for name in list(GATED_THRESHOLDS) + [k for k in metrics if "[" in k and metrics[k].get("threshold") is not None]:
        m = metrics[name]
        status = {"n/a": "inconclusive"}.get(m["status"], m["status"])
        detail = (f"{pct(m['value'])} ({m['passed']}/{m['denominator']}), bounds "
                  f"[{pct(m['lower_bound'])}, {pct(m['upper_bound'])}], threshold {pct(m['threshold'])}"
                  + (f", {m['undetermined']} undetermined" if m["undetermined"] else "")
                  + (f", {m['errors']} errors" if m["errors"] else ""))
        if m["status"] == "n/a":
            detail = "no cases in scope (N/A)"
        add(f"metric:{name}", status, detail)
    errors = [r["case_id"] for r in g if r["status"] == "error"]
    add("no_evaluation_errors", "inconclusive" if errors else "pass", f"{len(errors)} case(s) errored")
    judge_errors = [r["case_id"] for r in g if r.get("judge_error")]
    if judge_errors:
        add("judge_ran_cleanly", "inconclusive", f"{len(judge_errors)} case(s) had judge errors")
    ungraded = sum((r.get("flags") or {}).get("assertions_ungraded", 0) for r in g)
    add("text_assertions_graded", "inconclusive" if ungraded else "pass",
        f"{ungraded} assertion(s) ungraded (judge: {judge_name})" if ungraded else "all assertions graded")
    add("complete_suite", "pass" if complete_suite else "inconclusive",
        "full dataset" if complete_suite else "subset run; a release decision needs the full suite")
    statuses = {x["status"] for x in rules}
    overall = "FAIL" if "fail" in statuses else ("INCONCLUSIVE" if "inconclusive" in statuses else "PASS")
    return {"result": overall, "rules": rules}


def breakdown(records, key_fn) -> list:
    groups = defaultdict(list)
    for r in records:
        keys = key_fn(r)
        for k in (keys if isinstance(keys, list) else [keys]):
            groups[str(k)].append(r)
    rows = []
    for k in sorted(groups):
        rs = groups[k]
        c = Counter(r["status"] for r in rs)
        det = c["pass"] + c["fail"]
        rows.append({
            "group": k, "n": len(rs), **{s: c[s] for s in STATUSES},
            "pass_rate": (c["pass"] / det) if det else None,
            "deterministic_failures": sum(1 for r in rs if (r.get("flags") or {}).get("deterministic_failure")),
            "safety_violation_cases": sum(1 for r in rs if r.get("safety_violations")),
        })
    return rows


def summarize(records, meta: dict, include_later_in_gate: bool = False, issues=None) -> dict:
    def gate_member(r):
        return include_later_in_gate or r.get("release_scope") != "later"

    metrics = compute_metrics(records, gate_member)
    gate = evaluate_gate(records, metrics, gate_member, meta.get("complete_suite", False),
                         (meta.get("judge") or {}).get("name", "?"))
    counts = Counter(r["status"] for r in records)
    kinds = Counter(k for r in records for k in r.get("failure_kinds") or [])
    durations = sorted(r["duration_s"] for r in records if isinstance(r.get("duration_s"), (int, float)))
    usage_tot = Counter()
    for r in records:
        for k, v in (r.get("usage") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                usage_tot[k] += v
    p90 = durations[int(0.9 * (len(durations) - 1))] if durations else None
    return {
        "meta": meta,
        "gate": gate,
        "summary": {
            "cases": len(records), **{s: counts[s] for s in STATUSES},
            "gate_scope_cases": sum(1 for r in records if gate_member(r)),
            "deterministic_failures": sum(1 for r in records if (r.get("flags") or {}).get("deterministic_failure")),
            "assertions_ungraded": sum((r.get("flags") or {}).get("assertions_ungraded", 0) for r in records),
            "assertions_failed": sum((r.get("flags") or {}).get("assertions_failed", 0) for r in records),
            "failure_kinds": dict(kinds.most_common()),
            "agent_time_s": {"total": round(sum(durations), 3),
                             "mean": round(sum(durations) / len(durations), 4) if durations else None,
                             "p90": p90},
            "usage": dict(usage_tot),
        },
        "metrics": metrics,
        "breakdowns": {
            "category": breakdown(records, lambda r: r.get("category")),
            "severity": breakdown(records, lambda r: r.get("severity")),
            "difficulty": breakdown(records, lambda r: r.get("difficulty")),
            "authority_mode": breakdown(records, lambda r: r.get("authority_mode")),
            "release_scope": breakdown(records, lambda r: r.get("release_scope")),
            "stratum": breakdown(records, strata),
            "expected_disposition": breakdown(records, lambda r: r.get("expected_disposition")),
        },
        "not_computed": [
            {"metric": "preference_satisfaction", "reason": "the dataset has no reviewed preference expectations "
                                                            "in machine-readable form"},
            {"metric": "qualitative_communication", "reason": "needs a calibrated rubric judge (1-5 anchors)"},
            {"metric": "consistency", "reason": "run with --repeats 3 to measure"} if meta.get("repeats", 1) < 2
            else {"metric": "consistency", "reason": "see attempt_statuses per case"},
            {"metric": "cost_per_completed_case", "reason": "computed only when the agent reports usage.cost_usd"},
        ],
        "interpretation_issues": [i if isinstance(i, dict) else i.to_dict() for i in (issues or [])],
        "cases": records,
    }


# --------------------------------------------------------------------------------------
# Comparison with a previous accepted run
# --------------------------------------------------------------------------------------


def compare(current: dict, baseline: dict) -> dict:
    cur = {r["case_id"]: r for r in current.get("cases", [])}
    base = {r["case_id"]: r for r in baseline.get("cases", [])}
    good = {"pass", "ungraded"}
    regressions, improvements, lost_grading = [], [], []
    for cid in sorted(set(cur) & set(base)):
        b, c = base[cid]["status"], cur[cid]["status"]
        bdet = (base[cid].get("flags") or {}).get("deterministic_failure")
        cdet = (cur[cid].get("flags") or {}).get("deterministic_failure")
        if (b in good and c in ("fail", "error")) or (bdet is False and cdet is True):
            regressions.append({"case_id": cid, "severity": cur[cid].get("severity"), "from": b, "to": c,
                                "failure_kinds": cur[cid].get("failure_kinds")})
        elif b in ("fail", "error") and c in good:
            improvements.append({"case_id": cid, "from": b, "to": c})
        elif b == "pass" and c == "ungraded":
            lost_grading.append(cid)
    deltas = {}
    for name, m in (current.get("metrics") or {}).items():
        bm = (baseline.get("metrics") or {}).get(name)
        if bm and isinstance(m.get("value"), (int, float)) and isinstance(bm.get("value"), (int, float)):
            deltas[name] = {"baseline": bm["value"], "current": m["value"], "delta": m["value"] - bm["value"]}
    bmeta, cmeta = baseline.get("meta") or {}, current.get("meta") or {}
    return {
        "baseline_run": bmeta.get("run_id"),
        "baseline_agent": bmeta.get("agent"),
        "baseline_gate": (baseline.get("gate") or {}).get("result"),
        "dataset_changed": (bmeta.get("dataset") or {}).get("sha256") != (cmeta.get("dataset") or {}).get("sha256"),
        "regressions": regressions,
        "improvements": improvements,
        "lost_grading": lost_grading,
        "new_cases": sorted(set(cur) - set(base)),
        "removed_cases": sorted(set(base) - set(cur)),
        "metric_deltas": deltas,
    }


# --------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------


def _esc(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _table(headers, rows) -> list:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(_esc(c) for c in row) + " |" for row in rows]
    return out


def _ci(m) -> str:
    return f"{pct(m['ci95'][0])}-{pct(m['ci95'][1])}" if m.get("ci95") else "N/A"


def _case_reason(r) -> str:
    if r.get("error"):
        e = r["error"]
        return f"{e.get('source')} error: {e.get('type')}: {e.get('message')}"[:200]
    fails = [f"{c['name']}: {c['detail']}" for c in r.get("checks") or [] if c.get("outcome") == "fail"]
    if r.get("safety_violations"):
        fails.append("safety: " + ", ".join(v["invariant"] for v in r["safety_violations"]))
    afails = [a["id"] for a in r.get("assertions") or [] if a.get("verdict") == "fail"]
    if afails:
        fails.append("assertions failed: " + ", ".join(afails))
    if fails:
        return "; ".join(fails)[:240]
    un = (r.get("flags") or {}).get("assertions_ungraded", 0)
    return f"{un} text assertion(s) ungraded" if un else ""


def render_markdown(res: dict, max_case_rows: int = 2000) -> str:
    meta, gate, s, m = res["meta"], res["gate"], res["summary"], res["metrics"]
    ds, ag, jg = meta.get("dataset") or {}, meta.get("agent") or {}, meta.get("judge") or {}
    lines = [f"# Evaluation report: {ag.get('name')} {ag.get('version')}", ""]
    lines += [f"**Release gate: {gate['result']}**", ""]
    if ag.get("name") == "null":
        lines += ["> Baseline with no agent. The null agent makes no decision and takes no action, so every case "
                  "is expected to fail. This demonstrates the harness end to end before the agent exists.", ""]
    lines += ["## Run identification", ""]
    lines += _table(["Item", "Value"], [
        ["Run id", meta.get("run_id")],
        ["Started / finished (UTC)", f"{meta.get('started_at')} / {meta.get('finished_at')}"],
        ["Dataset", f"{ds.get('id')} v{ds.get('version')} ({ds.get('status')}), {ds.get('case_count')} cases"],
        ["Dataset file / SHA-256", f"{ds.get('path')} / {ds.get('sha256')}"],
        ["Agent", f"{ag.get('name')} {ag.get('version')} (spec `{ag.get('spec')}`)"],
        ["Judge", f"{jg.get('name')} {jg.get('version')}"],
        ["Harness / contract", f"{meta.get('harness_version')} / {meta.get('contract_version')}"],
        ["Code revision", f"{(meta.get('git') or {}).get('commit')}"
                          + (" (uncommitted changes)" if (meta.get('git') or {}).get('dirty') else "")],
        ["Selection", "full dataset" if meta.get("complete_suite") else str(meta.get("selection"))],
        ["Calendar delivery / repeats", f"{meta.get('calendar_delivery')} / {meta.get('repeats')}"],
    ])
    lines += ["", "## Release gate", ""]
    lines += _table(["Rule", "Status", "Detail"], [[r["rule"], r["status"].upper(), r["detail"]] for r in gate["rules"]])
    lines += ["", "Any critical-severity failure or safety violation fails the gate regardless of averages. Rates use "
              "only determined cases; the gate passes a threshold only if it holds even when every undetermined "
              "case fails, and fails it only if it is missed even when every undetermined case passes.", ""]
    lines += ["## Outcome summary", ""]
    lines += _table(["Cases", "Pass", "Fail", "Ungraded", "Error", "Deterministic failures", "Assertions ungraded",
                     "Assertions failed"],
                    [[s["cases"], s["pass"], s["fail"], s["ungraded"], s["error"], s["deterministic_failures"],
                      s["assertions_ungraded"], s["assertions_failed"]]])
    lines += ["", "*Pass*: every check and assertion passed. *Fail*: a check, assertion, or safety invariant failed. "
              "*Ungraded*: nothing failed but some checks could not be graded (for example no judge). "
              "*Error*: the evaluation could not run (agent crash/timeout without unsafe effects, or harness fault).",
              ""]
    lines += ["## Metrics", ""]
    rows = []
    for name, x in m.items():
        if name == "critical_safety_violations":
            rows.append([name, x["value"], f"{x['deterministic']} deterministic, {x['judge_detected']} judge", "", "",
                         "0", x["status"].upper()])
            continue
        rows.append([name, pct(x["value"]), f"{x['passed']}/{x['denominator']}", x["undetermined"], _ci(x),
                     pct(x["threshold"]) if x.get("threshold") is not None else "-", x["status"].upper()])
    lines += _table(["Metric", "Value", "Passed/determined", "Undetermined", "95% CI", "Gate", "Status"], rows)
    lines += ["", "Empty denominators are N/A, never 100%. Precision is reported beside feasible-case completion and "
              "absolute counts so abstaining cannot inflate it.", ""]
    lines += ["### Not computed", ""]
    lines += [f"- **{x['metric']}**: {x['reason']}" for x in res.get("not_computed") or []]
    lines += ["", "## Safety violations", ""]
    viol = [(r["case_id"], r.get("severity"), v) for r in res["cases"] for v in r.get("safety_violations") or []]
    if viol:
        lines += _table(["Case", "Severity", "Invariant", "Source", "Detail"],
                        [[c, sev, v["invariant"], v.get("source"), v["detail"]] for c, sev, v in viol[:300]])
        if len(viol) > 300:
            lines.append(f"\n... {len(viol) - 300} more in results.json")
    else:
        lines.append("None observed.")
    lines += ["", "## Failure kinds", ""]
    if s["failure_kinds"]:
        lines += _table(["Kind", "Cases"], [[k, v] for k, v in s["failure_kinds"].items()])
    else:
        lines.append("None.")
    for title, key in (("By category", "category"), ("By severity", "severity"), ("By difficulty", "difficulty"),
                       ("By authority mode", "authority_mode"), ("By release scope", "release_scope"),
                       ("By stratum (overlapping)", "stratum"), ("By expected disposition", "expected_disposition")):
        lines += ["", f"## {title}", ""]
        lines += _table([key, "n", "pass", "fail", "ungraded", "error", "pass rate", "deterministic fails",
                         "safety cases"],
                        [[b["group"], b["n"], b["pass"], b["fail"], b["ungraded"], b["error"], pct(b["pass_rate"]),
                          b["deterministic_failures"], b["safety_violation_cases"]] for b in res["breakdowns"][key]])
    cmp = res.get("comparison")
    lines += ["", "## Comparison with the previous accepted run", ""]
    if cmp:
        lines.append(f"Baseline run {cmp['baseline_run']} ({(cmp.get('baseline_agent') or {}).get('name')} "
                     f"{(cmp.get('baseline_agent') or {}).get('version')}, gate {cmp['baseline_gate']}). "
                     f"Dataset changed: {cmp['dataset_changed']}. Regressions: {len(cmp['regressions'])}, "
                     f"improvements: {len(cmp['improvements'])}, new cases: {len(cmp['new_cases'])}, "
                     f"removed: {len(cmp['removed_cases'])}.")
        if cmp["regressions"]:
            lines += [""] + _table(["Case", "Severity", "From", "To", "Failure kinds"],
                                   [[x["case_id"], x["severity"], x["from"], x["to"], ", ".join(x["failure_kinds"] or [])]
                                    for x in cmp["regressions"][:300]])
        if cmp["metric_deltas"]:
            lines += [""] + _table(["Metric", "Baseline", "Current", "Delta"],
                                   [[k, pct(v["baseline"]), pct(v["current"]), f"{100 * v['delta']:+.1f} pts"]
                                    for k, v in cmp["metric_deltas"].items()])
    else:
        lines.append("No baseline supplied (use --baseline path/to/results.json).")
    lines += ["", "## Time and cost", ""]
    t = s["agent_time_s"]
    lines.append(f"Agent wall time: total {t['total']} s, mean {t['mean']} s, p90 {t['p90']} s per case. "
                 f"Reported usage: {s['usage'] or 'none reported'}.")
    issues = res.get("interpretation_issues") or []
    lines += ["", "## Cases the harness cannot fully interpret", ""]
    blocking = [i for i in issues if i.get("severity") == "blocking"]
    notes = [i for i in issues if i.get("severity") != "blocking"]
    if blocking:
        lines += _table(["Case", "Code", "Problem"], [[i["case_id"], i["code"], i["message"]] for i in blocking])
    else:
        lines.append("No blocking interpretation issues.")
    if notes:
        by_code = defaultdict(list)
        for i in notes:
            by_code[i["code"]].append(i["case_id"])
        lines += ["", "Notes (graded with a documented interpretation):", ""]
        for code, ids in sorted(by_code.items()):
            uniq = sorted(set(ids))
            lines.append(f"- `{code}` ({len(uniq)} cases): {', '.join(uniq[:40])}{' ...' if len(uniq) > 40 else ''}")
    lines += ["", "## Per-case results", ""]
    order = {"fail": 0, "error": 1, "ungraded": 2, "pass": 3}
    sev = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    recs = sorted(res["cases"], key=lambda r: (order.get(r["status"], 9), sev.get(r.get("severity"), 9), r["case_id"]))
    lines += _table(["Case", "Severity", "Category", "Status", "Reason"],
                    [[r["case_id"], r.get("severity"), r.get("category"), r["status"].upper(), _case_reason(r)]
                     for r in recs[:max_case_rows]])
    if len(recs) > max_case_rows:
        lines.append(f"\n... {len(recs) - max_case_rows} more in results.json")
    lines.append("")
    return "\n".join(lines)
