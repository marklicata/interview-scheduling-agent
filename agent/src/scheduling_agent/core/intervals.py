"""Half-open integer intervals [start, end) on the minute axis (see timeutil.to_min)."""
from __future__ import annotations


def normalize(intervals) -> list:
    items = sorted((int(s), int(e)) for s, e in intervals if e > s)
    out: list = []
    for s, e in items:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def intersect(a, b) -> list:
    a, b = normalize(a), normalize(b)
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def subtract(a, b) -> list:
    out = []
    b = normalize(b)
    for s, e in normalize(a):
        cur = s
        for bs, be in b:
            if be <= cur or bs >= e:
                continue
            if bs > cur:
                out.append((cur, bs))
            cur = max(cur, be)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return out


def union(*lists) -> list:
    return normalize([iv for lst in lists for iv in lst])


def contains(intervals, s: int, e: int) -> bool:
    return any(a <= s and e <= b for a, b in intervals)


def overlaps(intervals, s: int, e: int) -> list:
    return [(a, b) for a, b in intervals if a < e and s < b]
