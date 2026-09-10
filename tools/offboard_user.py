#!/usr/bin/env python3
"""
Jira / Tempo User Offboarder
============================
The counterpart to onboard_user.py. When someone leaves, deactivating their
Atlassian account reassigns *nothing* — project leads, Tempo account leads,
assigned issues and owned filters all keep pointing at the dead account. This
script inventories what a leaver owns, transfers it to a successor, and then
tells you (or does, if you have an org admin key) the deactivation itself.

Usage:
    # Read-only: what does this person own?
    python3 offboard_user.py ben@statik.be --audit

    # Dry-run the transfer (DEFAULT — nothing is written without --apply)
    python3 offboard_user.py ben@statik.be --to maarten@statik.be

    # Actually transfer
    python3 offboard_user.py ben@statik.be --to maarten@statik.be --apply

    # Send their open issues back to the pool instead of to the successor
    python3 offboard_user.py ben@statik.be --to maarten@statik.be --unassign --apply

    # Already-deactivated account: user/search cannot see it, pass the id
    python3 offboard_user.py --account-id 712020:b161... --to ine@thekind.kids --apply

    # Attempt the deactivation too (needs ATLASSIAN_ORG_API_KEY)
    python3 offboard_user.py ben@statik.be --to maarten@statik.be --deactivate --apply

Credentials (source ~/.statik-jira-creds first):
    ATLASSIAN_EMAIL, ATLASSIAN_API_TOKEN   Jira    (required)
    TEMPO_API_TOKEN                        Tempo   (optional; skips Tempo if absent)
    ATLASSIAN_ORG_API_KEY                  org     (optional; only for --deactivate)

Note on the default: onboard_user.py writes by default and takes --dry-run.
This script is the opposite — it is dry by default and needs --apply, because a
single run can rewrite hundreds of records (Laurie Tilmant's handover moved 64
Jira leads and 198 Tempo accounts).
"""

import os
import sys
import ssl
import json
import time
import argparse
import textwrap
import urllib.request
import urllib.error
import urllib.parse
from base64 import b64encode

# macOS Python may lack root certificates; use certifi if available, else disable verification
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl._create_unverified_context()

JIRA_BASE = "https://statik.atlassian.net"
TEMPO_BASE = "https://api.tempo.io/4"
ORG_BASE = "https://api.atlassian.com/admin/v1"

# Groups whose members are worth scanning when we need to find an already
# deactivated account, which user/search refuses to return.
LOOKUP_GROUPS = ["jira-software-users", "team-statik"]

# Politeness delay between bulk writes. Jira Cloud does not rate-limit these
# tightly; this is here so a 200-record sweep does not look like an attack.
SLEEP = 0.08

# ── transport ─────────────────────────────────────────────────────────────

def _request(method, url, headers, body=None, timeout=60):
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = dict(headers, **{"Content-Type": "application/json"})
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, {"error": raw.decode(errors="replace")[:300]}
    except Exception as e:
        return 0, {"error": str(e)}


class Ctx:
    """Credentials + the three API callers, so nothing has to be threaded
    through every function signature."""

    def __init__(self, email, token, tempo_token="", org_key=""):
        self.jira_auth = b64encode(f"{email}:{token}".encode()).decode()
        self.tempo_token = tempo_token
        self.org_key = org_key

    def jira(self, method, path, body=None, **params):
        url = f"{JIRA_BASE}/rest/api/3/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _request(method, url, {"Authorization": f"Basic {self.jira_auth}",
                                      "Accept": "application/json"}, body)

    def agile(self, method, path, body=None, **params):
        url = f"{JIRA_BASE}/rest/agile/1.0/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _request(method, url, {"Authorization": f"Basic {self.jira_auth}",
                                      "Accept": "application/json"}, body)

    def tempo(self, method, path, body=None, **params):
        url = f"{TEMPO_BASE}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _request(method, url, {"Authorization": f"Bearer {self.tempo_token}",
                                      "Accept": "application/json"}, body)

    def org(self, method, path, body=None, **params):
        url = f"{ORG_BASE}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return _request(method, url, {"Authorization": f"Bearer {self.org_key}",
                                      "Accept": "application/json"}, body)


