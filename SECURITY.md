# Security policy

## Supported versions

Security fixes go into the latest release of AA FleetOps. Older releases are not patched; upgrade to the
latest release.

## Reporting a vulnerability

Report a vulnerability privately. Open the [Security tab](https://github.com/yilifaer/aa-fleetops/security)
of the repository and choose **Report a vulnerability**, or use
[this link](https://github.com/yilifaer/aa-fleetops/security/advisories/new). Do not open a public issue or
discussion for it.

Please include:

- the FleetOps, Alliance Auth and django-esi versions;
- what you found and what an attacker gains from it;
- the steps to reproduce it, as short as you can make them.

AA FleetOps is maintained by volunteers. Expect a first reply within a week. A confirmed problem is fixed in a
new release, and the advisory names the versions that are affected.

## What belongs here

Examples of problems to report privately:

- reading or changing fleets, statistics, SRP claims or configuration without the matching permission;
- ESI tokens or Discord webhook URLs reaching a page, a log, a stored error message or the audit log;
- a way to make FleetOps ping or mention people it should not;
- the public comms join page showing more than a voice link.

Bugs that do not expose data or bypass a permission are ordinary issues.

## Secrets

Never post an ESI token, a Discord webhook URL, a database password or a login cookie in an issue, a
discussion or a pull request, and check logs for them before you paste any. If a webhook URL has been
exposed, delete the webhook in Discord and create a new one; then enter the new URL on the **Ping Channels**
page of the FleetOps administration.
