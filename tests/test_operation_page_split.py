"""The fleet page is built from partial templates; the page shows what it showed as one template.

The markers below were taken from the page as rendered before it was split into
``fleetops/operation/*.html``. Later work changes the partials, not these markers' meaning.
"""

from datetime import timedelta

from django.utils import timezone

from fleetops.models import AttendanceRecord, FleetOperation, ReviewStatus

from . import factories as f
from .test_views_permissions import FleetOpsViewTestCase, add_member_state

PARTIALS = (
    "_header.html",
    "_review.html",
    "_stats_card.html",
    "_actions.html",
    "_controls.html",
    "_links.html",
    "_members.html",
    "_map.html",
    "_events.html",
    "_attendance_manual.html",
    "_multiplier.html",
    "_attendance_table.html",
    "_roles.html",
    "_messages.html",
    "_end_modal.html",
)


class OperationPageSplitTests(FleetOpsViewTestCase):
    def page(self, user, operation):
        response = self.client_for(user).get(self.detail_url(operation))
        self.assertEqual(response.status_code, 200)
        return response

    def assertAllContained(self, response, texts):
        for text in texts:
            with self.subTest(text=text):
                self.assertContains(response, text)

    def assertNoneContained(self, response, texts):
        for text in texts:
            with self.subTest(text=text):
                self.assertNotContains(response, text)

    def test_every_partial_is_rendered(self):
        response = self.page(self.fc, self.own_active)

        for name in PARTIALS:
            with self.subTest(partial=name):
                self.assertTemplateUsed(response, f"fleetops/operation/{name}")

    def test_active_fleet_for_its_fc(self):
        response = self.page(self.fc, self.own_active)

        self.assertAllContained(
            response,
            [
                "Edit Operation",
                "Open SRP",
                'class="end-fleet-form"',
                "STALE DATA",
                "Operation Actions",
                "Discord returned HTTP 500",
                "Retry MOTD",
                'id="regenerate-motd"',
                "Regenerate &amp; Push MOTD",
                "Fleet Control",
                "Kick 2 Capsules",
                "Neutral Pod",
                "Not in Alliance Auth",
                f"https://evewho.com/character/{self.member_main.character_id}",
                f"https://zkillboard.com/character/{self.member_main.character_id}/",
                "<th>Last Seen</th>",
                "Save Manual Attendance",
                # The multiplier of a running fleet is chosen when it ends.
                "The PAP multiplier is chosen when the fleet ends.",
                'id="pap-preview"',
                "Assign Role",
                'Special Roles <span class="badge bg-secondary">1</span>',
                'id="ping-snapshot"',
                "Ping channel: None (copy only) · Mention: No ping",
                'id="ping-text"',
                'id="motd-text"',
                # The Fleet tab replaces the minute reload of a running fleet.
                'id="fleet-root"',
            ],
        )
        self.assertNoneContained(
            response, ['id="composition-hidden"', 'id="endFleetModal"', "Retry SRP", "Retry Ping", "location.reload"]
        )

    def test_active_fleet_for_a_member_hides_the_composition(self):
        response = self.page(self.member, self.own_active)

        self.assertAllContained(
            response,
            [
                'id="composition-hidden"',
                "The map is shown once the fleet has closed.",
                "Member events are shown once the fleet has closed.",
                "While the fleet is open you see the attendance of your own characters.",
                "<td>member Main</td>",
                'Special Roles <span class="badge bg-secondary">1</span>',
            ],
        )
        self.assertNoneContained(
            response,
            ["Neutral Pod", "fc Main</td>", "Edit Operation", "End Fleet", "Fleet Control", "Assign Role",
             "Save Manual Attendance", "Retry MOTD"],
        )

    def test_active_fleet_for_a_fleet_manager(self):
        response = self.page(self.lead, self.own_active)

        self.assertAllContained(response, ["Neutral Pod", "End Fleet", "Fleet Control", "Kick 2 Capsules"])
        self.assertNotContains(response, 'id="composition-hidden"')

    def test_long_fleet_offers_the_end_modal_and_a_handover_disables_the_motd(self):
        long = f.create_operation(
            self.fc,
            started_at=timezone.now() - timedelta(minutes=120),
            ping_channel=self.webhook,
            ping_channel_name=self.webhook.name,
            ping_text="<@&123456789012345678> go",
            missing_reason="boss_handover",
        )
        add_member_state(long, self.fc_main, self.fc)

        response = self.page(self.fc, long)

        self.assertAllContained(
            response,
            [
                'data-bs-target="#endFleetModal"',
                'id="endFleetModal"',
                "End Fleet & Finalize Attendance",
                "Retry Ping",
                "Retry SRP",
                'id="ping-copy"',
                "Ping channel: Ops Pings",
                'disabled title="The fleet boss was handed over; only the boss can change the MOTD."',
            ],
        )

    def test_closed_fleet_for_its_fc(self):
        # FCs delete their own manual PAP while it waits for approval.
        AttendanceRecord.objects.filter(pk=self.own_manual.pk).update(
            created_by=self.fc, review_status=ReviewStatus.PENDING
        )
        response = self.page(self.fc, self.own_closed)

        self.assertAllContained(
            response,
            [
                "Edit Operation",
                "<td>outsider Main</td>",
                f"https://evewho.com/character/{self.outsider_main.character_id}",
                "Apply to Automatic Attendance",
                "Save Manual Attendance",
                "Delete</button>",
                "Assign Role",
            ],
        )
        self.assertNoneContained(
            response,
            ["End Fleet", "Fleet Control", 'id="composition-hidden"', "Regenerate", "location.reload"],
        )

    def test_closed_fleet_for_an_attendee_shows_the_composition(self):
        response = self.page(self.outsider, self.own_closed)

        self.assertAllContained(response, ["<td>fc Main</td>", "<td>outsider Main</td>"])
        self.assertNoneContained(response, ['id="composition-hidden"', "Edit Operation", "Apply to Automatic"])

    def test_ending_closed_manual_and_attendance_only_fleets(self):
        ending = f.create_operation(self.fc, status=FleetOperation.Status.ENDING)
        ended = f.create_operation(
            self.fc, status=FleetOperation.Status.CLOSED, end_reason="fleet_gone", srp_error="SRP unavailable"
        )
        manual = f.create_operation(self.fc, status=FleetOperation.Status.CLOSED, is_manual=True, esi_fleet_id=None)
        only = f.create_operation(self.fc, send_ping=False, last_error="ESI said no")

        self.assertAllContained(
            self.page(self.fc, ending),
            ['id="fleet-ending"', "Ending…", "setTimeout(()=>location.reload(),5000);"],
        )
        self.assertAllContained(
            self.page(self.fc, ended),
            ["Ended automatically: the fleet no longer exists or the FC left it", "<strong>SRP:</strong> SRP unavailable"],
        )
        self.assertAllContained(self.page(self.fc, manual), ["Manual Record", "Retry SRP"])
        attendance_only = self.page(self.fc, only)
        self.assertAllContained(attendance_only, ["Attendance Only", "ESI said no", "Kick 0 Capsules"])
        self.assertNoneContained(attendance_only, ["Retry MOTD", "Retry Ping"])
