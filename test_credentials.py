#!/usr/bin/env python3
"""Test every credential in users.yml against the relevant host API.

GitHub  : GET https://api.github.com/user with Bearer <token>
Bitbucket api_token : GET https://api.bitbucket.org/2.0/user with Basic <email:token>
Bitbucket app_password: GET https://api.bitbucket.org/2.0/user with Basic <user:pass>

Prints a per-user/per-platform OK / FAIL summary and exits non-zero if any fail.
"""

import sys
from pathlib import Path

import requests
import yaml


GITHUB_URL = "https://api.github.com/user"
BITBUCKET_URL = "https://api.bitbucket.org/2.0/user"
TIMEOUT = 15


def check_github(token: str):
    r = requests.get(
        GITHUB_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=TIMEOUT,
    )
    if r.ok:
        login = r.json().get("login", "?")
        return True, f"login={login}"
    return False, f"HTTP {r.status_code}: {r.text.strip()[:200]}"


def check_bitbucket_api_token(email: str, token: str):
    """Atlassian API tokens use Basic auth with email:token, not Bearer.

    https://support.atlassian.com/bitbucket-cloud/docs/using-api-tokens/
    """
    if not email:
        return False, "no email configured (api_token requires email for Basic auth)"
    r = requests.get(BITBUCKET_URL, auth=(email, token), timeout=TIMEOUT)
    if r.ok:
        data = r.json()
        return True, f"username={data.get('username') or data.get('nickname') or '?'}"
    return False, f"HTTP {r.status_code}: {r.text.strip()[:200]}"


def check_bitbucket_basic(username: str, app_password: str):
    r = requests.get(BITBUCKET_URL, auth=(username, app_password), timeout=TIMEOUT)
    if r.ok:
        data = r.json()
        return True, f"username={data.get('username') or data.get('nickname') or '?'}"
    return False, f"HTTP {r.status_code}: {r.text.strip()[:200]}"


def main(users_path: str = "users.yml") -> int:
    path = Path(users_path)
    if not path.exists():
        print(f"users file not found: {users_path}", file=sys.stderr)
        return 2

    data = yaml.safe_load(path.read_text()) or {}
    users = data.get("users", {}) or {}
    if not users:
        print("no users defined", file=sys.stderr)
        return 2

    results = []  # (user, platform, mode, ok, detail)

    for user_name, platforms in users.items():
        platforms = platforms or {}

        gh = (platforms.get("github") or {}).get("token")
        if gh:
            ok, detail = check_github(gh)
            results.append((user_name, "github", "token", ok, detail))

        bb = platforms.get("bitbucket") or {}
        api_token = bb.get("api_token")
        app_password = bb.get("app_password")
        email = bb.get("email")

        if api_token:
            ok, detail = check_bitbucket_api_token(email, api_token)
            results.append((user_name, "bitbucket", "api_token", ok, detail))

        if app_password:
            # No username field in users.yml — try the user key as the username
            # (matches how config.yaml references these entries).
            ok, detail = check_bitbucket_basic(user_name, app_password)
            results.append((user_name, "bitbucket", "app_password", ok, detail))

        if not gh and not api_token and not app_password:
            results.append((user_name, "-", "-", False, "no credentials defined"))

    # Print report
    width_user = max(len(r[0]) for r in results)
    width_plat = max(len(r[1]) for r in results)
    width_mode = max(len(r[2]) for r in results)

    print()
    print(f"{'USER':<{width_user}}  {'PLATFORM':<{width_plat}}  {'MODE':<{width_mode}}  STATUS  DETAIL")
    print("-" * (width_user + width_plat + width_mode + 30))
    for user, platform, mode, ok, detail in results:
        status = "OK  " if ok else "FAIL"
        print(f"{user:<{width_user}}  {platform:<{width_plat}}  {mode:<{width_mode}}  {status}    {detail}")

    failed = [r for r in results if not r[3]]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} credentials OK, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "users.yml"))
