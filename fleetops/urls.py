# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

from django.urls import path
from django.views.decorators.cache import cache_control
from django.views.i18n import JavaScriptCatalog

from fleetops import (
    public_views,
    views,
    views_config,
    views_controls,
    views_exports,
    views_fleet_actions,
    views_fleet_manage,
    views_fleet_state,
    views_fleet_tools,
    views_history,
    views_links,
    views_members,
    views_pap,
    views_presets,
    views_records,
    views_srp,
    views_srp_review,
    views_srp_summary,
    views_statistics,
)

app_name = "fleetops"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("start/", views.start_fleet_view, name="start_fleet"),
    path("start/preview/", views.preview_fleet, name="preview_fleet"),
    path("manual-fleet/", views.manual_fleet_view, name="manual_fleet"),
    path("esi/authorize/", views.authorize_esi, name="authorize_esi"),
    path("api/characters/<int:character_id>/fleet/", views.detect_fleet, name="detect_fleet"),
    path("operations/", views.fleet_operations_view, name="fleet_operations"),
    path("operations/<uuid:operation_uuid>/", views.operation_detail, name="operation_detail"),
    path("operations/<uuid:operation_uuid>/edit/", views.edit_operation_view, name="edit_operation"),
    path("operations/<uuid:operation_uuid>/start-scheduled/", views.start_scheduled_fleet, name="start_scheduled_fleet"),
    path("operations/<uuid:operation_uuid>/edit-scheduled/", views.edit_scheduled_fleet, name="edit_scheduled_fleet"),
    path("operations/<uuid:operation_uuid>/cancel-scheduled/", views.cancel_scheduled_fleet, name="cancel_scheduled_fleet"),
    path("operations/<uuid:operation_uuid>/end/", views.end_fleet_view, name="end_fleet"),
    path("operations/<uuid:operation_uuid>/attendance-multiplier/", views.set_attendance_multiplier_view, name="set_attendance_multiplier"),
    path("operations/<uuid:operation_uuid>/retry/ping/", views.retry_ping_view, name="retry_ping"),
    path("operations/<uuid:operation_uuid>/retry/motd/", views.retry_motd_view, name="retry_motd"),
    path("operations/<uuid:operation_uuid>/retry/srp/", views.retry_srp_view, name="retry_srp"),
    path("operations/<uuid:operation_uuid>/motd/regenerate/", views_fleet_actions.regenerate_motd, name="regenerate_motd"),
    path("operations/<uuid:operation_uuid>/roles/add/", views.add_operation_role, name="add_operation_role"),
    path("operations/roles/<int:pk>/delete/", views.delete_operation_role, name="delete_operation_role"),
    path("operations/<uuid:operation_uuid>/kick-capsules/", views.kick_capsules_view, name="kick_capsules"),
    path("statistics/me/", views_statistics.my_statistics_view, name="my_statistics"),
    path("statistics/corporation/", views_statistics.corporation_statistics_view, name="corporation_statistics"),
    path("statistics/corporation/<int:corporation_id>/", views_statistics.corporation_statistics_detail_view, name="corporation_statistics_detail"),
    path("statistics/corporations/", views_statistics.all_corporation_statistics_view, name="all_corporation_statistics"),
    path("statistics/fcs/", views_statistics.all_fc_statistics_view, name="all_fc_statistics"),
    path("statistics/fcs/<int:user_id>/", views_statistics.fc_statistics_detail_view, name="fc_statistics_detail"),
    path("attendance/manual/", views.manual_attendance_view, name="manual_attendance"),
    path("attendance/history/me/", views_history.attendance_history_me, name="attendance_history_me"),
    path("attendance/history/corporation/", views_history.attendance_history_corporation, name="attendance_history_corporation"),
    path("attendance/history/alliance/", views_history.attendance_history_alliance, name="attendance_history_alliance"),
    path("attendance/<uuid:operation_uuid>/add/", views.add_manual_attendance, name="add_manual_attendance"),
    path("attendance/<int:pk>/delete/", views.delete_manual_attendance, name="delete_manual_attendance"),
    path("audit/", views.audit_log_view, name="audit_log"),
    path("incentives/", views.incentive_review, name="incentive_review"),
    path("incentives/<int:pk>/recalculate/", views.incentive_recalculate, name="incentive_recalculate"),
    path("incentives/<int:pk>/finalize/", views.incentive_finalize, name="incentive_finalize"),
    path("incentives/<int:pk>/unlock/", views.incentive_unlock, name="incentive_unlock"),
    path("incentives/<int:pk>/waiver/<int:user_id>/", views.incentive_waiver, name="incentive_waiver"),
]

