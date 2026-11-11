"""Re-grade a finished run's stored responses with another judge and compare the two judges.

    python evals/rejudge.py evals/results/<run>/results.json --judge harness.judge_copilot:create_judge
    $env:SCHEDULING_JUDGE_MODEL = "gpt-5.6-sol"; python evals/rejudge.py <results.json> --judge ...

No agent runs: the stored response and sandbox transcript of every case are graded again. Writes
<run>/rejudge-<judge>.json (all verdicts) and rejudge-<judge>.md (agreement and every disagreement, for a human
to decide which judge was right). Use it to choose the judge that measures the agent best.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.dataset import load_dataset  # noqa: E402
from harness.graders import build_assertions  # noqa: E402
from harness.judges import JudgeRequest, load_judge  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results", help="results.json of a finished run (with transcripts)")
    ap.add_argument("--judge", required=True, help="judge spec, e.g. harness.judge_copilot:create_judge")
    ap.add_argument("--dataset", default=os.path.join(os.path.dirname(__file__), "interview-scheduling-test-cases.yaml"))
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    run = json.load(open(args.results, encoding="utf-8"))
    ds = load_dataset(args.dataset)
    cases = {c["id"]: c for c in (ds["test_cases"] if isinstance(ds, dict) else getattr(ds, "cases", ds))}
    judge = load_judge(args.judge)

    def grade(rec):
        case = cases[rec["case_id"]]
        req = JudgeRequest(case=case, response=rec.get("response") or {}, transcript=rec.get("transcript") or {},
                           assertions=build_assertions(case))
        try:
            return rec["case_id"], {v.assertion_id: v.to_dict() for v in judge.grade(req)}, None
        except Exception as exc:  # noqa: BLE001 - report and continue
            return rec["case_id"], {}, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        new = {cid: (v, err) for cid, v, err in pool.map(grade, run["cases"])}

    rows, counts = [], collections.Counter()
    for rec in run["cases"]:
        verdicts, err = new[rec["case_id"]]
        for a in rec.get("assertions") or []:
            old_v = a.get("verdict")
            nv = verdicts.get(a["id"], {})
            new_v = nv.get("verdict", "ungraded" if err else "missing")
            counts[(old_v, new_v)] += 1
            if old_v != new_v:
                rows.append({"case_id": rec["case_id"], "severity": rec.get("severity"), "assertion": a["id"],
                             "text": a.get("text"), "first_judge": old_v, "first_rationale": a.get("rationale"),
                             "second_judge": new_v, "second_rationale": nv.get("rationale") or err})

    first_name = (((run.get("meta") or {}).get("judge")) or {}).get("name")
    total = sum(counts.values())
    agree = sum(n for (a, b), n in counts.items() if a == b)
    tag = re.sub(r"[^A-Za-z0-9.-]+", "_", judge.name)
    base = os.path.dirname(os.path.abspath(args.results))
    json.dump({"first_judge": first_name, "second_judge": judge.name, "counts": {f"{a}->{b}": n for (a, b), n
               in counts.items()}, "verdicts": {cid: v for cid, (v, _) in new.items()}, "disagreements": rows},
              open(os.path.join(base, f"rejudge-{tag}.json"), "w", encoding="utf-8"), indent=1, default=str)
    lines = [f"# Judge comparison: {first_name} vs {judge.name}", "",
             f"Assertions: {total}. Agreement: {agree}/{total} ({agree / total:.1%})." if total else "No assertions.",
             "", "| First judge | Second judge | Count |", "|---|---|---|"]
    lines += [f"| {a} | {b} | {n} |" for (a, b), n in sorted(counts.items())]
    lines += ["", "## Disagreements (decide which judge is right)", ""]
    for r in rows:
        lines += [f"### {r['case_id']} {r['assertion']} ({r['severity']}): {r['first_judge']} vs {r['second_judge']}",
                  f"- Assertion: {r['text']}", f"- First judge: {r['first_rationale']}",
                  f"- Second judge: {r['second_rationale']}", ""]
    path = os.path.join(base, f"rejudge-{tag}.md")
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    print(f"Agreement {agree}/{total}; {len(rows)} disagreement(s). Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
