# GitHub Publishing Notes

The source tree is intended to be GitHub-ready.

## Before first public push

1. Choose the repository owner/organization.
2. Add the final GitHub repository URL to `pyproject.toml` project URLs when known.
3. Review author/contributor metadata.
4. Keep secrets out of the repository.
5. Never commit ESI refresh tokens, local.py secrets, database passwords or Discord webhook URLs.
6. Run the local smoke-test gate.

## Suggested first repository flow

```bash
git init
git add .
git commit -m "Initial AA FleetOps alpha"
git branch -M main
git remote add origin <YOUR-GITHUB-REPOSITORY>
git push -u origin main
```

## Release discipline

Recommended lifecycle:

```text
Development source
-> local AA smoke test
-> alpha wheel
-> staging AA + real ESI test
-> fixes
-> release candidate
-> production validation
-> stable release
```

Do not publish a stable `1.0.0` merely because the package builds. Real ESI/Celery/staging behavior must be proven first.
