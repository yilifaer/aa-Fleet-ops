# SRP providers

An SRP provider creates the SRP link of a fleet when the fleet starts. FleetOps does not tie a fleet to one SRP
application: `fleetops.providers.srp` keeps a small provider registry, and other SRP apps can register their own
provider without changing FleetOps.

FleetOps ships three providers:

| Key | App | Used when |
|---|---|---|
| `fleetops` | FleetOps SRP, the ship replacement program of FleetOps ([configuration](configuration.md#fleetops-srp)) | FleetOps SRP is enabled in the SRP settings |
| `aasrp` | [aa-srp](https://github.com/ppfeufer/aa-srp) (app label `aasrp`) | aa-srp is installed |
| `allianceauth_builtin` | Alliance Auth's built-in `allianceauth.srp` app | the app is installed |

## Choosing the provider

**Administration** > **General Settings** > **SRP provider** lists **Automatic** and every registered provider,
marked installed (FleetOps SRP: enabled) or not installed (not enabled). An SRP link is created at fleet start
only while **SRP auto create** is ticked; **Retry SRP** on the fleet page creates one later.

*Automatic* checks the providers in this order and uses the first that is available:

1. `fleetops` (only while FleetOps SRP is enabled)
2. `aasrp`
3. `allianceauth_builtin`
4. other registered providers that are available

The key `aa_srp`, stored by older versions, is read as `aasrp`. When a provider you picked is not installed, the
SRP step reports `SRP provider '<key>' is not installed.`; when `fleetops` is picked while FleetOps SRP is off,
it reports `FleetOps SRP is not enabled.`; in *Automatic* mode without any available provider, it reports `No
supported SRP provider is available.`

A site that does not enable FleetOps SRP sees no change. A site that enables it and still wants aa-srp or
Alliance Auth SRP links picks that provider explicitly instead of *Automatic*. A killmail that is also claimed
in aa-srp or Alliance Auth SRP shows a warning on the FleetOps claim ("Also claimed in …"); nothing is blocked,
and nothing is imported from the other apps except that check.

## FleetOps SRP provider

At fleet start (and on **Retry SRP** or **Create SRP link**) the provider creates the fleet's FleetOps SRP link:
one row per fleet, with a random code, the FC's user and main character name at that moment, open until an SRP
manager closes it. The link is stored on the fleet as the SRP reference and URL (`/fleetops/srp/link/<code>/`),
so `{{ srp_url }}` in ping and MOTD templates, the **Open SRP** button and the SRP card of the fleet page all use
it.

Creating the link is idempotent: a second start step, a retry or a click on **Create SRP link** finds the link
that exists and changes nothing. It writes only to the FleetOps database, so it runs before the ping and the
MOTD and cannot fail on a missing app. Fleets started from a PAP link get their link when they are created.
Creating, opening and closing a link are audited (`srp.link_create`, `srp.link_open`, `srp.link_close`); the
audit entry does not contain the link code.

## aa-srp provider

At fleet start (and on **Retry SRP**) FleetOps creates an aa-srp SRP link for the fleet:

| aa-srp field | Value |
|---|---|
| `srp_name` | `<fleet type> — <FC character> — YYYY-MM-DD HH:MM EVE` (fleet start time in EVE time) |
| `fleet_time` | Fleet start time (the scheduled time, or the current time if the fleet has not started) |
| `fleet_doctrine` | Doctrine name, or the fleet type when there is none |
| `fleet_commander` | The FC's main character, else the FC character |
| `fleet_type` | The enabled aa-srp fleet type whose name equals the FleetOps fleet type's name or short name (ignoring case), else none |
| `aar_link` | The FleetOps fleet page (absolute when `SITE_URL` is set) |
| `creator` | The FC's Alliance Auth user |

aa-srp fills in the SRP code when the link is saved. The **Open SRP** button of the fleet page opens aa-srp's
request page for that code (`/ship-replacement/srp-link/<code>/request-srp/`).

- **No second link.** Before creating a link, FleetOps looks for an aa-srp link whose AAR link is this fleet's
  page and reuses it, so a start interrupted between creating the link and saving the fleet never creates a
  second one. Retries are serialised by a row lock, and a fleet that already has an SRP link is never linked
  again.
- **Permissions.** The link is created for the FC without checking aa-srp's own permissions, like the built-in
  provider. Administrators control it with **SRP auto create**. Pilots need aa-srp's own permissions to file
  requests, and aa-srp accepts requests only while the link is Active.
- **Schema check.** If a newer aa-srp adds a required field that FleetOps does not know, the SRP step reports
  `Unsupported aasrp schema; required fields: …` and nothing is created.

## Alliance Auth SRP provider

The adapter reads the fields of Alliance Auth's `SrpFleetMain` model instead of importing its forms or views, so
it keeps working across Alliance Auth releases. It fills the fleet name, time, doctrine, commander and a random
8-character SRP code. When the model has a required field FleetOps does not know, the SRP step reports
`Unsupported built-in SRP schema; required fields: …` and nothing is created.

## Provider contract

A provider needs a `key`, an `available()` method and a `create_for_operation(operation)` method:

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

`create_for_operation` returns an `SRPLinkResult`:

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

Register the provider from your app's `AppConfig.ready()` or another safe startup hook:

```python
from fleetops.providers.srp import register_srp_provider

register_srp_provider(MySRPProvider())
```

Then choose it in **General Settings** > **SRP provider**, or leave *Automatic*, which uses it when neither
FleetOps SRP, aa-srp nor the built-in SRP app is available.

A provider may also set `state_labels` (a pair such as `("enabled", "not enabled")`, used instead of "installed"
and "not installed" in the settings list) and `unavailable_message` (the text of the SRP step when the provider
is chosen but not available).

## Failure handling

The SRP link is a step of fleet start that never blocks the start. If a provider is missing, its schema is
incompatible or the creation fails:

- the fleet still starts, and the ping, MOTD and tracking continue;
- the fleet records `srp_error` and the step shows the reason on the fleet page (**Retry SRP** tries again);
- no SRP address is used in the messages until a link exists.

A site that does not use SRP is not affected, and no SRP app is a runtime dependency.