def ok(msg):    print(f"      ✓ {msg}")
def skip(msg):  print(f"      · {msg}")
def err(msg):   print(f"  ❌ {msg}")
def warn(msg):  print(f"  ⚠  {msg}")

# ── user lookup ───────────────────────────────────────────────────────────

def resolve_user(ctx, address):
    """Find a user by email. Falls back to scanning group membership with
    includeInactiveUsers, because user/search silently omits deactivated and
    never-accepted accounts — the exact people you offboard."""
    code, data = ctx.jira("GET", "user/search", query=address, maxResults=50)
    if code == 200 and isinstance(data, list):
        exact = [u for u in data
                 if (u.get("emailAddress") or "").lower() == address.lower()]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            err(f"{len(exact)} accounts share {address} — pass --account-id")
            return None
        if len(data) == 1:
            return data[0]
        if len(data) > 1:
            err(f"'{address}' matched {len(data)} users; none by exact email:")
            for u in data[:10]:
                print(f"       - {u.get('displayName')} ({u.get('accountId')})")
            return None

    warn(f"user/search found nothing for {address} — scanning groups for an "
         f"inactive account")
    code, groups = ctx.jira("GET", "group/bulk", maxResults=200)
    gids = {g["name"]: g["groupId"] for g in groups.get("values", [])} \
        if code == 200 else {}
    needle = address.lower()
    for gname in LOOKUP_GROUPS:
        if gname not in gids:
            continue
        for m in _group_members(ctx, gids[gname]):
            if (m.get("emailAddress") or "").lower() == needle:
                warn(f"found via {gname}: {m.get('displayName')} "
                     f"(active={m.get('active')})")
                return m
    err(f"No account found for {address}. If it is already deactivated and its "
        f"email is hidden, pass --account-id explicitly.")
    return None


def _group_members(ctx, group_id):
    members, start = [], 0
    while True:
        code, d = ctx.jira("GET", "group/member", groupId=group_id, startAt=start,
                           maxResults=50, includeInactiveUsers="true")
        if code != 200:
            return members
        members += d.get("values", [])
        if d.get("isLast", True):
            break
        start += d.get("maxResults", 50)
    return members

# ── inventory ─────────────────────────────────────────────────────────────

def jql_keys(ctx, jql):
    """All issue keys for a JQL query.

    Pages via nextPageToken and counts what comes back. Do NOT switch this to
    reading `total` — /search/jql does not return that field, so it evaluates
    to None and a broken query reads as a clean zero.
    """
    keys, token = [], None
    while True:
        params = {"jql": jql, "maxResults": 100, "fields": "key"}
        if token:
            params["nextPageToken"] = token
        code, d = ctx.jira("GET", "search/jql", **params)
        if code != 200:
            err(f"JQL failed: HTTP {code} — {d}")
            return keys
        keys += [i["key"] for i in d.get("issues", [])]
        token = d.get("nextPageToken")
        if not token:
            return keys


