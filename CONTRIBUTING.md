# Contributing

AA FleetOps is intended as an open-source Alliance Auth community app.

Before submitting a change:

```bash
python -m compileall fleetops
python tests/test_calculations.py
```

Keep alliance policy configurable but keep the core workflow simple. Avoid adding a general rule DSL unless a future version has a demonstrated cross-alliance requirement for it.

Do not log or expose Discord webhook URLs, ESI access tokens or ESI refresh tokens.
