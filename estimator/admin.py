"""Django管理画面の表示、権限ラベル、管理フォームを定義する。"""

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
    """案件管理画面で部屋をインライン編集する管理画面設定。"""
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
    """壁紙マスタの管理画面設定。"""
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
        """管理画面で読み込むCSSを定義する。"""
        css = {"all": ("estimator/admin.css",)}

    def get_readonly_fields(self, request, obj=None):
        """壁紙No.に応じて管理画面の読み取り専用項目を返す。

        Args:
            request: HTTPリクエスト。
            obj: 管理画面で処理するモデルインスタンス。

        Returns:
            処理結果。
        """
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
        """管理画面で削除を許可するかを返す。

        Args:
            request: HTTPリクエスト。
            obj: 管理画面で処理するモデルインスタンス。

        Returns:
            処理結果。
        """
        return False

    @admin.display(description="ロール幅(m)", ordering="roll_width_m")
    def roll_width_display(self, obj):
        """管理画面一覧に表示するロール幅を返す。

        Args:
            obj: 管理画面で処理するモデルインスタンス。

        Returns:
            処理結果。
        """
        return obj.roll_width_m

    @admin.display(description="ロール長さ(m)", ordering="roll_length_m")
    def roll_length_display(self, obj):
        """管理画面一覧に表示するロール長さを返す。

        Args:
            obj: 管理画面で処理するモデルインスタンス。

        Returns:
            処理結果。
        """
        return obj.roll_length_m

    @admin.display(description="ロス率(%)", ordering="loss_rate_percent")
    def loss_rate_display(self, obj):
        """管理画面一覧に表示するロス率を返す。

        Args:
            obj: 管理画面で処理するモデルインスタンス。

        Returns:
            処理結果。
        """
        return obj.loss_rate_percent

    @admin.display(description="1ロール単価", ordering="unit_price_per_roll")
    def unit_price_display(self, obj):
        """管理画面一覧に表示する1ロール単価を返す。

        Args:
            obj: 管理画面で処理するモデルインスタンス。

        Returns:
            処理結果。
        """
        return obj.unit_price_per_roll


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    """案件の管理画面設定。"""
    list_display = ("name", "client_name", "uploaded_by", "total_rolls_display", "total_cost_display", "updated_at")
    search_fields = ("name", "client_name")
    readonly_fields = (
        "drawing_pdf_storage_path",
        "drawing_pdf_original_name",
        "drawing_pdf_content_type",
        "drawing_pdf_size",
        "uploaded_by",
    )
    inlines = [RoomInline]

    class Media:
        """管理画面で読み込むCSSを定義する。"""
        css = {"all": ("estimator/admin.css",)}

    @admin.display(description="ロール本数")
    def total_rolls_display(self, obj):
        """管理画面一覧に表示するロール本数を返す。

        Args:
            obj: 管理画面で処理するモデルインスタンス。

        Returns:
            処理結果。
        """
        return obj.total_rolls

    @admin.display(description="概算金額")
    def total_cost_display(self, obj):
        """管理画面一覧に表示する概算金額を返す。

        Args:
            obj: 管理画面で処理するモデルインスタンス。

        Returns:
            処理結果。
        """
        return obj.total_cost


@admin.register(EstimateDefaultSettings)
class EstimateDefaultSettingsAdmin(admin.ModelAdmin):
    """新規案件の初期値設定を管理する画面設定。"""
    fields = (
        "default_wallpaper",
        "wallpaper_roll_width_m",
        "wallpaper_roll_length_m",
        "loss_rate_percent",
        "unit_price_per_roll",
    )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """外部キー選択肢を管理画面用に絞り込む。

        Args:
            db_field: フォームフィールド化するモデルフィールド。
            request: HTTPリクエスト。
            kwargs: 追加のキーワード引数。

        Returns:
            処理結果。
        """
        if db_field.name == "default_wallpaper":
            kwargs["queryset"] = Wallpaper.objects.filter(is_active=True).order_by("display_order", "number")
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def has_add_permission(self, request):
        """管理画面で追加を許可するかを返す。

        Args:
            request: HTTPリクエスト。

        Returns:
            処理結果。
        """
        return not EstimateDefaultSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        """管理画面で削除を許可するかを返す。

        Args:
            request: HTTPリクエスト。
            obj: 管理画面で処理するモデルインスタンス。

        Returns:
            処理結果。
        """
        return False


class PermissionChoiceField(forms.ModelMultipleChoiceField):
    """権限選択肢を日本語ラベルで表示するフォームフィールド。"""
    def label_from_instance(self, permission):
        """権限オブジェクトの表示ラベルを返す。

        Args:
            permission: 表示ラベルを作成する権限。

        Returns:
            処理結果。
        """
        app_label = _app_verbose_name(permission.content_type.app_label)
        model_label = _model_verbose_name(permission)
        action_label = _action_label(permission.codename)

        return (
            f"{_pad_japanese(app_label, 8)}｜　"
            f"{_pad_japanese(model_label, 8)}｜　"
            f"{action_label}"
        )


class GroupAdminForm(forms.ModelForm):
    """グループ権限を日本語表示で編集する管理画面フォーム。"""
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
        """モデルやフォームのメタ情報を定義する。"""
        model = Group
        fields = "__all__"


class GroupAdmin(DjangoGroupAdmin):
    """グループ管理画面の表示設定。"""
    form = GroupAdminForm

    class Media:
        """管理画面で読み込むCSSを定義する。"""
        css = {"all": ("estimator/admin.css",)}


class UserAdmin(DjangoUserAdmin):
    """ユーザー管理画面の表示設定。"""
    list_display = ("username", "email", "first_name", "last_name", "is_staff", "is_superuser", "is_active")
    list_filter = ("is_superuser", "is_staff", "groups", "is_active")
    search_fields = ("username", "email", "first_name", "last_name")

    class Media:
        """管理画面で読み込むCSSを定義する。"""
        css = {"all": ("estimator/admin.css",)}


admin.site.unregister(Group)
admin.site.register(Group, GroupAdmin)
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


def _app_verbose_name(app_label):
    """Djangoアプリの表示名を返す。

    Args:
        app_label: Djangoアプリのラベル。

    Returns:
        処理結果。
    """
    try:
        return apps.get_app_config(app_label).verbose_name
    except LookupError:
        return app_label


def _model_verbose_name(permission):
    """権限に紐づくモデルの表示名を返す。

    Args:
        permission: 表示ラベルを作成する権限。

    Returns:
        処理結果。
    """
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
    """権限コードから操作種別の日本語ラベルを返す。

    Args:
        codename: 権限コード名。

    Returns:
        処理結果。
    """
    action = codename.split("_", 1)[0]
    return PERMISSION_ACTIONS.get(action, codename)


def _pad_japanese(value, width):
    """日本語ラベルを指定幅にそろえる。

    Args:
        value: 変換または正規化する値。
        width: 文字幅をそろえる基準幅。

    Returns:
        処理結果。
    """
    value = str(value)
    padding = max(width - len(value), 0)
    return value + ("　" * padding)
