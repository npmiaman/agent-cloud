## What and why

<!-- What does this change, and what problem does it solve? Link the issue: Fixes #123 -->

## How it was tested

<!-- Commands you ran and what they showed. New behaviour needs a test. -->

## Checklist

- [ ] `pytest -q` passes, with new tests for new behaviour
- [ ] `ruff check .` passes
- [ ] Docs updated (README, `docs/`, `/llms.txt` for agent-facing changes)
- [ ] `CHANGELOG.md` updated under **Unreleased**
- [ ] For performance-sensitive changes: before/after numbers from `python bench/bench.py`
- [ ] For security-sensitive changes (auth, storage, permissions, trust): threat considered in `SECURITY.md`