urlpatterns += [
    path("configuration/", views_config.configuration_index, name="configuration_index"),
    path("configuration/settings/", views_config.configuration_settings, name="configuration_settings"),
    path("configuration/ping-roles/sync/", views_config.sync_discord_roles, name="sync_discord_roles"),
    path("configuration/<slug:section>/", views_config.configuration_list, name="configuration_list"),
    path("configuration/<slug:section>/add/", views_config.configuration_edit, name="configuration_add"),
    path("configuration/<slug:section>/<int:pk>/edit/", views_config.configuration_edit, name="configuration_edit"),
    path("configuration/<slug:section>/<int:pk>/delete/", views_config.configuration_delete, name="configuration_delete"),
]

urlpatterns += [
    path("operations/<uuid:operation_uuid>/pap/recalculate/", views_pap.recalculate_pap_view, name="recalculate_pap"),
    path("operations/<uuid:operation_uuid>/statistics-inclusion/", views_pap.set_stats_mode_view, name="set_stats_mode"),
    path("api/me/live-pap/", views_pap.live_pap_partial, name="live_pap"),
    path("approvals/", views_records.approval_queue, name="approval_queue"),
    path("approvals/operations/<uuid:operation_uuid>/", views_records.review_operation_view, name="review_operation"),
    path("approvals/attendance/", views_records.review_attendance_view, name="review_attendance"),
    path("attendance/<uuid:operation_uuid>/roster/", views_records.roster_import_view, name="roster_import"),
    path("pap/<slug:code>/", views_links.pap_link_register, name="pap_link_register"),
    path("pap/<slug:code>/authorize/", views_links.pap_link_authorize, name="pap_link_authorize"),
    path("operations/<uuid:operation_uuid>/pap-links/", views_links.create_pap_link_view, name="create_pap_link"),
    path("pap-links/<int:pk>/revoke/", views_links.revoke_pap_link_view, name="revoke_pap_link"),
    path("pap-fleet/start/", views_links.start_link_only_fleet_view, name="start_link_only_fleet"),
    path("operations/<uuid:operation_uuid>/members/<int:character_id>/kick/", views_controls.kick_member_view, name="kick_member"),
    path("operations/<uuid:operation_uuid>/kick/preview/", views_controls.kick_preview_view, name="kick_preview"),
    path("operations/<uuid:operation_uuid>/kick/outside-system/", views_controls.kick_outside_system_view, name="kick_outside_system"),
    path("start/presets/", views_presets.preset_list_view, name="preset_list"),
    path("start/presets/save/", views_presets.preset_save_view, name="preset_save"),
    path("start/presets/<int:pk>/rename/", views_presets.preset_rename_view, name="preset_rename"),
    path("start/presets/<int:pk>/delete/", views_presets.preset_delete_view, name="preset_delete"),
    path("start/presets/<int:pk>/default/", views_presets.preset_default_view, name="preset_default"),
    path("incentives/<int:pk>/update/", views.incentive_update, name="incentive_update"),
]

# The Fleet tab: live state, wing/squad/member changes, invites, structure templates and ship functions.
urlpatterns += [
    path("operations/<uuid:operation_uuid>/fleet/state/", views_fleet_state.fleet_state_view, name="fleet_state"),
    path("operations/<uuid:operation_uuid>/fleet/refresh/", views_fleet_state.fleet_refresh_view, name="fleet_refresh"),
    path("operations/<uuid:operation_uuid>/fleet/wings/add/", views_fleet_manage.wing_add_view, name="fleet_wing_add"),
    path("operations/<uuid:operation_uuid>/fleet/wings/<int:wing_id>/rename/", views_fleet_manage.wing_rename_view, name="fleet_wing_rename"),
    path("operations/<uuid:operation_uuid>/fleet/wings/<int:wing_id>/delete/", views_fleet_manage.wing_delete_view, name="fleet_wing_delete"),
    path("operations/<uuid:operation_uuid>/fleet/wings/<int:wing_id>/squads/add/", views_fleet_manage.squad_add_view, name="fleet_squad_add"),
    path("operations/<uuid:operation_uuid>/fleet/squads/<int:squad_id>/rename/", views_fleet_manage.squad_rename_view, name="fleet_squad_rename"),
    path("operations/<uuid:operation_uuid>/fleet/squads/<int:squad_id>/delete/", views_fleet_manage.squad_delete_view, name="fleet_squad_delete"),
    path("operations/<uuid:operation_uuid>/fleet/members/move/", views_fleet_manage.members_move_view, name="fleet_members_move"),
    path("operations/<uuid:operation_uuid>/fleet/invite/candidates/", views_fleet_tools.invite_candidates_view, name="fleet_invite_candidates"),
    path("operations/<uuid:operation_uuid>/fleet/invite/", views_fleet_tools.invite_view, name="fleet_invite"),
    path("operations/<uuid:operation_uuid>/fleet/template/", views_fleet_tools.apply_template_view, name="fleet_apply_template"),
    path("operations/<uuid:operation_uuid>/fleet/classify/", views_fleet_tools.classify_view, name="fleet_classify"),
]

