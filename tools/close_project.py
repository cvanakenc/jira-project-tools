#!/usr/bin/env python3
"""
Deterministic Jira Project Closer
==================================
Archives a Jira project by applying the "Archived Scheme / STATIK"
permission scheme, following The Kind Kids' Handbook process.

Usage:
    python3 close_project.py SHICLA
    python3 close_project.py SHICLA --dry-run
    python3 close_project.py SHICLA --force
"""

import os
import sys
import ssl
import json
import argparse
import urllib.request
import urllib.error
from base64 import b64encode

# macOS Python may lack root certificates; use certifi if available, else disable verification
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl._create_unverified_context()

JIRA_BASE = "https://statik.atlassian.net"
ARCHIVED_SCHEME_NAME = "Archived Scheme / STATIK"

# ── helpers ──────────────────────────────────────────────────────────────

def api(method: str, path: str, body: dict | None = None,
        email: str = "", token: str = "") -> tuple[int, dict]:
    """Call Jira REST API v3. Returns (status_code, parsed_json)."""
    url = f"{JIRA_BASE}/rest/api/3/{path.lstrip('/')}"
    auth = b64encode(f"{email}:{token}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace") if e.fp else ""
        try:
            return e.code, json.loads(body_text)
        except json.JSONDecodeError:
            return e.code, {"error": body_text[:500]}
    except Exception as e:
        return 0, {"error": str(e)}

# ── steps ─────────────────────────────────────────────────────────────────

def step_1_fetch_project(key: str, email: str, token: str) -> dict | None:
    """Verify project exists and return its info."""
    code, data = api("GET", f"/project/{key}", email=email, token=token)
    if code == 404:
        print(f"  ❌ Project {key} not found")
        return None
    if code != 200:
        print(f"  ❌ Failed to fetch project: HTTP {code} — {data.get('errorMessages', data.get('error', ''))}")
        return None
    return data

def step_2_check_scheme(key: str, email: str, token: str) -> dict | None:
    """Get current permission scheme."""
    code, data = api("GET", f"/project/{key}/permissionscheme",
                     email=email, token=token)
    if code != 200:
        print(f"  ❌ Failed to get permission scheme: HTTP {code}")
        return None
    return data

def step_3_count_unresolved(key: str, email: str, token: str) -> int:
    """Return number of unresolved issues."""
    jql = f"resolution = Unresolved AND project = {key}"
    body = {"jql": jql, "maxResults": 1}
    code, data = api("POST", "/search/jql", body=body,
                     email=email, token=token)
    if code != 200:
        print(f"  ⚠ Could not count unresolved issues: HTTP {code}")
        return -1
    return data.get("total", 0)  # total is accurate even with maxResults=1

def step_4_find_archived_scheme(email: str, token: str) -> dict | None:
    """Look up the archived permission scheme by name."""
    code, data = api("GET", "/permissionscheme", email=email, token=token)
    if code != 200:
        print(f"  ❌ Failed to list permission schemes: HTTP {code}")
        return None
    schemes = data.get("values") or data.get("permissionSchemes") or []
    for s in schemes:
        if s["name"] == ARCHIVED_SCHEME_NAME:
            return s
    print(f"  ❌ Scheme '{ARCHIVED_SCHEME_NAME}' not found")
    return None

def step_5_apply_scheme(key: str, scheme_id: str, email: str, token: str,
                        dry_run: bool = False) -> bool:
    """Apply the archived permission scheme."""
    if dry_run:
        print(f"  [DRY-RUN] Would PUT scheme id={scheme_id} on {key}")
        return True
    code, data = api("PUT", f"/project/{key}/permissionscheme",
                     body={"id": scheme_id}, email=email, token=token)
    if code != 200:
        print(f"  ❌ Failed to apply scheme: HTTP {code} — {data}")
        return False
    return True

def step_6_verify(key: str, email: str, token: str) -> bool:
    """Confirm the scheme was applied."""
    scheme = step_2_check_scheme(key, email, token)
    if not scheme:
        return False
    if scheme["name"] == ARCHIVED_SCHEME_NAME:
        print(f"  ✅ Verified: '{ARCHIVED_SCHEME_NAME}' is active")
        return True
    print(f"  ❌ Scheme is still '{scheme['name']}', expected '{ARCHIVED_SCHEME_NAME}'")
    return False

