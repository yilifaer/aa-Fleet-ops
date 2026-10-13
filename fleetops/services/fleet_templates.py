"""Applying a fleet structure template (wing and squad names) to a running fleet.

Wings and squads are matched by position: template wing *i* is the *i*-th wing of the fleet in ESI
order, and squad *j* of a template wing is the *j*-th squad of that wing. The "template" job renames
what exists, creates what is missing and, when asked, deletes empty wings and squads beyond the
template. Members are never moved, so wings and squads with members are kept.

ESI answers fleet reads from a 5 second cache, so the job waits ``ESI_READ_LAG`` seconds before it
reads the wings again after creating some (EVE may give a new wing a squad of its own). Every call
uses the FC character's token and is one step of the job; a failed call is reported and the job
goes on, unless the failure ends it (handover, fleet gone, missing scope, ESI's error limit).
"""

import time

from fleetops.providers.esi import (
    FleetESIError,
    create_fleet_squad,
    create_fleet_wing,
    delete_fleet_squad,
    delete_fleet_wing,
    get_fleet_wings,
    rename_fleet_squad,
    rename_fleet_wing,
)
from fleetops.services import fleet_jobs, fleet_overlay, fleet_refresh
from fleetops.services.fleet_access import FleetManageError, require_writable
from fleetops.services.fleet_manage import FleetActionResult, job_failure, structure
from fleetops.services.fleet_rules import FleetRuleError, squad_members, wing_members

# Waits between ESI reads of the job; patched in tests.
sleep = time.sleep

TEMPLATE_GONE = "That structure template no longer exists or is disabled."
# Reads of the wings after creating some, until the new wings show.
READ_ATTEMPTS = 3


def job_label(template) -> str:
    return f"Apply template {template.name}"


# ---------------------------------------------------------------------------
# What a template changes
# ---------------------------------------------------------------------------


def wing_plan(existing, layout) -> list[tuple]:
    """``("rename", index, wing, name)`` and ``("create", index, None, name)`` for the template's wings."""
    plan = []
    for index, target in enumerate(layout):
        if index < len(existing):
            if existing[index]["name"] != target["name"]:
                plan.append(("rename", index, existing[index], target["name"]))
        else:
            plan.append(("create", index, None, target["name"]))
    return plan


def squad_plan(squads, names) -> list[tuple]:
    """``("rename", index, squad, name)`` and ``("create", index, None, name)`` for one wing's squads."""
    plan = []
    for index, name in enumerate(names):
        if index < len(squads):
            if squads[index]["name"] != name:
                plan.append(("rename", index, squads[index], name))
        else:
            plan.append(("create", index, None, name))
    return plan


def extra_plan(wings, mapped, layout, members) -> list[tuple]:
    """``(action, kind, unit, wing)`` for wings and squads beyond the template; action "delete" or "keep".

    ``mapped`` holds the wing id of each template wing (None when it does not exist). Squads beyond a
    template wing's squads and wings that no template wing maps to go, when they are empty.
    """
    members = list(members)
    by_id = {wing["id"]: wing for wing in wings}
    plan = []
    for wing_id, target in zip(mapped, layout):
        wing = by_id.get(wing_id)
        if wing is None:
            continue
        for squad in wing["squads"][len(target["squads"]) :]:
            plan.append(("keep" if squad_members(members, squad["id"]) else "delete", "squad", squad, wing))
    for wing in wings:
        if wing["id"] not in mapped:
            plan.append(("keep" if wing_members(members, wing["id"], wings) else "delete", "wing", wing, None))
    return plan


def estimate(existing, layout, *, members=(), delete_extra=False) -> int:
    """How many steps applying ``layout`` to the wings ``existing`` takes; at least 1."""
    steps = len(wing_plan(existing, layout))
    for index, target in enumerate(layout):
        squads = existing[index]["squads"] if index < len(existing) else []
        steps += len(squad_plan(squads, target["squads"]))
    if delete_extra:
        mapped = [wing["id"] for wing in existing[: len(layout)]]
        steps += len(extra_plan(existing, mapped, layout, members))
    return max(steps, 1)


# ---------------------------------------------------------------------------
# Starting the job
# ---------------------------------------------------------------------------