def inventory(ctx, aid, include_closed=False, map_boards=False):
    """Everything the leaver owns that outlives their account."""
    inv = {}

    code, u = ctx.jira("GET", "user", accountId=aid,
                       expand="groups,applicationRoles")
    if code != 200:
        err(f"Cannot read account {aid}: HTTP {code} — {u}")
        return None
    inv["account"] = {
        "accountId": aid,
        "displayName": u.get("displayName"),
        "email": u.get("emailAddress"),
        "active": u.get("active"),
        "groups": sorted(g["name"] for g in u.get("groups", {}).get("items", [])),
        "appRoles": [r["key"] for r in u.get("applicationRoles", {}).get("items", [])],
    }

    # Jira project leads. Archived projects are historical; changing their lead
    # is audit churn with no operational effect, so they are off by default.
    projects, start = [], 0
    while True:
        code, d = ctx.jira("GET", "project/search", startAt=start,
                           maxResults=50, expand="lead")
        if code != 200:
            err(f"project/search failed: HTTP {code} — {d}")
            break
        projects += d.get("values", [])
        if d.get("isLast", True):
            break
        start += d.get("maxResults", 50)
    led = [p for p in projects if (p.get("lead") or {}).get("accountId") == aid]
    inv["project_leads"] = [
        {"key": p["key"], "name": p["name"], "archived": bool(p.get("archived"))}
        for p in led if include_closed or not p.get("archived")
    ]
    inv["project_leads_skipped"] = sum(
        1 for p in led if p.get("archived") and not include_closed)

    inv["open_issues"] = jql_keys(ctx, f'assignee = "{aid}" AND statusCategory != Done')
    inv["done_issues"] = len(jql_keys(ctx, f'assignee = "{aid}" AND statusCategory = Done'))

    code, d = ctx.jira("GET", "filter/search", accountId=aid, maxResults=100,
                       expand="owner")
    inv["filters"] = [{"id": f["id"], "name": f["name"]}
                      for f in (d.get("values", []) if code == 200 else [])]

    code, d = ctx.jira("GET", "dashboard/search", accountId=aid, maxResults=100)
    inv["dashboards"] = [{"id": x["id"], "name": x["name"]}
                         for x in (d.get("values", []) if code == 200 else [])]

    # Which agile boards run off the leaver's filters. This is the failure the
    # Ben Verbist offboarding nearly shipped: deactivate the owner of a board's
    # filter and the board is left pointing at a dead account.
    inv["boards"] = []
    if map_boards and inv["filters"]:
        want = {str(f["id"]) for f in inv["filters"]}
        boards, start = [], 0
        while True:
            code, d = ctx.agile("GET", "board", startAt=start, maxResults=50)
            if code != 200:
                break
            boards += d.get("values", [])
            if d.get("isLast", True) or not d.get("values"):
                break
            start += len(d.get("values", []))
        for b in boards:
            code, cfg = ctx.agile("GET", f"board/{b['id']}/configuration")
            if code == 200 and str((cfg.get("filter") or {}).get("id")) in want:
                inv["boards"].append({"id": b["id"], "name": b["name"],
                                      "filter": str(cfg["filter"]["id"])})

    # Tempo. Account lead is the portfolio owner and the one that matters;
    # CLOSED accounts are historical buckets (and their PUTs are flaky), so
    # they are excluded unless asked for.
    inv["tempo_accounts"] = []
    inv["tempo_accounts_skipped"] = 0
    inv["tempo_teams_led"] = []
    inv["tempo_teams_member"] = []
    if ctx.tempo_token:
        accts, url_params = [], {"limit": 200, "offset": 0}
        while True:
            code, d = ctx.tempo("GET", "accounts", **url_params)
            if code != 200:
                err(f"Tempo /accounts failed: HTTP {code} — {d}")
                break
            res = d.get("results", [])
            accts += res
            if len(res) < url_params["limit"]:
                break
            url_params["offset"] += url_params["limit"]
        mine = [a for a in accts if (a.get("lead") or {}).get("accountId") == aid]
        inv["tempo_accounts"] = [a for a in mine
                                 if include_closed or a.get("status") == "OPEN"]
        inv["tempo_accounts_skipped"] = len(mine) - len(inv["tempo_accounts"])

        code, d = ctx.tempo("GET", "teams", limit=200)
        for t in (d.get("results", []) if code == 200 else []):
            if (t.get("lead") or {}).get("accountId") == aid:
                inv["tempo_teams_led"].append({"id": t["id"], "name": t["name"]})
            c, mem = ctx.tempo("GET", f"teams/{t['id']}/members", limit=200)
            if c == 200 and any((m.get("member") or {}).get("accountId") == aid
                                for m in mem.get("results", [])):
                inv["tempo_teams_member"].append({"id": t["id"], "name": t["name"]})
    return inv


