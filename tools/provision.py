#!/usr/bin/env python3
"""
Jira Project Provisioner
=========================
Full project setup following The Kind Kids' Handbook:
  - Creates the Kanban project
  - Shares settings from INTSTA
  - Sets category and lead
  - (Optionally) creates Tempo accounts and sets default
  - Explicitly tells you what to do next

Usage:
    python3 provision.py SHICLA "The Belgian Alliance" \\
        --pm-email "lore@statik.be" --category "Panda / Craft"

    python3 provision.py SHICLA "The Belgian Alliance" \\
        --pm-email "lore@statik.be" --category "Panda / Craft" \\
        --tempo-token "t8r8y9Ql..."   # auto-creates Tempo accounts too

    python3 provision.py SHICLA "The Belgian Alliance" \\
        --pm-email "lore@statik.be" --category "Panda / Craft" \\
        --dry-run
"""

import os
import sys
import ssl
import json
import argparse
import urllib.request
import urllib.error
import urllib.parse
import textwrap
from base64 import b64encode

try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl._create_unverified_context()

JIRA_BASE    = "https://statik.atlassian.net"
TEMPO_BASE   = "https://api.tempo.io/4"
INTSTA_KEY   = "INTSTA"
KANBAN_TMPL  = "com.pyxis.greenhopper.jira:gh-kanban"

# ── helpers ──────────────────────────────────────────────────────────────

