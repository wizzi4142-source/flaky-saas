"""
webhook_server.py — receives GitHub App webhooks, stores flaky-test data,
and serves a live dashboard.

Flow per completed workflow run:
  GitHub -> POST /webhook (workflow_run, action=completed)
    -> verify HMAC signature (proves it's really from GitHub)
    -> respond 200 immediately (GitHub requires <10s)
    -> in a background thread: exchange installation_id for a token,
       download artifacts, parse JUnit XML, store in the database

Configuration (environment variables):
    GITHUB_APP_ID              required
    GITHUB_WEBHOOK_SECRET      required
    GITHUB_APP_PRIVATE_KEY     the .pem file's TEXT (for hosting), or
    GITHUB_APP_PRIVATE_KEY_PATH  path to the .pem file (for local use)
    DATABASE_URL               Postgres connection string; if unset, a local
                               SQLite file (FLAKY_DB_PATH) is used instead
    DASHBOARD_KEY              optional; if set, /dashboard/... requires ?key=<this>

Local run:   python webhook_server.py
Hosting:     gunicorn webhook_server:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
"""

import hashlib
import hmac
import html
import os
import threading

from flask import Flask, Response, abort, request

from analyze import analyze as _analyze
from analyze import load_rows as _load_rows
from collector import (
    already_collected,
    download_artifact_zip,
    get_session,
    init_db,
    list_artifacts,
    mark_collected,
    parse_junit_zip,
    store_results,
)
from db import connect
from github_app_auth import get_installation_token
from report import build_html as _build_html

APP_ID = os.environ.get("GITHUB_APP_ID")
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
DB_PATH = os.environ.get("FLAKY_DB_PATH", "flaky_saas.db")
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "")

app = Flask(__name__)


def load_private_key() -> bytes:
    """Hosting platforms pass secrets as environment variables, not files,
    so accept the PEM text directly (with real newlines or literal \\n)."""
    pem = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    if pem:
        return pem.replace("\\n", "\n").encode()
    path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if path:
        with open(path, "rb") as f:
            return f.read()
    raise RuntimeError("set GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH")


def open_db():
    # A fresh connection per operation: hosted Postgres closes idle
    # connections, so a long-lived cached one would go stale.
    return connect(DB_PATH)


def verify_signature(payload_body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not WEBHOOK_SECRET:
        return False
    digest = hmac.new(WEBHOOK_SECRET.encode(), msg=payload_body, digestmod=hashlib.sha256)
    expected = "sha256=" + digest.hexdigest()
    return hmac.compare_digest(expected, signature_header)


def process_workflow_run(payload: dict) -> None:
    try:
        installation_id = payload["installation"]["id"]
        repo_full_name = payload["repository"]["full_name"]
        run = payload["workflow_run"]

        token = get_installation_token(APP_ID, load_private_key(), installation_id)
        session = get_session(token)

        with open_db() as conn:
            if already_collected(conn, repo_full_name, run["id"]):
                return

            found_any = False
            for art in list_artifacts(session, repo_full_name, run["id"]):
                aname = art["name"].lower()
                if not any(k in aname for k in ("test", "junit", "report", "results")):
                    continue
                zip_bytes = download_artifact_zip(session, repo_full_name, art["id"])
                parsed = parse_junit_zip(zip_bytes)
                if parsed:
                    store_results(conn, repo_full_name, run, parsed)
                    found_any = True

            mark_collected(conn, repo_full_name, run["id"])

        status = "found test reports" if found_any else "no usable test reports"
        print(f"[{repo_full_name}] run {run['id']}: {status}", flush=True)
    except Exception as exc:  # noqa: BLE001 - never let a webhook thread die silently
        print(f"error processing workflow_run webhook: {exc!r}", flush=True)


@app.route("/webhook", methods=["POST"])
def webhook():
    if not verify_signature(request.get_data(), request.headers.get("X-Hub-Signature-256")):
        abort(401, description="invalid signature")

    event = request.headers.get("X-GitHub-Event")
    payload = request.get_json(silent=True) or {}

    if event == "workflow_run" and payload.get("action") == "completed":
        # Answer GitHub immediately; do the slow work in the background.
        threading.Thread(target=process_workflow_run, args=(payload,), daemon=True).start()

    return "", 200


@app.route("/healthz", methods=["GET"])
def healthz():
    return {"status": "ok"}, 200


@app.route("/dashboard/<owner>/<repo>", methods=["GET"])
def dashboard(owner: str, repo: str):
    """Live HTML dashboard for one repo, read straight from the database."""
    if DASHBOARD_KEY and not hmac.compare_digest(request.args.get("key", ""), DASHBOARD_KEY):
        abort(403, description="missing or wrong ?key=")

    repo_full_name = f"{owner}/{repo}"
    with open_db() as conn:
        rows = _load_rows(conn, repo_full_name)
    if not rows:
        return (
            "<p style='font-family:sans-serif'>No data yet for "
            f"<code>{html.escape(repo_full_name)}</code>. Trigger a few CI runs and "
            "check back — this page reads live from the database.</p>",
            200,
        )
    return Response(_build_html(_analyze(rows)), mimetype="text/html")


def _startup_checks() -> None:
    missing = []
    if not APP_ID:
        missing.append("GITHUB_APP_ID")
    if not WEBHOOK_SECRET:
        missing.append("GITHUB_WEBHOOK_SECRET")
    if not (os.environ.get("GITHUB_APP_PRIVATE_KEY") or os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")):
        missing.append("GITHUB_APP_PRIVATE_KEY (or GITHUB_APP_PRIVATE_KEY_PATH)")
    if missing:
        print("WARNING: missing environment variables: " + ", ".join(missing), flush=True)
    try:
        init_db(DB_PATH).close()  # create tables if they don't exist yet
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not initialise the database: {exc!r}", flush=True)


_startup_checks()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
