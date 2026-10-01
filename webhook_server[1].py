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
    DASHBOARD_KEY              optional legacy bypass; if set, appending
                               ?key=<this> skips the login requirement
    SECRET_KEY                 required for login — signs the session cookie
    GITHUB_APP_CLIENT_ID       required for login — from the app's About page
    GITHUB_APP_CLIENT_SECRET   required for login — "Generate a new client secret"
    APP_BASE_URL               this service's own public URL, e.g.
                               https://flaky-saas.onrender.com (needed to build
                               the OAuth callback URL)

Local run:   python webhook_server.py
Hosting:     gunicorn webhook_server:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
"""

import hashlib
import hmac
import html
import os
import secrets
import threading

from flask import Flask, Response, abort, redirect, request, session, url_for

from analyze import analyze as _analyze
from analyze import analyze_jobs as _analyze_jobs
from analyze import load_job_rows as _load_job_rows
from analyze import load_rows as _load_rows
from collector import (
    already_collected,
    collect_jobs,
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
from github_oauth import (
    build_authorize_url,
    exchange_code_for_token,
    get_accessible_repos,
    get_user_login,
)
from report import build_html as _build_html

APP_ID = os.environ.get("GITHUB_APP_ID")
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
DB_PATH = os.environ.get("FLAKY_DB_PATH", "flaky_saas.db")
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "")
CLIENT_ID = os.environ.get("GITHUB_APP_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GITHUB_APP_CLIENT_SECRET", "")
BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")

app = Flask(__name__)
# SECRET_KEY signs the session cookie (login state). Without a stable one,
# every restart would log everyone out; a random fallback is fine for local
# testing but MUST be set explicitly when hosting with more than one worker.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


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
            # Job-level data first: needs nothing from the customer's CI and
            # must run on every event (a re-run bumps run_attempt).
            n_jobs = collect_jobs(session, conn, repo_full_name, run)
            print(f"[{repo_full_name}] run {run['id']}: stored {n_jobs} job results", flush=True)

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


def _login_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and BASE_URL)


def _current_user_repos():
    """None if not logged in; otherwise the list of repos (possibly empty)
    this session's user is allowed to see, as cached at login time."""
    return session.get("repos")


@app.route("/login", methods=["GET"])
def login():
    if not _login_configured():
        abort(500, description="login is not configured (missing GITHUB_APP_CLIENT_ID/SECRET/APP_BASE_URL)")
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    redirect_uri = f"{BASE_URL}/callback"
    return redirect(build_authorize_url(CLIENT_ID, redirect_uri, state))


@app.route("/callback", methods=["GET"])
def callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        abort(400, description="invalid OAuth state — please try logging in again")
    code = request.args.get("code")
    if not code:
        abort(400, description="missing code from GitHub")

    user_token = exchange_code_for_token(CLIENT_ID, CLIENT_SECRET, code)
    session["user_login"] = get_user_login(user_token)
    session["repos"] = get_accessible_repos(user_token)
    return redirect(url_for("home"))


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/", methods=["GET"])
def home():
    if not _login_configured():
        return (
            "<p style='font-family:sans-serif'>Login isn't configured on this "
            "deployment yet — set GITHUB_APP_CLIENT_ID, GITHUB_APP_CLIENT_SECRET "
            "and APP_BASE_URL.</p>",
            200,
        )

    repos = _current_user_repos()
    if repos is None:
        return (
            "<p style='font-family:sans-serif'>"
            "<a href='/login'>Sign in with GitHub</a> to see your repos' "
            "flaky-test dashboards.</p>",
            200,
        )

    who = html.escape(session.get("user_login", ""))
    if not repos:
        body = "<p>No repos found — install the GitHub App on a repo first.</p>"
    else:
        items = "".join(
            f"<li><a href='/dashboard/{html.escape(r)}'>{html.escape(r)}</a></li>"
            for r in sorted(repos)
        )
        body = f"<ul>{items}</ul>"

    return (
        f"<div style='font-family:sans-serif'>"
        f"<p>Signed in as <b>{who}</b> — <a href='/logout'>sign out</a></p>"
        f"{body}"
        f"</div>",
        200,
    )


@app.route("/dashboard/<owner>/<repo>", methods=["GET"])
def dashboard(owner: str, repo: str):
    """Live HTML dashboard for one repo, read straight from the database.
    Access is granted either by a valid login session that includes this
    repo, or (legacy / scripting) by the shared DASHBOARD_KEY."""
    repo_full_name = f"{owner}/{repo}"

    key_ok = bool(DASHBOARD_KEY) and hmac.compare_digest(request.args.get("key", ""), DASHBOARD_KEY)
    if not key_ok:
        repos = _current_user_repos()
        if repos is None:
            return redirect(url_for("login") if _login_configured() else url_for("home"))
        if repo_full_name not in repos:
            abort(403, description="you don't have access to this repo")

    with open_db() as conn:
        test_rows = _load_rows(conn, repo_full_name)
        job_rows = _load_job_rows(conn, repo_full_name)
    rows = test_rows or job_rows
    if not rows:
        return (
            "<p style='font-family:sans-serif'>No data yet for "
            f"<code>{html.escape(repo_full_name)}</code>. Trigger a few CI runs and "
            "check back — this page reads live from the database.</p>",
            200,
        )
    report = _analyze(test_rows) if test_rows else []
    report += _analyze_jobs(job_rows) if job_rows else []
    report.sort(key=lambda r: (-r["flake_score"], -r["wasted_minutes"]))
    return Response(_build_html(report), mimetype="text/html")


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
