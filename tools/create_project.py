#!/usr/bin/env python3
"""
Deterministic Jira Project Creator
===================================
Creates a company-managed Kanban project, shares settings from INTSTA,
sets category and lead, following The Kind Kids' Handbook.

Usage:
    python3 create_project.py SHICLA "The Belgian Alliance" \\
        --pm-email "lore@statik.be" --category "Panda / Craft"
"""

import os
import sys
import ssl
import json
import argparse
import urllib.request
import urllib.error
from base64 import b64encode

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl._create_unverified_context()

JIRA_BASE = "https://statik.atlassian.net"
INTSTA_KEY = "INTSTA"
KANBAN_TEMPLATE = "com.pyxis.greenhopper.jira:gh-kanban"

# ── helpers ──────────────────────────────────────────────────────────────

def api(method: str, path: str, body: dict | None = None,
        email: str = "", token: str = "") -> tuple[int, dict]:
    url = f"{JIRA_BASE}/rest/api/3/{path.lstrip('/')}"
    auth = b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
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
        try: return e.code, json.loads(body_text)
        except json.JSONDecodeError: return e.code, {"error": body_text[:500]}
    except Exception as e:
        return 0, {"error": str(e)}

def bail(msg: str) -> None:
    print(f"  ❌ {msg}")
    sys.exit(1)

# ── steps ─────────────────────────────────────────────────────────────────

def step_lookup_user(email_addr: str, at_email: str, token: str) -> str:
    """Find Atlassian account ID from email. Returns accountId string."""
    code, data = api("GET",
        f"/user/search?query={urllib.request.quote(email_addr)}",
        email=at_email, token=token)
    if code != 200:
        bail(f"Cannot search users: HTTP {code}")
    for u in data:
        if u.get("emailAddress", "").lower() == email_addr.lower():
            return u["accountId"]
    bail(f"User not found: {email_addr}")

def step_lookup_category(name: str, email: str, token: str) -> str:
    """Find project category ID by name. Returns id string."""
    code, data = api("GET", "/projectCategory", email=email, token=token)
    if code != 200:
        bail(f"Cannot list categories: HTTP {code}")
    for c in data:
        if c.get("name", "").lower() == name.lower():
            return c["id"]
    bail(f"Category not found: {name}")

def step_get_intsta_permission_scheme(email: str, token: str) -> str:
    """Return permission scheme ID of INTSTA project."""
    code, data = api("GET", f"/project/{INTSTA_KEY}/permissionscheme",
                     email=email, token=token)
    if code != 200:
        bail(f"Cannot get INTSTA permission scheme: HTTP {code}")
    return data["id"]

def step_get_intsta_notification_scheme(email: str, token: str) -> str:
    """Return notification scheme ID of INTSTA project."""
    code, data = api("GET", f"/project/{INTSTA_KEY}/notificationscheme",
                     email=email, token=token)
    if code != 200:
        print(f"  ⚠ Cannot get INTSTA notification scheme: HTTP {code} — skipping")
        return ""
    return data["id"]

def step_create_project(key: str, name: str, lead_id: str,
                        email: str, token: str, dry_run: bool) -> dict:
    """Create the Jira project. Returns project dict."""
    if dry_run:
        print(f"  [DRY-RUN] Would create project {key}")
        return {"key": key, "id": "DRY-RUN", "name": name}

    body = {
        "key": key,
        "name": name,
        "projectTypeKey": "software",
        "projectTemplateKey": KANBAN_TEMPLATE,
        "leadAccountId": lead_id,
        "assigneeType": "PROJECT_LEAD",
    }
    code, data = api("POST", "/project", body=body, email=email, token=token)
    if code not in (200, 201):
        errs = data.get("errorMessages", [data.get("error", "?")])
        bail(f"Failed to create project: {'; '.join(errs) if isinstance(errs, list) else errs}")
    return data

def step_apply_permission_scheme(key: str, scheme_id: str,
                                  email: str, token: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY-RUN] Would apply permission scheme {scheme_id}")
        return
    code, data = api("PUT", f"/project/{key}/permissionscheme",
                     body={"id": scheme_id}, email=email, token=token)
    if code != 200:
        errs = data.get("errorMessages", [str(data)])
        bail(f"Failed to apply permission scheme: {errs}")

def step_apply_notification_scheme(key: str, scheme_id: str,
                                    email: str, token: str, dry_run: bool) -> None:
    if not scheme_id:
        return
    if dry_run:
        print(f"  [DRY-RUN] Would apply notification scheme {scheme_id}")
        return
    code, data = api("PUT", f"/project/{key}/notificationscheme",
                     body={"id": scheme_id}, email=email, token=token)
    if code != 200:
        print(f"  ⚠ Failed to apply notification scheme: HTTP {code} — skipping")