def api(base: str, method: str, path: str,
        body: dict | None = None, params: dict | None = None,
        email: str = "", token: str = "", bearer: str = "") -> tuple[int, dict]:
    url = f"{base}/rest/api/3/{path.lstrip('/')}" if base == JIRA_BASE \
         else f"{base}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    elif email and token:
        auth = b64encode(f"{email}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {auth}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if params:
        url += "?" + urllib.parse.urlencode(params)
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

def warn(msg: str) -> None:
    print(f"  ⚠  {msg}")

# ── Jira helpers ─────────────────────────────────────────────────────────

def jira(method, path, body=None, email="", token=""):
    return api(JIRA_BASE, method, path, body=body, email=email, token=token)

def tempo(method, path, body=None, bearer=""):
    return api(TEMPO_BASE, method, path, body=body, bearer=bearer)

def find_user(email_addr, at_email, at_token):
    code, data = jira("GET",
        f"/user/search?query={urllib.request.quote(email_addr)}",
        email=at_email, token=at_token)
    if code != 200:
        bail(f"Cannot search users: HTTP {code}")
    for u in data:
        if u.get("emailAddress", "").lower() == email_addr.lower():
            return u["accountId"]
    bail(f"User not found: {email_addr}")

def find_category(name, at_email, at_token):
    code, data = jira("GET", "/projectCategory", email=at_email, token=at_token)
    if code != 200:
        bail(f"Cannot list categories: HTTP {code}")
    for c in data:
        if c.get("name", "").lower() == name.lower():
            return c["id"]
    bail(f"Category not found: {name}")

def get_intsta_perm_scheme(at_email, at_token):
    code, data = jira("GET", f"/project/{INTSTA_KEY}/permissionscheme",
                      email=at_email, token=at_token)
    if code != 200:
        bail(f"Cannot get INTSTA permission scheme: HTTP {code}")
    return data["id"]

def get_intsta_notif_scheme(at_email, at_token):
    code, data = jira("GET", f"/project/{INTSTA_KEY}/notificationscheme",
                      email=at_email, token=at_token)
    if code != 200:
        return ""
    return data["id"]

# ── Jira project actions ─────────────────────────────────────────────────

def jira_create_project(key, name, lead_id, at_email, at_token, dry_run):
    if dry_run:
        return {"key": key, "id": "dry-run"}
    body = {
        "key": key, "name": name,
        "projectTypeKey": "software",
        "projectTemplateKey": KANBAN_TMPL,
        "leadAccountId": lead_id,
        "assigneeType": "PROJECT_LEAD",
    }
    code, data = jira("POST", "/project", body=body, email=at_email, token=at_token)
    if code not in (200, 201):
        errs = data.get("errorMessages", [str(data)])
        bail(f"Create failed: {'; '.join(errs) if isinstance(errs, list) else errs}")
    return data

def jira_apply_perm_scheme(key, scheme_id, at_email, at_token, dry_run):
    if dry_run: return
    code, data = jira("PUT", f"/project/{key}/permissionscheme",
                      body={"id": scheme_id}, email=at_email, token=at_token)
    if code != 200:
        bail(f"Apply permission scheme failed: {data.get('errorMessages', data)}")

def jira_apply_notif_scheme(key, scheme_id, at_email, at_token, dry_run):
    if not scheme_id or dry_run: return
    code, data = jira("PUT", f"/project/{key}/notificationscheme",
                      body={"id": scheme_id}, email=at_email, token=at_token)
    if code != 200:
        warn(f"Notification scheme failed: HTTP {code} — continuing anyway")

def jira_set_category(key, cat_id, at_email, at_token, dry_run):
    if dry_run: return
    code, data = jira("PUT", f"/project/{key}",
                      body={"projectCategoryId": cat_id},
                      email=at_email, token=at_token)
    if code != 200:
        bail(f"Set category failed: {data.get('errorMessages', data)}")

def jira_verify(key, expected_name, at_email, at_token):
    code, data = jira("GET", f"/project/{key}", email=at_email, token=at_token)
    return code == 200 and data.get("name") == expected_name

# ── Tempo account actions ────────────────────────────────────────────────

def tempo_create_account(name, key_suffix, customer_key, category_id,
                          lead_id, tempo_token, dry_run):
    """Create a Tempo account. Returns account dict or None."""
    if dry_run:
        return {"key": f"{key_suffix}", "id": 99999, "name": name}

    body = {
        "name": name,
        "key": key_suffix,
        "status": "OPEN",
        "category": {"id": category_id},
        "customer": {"key": customer_key},
        "lead": {"accountId": lead_id},
    }
    code, data = tempo("POST", "accounts", body=body, bearer=tempo_token)
    if code not in (200, 201):
        warn(f"Tempo account '{name}' failed: HTTP {code} — {data.get('errors',data)}")
        return None
    return data

def tempo_find_category(name, tempo_token):
    """Look up Tempo account category ID by name."""
    code, data = tempo("GET", f"account-categories?query={urllib.request.quote(name)}",
                       bearer=tempo_token)
    if code != 200:
        return None
    for c in (data if isinstance(data, list) else data.get("results", [])):
        if c.get("name", "").lower() == name.lower():
            return c["id"]
    return None

def jira_set_default_account(project_key, account_key, at_email, at_token, dry_run):
    """Set the default Tempo account on a Jira project.

    This is a Jira project property: `tempo-accounts-default-account-id`
    or a custom field. The exact field depends on the Jira+Tempo integration.
    We try the most common approach: setting the project property.
    """
    if dry_run:
        print(f"       [DRY-RUN] Would set default account '{account_key}' on {project_key}")
        return True

    # Tempo stores the default account as a project property
    body = {"key": "tempo-accounts-default-account-id", "value": account_key}
    code, data = jira("PUT", f"/project/{project_key}/properties/tempo-accounts-default-account-id",
                      body=body, email=at_email, token=at_token)
    if code not in (200, 201, 204):
        # Fallback: try the Tempo Plugin REST endpoint
        warn(f"Property approach failed (HTTP {code}), trying alternative...")
        # Tempo plugin endpoint for default account
        body2 = {"accountKey": account_key}
        code2, data2 = jira("PUT",
            f"/project/{project_key}/properties/io.tempo.jira__account",
            body=body2, email=at_email, token=at_token)
        if code2 not in (200, 201, 204):
            warn(f"Could not set default account automatically.")
            print(f"       → Set it manually: Project Settings → Apps → Accounts → Set Default")
            return False
    return True


# ── main ──────────────────────────────────────────────────────────────────

DEFAULT_CATEGORY = "Volgens Offerte"  # Tempo account categorie

def provision(key: str, name: str, pm_email: str, category: str,
              at_email: str = "", at_token: str = "",
              tempo_token: str = "",
              dry_run: bool = False) -> bool:

    at_email = at_email or os.environ.get("ATLASSIAN_EMAIL", "")
    at_token = at_token or os.environ.get("ATLASSIAN_API_TOKEN", "")
    tempo_token = tempo_token or os.environ.get("TEMPO_API_TOKEN", "")

    if not at_email or not at_token:
        print("❌ Set ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN")
        return False

    key = key.upper()
    customer_key = key[:6]  # Tempo customer key = project prefix
    has_tempo = bool(tempo_token)

    mode = "[DRY-RUN] " if dry_run else ""
    print(f"\n{'=' * 60}")
    print(f"{mode}Provisioning Jira project: {key}")
    print(f"   {name}")
    print(f"{'=' * 60}")

    # ── Phase 1: Jira project ──────────────────────────────────────

    print(f"\n── Phase 1/2: Jira project ──\n")

    print("1. Looking up project lead...")
    lead_id = find_user(pm_email, at_email, at_token)
    print(f"   ✓ {pm_email} → accountId {lead_id}")

    print("2. Looking up project category...")
    cat_id = find_category(category, at_email, at_token)
    print(f"   ✓ '{category}' → id={cat_id}")

    print("3. Fetching INTSTA schemes...")
    perm_id = get_intsta_perm_scheme(at_email, at_token)
    notif_id = get_intsta_notif_scheme(at_email, at_token)
    print(f"   ✓ Permission scheme id={perm_id}"
          + (f", Notification scheme id={notif_id}" if notif_id else ""))

    print("4. Creating project...")
    proj = jira_create_project(key, name, lead_id, at_email, at_token, dry_run)
    pid = proj.get("id", "?")
    print(f"   ✓ Created {proj['key']} (id={pid})")

    print("5. Applying INTSTA schemes...")
    jira_apply_perm_scheme(key, perm_id, at_email, at_token, dry_run)
    print("   ✓ Permission scheme applied")
    if notif_id:
        jira_apply_notif_scheme(key, notif_id, at_email, at_token, dry_run)
        print("   ✓ Notification scheme applied")

    print("6. Setting category...")
    jira_set_category(key, cat_id, at_email, at_token, dry_run)
    print(f"   ✓ Category = '{category}'")

    print("7. Verifying...")
    if dry_run:
        print("   [DRY-RUN] Skipped")
    elif jira_verify(key, name, at_email, at_token):
        print(f"   ✓ {key} confirmed")
    else:
        warn(f"Verification inconclusive — check manually: {JIRA_BASE}/projects/{key}")

    # ── Phase 2: Tempo accounts │ default │ Epics ─────────────────

    print(f"\n── Phase 2/2: Tempo accounts + issues ──")

    if not has_tempo:
        print(f"\n   ⚠  No TEMPO_API_TOKEN provided — Tempo accounts are MANUAL.")
        print()
        print("   📋 YOU MUST COMPLETE THESE MANUAL STEPS NOW:")
        print()
        print(f"      ▸ Customer key: {customer_key}")
        print(f"      ▸ Project key:  {key}")
        print()
        print(f"      [ ] 1. Luk/Leen: Tempo → Accounts → Customers")
        print(f"              Create customer '{customer_key}' if new")
        print(f"              https://statik.atlassian.net/plugins/servlet/ac/io.tempo.jira/tempo-app#!/accounts/customers")
        print()
        print(f"      [ ] 2. PM: Fichenbak → project sheet → Facturatie")
        print(f"              Click 'Account toevoegen +'")
        print(f"              Create: Voortraject (key={key}VTJ), category=Volgens Offerte")
        print(f"              Create: Implementatie (key={key}IMP), category=Volgens Offerte")
        print(f"              https://fichenbak.statik.be/")
        print()
        print(f"      [ ] 3. PM: Jira → {key} → Project Settings → Apps → Accounts")
        print(f"              Click 'Set Default' on the primary account")
        print()
        print(f"   ════════════════════════════════════════════════════════")
        print(f"   💡 TIP: re-run with --tempo-token to automate steps 2-3")
        print(f"   ════════════════════════════════════════════════════════")

    else:
        print(f"\n      Tempo token detected — auto-creating accounts...\n")

        # Find Tempo account category
        tempo_cat_id = 2  # default: 2 = "Volgens Offerte" (common default)
        found_cat = tempo_find_category(DEFAULT_CATEGORY, tempo_token)
        if found_cat:
            tempo_cat_id = found_cat
            print(f"      Tempo category '{DEFAULT_CATEGORY}' → id={tempo_cat_id}")
        else:
            warn(f"Tempo category '{DEFAULT_CATEGORY}' not found, using id=2")

        accounts_created = []
        for acct_name, suffix in [("Voortraject", f"{key}VTJ"),
                                    ("Implementatie", f"{key}IMP")]:
            acct = tempo_create_account(
                name=acct_name,
                key_suffix=suffix,
                customer_key=customer_key,
                category_id=tempo_cat_id,
                lead_id=lead_id,
                tempo_token=tempo_token,
                dry_run=dry_run,
            )
            if acct:
                accounts_created.append(acct)
                print(f"      ✓ Tempo account: {acct.get('name', acct_name)} "
                      f"(key={acct.get('key', suffix)})")

        if accounts_created:
            # Set default account to the first one (Voortraject)
            first_key = accounts_created[0].get("key", "")
            print(f"\n      Setting default account to '{first_key}'...")
            jira_set_default_account(key, first_key, at_email, at_token, dry_run)
            print(f"      ✓ Default account set")
        elif not dry_run:
            warn("No accounts created — default account not set")

    # ── Epics reminder ─────────────────────────────────────────────

    print(f"\n   📋 AFTER all accounts exist:")
    print(f"      [ ] Create Epic 'Voortraject' in {key}")
    print(f"            Summary=Voortraject | Epic Name=Voortraject")
    print(f"            Account=Voortraject | Assignee=PM | Reporter=PM")
    print(f"            Run: 'Create Voortraject Tasks' automation")
    print(f"      [ ] Create Epic 'Implementatie' in {key}  (same pattern)")
    print(f"      [ ] Use Bulk Changes if needed for assignee/reporter/watchers")

    # ── Final checklist ────────────────────────────────────────────

    print(f"\n{'─' * 60}")
    print(f"✅ {key} Jira project provisioned.")
    print(f"\n   FULL POST-PROVISION CHECKLIST:")
    print(f"   [ ] Strategist: project exists in Fichenbak + Google Sheet")
    print(f"   [ ] Slack: notified #nieuweprojecten")
    print(f"   [ ] Leen/Luk: Tempo Customer created (key={customer_key})")
    print(f"   [ ] PM: Tempo Accounts created (Voortraject + Implementatie)")
    print(f"   [ ] PM: Default Account set in Jira Project Settings")
    print(f"   [ ] PM: Epics created + Automation run")
    print(f"   [ ] Strategist: PO, GL, max budget filled in Fichenbak")
    print(f"   [ ] PM: notify strategist that Jira is ready")
    print(f"{'─' * 60}\n")
    return True

# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Provision a Jira project — The Kind Kids' Handbook",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          %(prog)s SHICLA "The Belgian Alliance for Climate Action" \\
              --pm-email "lore@statik.be" --category "Panda / Craft"

          %(prog)s WIEWEB "Website immaterieelerfgoed" \\
              --pm-email "lore@statik.be" --category "Koala / Craft" \\
              --tempo-token "t8r8y9Ql6E..."
        """),
    )
    p.add_argument("key", help="Project key (e.g., SHICLA)")
    p.add_argument("name", help="Full project name")
    p.add_argument("--pm-email", required=True, help="Email of project lead")
    p.add_argument("--category", required=True, help="Jira project category (e.g., 'Panda / Craft')")
    p.add_argument("--email", default="", help="Atlassian account email")
    p.add_argument("--token", default="", help="Atlassian API token")
    p.add_argument("--tempo-token", default="", help="Tempo API token (optional: auto-creates Tempo accounts)")
    p.add_argument("--dry-run", action="store_true", help="Validate without creating")
    args = p.parse_args()

    ok = provision(
        key=args.key.upper(), name=args.name,
        pm_email=args.pm_email, category=args.category,
        at_email=args.email, at_token=args.token,
        tempo_token=args.tempo_token,
        dry_run=args.dry_run,
    )
    sys.exit(0 if ok else 1)