def print_inventory(inv):
    a = inv["account"]
    print(f"\n  {a['displayName']}  <{a['email'] or 'email hidden'}>")
    print(f"  accountId : {a['accountId']}")
    print(f"  active    : {a['active']}   appRoles: {', '.join(a['appRoles']) or '(none)'}")
    print(f"  groups    : {', '.join(a['groups']) or '(none)'}\n")

    rows = [
        ("Jira project leads",  len(inv["project_leads"]),  inv["project_leads_skipped"]),
        ("Tempo account leads", len(inv["tempo_accounts"]), inv["tempo_accounts_skipped"]),
        ("Tempo team leads",    len(inv["tempo_teams_led"]), 0),
        ("Open issues assigned", len(inv["open_issues"]),   0),
        ("Filters owned",       len(inv["filters"]),        0),
        ("Dashboards owned",    len(inv["dashboards"]),     0),
    ]
    width = max([len(r[0]) for r in rows] + [len("Done issues (history)")])
    for label, n, skipped in rows:
        extra = f"   ({skipped} closed/archived skipped)" if skipped else ""
        flag = "" if n == 0 else "  <-- needs transfer"
        print(f"  {label:<{width}} : {n:>4}{flag}{extra}")
    print(f"  {'Done issues (history)':<{width}} : {inv['done_issues']:>4}"
          f"   left as-is by design")

    def listing(title, items, fmt):
        if items:
            print(f"\n  {title}")
            for it in items:
                print(f"    {fmt(it)}")

    listing("Project leads:", inv["project_leads"],
            lambda p: f"{p['key']:<12} {p['name'][:55]}")
    listing("Tempo accounts:", inv["tempo_accounts"],
            lambda a2: f"{str(a2.get('key')):<18} [{a2.get('status')}] {a2.get('name','')[:45]}")
    listing("Tempo teams led:", inv["tempo_teams_led"],
            lambda t: f"id={t['id']:<5} {t['name']}")
    listing("Filters:", inv["filters"], lambda f: f"{f['id']:<8} {f['name'][:60]}")
    listing("Dashboards:", inv["dashboards"], lambda d: f"{d['id']:<8} {d['name'][:60]}")
    listing("Boards driven by those filters:", inv["boards"],
            lambda b: f"board {b['id']:<5} {b['name'][:45]} (filter {b['filter']})")
    if inv["filters"] and not inv["boards"]:
        print(f"\n  ⚠  {len(inv['filters'])} filter(s) owned. Re-run with --map-boards")
        print(f"     to see which boards depend on them (scans every board; slow).")
    if inv["tempo_teams_member"]:
        print(f"\n  Tempo team membership (harmless once deactivated): "
              f"{', '.join(t['name'] for t in inv['tempo_teams_member'])}")

# ── transfers ─────────────────────────────────────────────────────────────

def xfer_project_leads(ctx, inv, to_aid, apply):
    """Jira project lead. Partial body is fine here, unlike Tempo."""
    items = inv["project_leads"]
    if not items:
        skip("no project leads")
        return 0, 0
    good = bad = 0
    for p in items:
        if not apply:
            print(f"      [DRY-RUN] would move lead of {p['key']}")
            good += 1
            continue
        code, resp = ctx.jira("PUT", f"project/{p['key']}",
                              {"leadAccountId": to_aid})
        if code in (200, 204):
            ok(f"{p['key']:<12} {p['name'][:45]}")
            good += 1
        else:
            err(f"{p['key']}: HTTP {code} — {resp}")
            bad += 1
        time.sleep(SLEEP)
    return good, bad


def xfer_tempo_accounts(ctx, inv, to_aid, apply):
    """Tempo account lead.

    Three quirks, all confirmed the hard way:
      - GET is by numeric id, PUT is by string key. Each 404s on the other.
      - The PUT body must be complete; a partial body returns
        400 "No <field> supplied" on fields you never touched.
      - leadAccountId is FLAT in the PUT, though GET nests it as lead.accountId.
    """
    items = inv["tempo_accounts"]
    if not items:
        skip("no Tempo account leads")
        return 0, 0
    good = bad = 0
    for a in items:
        if not apply:
            print(f"      [DRY-RUN] would move lead of {a.get('key')}")
            good += 1
            continue
        body = {
            "key": a["key"],
            "name": a["name"],
            "status": a.get("status", "OPEN"),
            "global": a.get("global", False),
            "categoryKey": (a.get("category") or {}).get("key"),
            "customerKey": (a.get("customer") or {}).get("key"),
            "leadAccountId": to_aid,
        }
        body = {k: v for k, v in body.items() if v is not None}
        code, resp = ctx.tempo("PUT", f"accounts/{a['key']}", body)
        if code in (200, 204):
            ok(f"{a['key']:<18} {a.get('name','')[:42]}")
            good += 1
        else:
            err(f"{a['key']}: HTTP {code} — {resp}")
            bad += 1
        time.sleep(SLEEP)
    return good, bad


