from django.contrib import admin

from .models import (
    AuditLog,
    Booking,
    BookingStatusHistory,
    StudentLabAttendance,
    SupportMessage,
    SupportTicket,
    WaitlistEntry,
)


class ReadOnlyAdminMixin:
    """Просмотр без права менять: статусы и аудит идут только через сервисы."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return True


class NoDeleteAdminMixin:
    def has_delete_permission(self, request, obj=None):
        return False


class BookingStatusHistoryInline(admin.TabularInline):
    model = BookingStatusHistory
    extra = 0
    readonly_fields = ("status", "changed_by", "changed_at", "note", "acknowledged_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Booking)
class BookingAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("student", "lab_work", "scheduled_at", "current_status", "cancel_source", "had_no_show", "registration_type")
    list_filter = ("current_status", "cancel_source", "had_no_show", "discipline", "registration_type")
    search_fields = ("student__email", "student__last_name")
    inlines = [BookingStatusHistoryInline]


@admin.register(BookingStatusHistory)
class BookingStatusHistoryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("booking", "status", "changed_by", "changed_at")
    list_filter = ("status",)
    search_fields = ("booking__student__email",)


@admin.register(StudentLabAttendance)
class StudentLabAttendanceAdmin(admin.ModelAdmin):
    list_display = ("student", "laboratory", "no_show_count", "explanation_required", "updated_at")
    list_filter = ("explanation_required", "laboratory")
    search_fields = ("student__email", "student__last_name", "laboratory__name")


@admin.register(WaitlistEntry)
class WaitlistEntryAdmin(admin.ModelAdmin):
    list_display = ("lab_session", "student", "position", "created_at")


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("action", "entity_type", "entity_id", "actor", "created_at")
    list_filter = ("action", "entity_type")


class SupportMessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 0


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ("subject", "student", "status", "created_at")
    list_filter = ("status",)
    inlines = [SupportMessageInline]
