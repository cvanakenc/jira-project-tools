# Jira Project Tools

Deterministic scripts for managing Jira projects — provision and archive — following The Kind Kids' Handbook (Confluence).

## Scripts

| Script | What it does |
|--------|-------------|
| `tools/provision.py` | Full project setup: creates Kanban project, copies INTSTA schemes, sets category + lead, and (optionally) creates Tempo accounts + sets default |
| `tools/close_project.py` | Archive a project: checks unresolved issues, applies `Archived Scheme / STATIK`, verifies |

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
5. Applies INTSTA schemes
6. Sets category
7. Verifies everything

**What it asks YOU to do (Phase 2 — Tempo):**
- Explicitly lists every manual step with URLs, customer key, and account naming
- If `--tempo-token` is provided, auto-creates Voortraject + Implementatie accounts and sets the default

```bash
# With Tempo auto-creation:
python3 tools/provision.py SHICLA "The Belgian Alliance for Climate Action" \
  --pm-email "lore@statik.be" \
  --category "Panda / Craft" \
  --tempo-token "t8r8y9Ql..."
```

### Close a project

```bash
python3 tools/close_project.py SHICLA
python3 tools/close_project.py SHICLA --dry-run
python3 tools/close_project.py SHICLA --force  # skip unresolved-issues check
```

## Full checklist after provision

```
[ ] Strategist: project exists in Fichenbak + Google Sheet
[ ] Slack: notified #nieuweprojecten
[ ] Leen/Luk: Tempo Customer created
[ ] PM: Tempo Accounts created (Voortraject + Implementatie)
[ ] PM: Default Account set in Jira Project Settings
[ ] PM: Epics created + Automation run
[ ] Strategist: PO, GL, max budget filled in Fichenbak
[ ] PM: notify strategist that Jira is ready
```

## Reference

Based on [The Kind Kids' Handbook → Jira & Tempo](https://statik.atlassian.net/wiki/spaces/INTHAN/pages/121438257/Jira+Tempo) (Confluence).

Key pages:
- [Een nieuw project maken](https://statik.atlassian.net/wiki/spaces/INTHAN/pages/1736706)
- [Een project afsluiten](https://statik.atlassian.net/wiki/spaces/INTHAN/pages/1589346316)