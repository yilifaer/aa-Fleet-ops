"""Configurable special roles: the seeded roles, the FC credit switch, disabled roles and configuration."""

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fleetops.forms import OperationRoleAssignmentForm
from fleetops.models import (
    AttendanceRecord,
    AuditLog,
    EveEntityName,
    FleetOperation,
    OperationRoleAssignment,
    SpecialRole,
)
from fleetops.services.roles import NOT_IN_FLEET, add_role_assignment
from fleetops.services.statistics import fc_statistics, member_statistics

from . import factories as f

Status = FleetOperation.Status


def message_texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


class SpecialRoleTestCase(TestCase):
    def setUp(self):
        self.fc = f.create_user("fc", perms=f.FC_PERMS)
        self.lead = f.create_user("lead", perms=f.FC_LEAD_PERMS)
        self.member = f.create_user("member", perms=f.MEMBER_PERMS)
        self.operation = f.create_operation(self.fc, status=Status.CLOSED)
        f.member_state(self.operation, f.main_of(self.fc), active=False)
        f.member_state(self.operation, f.main_of(self.member), active=False)

    def assign(self, user, role_key, character_id, operation=None):
        self.client.force_login(user)
        return self.client.post(
            reverse("fleetops:add_operation_role", args=[(operation or self.operation).uuid]),
            {"role": role_key, "character_id": str(character_id), "notes": ""},
        )

    def page(self, user, operation=None):
        self.client.force_login(user)
        response = self.client.get(reverse("fleetops:operation_detail", args=[(operation or self.operation).uuid]))
        self.assertEqual(response.status_code, 200)
        return response


class SeededRoleTests(SpecialRoleTestCase):
    def test_the_three_former_roles_are_seeded(self):
        self.assertEqual(
            list(SpecialRole.objects.values_list("key", "name", "grants_fc_credit", "is_active", "sort_order")),
            [
                ("backseat_fc", "Back Seat FC", True, True, 10),
                ("logi_anchor", "Logi Anchor", False, True, 20),
                ("snowflake", "Snowflake Member", False, True, 30),
            ],
        )

    def test_form_offers_the_active_roles_by_key(self):
        form = OperationRoleAssignmentForm(operation=self.operation)

        self.assertEqual(
            [(str(value), label) for value, label in form.fields["role"].choices],
            [("backseat_fc", "Back Seat FC (FC credit)"), ("logi_anchor", "Logi Anchor"), ("snowflake", "Snowflake Member")],
        )


class FCCreditTests(SpecialRoleTestCase):
    def test_the_role_switch_alone_gives_fc_credit(self):
        response = self.assign(self.fc, "backseat_fc", f.main_of(self.member).character_id)

        assignment = OperationRoleAssignment.objects.get(operation=self.operation)
        self.assertFalse(self.member.has_perm("fleetops.start_fleet"))
        self.assertTrue(assignment.grants_fc_credit)
        self.assertEqual(assignment.special_role.key, "backseat_fc")
        self.assertEqual(message_texts(response), [f"{f.main_of(self.member).character_name} assigned as Back Seat FC; FC credit granted."])
        year, month = self.operation.started_at.year, self.operation.started_at.month
        self.assertEqual(fc_statistics(self.member, year, month)["fleet_count"], 1)

    def test_fc_credit_does_not_let_the_pilot_manage_the_fleet(self):
        add_role_assignment(self.operation, actor=self.fc, role="backseat_fc", character_id=f.main_of(self.member).character_id)

        page = self.page(self.member)
        response = self.assign(self.member, "logi_anchor", f.main_of(self.fc).character_id)

        self.assertNotContains(page, "Assign Role")
        self.assertNotContains(page, "Edit Operation")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(OperationRoleAssignment.objects.count(), 1)

    def test_with_manage_own_fleet_a_credited_pilot_manages_the_fleet(self):
        co_fc = f.create_user("co_fc", perms=f.MEMBER_PERMS + ["fleetops.manage_own_fleet"])
        f.member_state(self.operation, f.main_of(co_fc), active=False)
        add_role_assignment(self.operation, actor=self.fc, role="backseat_fc", character_id=f.main_of(co_fc).character_id)

        response = self.assign(co_fc, "logi_anchor", f.main_of(self.member).character_id)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(OperationRoleAssignment.objects.filter(role="logi_anchor").exists())

    def test_roles_without_the_switch_give_no_credit(self):
        self.assign(self.fc, "logi_anchor", f.main_of(self.member).character_id)

        self.assertFalse(OperationRoleAssignment.objects.get().grants_fc_credit)

    def test_characters_outside_alliance_auth_get_no_credit(self):
        f.member_state(self.operation, 2_119_555_001, name="Neutral Backseat", active=False)
        EveEntityName.objects.create(id=2_119_555_001, category="character", name="Neutral Backseat", updated_at=timezone.now())

        response = self.assign(self.fc, "backseat_fc", 2_119_555_001)

        assignment = OperationRoleAssignment.objects.get()
        self.assertEqual((assignment.auth_user, assignment.grants_fc_credit), (None, False))
        self.assertEqual(
            message_texts(response),
            ["Neutral Backseat assigned as Back Seat FC. No FC credit: the character is not registered in Alliance Auth."],
        )

    def test_switch_changes_apply_to_new_assignments(self):
        add_role_assignment(self.operation, actor=self.fc, role="backseat_fc", character_id=f.main_of(self.member).character_id)

        f.special_role(key="backseat_fc", grants_fc_credit=False)
        later = add_role_assignment(self.operation, actor=self.fc, role="backseat_fc", character_id=f.main_of(self.fc).character_id)

        self.assertEqual(
            dict(OperationRoleAssignment.objects.values_list("character_id", "grants_fc_credit")),
            {f.main_of(self.member).character_id: True, later.character_id: False},
        )

    def test_statistics_label_the_roles(self):
        add_role_assignment(self.operation, actor=self.fc, role="backseat_fc", character_id=f.main_of(self.member).character_id)

        stats = member_statistics(self.member, self.operation.started_at.year, self.operation.started_at.month)

        self.assertEqual(stats["roles"], [{"key": "backseat_fc", "label": "Back Seat FC", "count": 1}])


