from django.contrib import admin

from .models import (
    Holiday,
    LabDisciplineBinding,
    Laboratory,
    LaboratoryBookingSettings,
    LabSession,
    LabStand,
    Room,
    ScheduleEntry,
    ScheduleEntryDisciplineSelection,
    TrainingCenter,
)


@admin.register(TrainingCenter)
class TrainingCenterAdmin(admin.ModelAdmin):
    list_display = ("number", "name")
    search_fields = ("number", "name")


class LabDisciplineBindingInline(admin.TabularInline):
    model = LabDisciplineBinding
    extra = 0
    autocomplete_fields = ("discipline", "bound_by")


@admin.register(Laboratory)
class LaboratoryAdmin(admin.ModelAdmin):
    list_display = ("name", "training_center", "faculty", "lab_type", "short_name")
    list_filter = ("training_center", "faculty", "lab_type")
    search_fields = ("name", "short_name")
    inlines = [LabDisciplineBindingInline]


@admin.register(LabDisciplineBinding)
class LabDisciplineBindingAdmin(admin.ModelAdmin):
    list_display = ("laboratory", "discipline", "is_override", "bound_by", "created_at")
    list_filter = ("is_override",)
    search_fields = ("laboratory__name", "discipline__title", "reason")
    autocomplete_fields = ("laboratory", "discipline", "bound_by")


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("number", "name", "training_center", "laboratory", "default_lab_staff", "capacity", "is_blocked")
    list_filter = ("training_center", "laboratory", "is_blocked")
    filter_horizontal = ("disciplines",)
    autocomplete_fields = ("default_lab_staff",)


@admin.register(LabSession)
class LabSessionAdmin(admin.ModelAdmin):
    list_display = ("lab_work", "room", "starts_at", "capacity", "status")
    list_filter = ("status", "semester", "room__training_center")
    date_hierarchy = "starts_at"
    search_fields = ("lab_work__title",)


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name")


@admin.register(LabStand)
class LabStandAdmin(admin.ModelAdmin):
    list_display = ("name", "inventory_number", "training_center", "room", "is_published")
    list_filter = ("training_center", "is_published")


class ScheduleEntryDisciplineSelectionInline(admin.TabularInline):
    model = ScheduleEntryDisciplineSelection
    extra = 0
    filter_horizontal = ("lab_works",)


@admin.register(ScheduleEntry)
class ScheduleEntryAdmin(admin.ModelAdmin):
    list_display = ("room", "weekday", "start_time", "week_parity", "duty_role", "duty_person", "is_active")
    list_filter = ("semester", "week_parity", "is_active")
    inlines = [ScheduleEntryDisciplineSelectionInline]


@admin.register(LaboratoryBookingSettings)
class LaboratoryBookingSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "laboratory",
        "booking_horizon_days",
        "booking_day_opens_at",
        "booking_day_closes_at",
        "booking_cancel_hours",
        "restriction_hours_before_base",
        "auto_visited_mode",
    )
