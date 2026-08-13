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
AGILE_BASE   = "https://statik.atlassian.net/rest/agile/1.0"
GH_BASE      = "https://statik.atlassian.net/rest/greenhopper/1.0"
TEMPO_BASE   = "https://api.tempo.io/4"
INTSTA_KEY   = "INTSTA"
KANBAN_TMPL  = "com.pyxis.greenhopper.jira:gh-kanban-template"
# Forge field from the Productive app — holds the Productive budget (deal) id.
PRODUCTIVE_BUDGET_FIELD = "Productive Budget"

# ── helpers ──────────────────────────────────────────────────────────────

def api(base: str, method: str, path: str,
        body: dict | None = None, params: dict | None = None,
        email: str = "", token: str = "", bearer: str = "",
        extra_headers: dict | None = None) -> tuple[int, dict]:
    url = f"{base}/rest/api/3/{path.lstrip('/')}" if base == JIRA_BASE \
         else f"{base}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
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

def get_intsta_workflow_scheme(at_email, at_token):
    """INTSTA's workflow scheme ('STATIK / Classic Default Workflow Scheme').

    Its initial status is New — the Kanban template's own generated workflow
    starts issues in Backlog instead, which is not what we want.
    """
    code, proj = jira("GET", f"/project/{INTSTA_KEY}", email=at_email, token=at_token)
    if code != 200:
        return "", ""
    code, data = jira("GET", f"/workflowscheme/project?projectId={proj['id']}",
                      email=at_email, token=at_token)
    if code != 200 or not data.get("values"):
        return "", ""
    ws = data["values"][0]["workflowScheme"]
    return str(ws["id"]), ws.get("name", "")

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
    code, data = jira("PUT", f"/project/{key}",
                      body={"notificationScheme": int(scheme_id)},
                      email=at_email, token=at_token)
    if code != 200:
        warn(f"Notification scheme failed: HTTP {code} — continuing anyway")

def jira_apply_workflow_scheme(project_id, scheme_id, at_email, at_token, dry_run):
    """Assign a shared workflow scheme. Only works while the project is empty —
    Jira rejects this with 'Only empty projects can have workflow schemes
    assigned' once issues exist, so it has to happen right after creation.

    Heads-up: this is the SHARED scheme, used by ~586 projects. That is the
    Statik convention (and how the project gets New instead of Backlog as its
    initial status), but it also means Project settings -> Workflows in this
    project edits the workflow of every other project, with no warning from
    Jira. On 2026-08-13 that removed Testing/Selected/Approved/To Analyse/
    More input instance-wide and force-migrated ~3600 issues. Use
    --skip-workflow if you want the project isolated instead.
    """
    if dry_run or not scheme_id:
        return False
    code, data = jira("PUT", "/workflowscheme/project",
                      body={"workflowSchemeId": str(scheme_id),
                            "projectId": str(project_id)},
                      email=at_email, token=at_token)
    if code not in (200, 204):
        warn(f"Workflow scheme failed: HTTP {code} — {data.get('errorMessages', data)}")
        print("       → New issues will start in Backlog instead of New.")
        return False
    return True

def find_board(key, at_email, at_token):
    code, data = api(AGILE_BASE, "GET", f"board?projectKeyOrId={key}",
                     email=at_email, token=at_token)
    if code != 200 or not data.get("values"):
        return None
    return data["values"][0]["id"]

def get_board_columns(board_id, at_email, at_token):
    code, data = api(AGILE_BASE, "GET", f"board/{board_id}/configuration",
                     email=at_email, token=at_token)
    if code != 200:
        return None
    return [(c["name"], [s["id"] for s in c["statuses"]])
            for c in data["columnConfig"]["columns"]]

