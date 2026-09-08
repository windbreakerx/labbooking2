from django.urls import path

from apps.bookings.views import support

urlpatterns = [
    path("support/", support.SupportListView.as_view(), name="support"),
    path("support/create/", support.SupportCreateView.as_view(), name="support-create"),
    path("support/<int:pk>/", support.SupportChatView.as_view(), name="support-detail"),
    path("support/<int:pk>/messages/", support.SupportMessagesView.as_view(), name="support-messages"),
    path("support/<int:pk>/reply/", support.SupportReplyView.as_view(), name="support-reply"),
    path("staff/support/", support.StaffSupportView.as_view(), name="staff-support"),
    path(
        "staff/support/fragments/threads/",
        support.StaffThreadsView.as_view(),
        name="staff-support-threads",
    ),
    path("staff/support/<int:pk>/chat/", support.StaffChatView.as_view(), name="staff-support-chat"),
    path(
        "staff/support/<int:pk>/messages/",
        support.StaffMessagesView.as_view(),
        name="staff-support-messages",
    ),
    path(
        "staff/support/<int:pk>/reply/",
        support.StaffReplyView.as_view(),
        name="staff-support-reply",
    ),
    path(
        "staff/support/<int:pk>/status/",
        support.StaffStatusView.as_view(),
        name="staff-support-status",
    ),
]
