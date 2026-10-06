# SRP Provider Interface

FleetOps does not make its core FleetOperation depend on one SRP application.

`fleetops.providers.srp` owns a small provider registry. The bundled alpha provider attempts to integrate with Alliance Auth's built-in `allianceauth.srp` app. A future Better SRP app can register its own provider without modifying FleetOps core.

## Provider contract

A provider needs:

```python
class BetterSRPProvider:
    key = "better_srp"

    def available(self) -> bool:
        return True

    def create_for_operation(self, operation):
        ...
```

Return:

```python
from fleetops.providers.srp import SRPLinkResult

return SRPLinkResult(
    provider="better_srp",
    reference=str(your_srp_object.pk),
    url=your_srp_object.get_absolute_url(),
    created=True,
    message="Better SRP fleet created.",
)
```

Register the provider from your app's safe startup hook / `AppConfig.ready()`:

```python
from fleetops.providers.srp import register_srp_provider

register_srp_provider(BetterSRPProvider())
```

Then configure FleetOps Administration:

```text
SRP Provider = better_srp
```

or leave it as:

```text
SRP Provider = auto
```

`auto` currently checks preferred provider keys in this order:

1. `better_srp`
2. `aa_srp`
3. `allianceauth_builtin`
4. other registered available providers

## Important behavior

SRP is intentionally a **non-blocking** Fleet Start step.

If an SRP provider is missing, its schema is incompatible, or SRP creation fails:

- the FleetOperation still starts;
- Ping/MOTD/tracking still continue;
- the operation records `srp_error`;
- the Operation Actions panel reports the SRP step;
- no SRP URL is presented until creation/linking succeeds.

This makes FleetOps safe to deploy on installations that do not use SRP at all and makes a future Better SRP integration an optional extension rather than a hard runtime dependency.
