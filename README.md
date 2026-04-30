# Jira Project Tools

Deterministic scripts for managing Jira projects — create and close/archive — following The Kind Kids' Handbook (Confluence).

## Scripts

| Script | What it does |
|--------|-------------|
| `tools/close_project.py` | Archive a Jira project by applying the `Archived Scheme / STATIK` permission scheme |
| `tools/create_project.py` | Create a company-managed Kanban project, sharing settings from INTSTA |

## Prerequisites

```bash
export ATLASSIAN_EMAIL="your-email@statik.be"
export ATLASSIAN_API_TOKEN="your-api-token"
```

API tokens: https://id.atlassian.com/manage-profile/security/api-tokens

## Usage

### Close (archive) a project

```bash
python3 tools/close_project.py SHICLA
python3 tools/close_project.py SHICLA --dry-run
python3 tools/close_project.py SHICLA --force  # skip unresolved-issues check
```

**Process:**
1. Verifies project exists
2. Checks current permission scheme (skips if already archived)
3. Counts unresolved issues (fails if > 0, unless `--force`)
4. Applies `Archived Scheme / STATIK` permission scheme
5. Verifies the change

### Create a project

```bash
python3 tools/create_project.py WOOWEB "Website WooCommerce" \
  --pm-email "lore@statik.be" \
  --category "Panda / Craft"

python3 tools/create_project.py WOOWEB "Website WooCommerce" \
  --pm-email "lore@statik.be" \
  --category "Panda / Craft" \
  --dry-run
```

**Process:**
1. Looks up project lead by email
2. Looks up project category by name
3. Fetches INTSTA permission + notification schemes
4. Creates company-managed Kanban project
5. Applies INTSTA schemes
6. Sets category
7. Verifies everything

### What remains MANUAL after creation

| Step | Who |
|------|-----|
| Create project in Fichenbak | Strategist |
| Fill Google Sheet request | Strategist |
| Notify `#nieuweprojecten` | Anyone |
| Create Tempo Customer | Luk / Leen |
| Create Tempo Accounts (Voortraject / Implementatie) | PM |
| Set Default Account in Jira | PM |
| Run Automation on Epics (lightning bolt) | PM |
| Fill PO, GL, max budget in Fichenbak | Strategist |

## Reference

Based on [The Kind Kids' Handbook → Jira & Tempo](https://statik.atlassian.net/wiki/spaces/INTHAN/pages/121438257/Jira+Tempo) (Confluence).

Key pages:
- [Een nieuw project maken in JIRA](https://statik.atlassian.net/wiki/spaces/INTHAN/pages/1736706)
- [Een project afsluiten](https://statik.atlassian.net/wiki/spaces/INTHAN/pages/1589346316)