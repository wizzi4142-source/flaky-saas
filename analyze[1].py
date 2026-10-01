"""
analyze.py — reads the SQLite db built by collector.py and computes a
flakiness score per test.

A test is treated as a "flip" on a given commit if, across the runs we
saw for that exact commit (head_sha), it produced both a pass and a
fail. Repeated flips across different commits is strong evidence of a
flaky test rather than a real regression (a real bug fails consistently
until the code changes).

Usage:
    python analyze.py --db flaky.db --out flaky_report.json
"""

import argparse
import json
import sqlite3
from collections import defaultdict


def load_rows(conn, repo=None):
    q = "SELECT repo, head_sha, classname, test_name, outcome, duration_s FROM test_results"
    params = ()
    if repo:
        q += " WHERE repo = ?"
        params = (repo,)
    return conn.execute(q, params).fetchall()


def analyze(rows):
    # key: (repo, classname, test_name) -> commit -> list of (outcome, duration)
    by_test = defaultdict(lambda: defaultdict(list))
    for repo, sha, classname, tname, outcome, duration in rows:
        by_test[(repo, classname, tname)][sha].append((outcome, duration))

    report = []
    for (repo, classname, tname), by_commit in by_test.items():
        total_runs = sum(len(v) for v in by_commit.values())
        pass_count = sum(1 for v in by_commit.values() for o, _ in v if o == "pass")
        fail_count = sum(1 for v in by_commit.values() for o, _ in v if o == "fail")
        distinct_commits = len(by_commit)

        flip_commits = 0
        wasted_seconds = 0.0
        for sha, outcomes in by_commit.items():
            statuses = {o for o, _ in outcomes if o in ("pass", "fail")}
            if {"pass", "fail"}.issubset(statuses):
                flip_commits += 1
                wasted_seconds += sum(d for o, d in outcomes if o == "fail")

        flake_score = flip_commits / distinct_commits if distinct_commits else 0.0

        report.append(
            {
                "repo": repo,
                "test": f"{classname}::{tname}" if classname else tname,
                "total_runs": total_runs,
                "distinct_commits": distinct_commits,
                "pass_count": pass_count,
                "fail_count": fail_count,
                "flip_commits": flip_commits,
                "flake_score": round(flake_score, 3),
                "wasted_minutes": round(wasted_seconds / 60, 2),
            }
        )

    report.sort(key=lambda r: (-r["flake_score"], -r["wasted_minutes"]))
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="flaky.db")
    ap.add_argument("--repo", default=None, help="filter to one repo (owner/repo)")
    ap.add_argument("--out", default="flaky_report.json")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    rows = load_rows(conn, args.repo)
    if not rows:
        raise SystemExit(
            "No data found. Run collector.py first, and make sure your CI "
            "actually uploads JUnit XML artifacts."
        )

    report = analyze(rows)

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)

    flaky = [r for r in report if r["flake_score"] > 0]
    print(f"{len(report)} distinct tests analyzed, {len(flaky)} show flaky behavior.\n")
    print(f"{'flake%':>7}  {'flips':>5}  {'wasted min':>10}  test")
    for r in report[: args.top]:
        print(
            f"{r['flake_score']*100:6.1f}%  {r['flip_commits']:5d}  "
            f"{r['wasted_minutes']:10.1f}  {r['test']}"
        )

    print(f"\nFull report written to {args.out} — run report.py to render a dashboard.")


if __name__ == "__main__":
    main()


def load_job_rows(conn, repo=None):
    q = "SELECT repo, head_sha, job_name, conclusion, duration_s FROM job_results"
    params = ()
    if repo:
        q += " WHERE repo = ?"
        params = (repo,)
    return conn.execute(q, params).fetchall()


def analyze_jobs(rows):
    """Same flip logic as analyze(), applied to whole CI jobs: a job that
    both succeeded and failed on the SAME commit (e.g. someone clicked
    "Re-run failed jobs" and it went green) is flaky. Output rows match
    analyze()'s shape so the dashboard can show both together."""
    by_job = defaultdict(lambda: defaultdict(list))
    for repo, sha, name, conclusion, duration in rows:
        by_job[(repo, name)][sha].append(("pass" if conclusion == "success" else "fail", duration))

    report = []
    for (repo, name), by_commit in by_job.items():
        flips, wasted = 0, 0.0
        for outcomes in by_commit.values():
            if {o for o, _ in outcomes} == {"pass", "fail"}:
                flips += 1
                wasted += sum(d for o, d in outcomes if o == "fail")
        n_commits = len(by_commit)
        report.append(
            {
                "repo": repo,
                "test": f"[job] {name}",
                "total_runs": sum(len(v) for v in by_commit.values()),
                "distinct_commits": n_commits,
                "pass_count": sum(1 for v in by_commit.values() for o, _ in v if o == "pass"),
                "fail_count": sum(1 for v in by_commit.values() for o, _ in v if o == "fail"),
                "flip_commits": flips,
                "flake_score": round(flips / n_commits, 3) if n_commits else 0.0,
                "wasted_minutes": round(wasted / 60, 2),
            }
        )
    report.sort(key=lambda r: (-r["flake_score"], -r["wasted_minutes"]))
    return report
