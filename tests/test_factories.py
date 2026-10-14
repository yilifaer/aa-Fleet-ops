# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django.test import TestCase
from django.utils import timezone

from fleetops.models import FleetOperation
from fleetops.providers.esi import has_scope_token
from fleetops.services.identity import identity_for_character_id

from . import factories as f


class FactoryTests(TestCase):
    def test_user_with_main_alt_token_and_operation(self):
        fc = f.create_user(perms=f.FC_PERMS)
        alt = f.add_alt(fc, corporation=f.OTHER_CORP, alliance=f.FOREIGN_ALLIANCE)
        f.add_token(fc, alt)
        self.assertTrue(fc.has_perm("fleetops.start_fleet"))
        self.assertFalse(fc.has_perm("fleetops.manage_fleets"))
        self.assertTrue(has_scope_token(fc, alt.character_id, write=True))

        identity = identity_for_character_id(alt.character_id)
        self.assertEqual(identity.user, fc)
        self.assertEqual(identity.main_character_id, f.main_of(fc).character_id)
        self.assertEqual(identity.corporation_id, f.DEFAULT_CORP[0])

        operation = f.create_operation(fc, fc_character=alt)
        record = f.add_attendance(operation, fc, alt)
        self.assertEqual(record.corporation_id, f.DEFAULT_CORP[0])

    def test_ping_channel_roles_and_scheduled_operation(self):
        fc = f.create_user(perms=f.FC_PERMS)
        sig_a = f.ping_role("Sig A")
        channel = f.ping_channel("STR", allow_here=True, roles=[sig_a])
        self.assertTrue(channel.allow_here)
        self.assertFalse(channel.allow_everyone)
        self.assertEqual(list(channel.allowed_roles.all()), [sig_a])

        operation = f.create_operation(
            fc,
            ping_channel=channel,
            ping_mention_mode=FleetOperation.MentionMode.ROLES,
            ping_roles=[{"id": str(sig_a.role_id), "name": sig_a.name}],
            doctrine_fits=[{"fit_id": 3, "fit": "DPS Moa", "ship_type_id": 623, "ship": "Moa"}],
        )
        self.assertEqual(operation.ping_channel_name, "STR")
        self.assertEqual(operation.ping_roles, [{"id": str(sig_a.role_id), "name": "Sig A"}])
        self.assertEqual(operation.doctrine_fits[0]["ship"], "Moa")

        scheduled = f.create_scheduled_operation(fc)
        self.assertEqual(scheduled.status, FleetOperation.Status.SCHEDULED)
        self.assertIsNone(scheduled.started_at)
        self.assertIsNone(scheduled.esi_fleet_id)
        self.assertGreater(scheduled.scheduled_at, timezone.now())
