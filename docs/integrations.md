# Integrations

FleetOps needs only Alliance Auth and django-esi. Everything below is optional: FleetOps detects what is
installed and works without the rest. A Simplified Chinese version of this page is in
[zh-CN/integrations.md](zh-CN/integrations.md).

| App or service | What it adds | Without it |
|---|---|---|
| [fittings](#fittings) | Doctrine list on Start Fleet, doctrine links and hulls in pings and MOTDs | `None / Custom` doctrine only |
| [django-eveonline-sde](#django-eveonline-sde) (`eve_sde`) | English ship and system names, ship classes, bridgeable mass, 3-Jump Map | Composition by fleet position and ship type rules only; no map |
| [FleetOps SRP, aa-srp, Alliance Auth SRP](#srp-apps) | An SRP link for every fleet | No SRP link; the SRP step is skipped |
| [Discord webhooks](#discord) | The ping | Copy the ping by hand |
| [Alliance Auth Discord service](#discord) | Reading the server's roles for mentions | Add role IDs by hand |
| [aa-charlink](#aa-charlink) | The FleetOps ESI scopes on the CharLink page | Pilots authorize on FleetOps pages |
| [AFAT](#afat) | Shares ESI tokens with PAP links | Nothing changes |
| aa-fleetpings, optimer, django-eveuniverse, aadiscordbot | Not used by FleetOps | They can be installed next to it |

## fittings

When an installed app with the label `fittings` exposes a doctrine model, FleetOps lists its doctrines on
**Start Fleet** and on the PAP-link fleet page. If no supported doctrine model is found, **None / Custom**
stays available and FleetOps continues normally.

- FCs see the doctrines Fittings shows them: a doctrine in a category limited to groups the FC is not in is
  hidden. Users with `fittings.manage` see every doctrine. The administration forms (for example the SRP payout
  table) list all doctrines.
- A fleet stores the doctrine's name and source, so history does not depend on the doctrine continuing to
  exist. For a Fittings doctrine it also stores the fits (fit name and hull, in doctrine order) when the fleet
  starts or is pre pinged. They fill the template variables `doctrine_ships` and `doctrine_fits`;
  **Regenerate & Push MOTD** reads them again.
- Doctrines are linked to their Fittings page in pings and MOTDs (`doctrine_url`). Members need
  `fittings.access_fittings` to open the page.
- Install it with `pip install "aa-fleetops[fittings]"`, or install `fittings` yourself.

## django-eveonline-sde

The app label is `eve_sde`. FleetOps reads names and ship data from it:

- Ship and solar system names are stored in English whatever language the viewer or the worker uses, and shown
  in English everywhere. The names of solar systems also let the in-game MOTD link the form-up place.
- The **ship groups** list of the general settings (**Ship groups without bridge**) is checked against the SDE: an
  ID that is not a ship group is refused.
- The fleet **composition** classifies ships by ship function: ship classes, and the mass that a titan bridge
  can carry (the **Ship groups without bridge** setting). Rules can be entered by exact English name.
- The **3-Jump Map** tab shows active fleet members in systems up to three jumps from the FC's system, built
  from the Stargate data of the SDE. It is a fleet-member proximity view, not a map.

Without the SDE, the composition counts ship functions from fleet positions and ship type rules and lists the
other ships as unclassified; ship groups and types can still be entered by ID. If the Stargate data cannot be
read, the Map tab says that routing data is unavailable. Install it with
`pip install "aa-fleetops[sde]"`. After you add the SDE to a site with older fleets, run
`python manage.py fleetops_refresh_names --dry-run` and then without `--dry-run` to rewrite stored names in
English.

## ESI endpoints without a token

Besides the fleet endpoints that use the FC's token, FleetOps reads two public ESI endpoints for the default MOTD:
a character's bloodline (`GET /characters/{id}/`, to make the right in-game link to the character; 30 days of
cache, a failure is cached for ten minutes and the link then uses a default character type) and the ID of a solar
system by name (`POST /universe/ids/`, only when the form-up text is a name the SDE does not know; found names are
cached 30 days, unknown ones one day). Both never block a start: when ESI does not answer, the MOTD carries the
plain name. Pings without a fleet make no such request.

## SRP apps

FleetOps can create an SRP link for every fleet when it starts (**SRP auto create**). Three providers exist:
FleetOps SRP, aa-srp (`aasrp`) and Alliance Auth's built-in SRP app (`allianceauth.srp`). **SRP provider** in
the general settings is *Automatic* (FleetOps SRP while it is enabled, then aa-srp, then Alliance Auth SRP) or a
provider you pick. The link is created without blocking the start: if the provider is missing or fails, the
fleet still starts and the SRP step shows the reason. The link is available as `srp_url` in ping and MOTD
templates. Details, the provider interface for other SRP apps and the aa-srp fields are in
[SRP providers](srp-providers.md); FleetOps SRP itself is described in the
[configuration reference](configuration.md#fleetops-srp).

## Discord

- **Webhooks** send the ping. Each ping channel stores one webhook URL, which is write-only: no page shows it
  again. See [ping channels](configuration.md#ping-channels).
- **Roles for mentions**: the Alliance Auth Discord service (`allianceauth.services.modules.discord`) and
  `DISCORD_GUILD_ID` let **Sync roles from Discord** read the server's roles. Without them, add role IDs by
  hand. FleetOps does not use aadiscordbot.
- Discord notifies a role only when it is mentionable or when the webhook may mention every role.

## aa-charlink

When aa-charlink is installed, its page offers two FleetOps imports:

- **FleetOps FC fleet control**: `esi-fleets.read_fleet.v1` and `esi-fleets.write_fleet.v1`, for users with
  `fleetops.start_fleet`. Not ticked by default.
- **FleetOps PAP links**: `esi-location.read_location.v1`, `esi-location.read_ship_type.v1` and
  `esi-location.read_online.v1`, for users with `fleetops.basic_access`. Ticked by default.

Characters linked there are ready on Start Fleet and on PAP link pages. CharLink checks the app label of every
hook, so `"fleetops"` must appear literally in `INSTALLED_APPS`. Without CharLink the hook is never loaded.

## AFAT

FleetOps does not depend on AFAT and has no AFAT import. The three location scopes of PAP links are the same as
those of AFAT's click tracking, so tokens that AFAT or CharLink already hold are reused. The two apps can run
side by side; each keeps its own records.

## Other fleet apps

aa-fleetpings and optimer are not used. FleetOps sends its own ping through Discord webhooks and does not read
or write optimer entries. They can be installed next to FleetOps.