# ── main ──────────────────────────────────────────────────────────────────

def close_project(key: str, email: str = "", token: str = "",
                  dry_run: bool = False, force: bool = False) -> bool:
    """Run the full deterministic close process. Returns True on success."""

    email = email or os.environ.get("ATLASSIAN_EMAIL", "")
    token = token or os.environ.get("ATLASSIAN_API_TOKEN", "")

    if not email or not token:
        print("❌ Missing credentials. Set ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN")
        print("   or pass --email and --token arguments.")
        return False

    mode = "[DRY-RUN] " if dry_run else ""
    print(f"\n{'=' * 60}")
    print(f"{mode}Closing Jira project: {key}")
    print(f"{'=' * 60}\n")

    # Step 1 — Fetch project
    print("1. Fetching project...")
    proj = step_1_fetch_project(key, email, token)
    if not proj:
        return False
    lead = proj.get("lead", {}).get("displayName", "?")
    cat = proj.get("projectCategory", {}).get("name", "?")
    print(f"   ✓ {key} — {proj.get('name','?')}")
    print(f"     Lead: {lead} | Category: {cat}")

    # Step 2 — Check current scheme
    print("2. Checking current permission scheme...")
    scheme = step_2_check_scheme(key, email, token)
    if not scheme:
        return False
    print(f"   Current: {scheme['name']}")
    if scheme["name"] == ARCHIVED_SCHEME_NAME:
        print(f"   ⚠ Project is ALREADY archived. Nothing to do.")
        return True

    # Step 3 — Count unresolved issues
    print("3. Counting unresolved issues...")
    unresolved = step_3_count_unresolved(key, email, token)
    if unresolved < 0:
        return False  # API error, already printed
    print(f"   Unresolved issues: {unresolved}")
    if unresolved > 0 and not force:
        print(f"   ❌ Cannot close: {unresolved} unresolved issue(s) remain.")
        print(f"      Use --force to override, or resolve/close them first.")
        return False
    if unresolved > 0 and force:
        print(f"   ⚠ --force: ignoring {unresolved} unresolved issue(s)")

    # Step 4 — Find archived scheme
    print("4. Looking up archived scheme...")
    archived = step_4_find_archived_scheme(email, token)
    if not archived:
        return False
    print(f"   Found: {archived['name']} (id={archived['id']})")

    # Step 5 — Apply
    print("5. Applying archived permission scheme...")
    if not step_5_apply_scheme(key, archived["id"], email, token, dry_run):
        return False
    print(f"   ✓ Archived scheme applied to {key}")

    # Step 6 — Verify
    print("6. Verifying...")
    if dry_run:
        print(f"   [DRY-RUN] Would verify scheme")
    elif not step_6_verify(key, email, token):
        return False

    print(f"\n{'─' * 60}")
    print(f"✅ {key} archived successfully.")
    print(f"   No new issues can be created. Project hidden from boards/search.")
    print(f"   Accessible to Administrators group only.")
    print(f"{'─' * 60}\n")
    return True

# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Deterministically archive a Jira project",
        epilog="Follows The Kind Kids' Handbook process.",
    )
    parser.add_argument("project", help="Jira project key (e.g., SHICLA)")
    parser.add_argument("--email", default="", help="Atlassian account email")
    parser.add_argument("--token", default="", help="Atlassian API token")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check readiness without making changes")
    parser.add_argument("--force", action="store_true",
                        help="Archive even if unresolved issues exist")
    parser.add_argument("--json", action="store_true",
                        help="Output result as JSON (for scripting)")
    args = parser.parse_args()

    success = close_project(
        key=args.project.upper(),
        email=args.email,
        token=args.token,
        dry_run=args.dry_run,
        force=args.force,
    )

    if args.json:
        print(json.dumps({"project": args.project.upper(), "archived": success}))

    sys.exit(0 if success else 1)