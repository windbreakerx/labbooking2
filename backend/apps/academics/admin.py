from django.contrib import admin

from apps.scheduling.models import LabDisciplineBinding

from .models import (
    Department,
    Discipline,
    Faculty,
    GroupDisciplineLoad,
    GroupLabWorkOverride,
    LabWork,
    LabWorkMethodics,
    Semester,
    StudentGroup,
)


@admin.register(Faculty)
class FacultyAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "ordering")
    ordering = ("ordering", "title")
    search_fields = ("code", "title")


@admin.register(Semester)
class SemesterAdmin(admin.ModelAdmin):
    list_display = ("name", "start_date", "end_date", "is_active")
    list_filter = ("is_active",)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("title", "short_code", "faculty", "ordering")
    list_filter = ("faculty",)
    ordering = ("ordering", "title")
    search_fields = ("title", "short_code")


@admin.register(StudentGroup)
class StudentGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "faculty", "department", "dekanat_id")
    search_fields = ("name", "faculty", "dekanat_id")
    list_filter = ("faculty", "department")
    filter_horizontal = ("disciplines", "lab_works")


class LabDisciplineBindingInline(admin.TabularInline):
    model = LabDisciplineBinding
    extra = 0
    autocomplete_fields = ("laboratory", "bound_by")


@admin.register(Discipline)
class DisciplineAdmin(admin.ModelAdmin):
    list_display = ("title", "code", "short_code", "department", "semester", "is_published")
    list_filter = ("semester", "department", "is_published")
    search_fields = ("title", "code", "short_code")
    inlines = [LabDisciplineBindingInline]


@admin.register(LabWork)
class LabWorkAdmin(admin.ModelAdmin):
    list_display = ("title", "code", "number", "duration_minutes", "capacity", "primary_stand", "is_published")
    list_filter = ("is_published", "training_centers", "disciplines")
    search_fields = ("title", "code")
    filter_horizontal = ("disciplines", "training_centers", "laboratories")


@admin.register(LabWorkMethodics)
class LabWorkMethodicsAdmin(admin.ModelAdmin):
    list_display = ("display_name", "lab_work", "uploaded_at")
    list_filter = ("uploaded_at",)
    search_fields = ("title", "lab_work__title")


@admin.register(GroupDisciplineLoad)
class GroupDisciplineLoadAdmin(admin.ModelAdmin):
    list_display = ("group", "semester", "discipline", "source", "is_active")
    list_filter = ("semester", "source", "is_active")
    search_fields = ("group__name", "discipline__title", "import_batch_id")


@admin.register(GroupLabWorkOverride)
class GroupLabWorkOverrideAdmin(admin.ModelAdmin):
    list_display = ("group", "semester", "lab_work", "mode", "is_active")
    list_filter = ("semester", "mode", "is_active")
