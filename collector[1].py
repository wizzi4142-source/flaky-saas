"""
collector.py — pulls CI history from a GitHub repo and records per-test
pass/fail outcomes into a local SQLite database.

Requires the workflow to upload JUnit-style XML test reports as build
artifacts (most test runners can do this: pytest --junitxml=results.xml,
jest --reporters=jest-junit, go test with gotestsum, etc.).

Usage:
    export GITHUB_TOKEN=ghp_xxx        # needs 'actions:read' / repo scope
    python collector.py owner/repo --max-runs 200 --db flaky.db

Re-running is safe: results are keyed by (repo, run_id, test_id), so
re-collecting the same runs just no-ops on the duplicates.

IMPORTANT: GitHub Actions "Re-run all jobs" reuses the same run_id as a
new "attempt", and GitHub's REST API has no way to fetch artifacts from
an attempt that isn't the latest one. So to build up multiple pass/fail
observations for the same commit, trigger brand-new workflow runs
instead of re-running: add `workflow_dispatch:` to your workflow's `on:`
block and click "Run workflow" in the Actions tab several times — each
click is a genuinely new run_id with its own artifacts.
"""

import argparse
import io
import os
import sys
import time
import zipfile
from xml.etree import ElementTree as ET

import requests

import db

API = "https://api.github.com"


def get_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    return s