class DisabledRoleTests(SpecialRoleTestCase):
    def setUp(self):
        super().setUp()
        self.assignment = add_role_assignment(
            self.operation, actor=self.fc, role="snowflake", character_id=f.main_of(self.member).character_id
        )
        f.special_role(key="snowflake", is_active=False)

    def test_disabled_roles_are_not_offered_or_accepted(self):
        form = OperationRoleAssignmentForm(operation=self.operation)
        response = self.assign(self.fc, "snowflake", f.main_of(self.fc).character_id)

        self.assertNotIn("snowflake", [str(value) for value, _label in form.fields["role"].choices])
        self.assertIn("Invalid special-role assignment.", message_texts(response)[0])
        self.assertEqual(OperationRoleAssignment.objects.count(), 1)
        with self.assertRaisesMessage(ValueError, "The special role Snowflake Member is disabled."):
            add_role_assignment(self.operation, actor=self.fc, role="snowflake", character_id=f.main_of(self.fc).character_id)
        with self.assertRaisesMessage(ValueError, "Unknown special role."):
            add_role_assignment(self.operation, actor=self.fc, role="nonsense", character_id=f.main_of(self.fc).character_id)

    def test_existing_assignments_stay_visible(self):
        response = self.page(self.fc)

        self.assertContains(response, "Snowflake Member</span> <span class=\"badge bg-secondary\">Disabled role</span>")

    def test_no_enabled_roles(self):
        SpecialRole.objects.update(is_active=False)

        response = self.page(self.fc)

        self.assertContains(response, "No special roles are enabled.")
        self.assertNotContains(response, "Assign Role")


class ConfiguredRoleTests(SpecialRoleTestCase):
    def configure(self, data, pk=None):
        self.client.force_login(self.lead)
        url = (
            reverse("fleetops:configuration_edit", args=["special-roles", pk])
            if pk
            else reverse("fleetops:configuration_add", args=["special-roles"])
        )
        return self.client.post(url, data)

    def test_new_role_from_the_configuration_is_used_on_fleets(self):
        self.configure({"name": "Fleet Scout", "grants_fc_credit": "on", "is_active": "on", "sort_order": "5"})
        role = SpecialRole.objects.get(name="Fleet Scout")

        response = self.assign(self.fc, "fleet_scout", f.main_of(self.member).character_id)
        self.configure({"name": "Scout", "grants_fc_credit": "on", "is_active": "on", "sort_order": "5"}, pk=role.pk)
        page = self.page(self.fc)

        self.assertEqual(response.status_code, 302)
        assignment = OperationRoleAssignment.objects.get()
        self.assertEqual((assignment.role, assignment.special_role, assignment.grants_fc_credit), ("fleet_scout", role, True))
        self.assertContains(page, '<span class="badge bg-info text-dark">Scout</span>')
        self.assertContains(page, "Scout (FC credit)")
        entries = AuditLog.objects.filter(object_type="SpecialRole", object_id=str(role.pk)).order_by("pk")
        self.assertEqual([entry.action for entry in entries], ["configuration.create", "configuration.update"])
        self.assertEqual((entries[1].old_value["name"], entries[1].new_value["name"]), ("Fleet Scout", "Scout"))
        self.assertTrue(AuditLog.objects.filter(action="fleet.role_create", new_value__role_name="Fleet Scout").exists())

    def test_roles_in_use_are_disabled_instead_of_deleted(self):
        add_role_assignment(self.operation, actor=self.fc, role="logi_anchor", character_id=f.main_of(self.member).character_id)
        role = SpecialRole.objects.get(key="logi_anchor")
        self.client.force_login(self.lead)

        response = self.client.post(reverse("fleetops:configuration_delete", args=["special-roles", role.pk]))

        self.assertIn(
            "This item is referenced by existing fleet data and cannot be deleted. Disable it instead.",
            message_texts(response),
        )
        self.assertTrue(SpecialRole.objects.filter(pk=role.pk).exists())