# Member lists, leaderboard, member details and exports of the statistics.
urlpatterns += [
    path("statistics/members/", views_members.alliance_members_view, name="alliance_members"),
    path("statistics/members/<int:user_id>/", views_members.member_detail_view, name="member_detail"),
    path("statistics/corporation/members/", views_members.corporation_members_view, name="corporation_members"),
    path("statistics/corporation/<int:corporation_id>/members/", views_members.corporation_members_view, name="corporation_members_detail"),
    path("statistics/leaderboard/", views_members.leaderboard_view, name="leaderboard"),
    path("statistics/export/<slug:dataset>/", views_exports.export_view, name="export"),
]

# FleetOps SRP: claims, the fleet SRP link, review, payments and summaries.
urlpatterns += [
    path("srp/", views_srp.srp_home, name="srp_home"),
    path("srp/check/", views_srp.srp_check, name="srp_check"),
    path("srp/link/<str:code>/", views_srp.srp_link, name="srp_link"),
    path("srp/submit/", views_srp.srp_submit, name="srp_submit"),
    path("srp/claims/<uuid:claim_uuid>/", views_srp.srp_claim_detail, name="srp_claim"),
    path("srp/claims/<uuid:claim_uuid>/withdraw/", views_srp.srp_claim_withdraw, name="srp_claim_withdraw"),
    path("srp/claims/<uuid:claim_uuid>/resubmit/", views_srp.srp_claim_resubmit, name="srp_claim_resubmit"),
    path("srp/review/", views_srp_review.review_queue, name="srp_review"),
    path("srp/review/approve-clean/", views_srp_review.bulk_approve, name="srp_review_bulk_approve"),
    path("srp/review/recalculate/", views_srp_review.recalculate_pending, name="srp_review_recalculate"),
    path("srp/claims/<uuid:claim_uuid>/<str:action>/", views_srp_review.claim_action, name="srp_claim_action"),
    path("srp/payments/", views_srp_review.payments, name="srp_payments"),
    path("srp/links/<str:code>/open/", views_srp_summary.link_toggle, {"is_open": True}, name="srp_link_open"),
    path("srp/links/<str:code>/close/", views_srp_summary.link_toggle, {"is_open": False}, name="srp_link_close"),
    path("srp/summary/", views_srp_summary.alliance_summary, name="srp_summary"),
    path("srp/summary/corporation/", views_srp_summary.corporation_summary, name="srp_summary_own_corporation"),
    path(
        "srp/summary/corporation/<int:corporation_id>/",
        views_srp_summary.corporation_summary,
        name="srp_summary_corporation",
    ),
    path("srp/summary/fc/", views_srp_summary.fc_summary, name="srp_summary_own_fc"),
    path("srp/summary/fc/<int:user_id>/", views_srp_summary.fc_summary, name="srp_summary_fc"),
]

# Translations for the JavaScript of the FleetOps pages. The page asks for it with the language and the
# version in the query string, so the browser keeps one copy per language for a day.
urlpatterns += [
    path(
        "jsi18n/",
        cache_control(private=True, max_age=86400)(JavaScriptCatalog.as_view(packages=["fleetops"])),
        name="jsi18n",
    ),
]

# Public page; it must stay the last pattern. Alliance Auth stops adding its login check at the first
# view it excludes, so every pattern after this one would be reachable without a login.
urlpatterns += [
    path("comms/<uuid:token>/join/", public_views.comms_join, name="comms_join"),
]