def xfer_tempo_teams(ctx, inv, to_aid, apply):
    """Tempo team lead. Follows the account pattern (full body, flat
    leadAccountId) since Tempo is consistent about that. Less exercised than
    the account path — the HTTP body is printed verbatim on failure so a shape
    mismatch is loud rather than silent."""
    items = inv["tempo_teams_led"]
    if not items:
        skip("no Tempo team leads")
        return 0, 0
    good = bad = 0
    for t in items:
        if not apply:
            print(f"      [DRY-RUN] would move lead of team {t['id']} ({t['name']})")
            good += 1
            continue
        code, cur = ctx.tempo("GET", f"teams/{t['id']}")
        if code != 200:
            err(f"team {t['id']}: cannot read — HTTP {code} — {cur}")
            bad += 1
            continue
        body = {
            "name": cur.get("name"),
            "summary": cur.get("summary", ""),
            "leadAccountId": to_aid,
            "administrative": cur.get("administrative", False),
            "public": cur.get("public", False),
        }
        if (cur.get("program") or {}).get("id"):
            body["programId"] = cur["program"]["id"]
        code, resp = ctx.tempo("PUT", f"teams/{t['id']}", body)
        if code in (200, 204):
            ok(f"team {t['id']} ({t['name']})")
            good += 1
        else:
            err(f"team {t['id']}: HTTP {code} — {resp}")
            bad += 1
        time.sleep(SLEEP)
    return good, bad


def xfer_issues(ctx, inv, to_aid, apply, unassign=False):
    """Open issues. Done/closed issues keep the leaver as assignee — that is
    project history, not a loose end."""
    keys = inv["open_issues"]
    if not keys:
        skip("no open issues")
        return 0, 0
    target = None if unassign else to_aid
    what = "unassign" if unassign else "reassign"
    good = bad = 0
    for k in keys:
        if not apply:
            print(f"      [DRY-RUN] would {what} {k}")
            good += 1
            continue
        code, resp = ctx.jira("PUT", f"issue/{k}/assignee", {"accountId": target})
        if code in (200, 204):
            ok(k)
            good += 1
        else:
            err(f"{k}: HTTP {code} — {resp}")
            bad += 1
        time.sleep(SLEEP)
    return good, bad


def xfer_filters(ctx, inv, to_aid, apply):
    items = inv["filters"]
    if not items:
        skip("no filters")
        return 0, 0
    good = bad = 0
    for f in items:
        if not apply:
            print(f"      [DRY-RUN] would move filter {f['id']} ({f['name']})")
            good += 1
            continue
        code, resp = ctx.jira("PUT", f"filter/{f['id']}/owner",
                              {"accountId": to_aid})
        if code in (200, 204):
            ok(f"filter {f['id']:<8} {f['name'][:50]}")
            good += 1
        else:
            err(f"filter {f['id']}: HTTP {code} — {resp}")
            bad += 1
        time.sleep(SLEEP)
    return good, bad

# ── deactivation ──────────────────────────────────────────────────────────

MANUAL_STEPS = """\
Deactivate the account by hand:
  1. Open https://admin.atlassian.com
  2. Directory → Users → search the person
  3. → Deactivate access

To make this scriptable, create an org admin API key at
admin.atlassian.com → Settings → API keys, then:
  export ATLASSIAN_ORG_API_KEY=...
That is a DIFFERENT credential from a Jira user API token — the Jira token
returns HTTP 401 against the org admin API."""


