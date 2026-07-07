<!-- Thanks for contributing to Claude Mate! Keep this PR focused. -->

## What & why

<!-- What does this change, and why? Link any related issue, e.g. "Closes #12". -->

## Test ladder

Which rungs did you exercise? (see [CONTRIBUTING.md](../CONTRIBUTING.md) and [docs/TESTING.md](../docs/TESTING.md))

- [ ] 1 — Daemon in `--mock` mode
- [ ] 2 — Serial loopback / protocol
- [ ] 3 — Firmware on the bench
- [ ] 4 — End-to-end with real Claude Code sessions
- [ ] Ran the automated tests: `for t in tools/test_*.py; do python3 "$t"; done`

## Checklist

- [ ] Follows the coding conventions in CONTRIBUTING.md (daemon: Python 3.9+, `pyserial` only; firmware: RAM-frugal; the hook never blocks a turn).
- [ ] Updated README / docs / CHANGELOG where relevant.
- [ ] Commits are focused and explain the *why*.