class RoleMemberChoiceTests(SpecialRoleTestCase):
    def test_everybody_recorded_in_the_fleet_can_get_a_role(self):
        # A PAP-link fleet has no tracked members; registrations and PAP rows count.
        link_fleet = f.create_operation(self.fc, status=Status.CLOSED, link_only=True, esi_fleet_id=None)
        registered = f.add_alt(self.member, "Registered Alt")
        f.registration(link_fleet, registered)
        f.add_attendance(link_fleet, self.fc, source="manual")

        form = OperationRoleAssignmentForm(operation=link_fleet)

        self.assertEqual(
            [label for _value, label in form.fields["character_id"].choices],
            ["fc Main", "Registered Alt"],
        )
        response = self.assign(self.fc, "logi_anchor", registered.character_id, operation=link_fleet)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(OperationRoleAssignment.objects.filter(operation=link_fleet, character_id=registered.character_id).exists())

    def test_manual_pap_waiting_for_approval_or_rejected_gives_no_role(self):
        # Otherwise an FC could give FC credit to anybody by entering manual PAP for them.
        users = {status: f.create_user(status, perms=f.MEMBER_PERMS) for status in ("pending", "rejected", "approved")}
        for status, user in users.items():
            f.add_attendance(self.operation, user, source="manual", review_status=status)
        choices = OperationRoleAssignmentForm(operation=self.operation).fields["character_id"].choices
        offered = {int(value) for value, _label in choices}

        self.assign(self.fc, "backseat_fc", f.main_of(users["pending"]).character_id)

        self.assertNotIn(f.main_of(users["pending"]).character_id, offered)
        self.assertNotIn(f.main_of(users["rejected"]).character_id, offered)
        self.assertIn(f.main_of(users["approved"]).character_id, offered)
        self.assertFalse(OperationRoleAssignment.objects.filter(auth_user=users["pending"]).exists())
        self.assertEqual(fc_statistics(users["pending"], *self.month())["fleet_count"], 0)

    def test_the_role_service_refuses_characters_without_accepted_pap(self):
        # A crafted request or another caller cannot skip the form; a deleted row proves nothing either.
        pilot = f.create_user("pilot", perms=f.MEMBER_PERMS)
        character_id = f.main_of(pilot).character_id
        row = f.add_attendance(self.operation, pilot, source="manual", review_status="rejected")

        for state in ("rejected", "pending", "deleted"):
            with self.subTest(state=state):
                if state == "deleted":
                    row.delete()
                else:
                    AttendanceRecord.objects.filter(pk=row.pk).update(review_status=state)
                with self.assertRaisesMessage(ValueError, NOT_IN_FLEET):
                    add_role_assignment(self.operation, actor=self.fc, role="backseat_fc", character_id=character_id)
                response = self.assign(self.fc, "backseat_fc", character_id)
                self.assertIn("Invalid special-role assignment.", message_texts(response)[0])

        self.assertFalse(OperationRoleAssignment.objects.exists())
        self.assertEqual(fc_statistics(pilot, *self.month())["fleet_count"], 0)

    def test_fleets_waiting_for_approval_offer_their_waiting_manual_pap(self):
        # Their roles count only once an FC lead approves the fleet with its rows.
        manual = f.create_operation(
            self.fc, status=Status.CLOSED, is_manual=True, esi_fleet_id=None, review_status="pending"
        )
        f.add_attendance(manual, self.member, source="manual", review_status="pending")

        form = OperationRoleAssignmentForm(operation=manual)

        self.assertEqual([label for _value, label in form.fields["character_id"].choices], ["member Main"])

    def month(self):
        return self.operation.started_at.year, self.operation.started_at.month

    def test_tracked_members_show_their_corporation(self):
        form = OperationRoleAssignmentForm(operation=self.operation)

        self.assertIn(
            (str(f.main_of(self.member).character_id), f"member Main — {f.DEFAULT_CORP[1]}"),
            form.fields["character_id"].choices,
        )