def deactivate(ctx, aid, apply):
    """Suspend product access org-wide. Needs an org admin API key; a Jira
    user token cannot do this at all."""
    if not ctx.org_key:
        warn("ATLASSIAN_ORG_API_KEY not set — cannot deactivate from here.")
        print(textwrap.indent(MANUAL_STEPS, "     "))
        return False

    code, orgs = ctx.org("GET", "orgs")
    if code != 200:
        err(f"Org admin API rejected the key: HTTP {code} — {orgs}")
        print(textwrap.indent(MANUAL_STEPS, "     "))
        return False
    ids = [o["id"] for o in orgs.get("data", [])]
    if not ids:
        err("Org admin key is valid but lists no organisations.")
        return False

    for org_id in ids:
        if not apply:
            print(f"      [DRY-RUN] would suspend access in org {org_id}")
            return True
        code, resp = ctx.org("POST",
                             f"orgs/{org_id}/directory/users/{aid}/suspend-access", {})
        if code in (200, 204):
            ok(f"access suspended in org {org_id}")
            return True
        if code == 404:
            skip(f"not a member of org {org_id}")
            continue
        err(f"org {org_id}: HTTP {code} — {resp}")
    return False

# ── main ──────────────────────────────────────────────────────────────────

def run(args, ctx):
    print(f"\n{'=' * 64}")
    print(f"{'' if args.apply else '[DRY-RUN] '}Offboarding: "
          f"{args.address or args.account_id}")
    print(f"{'=' * 64}")

    print("\n1. Resolving leaver...")
    if args.account_id:
        aid = args.account_id
        print(f"   using --account-id {aid}")
    else:
        user = resolve_user(ctx, args.address)
        if not user:
            return False
        aid = user["accountId"]
        print(f"   ✓ {user.get('displayName')} — {aid}")

    print("\n2. Inventory...")
    inv = inventory(ctx, aid, include_closed=args.include_closed,
                    map_boards=args.map_boards)
    if not inv:
        return False
    print_inventory(inv)

    if args.audit:
        print(f"\n{'─' * 64}")
        print("Audit only — nothing written. Re-run with --to <email> to transfer.")
        print(f"{'─' * 64}\n")
        return True

    work = (len(inv["project_leads"]) + len(inv["tempo_accounts"])
            + len(inv["tempo_teams_led"]) + len(inv["open_issues"])
            + len(inv["filters"]))
    if not work:
        print("\n   Nothing to transfer.")
    else:
        print("\n3. Resolving successor...")
        succ = resolve_user(ctx, args.to)
        if not succ:
            return False
        to_aid = succ["accountId"]
        if to_aid == aid:
            err("Successor is the same account as the leaver.")
            return False
        if not succ.get("active", True):
            err(f"Successor {succ.get('displayName')} is INACTIVE — pick someone else.")
            return False
        print(f"   ✓ {succ.get('displayName')} — {to_aid}")

        print("\n4. Transferring...")
        totals = {}
        print("   Jira project leads:")
        totals["project leads"] = xfer_project_leads(ctx, inv, to_aid, args.apply)
        print("   Tempo account leads:")
        totals["tempo accounts"] = xfer_tempo_accounts(ctx, inv, to_aid, args.apply)
        print("   Tempo team leads:")
        totals["tempo teams"] = xfer_tempo_teams(ctx, inv, to_aid, args.apply)
        print(f"   Open issues ({'unassign' if args.unassign else 'reassign'}):")
        totals["issues"] = xfer_issues(ctx, inv, to_aid, args.apply, args.unassign)
        print("   Filters:")
        totals["filters"] = xfer_filters(ctx, inv, to_aid, args.apply)

        print("\n   Summary:")
        failed_any = False
        for label, (g, b) in totals.items():
            print(f"     {label:<16} {g:>4} ok  {b:>3} failed")
            failed_any = failed_any or b
        if inv["dashboards"]:
            warn(f"{len(inv['dashboards'])} dashboard(s) NOT transferred — Jira "
                 f"has no owner-change API for dashboards.")
            print(f"     Do it in the UI: Dashboards → ⋯ → Change owner")

        if args.apply:
            print("\n5. Verifying...")
            left_issues = len(jql_keys(
                ctx, f'assignee = "{aid}" AND statusCategory != Done'))
            post = inventory(ctx, aid, include_closed=args.include_closed)
            print(f"     open issues still on leaver : {left_issues}")
            print(f"     project leads still on leaver: {len(post['project_leads'])}")
            print(f"     tempo accounts still on leaver: {len(post['tempo_accounts'])}")
            print(f"     filters still on leaver      : {len(post['filters'])}")
            if failed_any:
                warn("Some writes failed — re-run to retry the remainder "
                     "(all operations are idempotent).")

    if args.deactivate:
        print("\n6. Deactivating account...")
        done = deactivate(ctx, aid, args.apply)
    else:
        done = False
        print("\n6. Deactivation: not requested (--deactivate to attempt).")
        print(textwrap.indent(MANUAL_STEPS, "     "))

    print(f"\n{'─' * 64}")
    if args.apply:
        print(f"✅ Transfers applied for {inv['account']['displayName']}.")
        if not done:
            print("   ⚠  The account is still ACTIVE — deactivation is the manual")
            print("      step above. Do not consider this person offboarded yet.")
    else:
        print("[DRY-RUN] nothing written. Re-run with --apply.")
    print(f"{'─' * 64}\n")
    return True


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Offboard a Jira/Tempo user: inventory, transfer, deactivate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          %(prog)s ben@statik.be --audit
          %(prog)s ben@statik.be --to maarten@statik.be
          %(prog)s ben@statik.be --to maarten@statik.be --apply
          %(prog)s ben@statik.be --to maarten@statik.be --unassign --apply
          %(prog)s --account-id 712020:b161... --to ine@thekind.kids --apply

        Dry-run is the DEFAULT; --apply writes. Deactivating an account does
        not reassign anything, so always transfer first.
        """),
    )
    p.add_argument("address", nargs="?", default="", help="Leaver's email address")
    p.add_argument("--account-id", default="",
                   help="Leaver's accountId, for accounts user/search cannot see")
    p.add_argument("--to", default="", help="Successor's email address")
    p.add_argument("--audit", action="store_true",
                   help="Inventory only; never writes")
    p.add_argument("--unassign", action="store_true",
                   help="Clear the assignee on open issues instead of reassigning")
    p.add_argument("--include-closed", action="store_true",
                   help="Also touch CLOSED Tempo accounts and archived Jira projects")
    p.add_argument("--map-boards", action="store_true",
                   help="Report which boards run off the leaver's filters (slow)")
    p.add_argument("--deactivate", action="store_true",
                   help="Also suspend org access (needs ATLASSIAN_ORG_API_KEY)")
    p.add_argument("--apply", action="store_true",
                   help="Actually write (default: dry-run)")
    p.add_argument("--email", default="", help="Atlassian account email")
    p.add_argument("--token", default="", help="Atlassian API token")
    args = p.parse_args()

    if not args.address and not args.account_id:
        p.error("give an email address or --account-id")
    if not args.audit and not args.to:
        p.error("--to <successor-email> is required unless --audit")
    # --audit never writes, so silently swallowing --deactivate would hide the
    # fact that the account was left active.
    if args.audit and args.deactivate:
        p.error("--audit never writes; drop --audit to use --deactivate")
    if args.audit and args.apply:
        p.error("--audit never writes; drop --audit to use --apply")

    at_email = args.email or os.environ.get("ATLASSIAN_EMAIL", "")
    at_token = args.token or os.environ.get("ATLASSIAN_API_TOKEN", "")
    if not at_email or not at_token:
        print("❌ Missing credentials. Set ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN")
        print("   or pass --email and --token arguments.")
        sys.exit(1)

    tempo_token = os.environ.get("TEMPO_API_TOKEN", "")
    if not tempo_token:
        print("⚠  TEMPO_API_TOKEN not set — Tempo account/team leads will be skipped.")

    ctx = Ctx(at_email, at_token, tempo_token, os.environ.get("ATLASSIAN_ORG_API_KEY", ""))
    sys.exit(0 if run(args, ctx) else 1)
