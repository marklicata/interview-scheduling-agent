"""Draw a representative, held-out sample of test cases.

    python evals/tools/draw_sample.py --size 280 --seed 20261109 --exclude-per-category 10 --out evals/samples/holdout-280.txt

Stratified by category in proportion to the full suite (largest-remainder allocation), simple random sampling within
each category, fixed seed. --exclude-per-category N removes the cases the harness's --per-category N selects (the
set used while tuning the agent), so scores on the sample are not inflated by tuning. Prints how the sample's
category, severity, difficulty, authority-mode and release-scope mix compares with the full suite.

A held-out sample stays honest only while it is not used for tuning: if you change the agent because of failures
seen on it, draw a new one (new seed) for the next unbiased estimate.
"""
from __future__ import annotations

import argparse
import collections
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from harness.dataset import Selection, load_dataset, select_cases  # noqa: E402


def mix(cases, key):
    c = collections.Counter(key(x) for x in cases)
    n = len(cases)
    return {k: v / n for k, v in c.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default=os.path.join(os.path.dirname(__file__), "..", "interview-scheduling-test-cases.yaml"))
    ap.add_argument("--size", type=int, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--exclude-per-category", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    ds = load_dataset(args.dataset)
    cases = ds["test_cases"] if isinstance(ds, dict) else getattr(ds, "cases", ds)
    excluded = set()
    if args.exclude_per_category:
        excluded = {c["id"] for c in select_cases(cases, Selection(per_category=args.exclude_per_category))}
    pool = [c for c in cases if c["id"] not in excluded]

    by_cat = collections.defaultdict(list)
    for c in pool:
        by_cat[c["category"]].append(c)
    full_share = mix(cases, lambda x: x["category"])
    quota = {k: args.size * full_share[k] for k in by_cat}
    alloc = {k: min(int(q), len(by_cat[k])) for k, q in quota.items()}
    for k in sorted(quota, key=lambda k: quota[k] - int(quota[k]), reverse=True):
        if sum(alloc.values()) >= args.size:
            break
        if alloc[k] < len(by_cat[k]):
            alloc[k] += 1

    rng = random.Random(args.seed)
    sample = []
    for k in sorted(by_cat):
        group = sorted(by_cat[k], key=lambda c: c["id"])
        sample += rng.sample(group, alloc[k])
    sample.sort(key=lambda c: c["id"])

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# Held-out representative sample: {len(sample)} of {len(cases)} cases\n")
        fh.write(f"# draw_sample.py --size {args.size} --seed {args.seed} --exclude-per-category "
                 f"{args.exclude_per_category} (excluded {len(excluded)} tuning cases)\n")
        fh.write("# Stratified by category in proportion to the full suite. Do not tune the agent on these cases.\n")
        for c in sample:
            fh.write(c["id"] + "\n")

    print(f"Sample: {len(sample)} cases from a pool of {len(pool)} (excluded {len(excluded)} tuning cases)")
    for name, key in (("category", lambda x: x["category"]), ("severity", lambda x: x.get("severity")),
                      ("difficulty", lambda x: x.get("difficulty")),
                      ("authority mode", lambda x: ((x.get("inputs") or {}).get("authority") or {}).get("mode")),
                      ("release scope", lambda x: x.get("release_scope"))):
        full, samp = mix(cases, key), mix(sample, key)
        worst = max(abs(full.get(k, 0) - samp.get(k, 0)) for k in set(full) | set(samp))
        print(f"\n{name}: largest share difference {worst:.1%}")
        for k in sorted(full, key=lambda k: -full[k]):
            print(f"  {str(k):<42} full {full[k]:6.1%}   sample {samp.get(k, 0):6.1%}")
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