def jira_mirror_board_columns(key, at_email, at_token, dry_run):
    """Copy INTSTA's board column layout onto the new project's board.

    Swapping the workflow scheme leaves the generated board mapped to statuses
    the new workflow doesn't have (Selected for Development, Done), so issues in
    the real statuses become invisible. Board columns aren't writable through the
    public Agile API — this uses the internal greenhopper endpoint.
    """
    if dry_run:
        return False

    src = find_board(INTSTA_KEY, at_email, at_token)
    dst = find_board(key, at_email, at_token)
    if not src or not dst:
        warn("Board not found — column mapping skipped.")
        return False

    layout = get_board_columns(src, at_email, at_token)
    if not layout:
        warn("Could not read INTSTA board columns — mapping skipped.")
        return False

    # The generated Kanban board has a backlog column (no statuses); INTSTA's
    # doesn't. Keep ours and append INTSTA's columns after it.
    current = get_board_columns(dst, at_email, at_token) or []
    has_kanplan = bool(current and not current[0][1])

    mapped = []
    if has_kanplan:
        mapped.append({"name": "Backlog", "mappedStatuses": [],
                       "min": "", "max": "", "isKanPlanColumn": True})
    for name, statuses in layout:
        if not statuses:      # INTSTA's own backlog column, if it ever gains one
            continue
        mapped.append({"name": name,
                       "mappedStatuses": [{"id": s} for s in statuses],
                       "min": "", "max": "", "isKanPlanColumn": False})

    code, data = api(GH_BASE, "PUT", "rapidviewconfig/columns",
                     body={"currentStatisticsField": {"id": "issueCount_"},
                           "rapidViewId": dst, "mappedColumns": mapped},
                     email=at_email, token=at_token,
                     extra_headers={"X-Atlassian-Token": "no-check"})
    if code != 200:
        warn(f"Board columns failed: HTTP {code}")
        print(f"       → Map them manually: board {dst} → Settings → Columns")
        return False
    return True

def find_field_id(name, at_email, at_token):
    code, data = jira("GET", "/field", email=at_email, token=at_token)
    if code != 200:
        return ""
    for f in data:
        if f.get("name", "").lower() == name.lower():
            return f["id"]
    return ""

def project_screen_ids(project_id, at_email, at_token):
    """Every screen reachable from a project, via its issue type screen scheme."""
    code, data = jira("GET", f"/issuetypescreenscheme/project?projectId={project_id}",
                      email=at_email, token=at_token)
    if code != 200 or not data.get("values"):
        return []
    itss = data["values"][0]["issueTypeScreenScheme"]["id"]
    code, data = jira("GET", f"/issuetypescreenscheme/mapping?issueTypeScreenSchemeId={itss}",
                      email=at_email, token=at_token)
    if code != 200:
        return []
    ss_ids = {str(m["screenSchemeId"]) for m in data.get("values", [])}
    if not ss_ids:
        return []
    qs = "&".join(f"id={i}" for i in sorted(ss_ids))
    code, data = jira("GET", f"/screenscheme?{qs}&maxResults=100",
                      email=at_email, token=at_token)
    if code != 200:
        return []
    screens = set()
    for ss in data.get("values", []):
        screens.update(str(s) for s in ss.get("screens", {}).values())
    return sorted(screens)

def jira_add_field_to_screens(key, project_id, field_name,
                              at_email, at_token, dry_run):
    """Put a custom field on the project's own screens.

    Company-managed projects created from a template get dedicated screens named
    '<KEY>: ...'. Anything else is shared with other projects — editing it would
    change them too, so those are skipped loudly rather than silently touched.
    """
    if dry_run:
        return None
    field_id = find_field_id(field_name, at_email, at_token)
    if not field_id:
        warn(f"Field '{field_name}' not found — add it to {key}'s screens manually.")
        return False
    screen_ids = project_screen_ids(project_id, at_email, at_token)
    if not screen_ids:
        warn(f"No screens resolved for {key} — add '{field_name}' manually.")
        return False
    code, screens = jira("GET", f"/screens?queryString={key}&maxResults=100",
                         email=at_email, token=at_token)
    owned = {str(s["id"]) for s in screens.get("values", [])
             if s.get("name", "").startswith(f"{key}:")} if code == 200 else set()
    added = 0
    for sid in screen_ids:
        if sid not in owned:
            warn(f"Screen {sid} is shared with other projects — skipped.")
            continue
        code, tabs = jira("GET", f"/screens/{sid}/tabs", email=at_email, token=at_token)
        if code != 200 or not tabs:
            warn(f"Screen {sid}: cannot list tabs (HTTP {code})")
            continue
        tab_id = tabs[0]["id"]
        code, existing = jira("GET", f"/screens/{sid}/tabs/{tab_id}/fields",
                              email=at_email, token=at_token)
        if code == 200 and any(f.get("id") == field_id for f in existing):
            added += 1
            continue
        code, data = jira("POST", f"/screens/{sid}/tabs/{tab_id}/fields",
                          body={"fieldId": field_id}, email=at_email, token=at_token)
        if code in (200, 201):
            added += 1
        else:
            warn(f"Screen {sid}: adding '{field_name}' failed (HTTP {code}) — {data}")
    return added == len(screen_ids)

