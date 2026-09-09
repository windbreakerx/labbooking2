from django.urls import path

from apps.bookings.views import staff, student, support

urlpatterns = [
    path("disciplines/", student.DisciplinesView.as_view(), name="disciplines"),
    path("disciplines/<int:pk>/lab-works/", student.LabWorksView.as_view(), name="lab-works"),
    path("lab-works/<int:pk>/book/", student.BookWizardView.as_view(), name="book-lab-work"),
    path(
        "lab-works/<int:pk>/book/<int:session_id>/",
        student.BookConfirmView.as_view(),
        name="book-session",
    ),
    path("my-bookings/", student.MyBookingsView.as_view(), name="my-bookings"),
    path(
        "my-bookings/<int:pk>/cancel/",
        student.CancelBookingView.as_view(),
        name="cancel-booking",
    ),
    path("support/", support.SupportListView.as_view(), name="support"),
    path("support/create/", support.SupportCreateView.as_view(), name="support-create"),
    path("support/<int:pk>/", support.SupportChatView.as_view(), name="support-detail"),
    path("support/<int:pk>/messages/", support.SupportMessagesView.as_view(), name="support-messages"),
    path("support/<int:pk>/reply/", support.SupportReplyView.as_view(), name="support-reply"),
    path("staff/bookings/", staff.StaffBookingsView.as_view(), name="staff-bookings"),
    path(
        "staff/bookings/<int:pk>/status/",
        staff.StaffBookingStatusView.as_view(),
        name="staff-booking-status",
    ),
    path("staff/manual-booking/", staff.StaffManualBookingView.as_view(), name="staff-manual-booking"),
    path(
        "staff/manual-booking/students/",
        staff.StaffManualStudentSearchView.as_view(),
        name="staff-manual-students",
    ),
    path(
        "staff/manual-booking/lab-works/",
        staff.StaffManualLabWorksView.as_view(),
        name="staff-manual-lab-works",
    ),
    path(
        "staff/manual-booking/slots/",
        staff.StaffManualSlotsView.as_view(),
        name="staff-manual-slots",
    ),
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
