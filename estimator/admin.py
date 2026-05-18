from django import forms
from django.apps import apps
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.models import Group, Permission
from django.contrib.admin.widgets import FilteredSelectMultiple

from .models import EstimateDefaultSettings, Project, Room


PERMISSION_ACTIONS = {
    "add": "追加",
    "change": "変更",
    "delete": "削除",
    "view": "表示",
}

MODEL_LABELS = {
    ("auth", "permission"): "権限",
}


class RoomInline(admin.TabularInline):
    model = Room
    extra = 1


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "client_name", "total_rolls", "total_cost", "updated_at")
    search_fields = ("name", "client_name")
    inlines = [RoomInline]


@admin.register(EstimateDefaultSettings)
class EstimateDefaultSettingsAdmin(admin.ModelAdmin):
    fields = (
        "wallpaper_roll_width_m",
        "wallpaper_roll_length_m",
        "loss_rate_percent",
        "unit_price_per_roll",
    )

    def has_add_permission(self, request):
        return not EstimateDefaultSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class PermissionChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, permission):
        app_label = _app_verbose_name(permission.content_type.app_label)
        model_label = _model_verbose_name(permission)
        action_label = _action_label(permission.codename)

        return (
            f"{_pad_japanese(app_label, 8)}｜　"
            f"{_pad_japanese(model_label, 8)}｜　"
            f"{action_label}"
        )


class GroupAdminForm(forms.ModelForm):
    permissions = PermissionChoiceField(
        label="権限",
        queryset=Permission.objects.select_related("content_type").order_by(
            "content_type__app_label",
            "content_type__model",
            "codename",
        ),
        required=False,
        widget=FilteredSelectMultiple("権限", is_stacked=False),
        help_text="このグループに所属するユーザーへ付与する権限です。",
    )

    class Meta:
        model = Group
        fields = "__all__"


class GroupAdmin(DjangoGroupAdmin):
    form = GroupAdminForm

    class Media:
        css = {"all": ("estimator/admin.css",)}


admin.site.unregister(Group)
admin.site.register(Group, GroupAdmin)


def _app_verbose_name(app_label):
    try:
        return apps.get_app_config(app_label).verbose_name
    except LookupError:
        return app_label


def _model_verbose_name(permission):
    model_label = MODEL_LABELS.get(
        (permission.content_type.app_label, permission.content_type.model)
    )
    if model_label:
        return model_label

    model_class = permission.content_type.model_class()
    if model_class:
        return str(model_class._meta.verbose_name)
    return permission.content_type.name


def _action_label(codename):
    action = codename.split("_", 1)[0]
    return PERMISSION_ACTIONS.get(action, codename)


def _pad_japanese(value, width):
    value = str(value)
    padding = max(width - len(value), 0)
    return value + ("　" * padding)