def apply_template(operation, template, *, actor, delete_extra=False) -> FleetActionResult:
    """Start the "template" job; ``delete_extra`` also deletes empty wings and squads beyond the template."""
    require_writable(operation, actor)
    if template is None or not template.is_active:
        raise FleetManageError(TEMPLATE_GONE, 404)
    try:
        layout = template.wings
    except FleetRuleError as exc:
        raise FleetManageError(f"The template “{template.name}” cannot be applied. {exc}", 400) from exc
    wings, members, known = structure(operation)
    job = fleet_jobs.start_job(
        operation,
        kind="template",
        label=job_label(template),
        payload={
            "template_id": template.pk,
            "name": template.name,
            "layout": template.layout,
            "wings": layout,
            "delete_extra": bool(delete_extra),
        },
        actor=actor,
        total=estimate(wings if known else [], layout, members=members.values(), delete_extra=delete_extra),
    )
    return FleetActionResult(
        True,
        f"Applying template “{template.name}” in the background. Members are not moved.",
        level="info",
        data={"job_id": job["id"]},
    )


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


class _TemplateRun:
    def __init__(self, job, operation, payload, actor):
        self.job = job
        self.operation = operation
        self.actor = actor
        self.layout = payload["wings"]
        self.delete_extra = bool(payload.get("delete_extra"))
        self.record = {
            "template_id": payload.get("template_id"),
            "template": payload.get("name", ""),
            "layout": payload.get("layout", ""),
            "delete_extra": self.delete_extra,
            "created": [],
            "renamed": [],
            "deleted": [],
            "kept": [],
            "failures": [],
        }
        self.changed = False

    # Plumbing

    def _fc(self):
        return self.operation.fc_user, self.operation.fc_character_id, self.operation.esi_fleet_id

    def wait(self, seconds=fleet_overlay.ESI_READ_LAG) -> None:
        """Wait for ESI's cache while keeping the job's heartbeat."""
        fleet_jobs.touch(self.job)
        if seconds > 0:
            sleep(seconds)
        fleet_jobs.touch(self.job)

    def wait_for_recent_changes(self) -> None:
        """A change made just before the job may not show in ESI's answers yet."""
        changes = [
            change["at"]
            for change in fleet_overlay.expectations(self.operation.pk)
            if change["kind"] in fleet_overlay.STRUCTURE_KINDS
        ]
        if changes:
            remaining = max(changes) + fleet_overlay.ESI_READ_LAG - time.time()
            if remaining > 0:
                self.wait(min(remaining, fleet_overlay.ESI_READ_LAG))

    def read(self, created=()) -> list[dict]:
        """The wings, read past every cache; read again while wings this job created are missing."""
        created = set(created)
        for attempt in range(READ_ATTEMPTS):
            if attempt:
                self.wait()
            self.check()
            try:
                wings = get_fleet_wings(*self._fc(), fresh=True)
            except FleetESIError as exc:
                if exc.code == "FLEET_NOT_FOUND":
                    problem = fleet_refresh.check_boss(self.operation)
                    if problem is not None:
                        raise fleet_jobs.JobStopped(fleet_refresh.problem_message(self.operation, problem)) from exc
                raise fleet_jobs.JobStopped(str(exc)) from exc
            if created <= {wing["id"] for wing in wings}:
                break
        return wings

    def set_remaining(self, steps) -> None:
        """Make the job's total the steps done plus ``steps`` still to come."""
        fleet_jobs.set_remaining(self.job, steps)

    def done(self, message, record=None, key=None, expected=None) -> None:
        if expected is not None:
            fleet_overlay.expect(self.operation.pk, expected)
        if key is not None:
            self.record[key].append(record)
        self.changed = self.changed or key in ("created", "renamed", "deleted")
        fleet_jobs.step(self.job, ok=True, message=message)

    def failed(self, where, exc, step) -> None:
        """Report a failed call; raises JobStopped when it ends the job."""
        message, stop = job_failure(self.job, self.operation, exc, step)
        self.record["failures"].append({"unit": where, "error": message})
        fleet_jobs.step(self.job, ok=False, message=f"{where}: {message}")
        if stop:
            raise fleet_jobs.JobStopped(stop) from exc

    def check(self) -> None:
        """Before each ESI call: the fleet can still be changed by the job's user (else JobStopped)."""
        fleet_jobs.ensure_writable(self.job, self.operation, self.actor)

    def created_unnamed(self, expected, entry) -> None:
        """A wing or squad exists now, but kept EVE's name."""
        fleet_overlay.expect(self.operation.pk, {**expected, "name": ""})
        self.record["created"].append({**entry, "name": ""})
        self.changed = True

    # Wings

    def rename_wing(self, wing, name) -> None:
        self.check()
        try:
            rename_fleet_wing(*self._fc(), wing["id"], name)
        except Exception as exc:
            self.failed(f"Wing “{wing['name']}”", exc, "Renaming a wing")
            return
        self.done(
            f"Renamed wing “{wing['name']}” to “{name}”.",
            {"kind": "wing", "wing_id": wing["id"], "old": wing["name"], "name": name},
            "renamed",
            {"kind": "wing_renamed", "wing_id": wing["id"], "name": name},
        )

    def create_wing(self, index, name):
        """Create and name a wing; returns its id, or None when it could not be created."""
        self.check()
        try:
            wing_id = create_fleet_wing(*self._fc())
        except Exception as exc:
            self.failed(f"Wing {index + 1} “{name}”", exc, "Creating a wing")
            return None
        expected = {"kind": "wing_created", "wing_id": wing_id}
        entry = {"kind": "wing", "wing_id": wing_id}
        try:
            self.check()
        except fleet_jobs.JobStopped:
            self.created_unnamed(expected, entry)
            raise
        try:
            rename_fleet_wing(*self._fc(), wing_id, name)
        except Exception as exc:
            self.created_unnamed(expected, entry)
            self.failed(f"Wing {index + 1} was created, but not named “{name}”", exc, "Renaming a wing")
            return wing_id
        self.done(
            f"Created wing {index + 1} “{name}”.", {**entry, "name": name}, "created", {**expected, "name": name}
        )
        return wing_id

    # Squads

    def rename_squad(self, wing_name, squad, name) -> None:
        self.check()
        try:
            rename_fleet_squad(*self._fc(), squad["id"], name)
        except Exception as exc:
            self.failed(f"{wing_name} / {squad['name']}", exc, "Renaming a squad")
            return
        self.done(
            f"Renamed squad “{squad['name']}” of {wing_name} to “{name}”.",
            {"kind": "squad", "squad_id": squad["id"], "wing": wing_name, "old": squad["name"], "name": name},
            "renamed",
            {"kind": "squad_renamed", "squad_id": squad["id"], "name": name},
        )

    def create_squad(self, wing_id, wing_name, name) -> None:
        self.check()
        try:
            squad_id = create_fleet_squad(*self._fc(), wing_id)
        except Exception as exc:
            self.failed(f"{wing_name} / {name}", exc, "Creating a squad")
            return
        expected = {"kind": "squad_created", "wing_id": wing_id, "squad_id": squad_id}
        entry = {"kind": "squad", "wing_id": wing_id, "squad_id": squad_id, "wing": wing_name}
        try:
            self.check()
        except fleet_jobs.JobStopped:
            self.created_unnamed(expected, entry)
            raise
        try:
            rename_fleet_squad(*self._fc(), squad_id, name)
        except Exception as exc:
            self.created_unnamed(expected, entry)
            self.failed(f"{wing_name}: a squad was created, but not named “{name}”", exc, "Renaming a squad")
            return
        message = f"Created squad “{name}” in {wing_name}."
        self.done(message, {**entry, "name": name}, "created", {**expected, "name": name})

    # Extras

    def remove(self, action, kind, unit, wing_name) -> None:
        name = unit["name"] or f"{kind.title()} {unit['id']}"
        where = f"squad “{name}” of {wing_name}" if kind == "squad" else f"wing “{name}”"
        entry = {"kind": kind, f"{kind}_id": unit["id"], "name": unit["name"]}
        if action == "keep":
            self.done(f"Kept the {where}: it has members.", entry, "kept")
            return
        self.check()
        delete = delete_fleet_squad if kind == "squad" else delete_fleet_wing
        try:
            delete(*self._fc(), unit["id"])
        except Exception as exc:
            self.failed(where[0].upper() + where[1:], exc, f"Deleting a {kind}")
            return
        expected = {"kind": f"{kind}_deleted", f"{kind}_id": unit["id"]}
        self.done(f"Deleted the empty {where}.", entry, "deleted", expected)

    # The whole run

    def members(self):
        """The fleet's members as FleetOps knows them now, for the empty checks."""
        return structure(self.operation)[1].values()

    def apply_wings(self, wings) -> tuple[list, list]:
        """Rename and create the template's wings; returns the wing id of each template wing and the new ids."""
        mapped = [wings[index]["id"] if index < len(wings) else None for index in range(len(self.layout))]
        created = []
        for action, index, wing, name in wing_plan(wings, self.layout):
            if action == "rename":
                self.rename_wing(wing, name)
                continue
            mapped[index] = self.create_wing(index, name)
            if mapped[index] is not None:
                created.append(mapped[index])
        return mapped, created

    def expect_own_squads(self, wings, created) -> None:
        """The fleet view learns the squads EVE gave the new wings, so their renames show at once."""
        for wing in wings:
            if wing["id"] in created:
                for squad in wing["squads"]:
                    change = {"kind": "squad_created", "wing_id": wing["id"], "squad_id": squad["id"]}
                    fleet_overlay.expect(self.operation.pk, {**change, "name": squad["name"]})

    def squad_plans(self, wings, mapped, created) -> list[tuple]:
        """``(wing id, wing name, squad_plan)`` of each template wing that exists."""
        by_id = {wing["id"]: wing for wing in wings}
        plans = []
        for wing_id, target in zip(mapped, self.layout):
            if wing_id is None:
                continue
            wing = by_id.get(wing_id)
            if wing is None and wing_id not in created:
                error = "The wing is no longer in the fleet."
                self.record["failures"].append({"unit": target["name"], "error": error})
                fleet_jobs.step(self.job, ok=False, message=f"Wing “{target['name']}”: {error}")
                continue
            # A new wing ESI does not show yet gets all its squads created.
            squads = wing["squads"] if wing is not None else []
            plans.append((wing_id, target["name"], squad_plan(squads, target["squads"])))
        return plans

    def run(self) -> None:
        self.wait_for_recent_changes()
        wings = self.read()
        self.set_remaining(estimate(wings, self.layout, members=self.members(), delete_extra=self.delete_extra))
        mapped, created = self.apply_wings(wings)
        if created:
            # EVE may have given the new wings squads of their own.
            self.wait()
            wings = self.read(created)
            self.expect_own_squads(wings, created)
        plans = self.squad_plans(wings, mapped, created)
        extras = extra_plan(wings, mapped, self.layout, self.members()) if self.delete_extra else []
        self.set_remaining(sum(len(plan) for _wing_id, _name, plan in plans) + len(extras))

        for wing_id, wing_name, plan in plans:
            for action, _index, squad, name in plan:
                if action == "rename":
                    self.rename_squad(wing_name, squad, name)
                else:
                    self.create_squad(wing_id, wing_name, name)
        # Wings of the template go by their new names.
        names = {wing_id: target["name"] for wing_id, target in zip(mapped, self.layout) if wing_id is not None}
        for action, kind, unit, wing in extras:
            self.remove(action, kind, unit, names.get(wing["id"], wing["name"]) if wing else "")

        if not self.job["done"] and not self.job["failed"]:
            self.set_remaining(1)
            message = f"Nothing to change: the fleet already matches “{self.record['template']}”."
            fleet_jobs.step(self.job, ok=True, message=message)
        if self.changed:
            # Read the fleet once ESI's cache shows the changes, so the fleet page shows them too.
            self.wait()
            fleet_refresh.refresh_now(self.operation, min_interval=0)


def run_template_job(job, operation, payload, actor) -> None:
    """Runner of the "template" fleet job (see the module documentation)."""
    run = _TemplateRun(job, operation, payload, actor)
    try:
        run.run()
    finally:
        job["audit"] = run.record
