from django.test import TestCase

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
