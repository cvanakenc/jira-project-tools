# Jira Project Tools

Deterministic scripts for managing Jira projects and users — provision, archive, onboard — following The Kind Kids' Handbook (Confluence).

## Scripts

| Script | What it does |
|--------|-------------|
| `tools/provision.py` | Full project setup: creates Kanban project, copies INTSTA schemes, sets category + lead. Tempo is opt-in (`--tempo`) and retired |
| `tools/close_project.py` | Archive a project: checks unresolved issues, applies `Archived Scheme / STATIK`, verifies |
| `tools/onboard_user.py` | Invite a user and enforce the standard group set (`jira-software-users` + `confluence-users` + `team-statik` + `team-<animal>`); `--audit` finds anyone missing `team-statik` |
| `tools/offboard_user.py` | Offboard a leaver: inventory what they own, transfer it to a successor, then deactivate; `--audit` is read-only |

## Prerequisites

```bash
export ATLASSIAN_EMAIL="your-email@statik.be"
export ATLASSIAN_API_TOKEN="your-api-token"
```

API tokens: https://id.atlassian.com/manage-profile/security/api-tokens

## Usage

### Provision a project

```bash
python3 tools/provision.py SHICLA "The Belgian Alliance for Climate Action" \
  --pm-email "lore@statik.be" \
  --category "Panda / Craft"
```

**What it does (Phase 1 — Jira):**
1. Looks up project lead by email
2. Looks up project category by name
3. Fetches INTSTA permission + notification schemes
4. Creates company-managed Kanban project
5. Applies INTSTA schemes + adds the "Productive Budget" field to the project's screens
6. Sets category
7. Verifies everything
8. With `--productive-budget <id>`: creates the Voortraject + Implementatie epics
   carrying that budget, and stamps any issue already in the project

### Productive budget

`--productive-budget` takes the numeric Productive **budget (deal) id** — the one in
`app.productive.io/55588-the-kind-kids/projects/<project>/budgets/<id>`. Do not use the
`?filter=` query param on the budgets list; that is a base64 filter id, not a budget id,
and the Jira field will fail to resolve a name from it.

The budget usually only exists after provisioning, so `--budget-only` backfills an
existing project: it puts the field on the screens, creates whichever of the two epics
are missing, and stamps every issue. Safe to re-run.

```bash
python3 tools/provision.py TIGWEB \
  --pm-email "eva.boelen@statik.be" \
  --budget-only --productive-budget 4152761
```

### Tempo (retired — opt-in)

Statik no longer uses Tempo; time tracking lives in Productive. The script
therefore **never touches Tempo unless you ask it to**, even though
`~/.statik-jira-creds` always exports `TEMPO_API_TOKEN` — an exported token is
not intent. Pass `--tempo` (or an explicit `--tempo-token`) to opt in to the old
flow, which creates the Voortraject + Implementatie accounts.

```bash
# Opt in to the retired Tempo flow:
python3 tools/provision.py SHICLA "The Belgian Alliance for Climate Action" \
  --pm-email "lore@statik.be" \
  --category "Panda / Craft" \
  --tempo --customer-key SUI
```

`--no-tempo` is now a deprecated no-op: skipping Tempo is the default.

> Known bug in that path: the "Default account set" step writes a Jira project
> property `tempo-accounts-default-account-id` that Tempo never reads — real
> links live at `POST /4/account-links` (scope `PROJECT`). It reports success
> either way. Also, Tempo v4 addresses accounts by numeric **id** on GET but by
> **key** on DELETE.

### Close a project

```bash
python3 tools/close_project.py SHICLA
python3 tools/close_project.py SHICLA --dry-run
python3 tools/close_project.py SHICLA --force  # skip unresolved-issues check
```

### Onboard a user

```bash
python3 tools/onboard_user.py zias@thekind.kids --team rhino
python3 tools/onboard_user.py zias@thekind.kids --team rhino --tempo-admin
python3 tools/onboard_user.py zias@thekind.kids --no-create   # fix groups only
```

Creates the user if needed (sends the Atlassian invite), then adds whatever is
missing from the standard set. Safe to re-run — groups the user already has are
skipped, and a duplicate add is treated as success.

