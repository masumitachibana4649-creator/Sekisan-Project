from django import forms
from django.apps import apps
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import Group, Permission, User
from django.contrib.admin.widgets import FilteredSelectMultiple

from .models import EstimateDefaultSettings, Project, Room, Wallpaper


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
    fields = (
        "name",
        "perimeter_m",
        "height_m",
        "opening_area_m2",
        "ceiling_area_m2",
        "east_wallpaper_name",
        "west_wallpaper_name",
        "south_wallpaper_name",
        "north_wallpaper_name",
        "ceiling_wallpaper_name",
        "note",
    )
    readonly_fields = (
        "east_wallpaper_name",
        "west_wallpaper_name",
        "south_wallpaper_name",
        "north_wallpaper_name",
        "ceiling_wallpaper_name",
    )


@admin.register(Wallpaper)
class WallpaperAdmin(admin.ModelAdmin):
    list_display = (
        "display_order",
        "number",
        "name",
        "roll_width_display",
        "roll_length_display",
        "loss_rate_display",
        "unit_price_display",
        "is_active",
    )
    list_display_links = ("number",)
    list_editable = ("display_order", "is_active")
    search_fields = ("number", "name")
    ordering = ("display_order", "number")

    class Media:
        css = {"all": ("estimator/admin.css",)}

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.number == "000":
            return (
                "display_order",
                "number",
                "name",
                "roll_width_m",
                "roll_length_m",
                "loss_rate_percent",
                "unit_price_per_roll",
                "is_active",
            )
        if obj and obj.number == "001":
            return (
                "number",
                "name",
                "roll_width_m",
                "roll_length_m",
                "loss_rate_percent",
                "unit_price_per_roll",
                "is_active",
            )
        return ()

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="ロール幅(m)", ordering="roll_width_m")
    def roll_width_display(self, obj):
        return obj.roll_width_m

    @admin.display(description="ロール長さ(m)", ordering="roll_length_m")
    def roll_length_display(self, obj):
        return obj.roll_length_m

    @admin.display(description="ロス率(%)", ordering="loss_rate_percent")
    def loss_rate_display(self, obj):
        return obj.loss_rate_percent

    @admin.display(description="1ロール単価", ordering="unit_price_per_roll")
    def unit_price_display(self, obj):
        return obj.unit_price_per_roll


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "client_name", "total_rolls_display", "total_cost_display", "updated_at")
    search_fields = ("name", "client_name")
    inlines = [RoomInline]

    class Media:
        css = {"all": ("estimator/admin.css",)}

    @admin.display(description="ロール本数")
    def total_rolls_display(self, obj):
        return obj.total_rolls

    @admin.display(description="概算金額")
    def total_cost_display(self, obj):
        return obj.total_cost


@admin.register(EstimateDefaultSettings)
class EstimateDefaultSettingsAdmin(admin.ModelAdmin):
    fields = (
        "default_wallpaper",
        "wallpaper_roll_width_m",
        "wallpaper_roll_length_m",
        "loss_rate_percent",
        "unit_price_per_roll",
    )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "default_wallpaper":
            kwargs["queryset"] = Wallpaper.objects.filter(is_active=True).order_by("display_order", "number")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

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


class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "first_name", "last_name", "is_staff", "is_superuser", "is_active")
    list_filter = ("is_superuser", "is_staff", "groups", "is_active")
    search_fields = ("username", "email", "first_name", "last_name")

    class Media:
        css = {"all": ("estimator/admin.css",)}


admin.site.unregister(Group)
admin.site.register(Group, GroupAdmin)
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


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
