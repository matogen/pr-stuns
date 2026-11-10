#!/usr/bin/env python3
"""One-shot migration: extract secrets from config.yaml into users.yml.

After running this:
  • config.yaml has no secrets — only references (`username:` for Bitbucket,
    `user:` for GitHub).
  • users.yml owns every credential. Existing ``api_token`` values in
    users.yml are preserved.
  • config.yaml.bak holds the pre-migration copy.

Usage:
  python migrate_secrets.py            # rewrite in place (with backup)
  python migrate_secrets.py --dry-run  # show what would change, no writes
"""

import argparse
import os
import re
import shutil
import sys
from typing import Dict, Any, Tuple

import yaml


CONFIG_PATH = "config.yaml"
USERS_PATH = "users.yml"
BACKUP_PATH = "config.yaml.bak"


def _derive_github_key(owner: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", owner).strip("-").lower()
    return slug or "github-user"


def _scan_config(cfg: Dict[str, Any], existing_users: Dict[str, Any]
                 ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Walk repos and build (new_users_dict, github_token_to_key_map).

    Existing api_tokens in users.yml are preserved. GitHub tokens that already
    exist in users.yml reuse their original key.
    """
    new_users: Dict[str, Any] = {}
    gh_token_to_key: Dict[str, str] = {}

    # Pre-seed gh_token_to_key from existing users.yml so we reuse keys.
    for key, entry in existing_users.items():
        tok = (entry.get("github") or {}).get("token")
        if tok:
            gh_token_to_key[tok] = key

    for repo in cfg.get("repositories", []):
        rtype = (repo.get("type") or "github").lower()

        if rtype == "bitbucket":
            for entry in [repo] + list(repo.get("fallback_users") or []):
                user = entry.get("username")
                if not user:
                    continue
                pw = entry.get("app_password")
                bucket = new_users.setdefault(user, {}).setdefault("bitbucket", {})
                # Preserve existing api_token if user already in users.yml
                existing_bb = (existing_users.get(user) or {}).get("bitbucket") or {}
                if existing_bb.get("api_token") and "api_token" not in bucket:
                    bucket["api_token"] = existing_bb["api_token"]
                elif "api_token" not in bucket:
                    bucket["api_token"] = ""
                if pw:
                    bucket["app_password"] = pw

        elif rtype == "github":
            tok = repo.get("token")
            if not tok:
                continue
            key = gh_token_to_key.get(tok) or _derive_github_key(repo.get("owner", ""))
            gh_token_to_key[tok] = key
            new_users.setdefault(key, {}).setdefault("github", {})["token"] = tok

    # Carry over any users.yml entries we didn't see in config (don't lose data).
    for key, entry in existing_users.items():
        new_users.setdefault(key, entry)

    return new_users, gh_token_to_key


def _rewrite_config_lines(text: str, gh_token_to_key: Dict[str, str]) -> Tuple[str, int, int]:
    """Strip Bitbucket app_password lines and replace GitHub token lines.

    Line-based to preserve every comment, blank line, and quirk in config.yaml.
    """
    lines = text.splitlines(keepends=True)
    out = []
    current_type = None
    removed_passwords = 0
    replaced_tokens = 0

    type_re = re.compile(r"^\s*-\s+type:\s*(\w+)")
    pw_re = re.compile(r"^\s*app_password:\s*\S+")
    tok_re = re.compile(r"^(\s*)token:\s*(\S+)\s*(#.*)?$")
    # In commented-out (documentation) blocks, just neuter the secret values
    # so the blocks remain as readable templates.
    secret_in_comment_re = re.compile(r"(ATBB[A-Za-z0-9]+|ATATT[A-Za-z0-9_-]+|ghp_[A-Za-z0-9]+)")

    for line in lines:
        m_type = type_re.match(line)
        if m_type:
            current_type = m_type.group(1).lower()
            out.append(line)
            continue

        if pw_re.match(line):
            removed_passwords += 1
            continue

        m_tok = tok_re.match(line)
        if m_tok and current_type == "github":
            indent, tok, _ = m_tok.group(1), m_tok.group(2), m_tok.group(3)
            key = gh_token_to_key.get(tok)
            if not key:
                raise SystemExit(
                    f"No user key for GitHub token {tok[:12]}...; "
                    f"add it to {USERS_PATH} first."
                )
            out.append(f"{indent}user: {key}\n")
            replaced_tokens += 1
            continue

        if line.lstrip().startswith("#") and secret_in_comment_re.search(line):
            line = secret_in_comment_re.sub("<see users.yml>", line)

        out.append(line)

    return "".join(out), removed_passwords, replaced_tokens


def _dump_users(users: Dict[str, Any]) -> str:
    """Render users.yml with a header banner and stable ordering."""
    header = (
        "# ─────────────────────────────────────────────────────────────────────\n"
        "# users.yml — credential store for repository hosts\n"
        "#\n"
        "# Reference these entries from config.yaml:\n"
        "#   • Bitbucket repos: set `username:` to the user key below.\n"
        "#   • GitHub repos:    set `user:`     to the user key below.\n"
        "#\n"
        "# Inline secrets in config.yaml still take precedence (handy during\n"
        "# migration), but config.yaml should not contain secrets.\n"
        "#\n"
        "# WARNING: this file contains secrets. Keep it out of version control.\n"
        "# ─────────────────────────────────────────────────────────────────────\n\n"
    )
    body = yaml.safe_dump({"users": users}, sort_keys=True, default_flow_style=False)
    return header + body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--users", default=USERS_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"error: {args.config} not found", file=sys.stderr)
        return 1

    with open(args.config) as f:
        cfg_text = f.read()
    cfg = yaml.safe_load(cfg_text) or {}

    existing_users: Dict[str, Any] = {}
    if os.path.exists(args.users):
        with open(args.users) as f:
            existing_users = (yaml.safe_load(f) or {}).get("users", {}) or {}

    new_users, gh_token_to_key = _scan_config(cfg, existing_users)
    new_cfg_text, removed_pw, replaced_tok = _rewrite_config_lines(cfg_text, gh_token_to_key)

    print(f"Bitbucket users found: {sum(1 for u in new_users.values() if 'bitbucket' in u)}")
    print(f"GitHub users found:    {sum(1 for u in new_users.values() if 'github' in u)}")
    print(f"app_password lines to remove: {removed_pw}")
    print(f"token lines to replace:       {replaced_tok}")

    if args.dry_run:
        print("\n--dry-run: no files written")
        return 0

    if os.path.exists(args.config):
        shutil.copy2(args.config, BACKUP_PATH)
        print(f"backed up {args.config} → {BACKUP_PATH}")

    with open(args.users, "w") as f:
        f.write(_dump_users(new_users))
    print(f"wrote {args.users}")

    with open(args.config, "w") as f:
        f.write(new_cfg_text)
    print(f"wrote {args.config}")

    leftover = re.findall(r"(?:app_password|ghp_|ATBB|ATATT)\S{6,}", new_cfg_text)
    if leftover:
        print(f"\nWARNING: possible secrets remain in {args.config}: {len(leftover)} hits")
        for s in leftover[:5]:
            print(f"  {s[:30]}...")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