**`team-statik` is the one that matters.** It grants `BROWSE_PROJECTS` on
Statik's default permission scheme; the "Interne medewerkers" project role does
not. A user without it looks fully provisioned but opens Jira to nothing, so the
script always adds it rather than making it a flag.

Two things the API cannot do: `displayName` can't be set (the user picks it when
accepting the invite, or an org admin sets it at admin.atlassian.com), and a
pending account can't be reactivated with a Jira token. Group membership alone
does not activate a licence.

#### Audit

```bash
python3 tools/onboard_user.py --audit        # report gaps
python3 tools/onboard_user.py --audit --fix  # add them to team-statik
```

"Internal" is derived from `team-<animal>` membership rather than email domain —
`jira-software-users` also holds app/bot accounts and external customer users,
and most accounts hide their email address.

`team-shavedmonkey` members are listed separately under "review manually" and
left alone by `--fix`: it's a different legal entity, so withholding Statik
project access may be deliberate. Use `--all-teams` to treat them as gaps too.

### Offboard a user

```bash
python3 tools/offboard_user.py ben@statik.be --audit              # read-only: what do they own?
python3 tools/offboard_user.py ben@statik.be --to maarten@statik.be          # dry-run
python3 tools/offboard_user.py ben@statik.be --to maarten@statik.be --apply  # transfer
```

**Dry-run is the default here** — the opposite of `onboard_user.py`. One run can
rewrite hundreds of records (Laurie Tilmant's handover moved 64 Jira leads and
198 Tempo accounts), so writes need `--apply`.

**Deactivating an account reassigns nothing.** Project leads, Tempo account
leads, assigned issues and owned filters all keep pointing at the dead account,
so always transfer before deactivating. What gets moved:

| Item | How |
|---|---|
| Jira project leads | `PUT /project/{key}` — partial body is fine |
| Tempo account leads | `PUT /accounts/{key}` — **full** body, flat `leadAccountId`; GET wants the numeric id, PUT wants the string key |
| Tempo team leads | `PUT /teams/{id}` — full body, flat `leadAccountId` |
| Open issues | `PUT /issue/{key}/assignee`, or `--unassign` to send them back to the pool |
| Filters | `PUT /filter/{id}/owner` |
| Dashboards | **not automated** — Jira has no owner-change API; do it in the UI |

Closed Tempo accounts and archived Jira projects are skipped by default: they're
historical buckets, so moving their lead is audit churn with no operational
effect. `--include-closed` widens the sweep.

**Watch for filters that drive boards.** A board whose filter is owned by a
deactivated user is the failure mode worth avoiding — `--map-boards` reports
which boards depend on the leaver's filters. It scans every board, so it takes a
couple of minutes and is opt-in.

Already-deactivated accounts are invisible to `user/search`, which is exactly
the population you offboard. The script falls back to scanning group membership
with `includeInactiveUsers`; if the email is hidden too, pass `--account-id`.

#### Deactivation needs a different credential

The final flip is an org-level action. A Jira API token gets `HTTP 401` against
the org admin API, so by default the script prints the manual steps
(admin.atlassian.com → Directory → Users → Deactivate access). To automate it,
create an **org admin API key** at admin.atlassian.com → Settings → API keys:

```bash
export ATLASSIAN_ORG_API_KEY="..."
python3 tools/offboard_user.py ben@statik.be --to maarten@statik.be --deactivate --apply
```

Without that key the script will not claim the person is offboarded — it says
the account is still active.

## Full checklist after provision

```
[ ] Strategist: project exists in Fichenbak + Google Sheet
[ ] Slack: notified #nieuweprojecten
[ ] PM: Epics created + Automation run
[ ] Strategist: PO, GL, max budget filled in Fichenbak
[ ] PM: notify strategist that Jira is ready
```

## Reference

Based on [The Kind Kids' Handbook → Jira & Tempo](https://statik.atlassian.net/wiki/spaces/INTHAN/pages/121438257/Jira+Tempo) (Confluence).

Key pages:
- [Een nieuw project maken](https://statik.atlassian.net/wiki/spaces/INTHAN/pages/1736706)
- [Een project afsluiten](https://statik.atlassian.net/wiki/spaces/INTHAN/pages/1589346316)