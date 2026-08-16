# Ralph Agent Instructions — Verax Statement Data Integration (Phase 1)

You are an autonomous coding agent working on the Verax publishing backend.

## Project context (read first)

- Repo root: `/Users/stevengarcia/VERAX_2/verax_backend` (FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL). Always work from the repo root.
- The full product spec is at `docs/PRD-statement-data-integration.md`. Read §2 (source data ground truth), §5 (data model), §6 (import pipeline), §7 (validation rules) before implementing anything. The PRD encodes verified facts about 2,612 real statement files — do not deviate from its parsing rules.
- Real statement fixtures with verified expected values: `tests/fixtures/statements/` (7 PDF+XLSX pairs + `expected_values.json`). Every parser story must test against these REAL files, not synthetic ones.
- Python: use `venv/bin/python` (the repo's virtualenv). Install new deps with `venv/bin/pip install ...` AND add them to `requirements.txt`. `openpyxl` is NOT yet installed (needed for XLSX); `pdfplumber==0.11.0` IS installed (use it for PDFs — do not require the `pdftotext` binary).
- DB: SQLAlchemy models live in `app/models/models.py`; engine/Base in `app/database/database.py`; settings in `app/settings/settings.py`. Alembic migrations in `migrations/versions/`. For tests, use a temporary SQLite database (`sqlite:///` with `Base.metadata.create_all`) — do NOT touch the dev Postgres.
- Routers live in `app/routers/`, registered in `app/main.py`. Services in `app/services/`.

## IMPORTANT: pre-existing uncommitted changes

`app/models/models.py` and `app/routers/auth.py` carry 5 uncommitted lines (an `auto_register_enabled` feature flag) that belong to the user. On your FIRST iteration only: after creating the branch, commit those existing changes alone as `chore: preserve pre-existing auto_register_enabled WIP` before starting your story. Never revert or delete them.

## Your Task

1. Read the PRD at `scripts/ralph/prd.json`
2. Read the progress log at `scripts/ralph/progress.txt` (check Codebase Patterns section first)
3. Check you're on the correct branch from PRD `branchName`. If not, check it out or **create it from the `newui` branch** (NOT main — the team works on `newui`).
4. Pick the **highest priority** user story where `passes: false`
5. Implement that single user story
6. Run quality checks (see below)
7. Update CLAUDE.md files if you discover reusable patterns
8. If checks pass, commit ALL changes with message: `feat: [Story ID] - [Story Title]`
9. Update `scripts/ralph/prd.json` to set `passes: true` for the completed story
10. Append your progress to `scripts/ralph/progress.txt`

## Quality checks (all must pass before committing)

```bash
venv/bin/python -c "import app.main"                 # app imports cleanly
venv/bin/python -m pytest tests/ -x -q               # all tests green
```

If `pytest` is not installed yet: `venv/bin/pip install pytest` and add to `requirements.txt` (US-001 does this).

## Progress Report Format

APPEND to `scripts/ralph/progress.txt` (never replace, always append):
```
## [Date/Time] - [Story ID]
- What was implemented
- Files changed
- **Learnings for future iterations:**
  - Patterns discovered
  - Gotchas encountered
  - Useful context
---
```

## Consolidate Patterns

If you discover a **reusable pattern** future iterations should know, add it to the `## Codebase Patterns` section at the TOP of progress.txt. Only general, reusable patterns — not story-specific details.

## Quality Requirements

- ALL commits must pass the quality checks. Do NOT commit broken code.
- Keep changes focused and minimal. Follow existing code patterns (look at neighboring routers/models before writing new ones).
- Money values: store as `Numeric(14, 6)` in SQLAlchemy / `Decimal` in Python. Never float-compare money — compare with a `Decimal('0.02')` tolerance where the PRD specifies.
- Statement files are immutable records — parsers must never modify fixture files.

## Stop Condition

After completing a user story, check if ALL stories have `passes: true`. If ALL are complete and passing, reply with:
<promise>COMPLETE</promise>

Otherwise end your response normally (another iteration picks up the next story).

## Important

- Work on ONE story per iteration
- Commit frequently, keep checks green
- Read the Codebase Patterns section in progress.txt before starting