def jira_epic_type_id(key, at_email, at_token):
    code, data = jira("GET", f"/issue/createmeta/{key}/issuetypes",
                      email=at_email, token=at_token)
    if code != 200:
        return ""
    for t in data.get("issueTypes", []):
        if t.get("name", "").lower() == "epic":
            return t["id"]
    return ""

def jira_project_issues(key, at_email, at_token):
    """Every issue key in the project. Empty right after provisioning."""
    code, data = api(JIRA_BASE, "GET", "/search/jql",
                     params={"jql": f"project = {key}", "fields": "summary",
                             "maxResults": 200},
                     email=at_email, token=at_token)
    if code != 200:
        return []
    return [(i["key"], i["fields"]["summary"]) for i in data.get("issues", [])]

def jira_create_epics(key, lead_id, budget_id, at_email, at_token, dry_run):
    """Create the handbook's two standard epics, stamped with the budget.

    The budget goes in at creation time rather than via a follow-up stamp pass:
    Jira's search index lags issue creation by seconds, so a JQL sweep run
    immediately afterwards would not see these epics yet.
    """
    if dry_run:
        return []
    type_id = jira_epic_type_id(key, at_email, at_token)
    if not type_id:
        warn(f"No Epic issue type on {key} — create the epics manually.")
        return []
    field_id = find_field_id(PRODUCTIVE_BUDGET_FIELD, at_email, at_token)
    existing = {s for _, s in jira_project_issues(key, at_email, at_token)}
    created = []
    for summary in ("Voortraject", "Implementatie"):
        if summary in existing:
            print(f"   • Epic '{summary}' already exists — skipped")
            continue
        fields = {"project": {"key": key}, "issuetype": {"id": type_id},
                  "summary": summary,
                  "assignee": {"id": lead_id}, "reporter": {"id": lead_id}}
        if field_id and budget_id:
            fields[field_id] = budget_id
        code, data = jira("POST", "/issue", body={"fields": fields},
                          email=at_email, token=at_token)
        if code in (200, 201):
            created.append(data["key"])
        else:
            warn(f"Epic '{summary}' failed (HTTP {code}) — {data}")
    return created

def jira_stamp_budget(key, budget_id, at_email, at_token, dry_run):
    """Set the Productive budget on every issue in the project."""
    if dry_run:
        return 0
    field_id = find_field_id(PRODUCTIVE_BUDGET_FIELD, at_email, at_token)
    if not field_id:
        warn(f"Field '{PRODUCTIVE_BUDGET_FIELD}' not found — set the budget manually.")
        return 0
    done = 0
    for issue_key, _ in jira_project_issues(key, at_email, at_token):
        code, data = jira("PUT", f"/issue/{issue_key}",
                          body={"fields": {field_id: budget_id}},
                          email=at_email, token=at_token)
        if code in (200, 204):
            done += 1
        else:
            warn(f"{issue_key}: budget not set (HTTP {code}) — {data}")
    return done

def jira_set_category(key, cat_id, at_email, at_token, dry_run):
    if dry_run: return
    code, data = jira("PUT", f"/project/{key}",
                      body={"categoryId": int(cat_id)},
                      email=at_email, token=at_token)
    if code != 200:
        bail(f"Set category failed: {data.get('errorMessages', data)}")

def jira_verify(key, expected_name, at_email, at_token):
    code, data = jira("GET", f"/project/{key}", email=at_email, token=at_token)
    return code == 200 and data.get("name") == expected_name

# ── Tempo account actions ────────────────────────────────────────────────

def tempo_create_account(name, key_suffix, customer_key, category_key,
                          lead_id, tempo_token, dry_run):
    """Create a Tempo account. Returns account dict or None."""
    if dry_run:
        return {"key": f"{key_suffix}", "id": 99999, "name": name}

    # Tempo v4 POST /accounts expects FLAT fields (leadAccountId / categoryKey /
    # customerKey), not the nested objects returned by GET. Nested objects yield
    # "No lead supplied".
    body = {
        "name": name,
        "key": key_suffix,
        "status": "OPEN",
        "categoryKey": category_key,
        "customerKey": customer_key,
        "leadAccountId": lead_id,
    }
    code, data = tempo("POST", "accounts", body=body, bearer=tempo_token)
    if code not in (200, 201):
        warn(f"Tempo account '{name}' failed: HTTP {code} — {data.get('errors',data)}")
        return None
    return data