def step_set_category(key: str, category_id: str,
                      email: str, token: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [DRY-RUN] Would set category id={category_id}")
        return
    body = {"projectCategoryId": category_id}
    code, data = api("PUT", f"/project/{key}", body=body, email=email, token=token)
    if code != 200:
        errs = data.get("errorMessages", [str(data)])
        bail(f"Failed to set category: {errs}")

def step_verify(key: str, expected_name: str, email: str, token: str) -> bool:
    code, data = api("GET", f"/project/{key}", email=email, token=token)
    if code != 200:
        print(f"  ⚠ Verification failed: HTTP {code}")
        return False
    actual_name = data.get("name", "")
    if actual_name != expected_name:
        print(f"  ⚠ Name mismatch: expected '{expected_name}', got '{actual_name}'")
        return False
    return True

def step_create_epics(key: str, account_id: str,
                      email: str, token: str, dry_run: bool) -> None:
    """Create Voortraject and Implementatie Epics with account set."""
    for epic_name in ("Voortraject", "Implementatie"):
        body = {
            "fields": {
                "project": {"key": key},
                "summary": epic_name,
                "issuetype": {"name": "Epic"},
                "customfield_10001": epic_name,  # Epic Name — common field ID
            }
        }
        # Add Tempo account if provided
        if account_id:
            body["fields"]["customfield_10100"] = account_id  # Tempo Account field

        if dry_run:
            print(f"  [DRY-RUN] Would create Epic '{epic_name}' in {key}")
            continue

        code, data = api("POST", "/issue", body=body, email=email, token=token)
        if code not in (200, 201):
            print(f"  ⚠ Failed to create Epic '{epic_name}': HTTP {code}")
            continue
        issue_key = data.get("key", "?")
        print(f"  ✓ Created {issue_key}: {epic_name}")
        print(f"    ⚠ REMINDER: Run Automation via Jira UI (lightning bolt icon)")

# ── main ──────────────────────────────────────────────────────────────────

def create_project(key: str, name: str, pm_email: str, category: str,
                   email: str = "", token: str = "",
                   dry_run: bool = False, with_epics: bool = False,
                   tempo_account_id: str = "") -> bool:

    email = email or os.environ.get("ATLASSIAN_EMAIL", "")
    token = token or os.environ.get("ATLASSIAN_API_TOKEN", "")

    if not email or not token:
        print("❌ Set ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN env vars")
        return False

    # Validate key format
    if not key.isalnum() or not key.isupper():
        print(f"  ⚠ Key should be uppercase alphanumeric (e.g., SHICLA), got: {key}")
        key = key.upper()

    mode = "[DRY-RUN] " if dry_run else ""
    print(f"\n{'=' * 60}")
    print(f"{mode}Creating Jira project: {key} — {name}")
    print(f"{'=' * 60}\n")

    # 1. Look up PM
    print("1. Looking up project lead...")
    lead_id = step_lookup_user(pm_email, email, token)
    print(f"   ✓ {pm_email} → accountId {lead_id}")

    # 2. Look up category
    print("2. Looking up project category...")
    cat_id = step_lookup_category(category, email, token)
    print(f"   ✓ '{category}' → id={cat_id}")

    # 3. Get INTSTA schemes
    print("3. Fetching INTSTA permission scheme...")
    perm_scheme_id = step_get_intsta_permission_scheme(email, token)
    print(f"   ✓ Permission scheme id={perm_scheme_id}")

    notif_scheme_id = step_get_intsta_notification_scheme(email, token)
    if notif_scheme_id:
        print(f"   ✓ Notification scheme id={notif_scheme_id}")

    # 4. Create project
    print("4. Creating project...")
    proj = step_create_project(key, name, lead_id, email, token, dry_run)
    pid = proj.get("id", "?")
    print(f"   ✓ Created {proj['key']} (id={pid})")

    # 5. Apply schemes
    print("5. Applying INTSTA schemes...")
    step_apply_permission_scheme(key, perm_scheme_id, email, token, dry_run)
    print(f"   ✓ Permission scheme applied")
    if notif_scheme_id:
        step_apply_notification_scheme(key, notif_scheme_id, email, token, dry_run)
        print(f"   ✓ Notification scheme applied")

    # 6. Set category
    print("6. Setting project category...")
    step_set_category(key, cat_id, email, token, dry_run)
    print(f"   ✓ Category set to '{category}'")

    # 7. Verify
    print("7. Verifying...")
    if dry_run:
        print(f"   [DRY-RUN] Would verify")
    elif step_verify(key, name, email, token):
        print(f"   ✓ Project {key} confirmed")

    # 8. Optional Epics
    if with_epics:
        print("8. Creating Epics...")
        step_create_epics(key, tempo_account_id, email, token, dry_run)

    print(f"\n{'─' * 60}")
    print(f"✅ {key} created successfully.")
    print(f"\n📋 MANUAL STEPS REMAINING:")
    print(f"   1. Strategist: create project in Fichenbak + fill Google Sheet")
    print(f"   2. Slack: notify #nieuweprojecten")
    print(f"   3. Luk/Leen: create Tempo Customer (if new client)")
    print(f"   4. PM: create Tempo accounts via Fichenbak → Facturatie")
    print(f"   5. PM: set Default Account in Jira Project Settings")
    print(f"   6. PM: run Automation on Epics (lightning bolt icon)")
    print(f"   7. Strategist: fill PO, GL, max budget in Fichenbak")
    print(f"{'─' * 60}\n")
    return True

# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Deterministically create a Jira project",
        epilog="Follows The Kind Kids' Handbook process. Tempo + Fichenbak steps are manual."
    )
    parser.add_argument("key", help="Project key (e.g., SHICLA)")
    parser.add_argument("name", help="Full project name")
    parser.add_argument("--pm-email", required=True, help="Email of project lead")
    parser.add_argument("--category", required=True, help="Project category (e.g., 'Panda / Craft')")
    parser.add_argument("--email", default="", help="Atlassian account email")
    parser.add_argument("--token", default="", help="Atlassian API token")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--with-epics", action="store_true",
                        help="Also create Voortraject + Implementatie Epics")
    parser.add_argument("--tempo-account-id", default="",
                        help="Tempo account ID for Epics (with --with-epics)")
    args = parser.parse_args()

    ok = create_project(
        key=args.key.upper(),
        name=args.name,
        pm_email=args.pm_email,
        category=args.category,
        email=args.email,
        token=args.token,
        dry_run=args.dry_run,
        with_epics=args.with_epics,
        tempo_account_id=args.tempo_account_id,
    )
    sys.exit(0 if ok else 1)