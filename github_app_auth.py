"""
github_app_auth.py — GitHub App authentication helpers.

A GitHub App authenticates two ways:
1. As the app itself, using a JWT signed with the app's private key
   (needed to ask GitHub for an installation token).
2. As a specific installation (one user/org that installed the app),
   using a short-lived installation access token fetched with that JWT.

This is what replaces asking every user for a personal access token —
the app authenticates itself, and GitHub scopes each token to only the
repos that installation was granted access to.
"""

import time

import jwt
import requests

API = "https://api.github.com"


def make_app_jwt(app_id: str, private_key_pem: bytes) -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,  # allow for clock drift
        "exp": now + 9 * 60,  # GitHub caps this at 10 minutes
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def get_installation_token(app_id: str, private_key_pem: bytes, installation_id: int) -> str:
    """Exchange the app's JWT for a short-lived (1 hour) token scoped to
    one installation. This is what collector logic authenticates with —
    never the app's own JWT, and never a user's personal token."""
    app_jwt = make_app_jwt(app_id, private_key_pem)
    resp = requests.post(
        f"{API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]