def tempo_find_category(name, tempo_token):
    """Look up a Tempo account category KEY by name (e.g. 'Volgens Offerte' → 'VOF')."""
    code, data = tempo("GET", f"account-categories?query={urllib.request.quote(name)}",
                       bearer=tempo_token)
    if code != 200:
        return None
    for c in (data if isinstance(data, list) else data.get("results", [])):
        if c.get("name", "").lower() == name.lower():
            return c["key"]
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

def backfill_budget(key: str, pm_email: str, budget_id: int,
                    at_email: str = "", at_token: str = "",
                    dry_run: bool = False) -> bool:
    """Apply a Productive budget to a project that already exists.

    The budget is usually created in Productive only after the Jira project is
    provisioned, so it cannot be passed on the original run.
    """
    at_email = at_email or os.environ.get("ATLASSIAN_EMAIL", "")
    at_token = at_token or os.environ.get("ATLASSIAN_API_TOKEN", "")
    if not at_email or not at_token:
        print("❌ Set ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN")
        return False

    mode = "[DRY-RUN] " if dry_run else ""
    print(f"\n{mode}Applying Productive budget {budget_id} to {key}\n")

    code, proj = jira("GET", f"/project/{key}", email=at_email, token=at_token)
    if code != 200:
        bail(f"Project {key} not found (HTTP {code})")
    lead_id = find_user(pm_email, at_email, at_token)

    if dry_run:
        print(f"   [DRY-RUN] Would stamp every issue in {key} and create missing epics")
        return True

    if jira_add_field_to_screens(key, proj["id"], PRODUCTIVE_BUDGET_FIELD,
                                 at_email, at_token, dry_run):
        print(f"   ✓ '{PRODUCTIVE_BUDGET_FIELD}' present on {key}'s screens")
    for k in jira_create_epics(key, lead_id, budget_id, at_email, at_token, dry_run):
        print(f"   ✓ Epic {k} created with budget {budget_id}")
    stamped = jira_stamp_budget(key, budget_id, at_email, at_token, dry_run)
    print(f"   ✓ Budget {budget_id} set on {stamped} issue(s)")
    print(f"\n   {JIRA_BASE}/projects/{key}\n")
    return True

