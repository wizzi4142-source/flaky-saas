"""
github_oauth.py — "Sign in with GitHub" for the dashboard.

This is separate from github_app_auth.py: that module authenticates the
APP itself (to fetch a repo's CI artifacts); this module authenticates a
HUMAN (so the dashboard can show them only the repos they're actually
allowed to see).

Flow:
  1. /login redirects the browser to GitHub's authorize URL.
  2. GitHub redirects back to /callback with a one-time `code`.
  3. We exchange that code for a user access token.
  4. We use that token to ask GitHub which of the app's installations
     this user can access, and which repos are in each.
"""

import requests

API = "https://api.github.com"
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return (
        f"{AUTHORIZE_URL}?client_id={client_id}"
        f"&redirect_uri={redirect_uri}&state={state}"
    )


def exchange_code_for_token(client_id: str, client_secret: str, code: str) -> str:
    resp = requests.post(
        TOKEN_URL,
        headers={"Accept": "application/json"},
        data={"client_id": client_id, "client_secret": client_secret, "code": code},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"GitHub OAuth exchange failed: {data}")
    return data["access_token"]


def get_user_login(user_token: str) -> str:
    resp = requests.get(
        f"{API}/user",
        headers={"Authorization": f"Bearer {user_token}", "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["login"]


def get_accessible_repos(user_token: str) -> list[str]:
    """All repos (as 'owner/name' strings) that this user can see through
    an installation of our app — i.e. exactly the repos we might have
    flaky-test data for that this person is allowed to look at."""
    headers = {"Authorization": f"Bearer {user_token}", "Accept": "application/vnd.github+json"}
    repos: list[str] = []

    installs_resp = requests.get(f"{API}/user/installations", headers=headers, timeout=10)
    installs_resp.raise_for_status()
    for install in installs_resp.json().get("installations", []):
        install_id = install["id"]
        page = 1
        while True:
            repos_resp = requests.get(
                f"{API}/user/installations/{install_id}/repositories",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=10,
            )
            repos_resp.raise_for_status()
            data = repos_resp.json()
            batch = data.get("repositories", [])
            repos.extend(r["full_name"] for r in batch)
            if len(batch) < 100:
                break
            page += 1

    return repos
