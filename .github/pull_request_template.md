## Summary

What does this change do, and why?

## Areas

- [ ] Fleet start / ESI
- [ ] Ping / MOTD
- [ ] Tracking / fleet view
- [ ] PAP
- [ ] Statistics
- [ ] FC incentives
- [ ] SRP
- [ ] Permissions / audit
- [ ] Translations
- [ ] Documentation / packaging

## Checklist

- [ ] Tests are added or updated and the full suite passes
- [ ] Model changes come with their migration (`python manage.py makemigrations fleetops --check --dry-run` reports nothing)
- [ ] New texts are marked for translation; the catalogs are updated when you can (`docs/translations.md`)
- [ ] CHANGELOG entry under Unreleased
- [ ] Documentation is updated
- [ ] Alliance Auth 5.2 to 5.x and django-esi 9 and 10 still work
- [ ] No ESI tokens, webhook URLs or other secrets in logs, pages, stored errors or the audit log
