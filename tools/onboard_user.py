#!/usr/bin/env python3
"""
Jira User Onboarder
===================
Creates (or finds) a Statik Jira user and enforces the standard group set
from The Kind Kids' Handbook:

    jira-software-users + confluence-users + team-statik [+ team-<animal>]

`team-statik` is the group that grants BROWSE_PROJECTS on Statik's default
permission scheme. Without it a user looks fully provisioned but opens Jira
to nothing — so this script treats a missing team-statik as a bug to fix,
not an option to pick.

Usage:
    python3 onboard_user.py zias@thekind.kids --team rhino
    python3 onboard_user.py zias@thekind.kids --team rhino --tempo-admin
    python3 onboard_user.py zias@thekind.kids --no-create   # fix groups only
    python3 onboard_user.py --audit                         # find gaps
    python3 onboard_user.py --audit --fix                   # ...and close them
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

# macOS Python may lack root certificates; use certifi if available, else disable verification
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl._create_unverified_context()

JIRA_BASE = "https://statik.atlassian.net"

# Every internal employee gets these three, always.
# jira-software-users is added automatically by the invite, but we verify it
# anyway — a pending account can sit in it while still being unlicensed.
STANDARD_GROUPS = ["jira-software-users", "confluence-users", "team-statik"]
BROWSE_GROUP = "team-statik"
TEMPO_ADMIN_GROUP = "tempo-account-administrators"

# Team groups are team-<animal>. team-statik itself and team-statik-stagiairs
# are excluded: the first is the thing we are checking for, the second is the
# intern group, which does not follow the employee set.
TEAM_PREFIX = "team-"
TEAM_EXCLUDE = {"team-statik", "team-statik-stagiairs"}

# Teams belonging to a different legal entity under The Kind Kids. Their people
# are internal, but whether they should browse Statik's projects is a business
# call, not a provisioning slip — so --audit reports them separately and --fix
# leaves them alone unless --all-teams says otherwise.
REVIEW_TEAMS = {"team-shavedmonkey"}

# ── helpers ──────────────────────────────────────────────────────────────

def api(method: str, path: str, body: dict | None = None,
        params: dict | None = None,
        email: str = "", token: str = "") -> tuple[int, dict | list]:
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
    if params:
        url += "?" + urllib.parse.urlencode(params)

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

def err(msg: str) -> None:
    print(f"  ❌ {msg}")

def warn(msg: str) -> None:
    print(f"  ⚠  {msg}")

# ── group plumbing ────────────────────────────────────────────────────────

def load_groups(email: str, token: str) -> dict[str, str]:
    """Map every group name -> groupId. Group names are ambiguous in the
    modern API; groupId is what the write endpoints actually want."""
    groups: dict[str, str] = {}
    start = 0
    while True:
        code, data = api("GET", "/group/bulk",
                         params={"startAt": start, "maxResults": 200},
                         email=email, token=token)
        if code != 200:
            err(f"Failed to list groups: HTTP {code} — {data}")
            return {}
        for g in data.get("values", []):
            groups[g["name"]] = g["groupId"]
        if data.get("isLast", True):
            break
        start += data.get("maxResults", 200)
    return groups

def group_members(group_id: str, email: str, token: str,
                  include_inactive: bool = False) -> list[dict]:
    """All members of a group, paginated."""
    members: list[dict] = []
    start = 0
    while True:
        code, data = api("GET", "/group/member",
                         params={"groupId": group_id, "startAt": start,
                                 "maxResults": 50,
                                 "includeInactiveUsers": str(include_inactive).lower()},
                         email=email, token=token)
        if code != 200:
            err(f"Failed to list members: HTTP {code} — {data}")
            return members
        members.extend(data.get("values", []))
        if data.get("isLast", True):
            break
        start += data.get("maxResults", 50)
    return members

def add_to_group(name: str, account_id: str, groups: dict[str, str],
                 email: str, token: str, dry_run: bool = False) -> bool:
    """Add a user to a group by name. Already-a-member counts as success."""
    gid = groups.get(name)
    if not gid:
        err(f"Group '{name}' does not exist")
        return False
    if dry_run:
        print(f"      [DRY-RUN] would add to {name}")
        return True
    code, data = api("POST", "/group/user", params={"groupId": gid},
                     body={"accountId": account_id}, email=email, token=token)
    if code in (200, 201):
        print(f"      ✓ added to {name}")
        return True
    # Re-running the script must be harmless.
    msgs = " ".join(data.get("errorMessages", [])) if isinstance(data, dict) else ""
    if code == 400 and "already a member" in msgs.lower():
        print(f"      · already in {name}")
        return True
    err(f"Could not add to {name}: HTTP {code} — {data}")
    return False

# ── user plumbing ─────────────────────────────────────────────────────────

def find_user(address: str, email: str, token: str) -> dict | None:
    """Look up a user by email address. Note: user/search omits accounts that
    have never accepted their invite — see create_user() for that case."""
    code, data = api("GET", "/user/search",
                     params={"query": address, "maxResults": 50},
                     email=email, token=token)
    if code != 200 or not isinstance(data, list):
        return None
    exact = [u for u in data
             if (u.get("emailAddress") or "").lower() == address.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        err(f"{len(exact)} accounts share {address} — resolve by hand")
        return None
    # Email is hidden on many accounts; fall back to a single fuzzy hit.
    if len(data) == 1:
        return data[0]
    if len(data) > 1:
        err(f"'{address}' matched {len(data)} users; none by exact email:")
        for u in data[:10]:
            print(f"       - {u.get('displayName')} ({u.get('accountId')})")
    return None

def create_user(address: str, email: str, token: str,
                dry_run: bool = False) -> dict | None:
    """Invite a new user. Returns the user record (new or pre-existing)."""
    if dry_run:
        print(f"      [DRY-RUN] would invite {address} with jira-software access")
        return {"accountId": "DRY-RUN", "displayName": address.split("@")[0]}
    code, data = api("POST", "/user",
                     body={"emailAddress": address, "products": ["jira-software"]},
                     email=email, token=token)
    if code in (200, 201):
        print(f"      ✓ invited {address} (accountId={data.get('accountId')})")
        return data
    # Documented quirk: for someone who exists but never accepted their invite,
    # user/search returns nothing and POST /user returns 400 whose *body is the
    # existing account record*. Read it rather than treating the 400 as failure.
    if code == 400 and isinstance(data, dict) and data.get("accountId"):
        state = "pending invite" if not data.get("active") else "active"
        print(f"      · already exists ({state}) — accountId={data['accountId']}")
        return data
    err(f"Could not create user: HTTP {code} — {data}")
    return None

def user_groups(account_id: str, email: str, token: str) -> list[str]:
    code, data = api("GET", "/user/groups", params={"accountId": account_id},
                     email=email, token=token)
    if code != 200 or not isinstance(data, list):
        err(f"Could not read groups: HTTP {code} — {data}")
        return []
    return sorted(g["name"] for g in data)

# ── onboard ───────────────────────────────────────────────────────────────

def onboard(address: str, team: str = "", tempo_admin: bool = False,
            no_create: bool = False, at_email: str = "", at_token: str = "",
            dry_run: bool = False) -> bool:
    """Create-or-find the user, then enforce the standard group set."""

    mode = "[DRY-RUN] " if dry_run else ""
    print(f"\n{'=' * 60}")
    print(f"{mode}Onboarding: {address}")
    print(f"{'=' * 60}\n")

    print("1. Loading groups...")
    groups = load_groups(at_email, at_token)
    if not groups:
        return False
    print(f"   ✓ {len(groups)} groups")

    # Resolve the team group before touching anything, so a typo fails early
    # rather than half-way through provisioning.
    wanted = list(STANDARD_GROUPS)
    if team:
        name = team if team.startswith(TEAM_PREFIX) else TEAM_PREFIX + team
        if name not in groups:
            err(f"Team group '{name}' does not exist. Available:")
            for g in sorted(n for n in groups
                            if n.startswith(TEAM_PREFIX) and n not in TEAM_EXCLUDE):
                print(f"       - {g}")
            return False
        wanted.append(name)
    if tempo_admin:
        wanted.append(TEMPO_ADMIN_GROUP)

    print("2. Looking up user...")
    user = find_user(address, at_email, at_token)
    if user:
        active = "active" if user.get("active") else "INACTIVE / pending invite"
        print(f"   ✓ {user.get('displayName')} — {active}")
        print(f"     accountId={user['accountId']}")
    elif no_create:
        err(f"No user found for {address} and --no-create was given")
        return False
    else:
        print(f"   · not found — creating")
        user = create_user(address, at_email, at_token, dry_run)
        if not user:
            return False

    account_id = user["accountId"]

    print("3. Checking current groups...")
    current = [] if dry_run and account_id == "DRY-RUN" \
        else user_groups(account_id, at_email, at_token)
    print(f"   Current: {', '.join(current) or '(none)'}")
    print(f"   Wanted:  {', '.join(wanted)}")

    missing = [g for g in wanted if g not in current]
    if not missing:
        print(f"   ✅ Already has the full standard set — nothing to do.")
        return True
    print(f"   Missing: {', '.join(missing)}")

    print("4. Adding missing groups...")
    ok = True
    for g in missing:
        if not add_to_group(g, account_id, groups, at_email, at_token, dry_run):
            ok = False

    if dry_run:
        print(f"\n   [DRY-RUN] no changes made.\n")
        return ok

    print("5. Verifying...")
    final = user_groups(account_id, at_email, at_token)
    still = [g for g in wanted if g not in final]
    if still:
        err(f"Still missing after write: {', '.join(still)}")
        return False
    print(f"   ✓ {', '.join(final)}")

    print(f"\n{'─' * 60}")
    print(f"✅ {user.get('displayName', address)} onboarded.")
    if BROWSE_GROUP in missing:
        print(f"   {BROWSE_GROUP} added — they can now browse Statik projects.")
    if not user.get("active", True):
        print(f"   ⚠  Account is still pending: the invite must be accepted")
        print(f"      before the licence activates. Groups alone do not do it;")
        print(f"      re-invite from admin.atlassian.com if it never arrived.")
    print(f"{'─' * 60}\n")
    return ok

# ── audit ─────────────────────────────────────────────────────────────────

def audit(fix: bool = False, all_teams: bool = False,
          at_email: str = "", at_token: str = "",
          dry_run: bool = False) -> bool:
    """Find internal staff who are in a team-<animal> group but not in
    team-statik. Being in a team group is what marks someone as internal —
    it cleanly separates employees from app accounts and customer users,
    which is why we do not try to filter on email domain."""

    print(f"\n{'=' * 60}")
    print(f"Auditing {BROWSE_GROUP} membership")
    print(f"{'=' * 60}\n")

    print("1. Loading groups...")
    groups = load_groups(at_email, at_token)
    if not groups:
        return False
    team_groups = sorted(n for n in groups
                         if n.startswith(TEAM_PREFIX) and n not in TEAM_EXCLUDE)
    print(f"   ✓ {len(team_groups)} team groups: {', '.join(team_groups)}")

    print(f"2. Reading {BROWSE_GROUP}...")
    if BROWSE_GROUP not in groups:
        err(f"Group '{BROWSE_GROUP}' does not exist")
        return False
    in_statik = {m["accountId"]
                 for m in group_members(groups[BROWSE_GROUP], at_email, at_token)}
    print(f"   ✓ {len(in_statik)} members")

    print("3. Reading team groups...")
    internal: dict[str, dict] = {}
    for name in team_groups:
        for m in group_members(groups[name], at_email, at_token):
            rec = internal.setdefault(m["accountId"], dict(m, teams=[]))
            rec["teams"].append(name)
    print(f"   ✓ {len(internal)} people across all team groups")

    absent = [u for aid, u in internal.items()
              if aid not in in_statik and u.get("active")]

    # Someone in a review team AND a Statik team is a real gap; someone only in
    # a review team is a separate-entity question for a human to answer.
    def review_only(u: dict) -> bool:
        return not all_teams and all(t in REVIEW_TEAMS for t in u["teams"])

    gaps = sorted((u for u in absent if not review_only(u)),
                  key=lambda x: x.get("displayName") or "")
    review = sorted((u for u in absent if review_only(u)),
                    key=lambda x: x.get("displayName") or "")

    def show(u: dict) -> None:
        print(f"   - {u.get('displayName')} ({', '.join(u['teams'])})")
        print(f"     {u.get('emailAddress') or 'email hidden'} — {u['accountId']}")

    print(f"\n{'─' * 60}")
    if gaps:
        print(f"⚠  {len(gaps)} active team member(s) missing {BROWSE_GROUP}:")
        for u in gaps:
            show(u)
    else:
        print(f"✅ No gaps. Every active team member is in {BROWSE_GROUP}.")

    if review:
        print(f"\n   ── review manually ({len(review)}) ──")
        print(f"   Only in {'/'.join(sorted(REVIEW_TEAMS))} — a separate entity, so")
        print(f"   {BROWSE_GROUP} may be intentional to withhold. Not touched by")
        print(f"   --fix; pass --all-teams to treat these as gaps too.")
        for u in review:
            show(u)
    print(f"{'─' * 60}\n")

    if not gaps:
        return True

    if not fix:
        print(f"Re-run with --fix to add them to {BROWSE_GROUP}.\n")
        return False

    print(f"Adding {len(gaps)} user(s) to {BROWSE_GROUP}...")
    ok = True
    for u in gaps:
        print(f"   {u.get('displayName')}:")
        if not add_to_group(BROWSE_GROUP, u["accountId"], groups,
                            at_email, at_token, dry_run):
            ok = False
    print()
    return ok

# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Onboard a Jira user with the standard Statik group set",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
        Examples:
          %(prog)s zias@thekind.kids --team rhino
          %(prog)s zias@thekind.kids --team rhino --tempo-admin
          %(prog)s zias@thekind.kids --no-create      # only fix groups
          %(prog)s --audit                            # who is missing team-statik
          %(prog)s --audit --fix                      # ...and add them

        Standard set: jira-software-users + confluence-users + team-statik
        (+ team-<animal>). team-statik is what grants project BROWSE.
        """),
    )
    p.add_argument("address", nargs="?", default="",
                   help="Email address of the user (e.g. zias@thekind.kids)")
    p.add_argument("--team", default="",
                   help="Team group, with or without prefix (e.g. 'rhino')")
    p.add_argument("--tempo-admin", action="store_true",
                   help="Also add tempo-account-administrators (log time for others)")
    p.add_argument("--no-create", action="store_true",
                   help="Never invite; only fix the groups of an existing user")
    p.add_argument("--audit", action="store_true",
                   help="Report team members missing team-statik, then exit")
    p.add_argument("--fix", action="store_true",
                   help="With --audit: add the missing users to team-statik")
    p.add_argument("--all-teams", action="store_true",
                   help=f"With --audit: also treat {'/'.join(sorted(REVIEW_TEAMS))} "
                        f"members as gaps instead of listing them for review")
    p.add_argument("--email", default="", help="Atlassian account email")
    p.add_argument("--token", default="", help="Atlassian API token")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would change without writing")
    args = p.parse_args()

    at_email = args.email or os.environ.get("ATLASSIAN_EMAIL", "")
    at_token = args.token or os.environ.get("ATLASSIAN_API_TOKEN", "")
    if not at_email or not at_token:
        print("❌ Missing credentials. Set ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN")
        print("   or pass --email and --token arguments.")
        sys.exit(1)

    if args.audit:
        success = audit(fix=args.fix, all_teams=args.all_teams,
                        at_email=at_email, at_token=at_token,
                        dry_run=args.dry_run)
    else:
        if not args.address:
            p.error("an email address is required (or use --audit)")
        if args.fix or args.all_teams:
            p.error("--fix and --all-teams only apply to --audit")
        success = onboard(
            address=args.address, team=args.team, tempo_admin=args.tempo_admin,
            no_create=args.no_create, at_email=at_email, at_token=at_token,
            dry_run=args.dry_run,
        )

    sys.exit(0 if success else 1)
