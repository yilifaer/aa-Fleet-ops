# SRP Provider Interface

FleetOps does not make its core FleetOperation depend on one SRP application.

`fleetops.providers.srp` owns a small provider registry. FleetOps ships three providers:

| Key | App | Used when |
|---|---|---|
| `fleetops` | FleetOps SRP, FleetOps' own SRP (see the README) | **FleetOps SRP** is enabled in the SRP settings |
| `aasrp` | [aa-srp](https://github.com/ppfeufer/aa-srp) (app label `aasrp`) | aa-srp is installed |
| `allianceauth_builtin` | Alliance Auth's built-in `allianceauth.srp` app | the app is installed |

Other SRP apps can register their own provider without modifying FleetOps core.

## Choosing the provider

FleetOps Administration → General Settings → **SRP provider** lists **Automatic (FleetOps SRP when enabled, then aa-srp, then Alliance Auth SRP)** and every registered provider, marked installed (FleetOps SRP: enabled) or not installed (not enabled). SRP links are only created at fleet start while **SRP auto create** is ticked; **Retry SRP** on the fleet page creates one later.

`auto` checks the providers in this order:

1. `fleetops` (only while FleetOps SRP is enabled)
2. `aasrp`
3. `allianceauth_builtin`
4. other registered available providers

The key `aa_srp` stored by older versions is read as `aasrp`. When an explicitly chosen provider is not installed, the SRP step reports `SRP provider '<key>' is not installed.`; when `fleetops` is chosen while FleetOps SRP is off it reports `FleetOps SRP is not enabled.`; in `auto` mode without any available provider it reports `No supported SRP provider is available.`

A site that does not enable FleetOps SRP sees no change. A site that enables it and still wants aa-srp or Alliance Auth SRP links picks that provider explicitly instead of *Automatic*. A killmail that is also claimed in aa-srp or Alliance Auth SRP shows a warning on the FleetOps claim ("Also claimed in …"); nothing is blocked, and nothing is read back from the old apps except that check.

## FleetOps SRP provider

At fleet start (and on **Retry SRP** / **Create SRP link**) the provider creates the fleet's FleetOps SRP link: one row per fleet, with a random code, the FC's user and main character name at that moment, open until an SRP manager closes it. The link is stored on the fleet as the SRP reference and URL (`/fleetops/srp/link/<code>/`), so `{{ srp_url }}` in ping and MOTD templates, the **Open SRP** button and the SRP card of the fleet page all use it. Creating it is idempotent: a second start step, a retry or a click on **Create SRP link** finds the link that exists and changes nothing. It only writes to the FleetOps database, so it runs before the ping and the MOTD and cannot fail on a missing app. Fleets started from a PAP link get the link when they are created. Creating, opening and closing a link are audited (`srp.link_create`, `srp.link_open`, `srp.link_close`; the audit entry does not contain the link code).

## aa-srp provider

At fleet start (and on **Retry SRP**) FleetOps creates an aa-srp SRP link for the fleet:

| aa-srp field | Value |
|---|---|
| `srp_name` | `<fleet type> — <FC character> — YYYY-MM-DD HH:MM EVE` (fleet start time in EVE time) |
| `fleet_time` | Fleet start time (the scheduled time or the current time if the fleet has not started) |
| `fleet_doctrine` | Doctrine name, or the fleet type when there is none |
| `fleet_commander` | The FC's main character, else the FC character |
| `fleet_type` | The enabled aa-srp fleet type whose name equals the FleetOps fleet type's name or short name (ignoring case), else none |
| `aar_link` | The FleetOps fleet page (absolute when `SITE_URL` is set) |
| `creator` | The FC's Alliance Auth user |

aa-srp fills in the SRP code when the link is saved. The fleet page's **Open SRP** button opens aa-srp's request page for that code (`/ship-replacement/srp-link/<code>/request-srp/`).

- **No second link:** before creating a link, FleetOps looks for an aa-srp link whose AAR link is this fleet's page and reuses it, so a start interrupted between creating the link and saving the fleet never creates a second one. Retries are also serialised by a row lock, and a fleet that already has an SRP link is never linked again.
- **Permissions:** the link is created for the FC without checking aa-srp's own permissions (the same as the built-in provider). Admins control it with **SRP auto create**. Pilots need aa-srp's own permissions to file requests, and aa-srp only accepts requests while the link is Active.
- **Schema check:** if a newer aa-srp adds a required field FleetOps does not know, the SRP step reports `Unsupported aasrp schema; required fields: …` and nothing is created.

## Provider contract

A provider needs:

```python
class MySRPProvider:
    key = "my_srp"
    # Optional: the name shown in General Settings.
    label = "My SRP"

    def available(self) -> bool:
        return True

    def create_for_operation(self, operation):
        ...
```

Return:

```python
from fleetops.providers.srp import SRPLinkResult

return SRPLinkResult(
    provider="my_srp",
    reference=str(your_srp_object.pk),
    url=your_srp_object.get_absolute_url(),
    created=True,
    message="SRP fleet created.",
)
```

Register the provider from your app's safe startup hook / `AppConfig.ready()`:

```python
from fleetops.providers.srp import register_srp_provider

register_srp_provider(MySRPProvider())
```

Then choose it in FleetOps Administration → General Settings → **SRP provider**, or leave **Automatic**, which uses it when neither FleetOps SRP, aa-srp nor the built-in SRP app is available.

A provider may also set `state_labels` (a pair such as `("enabled", "not enabled")`, used instead of "installed" / "not installed" in the settings list) and `unavailable_message` (the text of the SRP step when the provider is chosen but not available).

## Important behavior

SRP is intentionally a **non-blocking** Fleet Start step.

If an SRP provider is missing, its schema is incompatible, or SRP creation fails:

- the FleetOperation still starts;
- Ping/MOTD/tracking still continue;
- the operation records `srp_error`;
- the Operation Actions panel reports the SRP step;
- no SRP URL is presented until creation/linking succeeds.

This makes FleetOps safe to deploy on installations that do not use SRP at all and keeps every SRP app an optional extension rather than a hard runtime dependency.