def _get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """GET with basic rate-limit backoff."""
    while True:
        resp = session.get(url, **kwargs)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(reset - time.time(), 5)
            print(f"  rate limited, sleeping {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp


def list_workflow_runs(session: requests.Session, repo: str, max_runs: int):
    runs = []
    page = 1
    per_page = min(100, max_runs)
    while len(runs) < max_runs:
        url = f"{API}/repos/{repo}/actions/runs"
        resp = _get(
            session,
            url,
            params={"status": "completed", "per_page": per_page, "page": page},
        )
        batch = resp.json().get("workflow_runs", [])
        if not batch:
            break
        runs.extend(batch)
        page += 1
        if len(batch) < per_page:
            break
    return runs[:max_runs]


def list_artifacts(session: requests.Session, repo: str, run_id: int):
    url = f"{API}/repos/{repo}/actions/runs/{run_id}/artifacts"
    resp = _get(session, url, params={"per_page": 100})
    return resp.json().get("artifacts", [])


def download_artifact_zip(session: requests.Session, repo: str, artifact_id: int) -> bytes:
    url = f"{API}/repos/{repo}/actions/artifacts/{artifact_id}/zip"
    resp = _get(session, url)
    return resp.content


def parse_junit_zip(zip_bytes: bytes):
    """Yield (classname, test_name, outcome, duration_s) for every <testcase>
    found in any *.xml file inside the zip that looks like a JUnit report."""
    results = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return results

    for name in zf.namelist():
        if not name.lower().endswith(".xml"):
            continue
        try:
            root = ET.fromstring(zf.read(name))
        except ET.ParseError:
            continue

        testcases = root.iter("testcase")
        for tc in testcases:
            classname = tc.attrib.get("classname", "")
            tname = tc.attrib.get("name", "unknown")
            duration = float(tc.attrib.get("time", 0) or 0)

            if tc.find("failure") is not None or tc.find("error") is not None:
                outcome = "fail"
            elif tc.find("skipped") is not None:
                outcome = "skip"
            else:
                outcome = "pass"

            results.append((classname, tname, outcome, duration))
    return results


def list_jobs_for_attempt(session: requests.Session, repo: str, run_id: int, attempt: int):
    """Jobs of one specific attempt of a workflow run (GitHub keeps every
    attempt's jobs, unlike artifacts)."""
    jobs, page = [], 1
    while True:
        url = f"{API}/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
        batch = _get(session, url, params={"per_page": 100, "page": page}).json().get("jobs", [])
        jobs.extend(batch)
        if len(batch) < 100:
            return jobs
        page += 1


def _duration_s(job: dict) -> float:
    from datetime import datetime

    try:
        start = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
        return max((end - start).total_seconds(), 0.0)
    except (KeyError, TypeError, ValueError, AttributeError):
        return 0.0


def store_jobs(conn, repo: str, run: dict, attempt: int, jobs: list) -> int:
    stored = 0
    for job in jobs:
        # cancelled / skipped / still running say nothing about flakiness
        if job.get("conclusion") not in ("success", "failure"):
            continue
        conn.execute(
            """
            INSERT INTO job_results
                (repo, run_id, run_attempt, head_sha, job_name, conclusion, duration_s)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (repo, run["id"], attempt, job.get("head_sha") or run["head_sha"],
             job["name"], job["conclusion"], _duration_s(job)),
        )
        stored += 1
    return stored


def collect_jobs(session: requests.Session, conn, repo: str, run: dict) -> int:
    """Record every attempt (1..latest) of this run. Safe to repeat: a re-run
    produces a new webhook with a higher run_attempt, and rows already
    stored are ignored by the primary key."""
    stored = 0
    for attempt in range(1, (run.get("run_attempt") or 1) + 1):
        try:
            jobs = list_jobs_for_attempt(session, repo, run["id"], attempt)
        except requests.HTTPError:
            continue
        stored += store_jobs(conn, repo, run, attempt, jobs)
    return stored


def init_db(path: str = "flaky.db"):
    """Open the database (SQLite file, or Postgres if DATABASE_URL is set)
    and make sure the tables exist. BIGINT / DOUBLE PRECISION are used
    because GitHub run ids exceed 32 bits and Postgres REAL is too coarse;
    SQLite accepts both names fine."""
    conn = db.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS test_results (
            repo        TEXT NOT NULL,
            run_id      BIGINT NOT NULL,
            head_sha    TEXT NOT NULL,
            run_created TEXT NOT NULL,
            classname   TEXT NOT NULL,
            test_name   TEXT NOT NULL,
            outcome     TEXT NOT NULL,
            duration_s  DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (repo, run_id, classname, test_name)
        )
        """
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS collected_runs (repo TEXT, run_id BIGINT, PRIMARY KEY (repo, run_id))"
    )
    # Job-level results: needs NO changes to the customer's CI. One row per
    # job per attempt, so "Re-run failed jobs" shows up as a pass/fail flip.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS job_results (
            repo        TEXT NOT NULL,
            run_id      BIGINT NOT NULL,
            run_attempt INTEGER NOT NULL,
            head_sha    TEXT NOT NULL,
            job_name    TEXT NOT NULL,
            conclusion  TEXT NOT NULL,
            duration_s  DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (repo, run_id, run_attempt, job_name)
        )
        """
    )
    conn.commit()
    return conn


def already_collected(conn, repo, run_id) -> bool:
    row = conn.execute(
        "SELECT 1 FROM collected_runs WHERE repo=? AND run_id=?", (repo, run_id)
    ).fetchone()
    return row is not None


def mark_collected(conn, repo, run_id):
    conn.execute(
        "INSERT INTO collected_runs (repo, run_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (repo, run_id),
    )


def store_results(conn, repo, run, test_results):
    for classname, tname, outcome, duration in test_results:
        conn.execute(
            """
            INSERT INTO test_results
                (repo, run_id, head_sha, run_created, classname, test_name, outcome, duration_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                repo,
                run["id"],
                run["head_sha"],
                run["created_at"],
                classname,
                tname,
                outcome,
                duration,
            ),
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("repo", help="owner/repo")
    ap.add_argument("--max-runs", type=int, default=200)
    ap.add_argument("--db", default="flaky.db")
    ap.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token (or set GITHUB_TOKEN env var)",
    )
    args = ap.parse_args()

    if not args.token:
        sys.exit("error: no GitHub token. Set GITHUB_TOKEN or pass --token.")

    session = get_session(args.token)
    conn = init_db(args.db)

    print(f"Fetching up to {args.max_runs} completed runs for {args.repo} ...")
    runs = list_workflow_runs(session, args.repo, args.max_runs)
    print(f"Found {len(runs)} completed runs.")

    processed = 0
    with_reports = 0
    for run in runs:
        if already_collected(conn, args.repo, run["id"]):
            processed += 1
            continue

        artifacts = list_artifacts(session, args.repo, run["id"])
        found_any = False
        for art in artifacts:
            # Heuristic: only bother with artifacts likely to hold test reports.
            aname = art["name"].lower()
            if not any(k in aname for k in ("test", "junit", "report", "results")):
                continue
            try:
                zip_bytes = download_artifact_zip(session, args.repo, art["id"])
            except requests.HTTPError:
                continue
            parsed = parse_junit_zip(zip_bytes)
            if parsed:
                store_results(conn, args.repo, run, parsed)
                found_any = True

        if found_any:
            with_reports += 1
        mark_collected(conn, args.repo, run["id"])
        conn.commit()
        processed += 1
        if processed % 10 == 0:
            print(f"  processed {processed}/{len(runs)} runs...")

    print(f"Done. {with_reports}/{len(runs)} runs had usable JUnit test reports.")
    print(f"Data stored in {args.db} — run analyze.py next.")


if __name__ == "__main__":
    main()
