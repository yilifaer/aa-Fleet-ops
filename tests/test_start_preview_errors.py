# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Errors of the start preview."""

import json
import uuid
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from fleetops import views as views_module

from . import factories as f

WEBHOOK_SECRET = "hook-secret-token"


class PreviewErrorTests(TestCase):
    def test_render_failure_returns_a_json_error_without_details(self):
        fc = f.create_user("fc", perms=f.FC_PERMS)
        fleet_type = f.fleet_type()
        self.client.force_login(fc)
        error = RuntimeError(f"Failed posting to https://discord.com/api/webhooks/1/{WEBHOOK_SECRET}")

        with mock.patch("fleetops.views.render_messages", side_effect=error), self.assertLogs(
            views_module.logger, "ERROR"
        ) as logs:
            response = self.client.post(
                reverse("fleetops:preview_fleet"),
                {
                    "request_id": str(uuid.uuid4()),
                    "operation_mode": "full",
                    "fc_character_id": str(f.main_of(fc).character_id),
                    "fleet_type": str(fleet_type.pk),
                    "formup": "Jita",
                },
            )

        self.assertEqual(response.status_code, 400)
        body = json.loads(response.content)
        self.assertFalse(body["ok"])
        self.assertIn("could not be rendered", body["error"])
        self.assertNotIn(WEBHOOK_SECRET, response.content.decode())
        log = "\n".join(logs.output)
        self.assertIn("Message preview failed (RuntimeError)", log)
        self.assertIn("Traceback", log)
        self.assertNotIn(WEBHOOK_SECRET, log)
        self.assertTrue(all(record.exc_info is None for record in logs.records))
