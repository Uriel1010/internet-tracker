# Contributing Guide

Thanks for considering contributing!

## Ways to Help
- Bug reports (include reproduction steps, logs, environment)
- Feature requests (state use-case & value)
- Documentation improvements
- Performance profiling & optimizations
- Test coverage expansion

## Development Setup
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Run tests:
```bash
pytest -q
```

## Branching & PRs
- Fork or create a feature branch off `main`.
- Keep commits focused; rebase before opening PR if needed.
- Reference related issue numbers.
- Provide before/after screenshots for UI changes.

## Coding Style
- Follow PEP8 / Black formatting
- Prefer clear, explicit names
- Add docstrings for non-trivial functions

## Tests
Add tests under `tests/` for new logic, especially edge cases & regressions.

## Commit Messages
Use present tense, imperative mood:
```
Add jitter calculation to metrics utils
Fix SSE reconnect race condition
Refactor monitoring loop for clarity
```

## Release Notes
For user-facing changes, append a bullet to UNRELEASED section in CHANGELOG if added later.

## Security
Do not open public issues for sensitive vulnerabilities. See SECURITY.md.

## License
By contributing you agree your contributions are licensed under the project MIT license.
