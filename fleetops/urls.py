from django.urls import path

from fleetops import views

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
    path("operations/<uuid:operation_uuid>/end/", views.end_fleet_view, name="end_fleet"),
    path("operations/<uuid:operation_uuid>/attendance-multiplier/", views.set_attendance_multiplier_view, name="set_attendance_multiplier"),
    path("operations/<uuid:operation_uuid>/retry/ping/", views.retry_ping_view, name="retry_ping"),
    path("operations/<uuid:operation_uuid>/retry/motd/", views.retry_motd_view, name="retry_motd"),
    path("operations/<uuid:operation_uuid>/retry/srp/", views.retry_srp_view, name="retry_srp"),
    path("operations/<uuid:operation_uuid>/roles/add/", views.add_operation_role, name="add_operation_role"),
    path("operations/roles/<int:pk>/delete/", views.delete_operation_role, name="delete_operation_role"),
    path("operations/<uuid:operation_uuid>/kick-capsules/", views.kick_capsules_view, name="kick_capsules"),
    path("statistics/me/", views.my_statistics_view, name="my_statistics"),
    path("statistics/corporation/", views.corporation_statistics_view, name="corporation_statistics"),
    path("statistics/corporation/<int:corporation_id>/", views.corporation_statistics_detail_view, name="corporation_statistics_detail"),
    path("statistics/corporations/", views.all_corporation_statistics_view, name="all_corporation_statistics"),
    path("statistics/fcs/", views.all_fc_statistics_view, name="all_fc_statistics"),
    path("statistics/fcs/<int:user_id>/", views.fc_statistics_detail_view, name="fc_statistics_detail"),
    path("attendance/manual/", views.manual_attendance_view, name="manual_attendance"),
    path("attendance/history/me/", views.attendance_history_me, name="attendance_history_me"),
    path("attendance/history/corporation/", views.attendance_history_corporation, name="attendance_history_corporation"),
    path("attendance/history/alliance/", views.attendance_history_alliance, name="attendance_history_alliance"),
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
    path("configuration/", views.configuration_index, name="configuration_index"),
    path("configuration/settings/", views.configuration_settings, name="configuration_settings"),
    path("configuration/<slug:section>/", views.configuration_list, name="configuration_list"),
    path("configuration/<slug:section>/add/", views.configuration_edit, name="configuration_add"),
    path("configuration/<slug:section>/<int:pk>/edit/", views.configuration_edit, name="configuration_edit"),
    path("configuration/<slug:section>/<int:pk>/delete/", views.configuration_delete, name="configuration_delete"),
]