def provision(key: str, name: str, pm_email: str, category: str,
              at_email: str = "", at_token: str = "",
              tempo_token: str = "", customer_key: str = "",
              no_tempo: bool = False, skip_workflow: bool = False,
              productive_budget: int = 0, dry_run: bool = False) -> bool:

    at_email = at_email or os.environ.get("ATLASSIAN_EMAIL", "")
    at_token = at_token or os.environ.get("ATLASSIAN_API_TOKEN", "")
    # --no-tempo forces a Jira-only run even when TEMPO_API_TOKEN is exported
    # (the creds file always exports it, so env presence != intent to use it).
    tempo_token = "" if no_tempo else (tempo_token or os.environ.get("TEMPO_API_TOKEN", ""))

    if not at_email or not at_token:
        print("❌ Set ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN")
        return False

    key = key.upper()
    # Tempo customer key = Fichenbak clientId (a short, usually 3-letter code),
    # NOT the project-key prefix. Pass it explicitly via --customer-key; only
    # fall back to the prefix heuristic when nothing is supplied.
    customer_key = (customer_key or key[:6]).upper()
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
    wf_id, wf_name = ("", "") if skip_workflow \
        else get_intsta_workflow_scheme(at_email, at_token)
    print(f"   ✓ Permission scheme id={perm_id}"
          + (f", Notification scheme id={notif_id}" if notif_id else ""))
    if wf_id:
        print(f"   ✓ Workflow scheme id={wf_id} ({wf_name})")
    elif not skip_workflow:
        warn("INTSTA workflow scheme not found — issues will start in Backlog.")

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
    if wf_id:
        # Must run before anyone files an issue — see jira_apply_workflow_scheme.
        if jira_apply_workflow_scheme(pid, wf_id, at_email, at_token, dry_run):
            print("   ✓ Workflow scheme applied (new issues start in 'New')")
            if jira_mirror_board_columns(key, at_email, at_token, dry_run):
                print("   ✓ Board columns mirrored from INTSTA")
        elif dry_run:
            print("   [DRY-RUN] Workflow scheme + board columns skipped")
    if dry_run:
        print("   [DRY-RUN] 'Productive Budget' field skipped")
    elif jira_add_field_to_screens(key, pid, PRODUCTIVE_BUDGET_FIELD,
                                   at_email, at_token, dry_run):
        print(f"   ✓ '{PRODUCTIVE_BUDGET_FIELD}' added to {key}'s screens")

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

    if productive_budget:
        print("8. Epics + Productive budget...")
        if dry_run:
            print(f"   [DRY-RUN] Epics + budget {productive_budget} skipped")
        else:
            stamped = jira_stamp_budget(key, productive_budget,
                                        at_email, at_token, dry_run)
            if stamped:
                print(f"   ✓ Budget {productive_budget} set on {stamped} existing issue(s)")
            made = jira_create_epics(key, lead_id, productive_budget,
                                     at_email, at_token, dry_run)
            for k in made:
                print(f"   ✓ Epic {k} created with budget {productive_budget}")

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

        # Find Tempo account category KEY ("Volgens Offerte" → "VOF")
        tempo_cat_key = "VOF"  # default key for "Volgens Offerte"
        found_cat = tempo_find_category(DEFAULT_CATEGORY, tempo_token)
        if found_cat:
            tempo_cat_key = found_cat
            print(f"      Tempo category '{DEFAULT_CATEGORY}' → key={tempo_cat_key}")
        else:
            warn(f"Tempo category '{DEFAULT_CATEGORY}' not found, using key=VOF")

        accounts_created = []
        for acct_name, suffix in [("Voortraject", f"{key}VTJ"),
                                    ("Implementatie", f"{key}IMP")]:
            acct = tempo_create_account(
                name=acct_name,
                key_suffix=suffix,
                customer_key=customer_key,
                category_key=tempo_cat_key,
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
    p.add_argument("name", nargs="?", default="",
                   help="Full project name (not needed with --budget-only)")
    p.add_argument("--pm-email", required=True, help="Email of project lead")
    p.add_argument("--category", default="", help="Jira project category (e.g., 'Panda / Craft')")
    p.add_argument("--email", default="", help="Atlassian account email")
    p.add_argument("--token", default="", help="Atlassian API token")
    p.add_argument("--tempo-token", default="", help="Tempo API token (optional: auto-creates Tempo accounts)")
    p.add_argument("--customer-key", default="", help="Tempo customer key / Fichenbak clientId (e.g. 'SUI'). Defaults to first 6 chars of project key.")
    p.add_argument("--no-tempo", action="store_true", help="Jira only: skip Tempo even if TEMPO_API_TOKEN is set (PM creates accounts manually)")
    p.add_argument("--skip-workflow", action="store_true", help="Keep Jira's generated Kanban workflow (issues start in Backlog) instead of INTSTA's")
    p.add_argument("--productive-budget", type=int, default=0, metavar="ID",
                   help="Productive budget (deal) id. Creates the Voortraject + "
                        "Implementatie epics with it, and stamps any issue already "
                        "in the project. Re-run later to backfill.")
    p.add_argument("--budget-only", action="store_true",
                   help="Backfill mode: skip provisioning, only apply "
                        "--productive-budget to an existing project")
    p.add_argument("--dry-run", action="store_true", help="Validate without creating")
    args = p.parse_args()

    if args.budget_only:
        if not args.productive_budget:
            p.error("--budget-only requires --productive-budget")
        sys.exit(0 if backfill_budget(
            key=args.key.upper(), pm_email=args.pm_email,
            budget_id=args.productive_budget,
            at_email=args.email, at_token=args.token,
            dry_run=args.dry_run) else 1)

    if not args.name or not args.category:
        p.error("name and --category are required (omit them only with --budget-only)")

    ok = provision(
        key=args.key.upper(), name=args.name,
        pm_email=args.pm_email, category=args.category,
        at_email=args.email, at_token=args.token,
        tempo_token=args.tempo_token, customer_key=args.customer_key,
        no_tempo=args.no_tempo, skip_workflow=args.skip_workflow,
        productive_budget=args.productive_budget,
        dry_run=args.dry_run,
    )
    sys.exit(0 if ok else 1)