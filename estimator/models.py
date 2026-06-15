"""壁紙積算の案件、部屋、壁紙マスタ、初期値設定モデルを定義する。"""

from django.core.exceptions import ValidationError
from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver
from decimal import Decimal, ROUND_CEILING
import logging
import re


WALLPAPER_TOTAL_METHOD = "wallpaper_total"
ROOM_TOTAL_METHOD = "room_total"

ESTIMATE_METHOD_CHOICES = (
    (WALLPAPER_TOTAL_METHOD, "壁紙別合算方式"),
    (ROOM_TOTAL_METHOD, "部屋別積上方式"),
)

SURFACE_FIELDS = (
    ("east", "1面", "wall"),
    ("west", "2面", "wall"),
    ("south", "3面", "wall"),
    ("north", "4面", "wall"),
    ("ceiling", "天井", "ceiling"),
)

ROOM_SOURCE_AI = "ai"
ROOM_SOURCE_AI_MISSING = "ai_missing"
ROOM_SOURCE_MANUAL = "manual"

ROOM_SOURCE_CHOICES = (
    (ROOM_SOURCE_AI, "AI読取"),
    (ROOM_SOURCE_AI_MISSING, "抽出失敗"),
    (ROOM_SOURCE_MANUAL, "手動追加"),
)

ANALYSIS_STATUS_NOT_STARTED = "not_started"
ANALYSIS_STATUS_PENDING = "pending"
ANALYSIS_STATUS_RUNNING = "running"
ANALYSIS_STATUS_SUCCEEDED = "succeeded"
ANALYSIS_STATUS_FAILED = "failed"

ANALYSIS_STATUS_CHOICES = (
    (ANALYSIS_STATUS_NOT_STARTED, "未実行"),
    (ANALYSIS_STATUS_PENDING, "待機中"),
    (ANALYSIS_STATUS_RUNNING, "解析中"),
    (ANALYSIS_STATUS_SUCCEEDED, "解析完了"),
    (ANALYSIS_STATUS_FAILED, "解析失敗"),
)

logger = logging.getLogger(__name__)


class Wallpaper(models.Model):
    """壁紙マスタを表すモデル。"""
    display_order = models.CharField("表示順", max_length=3, default="999")
    number = models.CharField("壁紙No.", max_length=3, unique=True)
    name = models.CharField("壁紙名称", max_length=80)
    roll_width_m = models.DecimalField("ロール幅(m)", max_digits=5, decimal_places=2, default=Decimal("0.92"))
    roll_length_m = models.DecimalField("ロール長さ(m)", max_digits=5, decimal_places=2, default=Decimal("50"))
    loss_rate_percent = models.DecimalField("ロス率(%)", max_digits=5, decimal_places=1, default=Decimal("8"))
    unit_price_per_roll = models.PositiveIntegerField("1ロール単価", default=11800)
    is_active = models.BooleanField("有効", default=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        """モデルやフォームのメタ情報を定義する。"""
        ordering = ["display_order", "number"]
        verbose_name = "壁紙マスタ"
        verbose_name_plural = "壁紙マスタ"

    def __str__(self):
        """画面表示用の文字列表現を返す。

        Returns:
            表示用の文字列。
        """
        return f"{self.number}：{self.name}"

    @property
    def roll_area(self):
        """1ロールあたりの面積を返す。

        Returns:
            ロール幅とロール長さから算出した面積。
        """
        return self.roll_width_m * self.roll_length_m

    @property
    def is_none_wallpaper(self):
        """壁紙無しを表すマスタかどうかを返す。

        Returns:
            壁紙No.が000の場合はTrue。
        """
        return self.number == "000"

    def clean(self):
        """保存前に壁紙マスタの値を正規化・検証する。"""
        self.display_order = _normalize_code(self.display_order)
        self.number = _normalize_code(self.number)
        if self.number == "000":
            self.display_order = "000"
            self.name = "壁紙無し"
            self.roll_width_m = Decimal("0")
            self.roll_length_m = Decimal("0")
            self.loss_rate_percent = Decimal("0")
            self.unit_price_per_roll = 0
            self.is_active = True
        elif self.number == "001":
            existing = Wallpaper.objects.filter(number="001").exclude(pk=self.pk).first()
            if existing:
                raise ValidationError("壁紙No.001は複数登録できません。")
            self.display_order = _normalize_code(self.display_order or "001")
            self.name = "標準壁紙"
            self.roll_width_m = Decimal("0.92")
            self.roll_length_m = Decimal("50")
            self.loss_rate_percent = Decimal("8")
            self.unit_price_per_roll = 11800
            self.is_active = True

    def save(self, *args, **kwargs):
        """モデル検証を行ってから保存する。

        Args:
            args: Django標準の保存処理へ渡す位置引数。
            kwargs: 追加のキーワード引数。
        """
        self.full_clean()
        super().save(*args, **kwargs)

    @classmethod
    def ensure_defaults(cls):
        """必須の壁紙マスタを作成または取得する。

        Returns:
            壁紙無しと標準壁紙のタプル。
        """
        none_wallpaper, _created = cls.objects.get_or_create(
            number="000",
            defaults={
                "display_order": "000",
                "name": "壁紙無し",
                "roll_width_m": Decimal("0"),
                "roll_length_m": Decimal("0"),
                "loss_rate_percent": Decimal("0"),
                "unit_price_per_roll": 0,
                "is_active": True,
            },
        )
        standard, _created = cls.objects.get_or_create(
            number="001",
            defaults={
                "display_order": "001",
                "name": "標準壁紙",
                "roll_width_m": Decimal("0.92"),
                "roll_length_m": Decimal("50"),
                "loss_rate_percent": Decimal("8"),
                "unit_price_per_roll": 11800,
                "is_active": True,
            },
        )
        return none_wallpaper, standard


class Project(models.Model):
    """壁紙積算の案件を表すモデル。"""
    name = models.CharField("案件名", max_length=120)
    client_name = models.CharField("顧客名", max_length=120, blank=True)
    drawing_pdf = models.FileField("図面PDF", upload_to="drawings/", blank=True)
    drawing_pdf_storage_path = models.CharField("図面PDF Storageパス", max_length=512, blank=True)
    drawing_pdf_original_name = models.CharField("図面PDF 元ファイル名", max_length=255, blank=True)
    drawing_pdf_content_type = models.CharField("図面PDF MIMEタイプ", max_length=100, blank=True)
    drawing_pdf_size = models.PositiveIntegerField("図面PDF サイズ", null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="アップロードユーザー",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_projects",
    )
    wallpaper_roll_width_m = models.DecimalField("ロール幅(m)", max_digits=5, decimal_places=2, default=Decimal("0.92"))
    wallpaper_roll_length_m = models.DecimalField("ロール長さ(m)", max_digits=5, decimal_places=2, default=Decimal("50"))
    loss_rate_percent = models.DecimalField("ロス率(%)", max_digits=5, decimal_places=1, default=Decimal("8"))
    unit_price_per_roll = models.PositiveIntegerField("1ロール単価", default=11800)
    adopted_estimate_method = models.CharField(
        "採用見積方式",
        max_length=24,
        choices=ESTIMATE_METHOD_CHOICES,
        default=WALLPAPER_TOTAL_METHOD,
    )
    page_1f_plan = models.CharField("1F平面図ページ", max_length=8, default="ー")
    page_2f_plan = models.CharField("2F平面図ページ", max_length=8, default="ー")
    page_3f_plan = models.CharField("3F平面図ページ", max_length=8, default="ー")
    page_development_start = models.CharField("展開図開始ページ", max_length=8, default="ー")
    page_development_end = models.CharField("展開図終了ページ", max_length=8, default="ー")
    page_1f_ceiling_plan = models.CharField("1F天井伏図ページ", max_length=8, default="ー")
    page_2f_ceiling_plan = models.CharField("2F天井伏図ページ", max_length=8, default="ー")
    page_3f_ceiling_plan = models.CharField("3F天井伏図ページ", max_length=8, default="ー")
    page_floor_area_table = models.CharField("床面積表ページ", max_length=8, default="ー")
    page_living_area_table = models.CharField("居室区画面積表ページ", max_length=8, default="ー")
    page_finish_table = models.CharField("室内仕上表ページ", max_length=8, default="ー")
    page_internal_finish_table = models.CharField("内部仕上表ページ", max_length=8, default="ー")
    page_fixture_table_start = models.CharField("建具表開始ページ", max_length=8, default="ー")
    page_fixture_table_end = models.CharField("建具表終了ページ", max_length=8, default="ー")
    page_other_tables = models.CharField("その他表ページ", max_length=80, blank=True, default="")
    memo = models.TextField("メモ", blank=True)
    analysis_status = models.CharField(
        "解析ステータス",
        max_length=20,
        choices=ANALYSIS_STATUS_CHOICES,
        default=ANALYSIS_STATUS_NOT_STARTED,
    )
    analysis_error_message = models.TextField("解析エラーメッセージ", blank=True)
    analysis_started_at = models.DateTimeField("解析開始日時", null=True, blank=True)
    analysis_finished_at = models.DateTimeField("解析終了日時", null=True, blank=True)
    analysis_model = models.CharField("解析モデル", max_length=80, blank=True)
    last_calculation_seconds = models.PositiveIntegerField("直近計算時間(秒)", null=True, blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        """モデルやフォームのメタ情報を定義する。"""
        ordering = ["-updated_at"]
        verbose_name = "案件"
        verbose_name_plural = "案件"

    def __str__(self):
        """画面表示用の文字列表現を返す。

        Returns:
            表示用の文字列。
        """
        return self.name

    @property
    def has_drawing_pdf(self):
        """案件に図面PDFが紐づいているかを返す。

        Returns:
            StorageパスまたはローカルPDFがある場合はTrue。
        """
        return bool(self.drawing_pdf_storage_path or self.drawing_pdf)

    @property
    def drawing_pdf_filename(self):
        """画面表示用の図面PDFファイル名を返す。

        Returns:
            元ファイル名、保存ファイル名、または既定のPDFファイル名。
        """
        if self.drawing_pdf_original_name:
            return self.drawing_pdf_original_name
        if self.drawing_pdf:
            return self.drawing_pdf.name.rsplit("/", 1)[-1]
        return "drawing.pdf"

    def can_view_drawing_pdf(self, user):
        """指定ユーザーが図面PDFを閲覧できるかを返す。

        Args:
            user: 権限確認対象のユーザー。

        Returns:
            閲覧できる場合はTrue、それ以外はFalse。
        """
        if not self.has_drawing_pdf:
            return False
        if not self.uploaded_by_id:
            return bool(user.is_authenticated)
        return bool(user.is_authenticated and user.pk == self.uploaded_by_id)

    @property
    def roll_area(self):
        """1ロールあたりの面積を返す。

        Returns:
            ロール幅とロール長さから算出した面積。
        """
        return self.wallpaper_roll_width_m * self.wallpaper_roll_length_m

    @property
    def subtotal_area(self):
        """ロス率を含めない合計施工面積を返す。

        Returns:
            案件全体のロス率適用前の施工面積。
        """
        return self.wallpaper_summary["total_base_area"]

    @property
    def total_area(self):
        """ロス率を含めた合計必要面積を返す。

        Returns:
            案件全体のロス率適用後の必要面積。
        """
        return self.wallpaper_summary["total_required_area"]

    @property
    def total_rolls(self):
        """採用見積方式での合計ロール本数を返す。

        Returns:
            採用見積方式で計算したロール本数。
        """
        return self.selected_estimate_totals["rolls"]

    @property
    def total_cost(self):
        """採用見積方式での概算金額を返す。

        Returns:
            採用見積方式で計算した概算金額。
        """
        return self.selected_estimate_totals["cost"]

    @property
    def selected_estimate_totals(self):
        """採用見積方式に対応する合計値を返す。

        Returns:
            採用見積方式に対応するロール本数と金額の辞書。
        """
        summary = self.wallpaper_summary
        if self.adopted_estimate_method == ROOM_TOTAL_METHOD:
            return summary["room_total"]
        return summary["wallpaper_total"]

    @property
    def wallpaper_summary(self):
        """案件内の部屋情報から壁紙別・部屋別の集計を返す。

        Returns:
            壁紙別行、部屋別行、合計面積、ロール本数、金額を含む集計辞書。
        """
        rooms = list(self.rooms.all())
        by_wallpaper = {}
        room_rolls = {}
        for room in rooms:
            room_groups = {}
            if room.excluded_from_summary:
                room_rolls[room.pk] = []
                continue
            for item in room.wallpaper_surface_items():
                if item["wallpaper_no"] == "000":
                    continue
                key = item["key"]
                current = by_wallpaper.setdefault(key, _empty_wallpaper_total(item))
                current["base_area"] += item["base_area"]
                current["required_area"] += item["required_area"]
                if item["surface_type"] == "ceiling":
                    current["ceiling_area"] += item["base_area"]
                else:
                    current["wall_area"] += item["base_area"]

                room_current = room_groups.setdefault(key, _empty_wallpaper_total(item))
                room_current["base_area"] += item["base_area"]
                room_current["required_area"] += item["required_area"]
                if item["surface_type"] == "ceiling":
                    room_current["ceiling_area"] += item["base_area"]
                else:
                    room_current["wall_area"] += item["base_area"]

            room_rolls[room.pk] = []
            for total in room_groups.values():
                rolls = _ceil_rolls(total["required_area"], total["roll_area"])
                total["rolls"] = rolls
                total["cost"] = rolls * total["unit_price_per_roll"]
                room_rolls[room.pk].append(total)

        wallpaper_rows = []
        wallpaper_rolls = 0
        wallpaper_cost = 0
        room_total_rolls = 0
        room_total_cost = 0
        for total in by_wallpaper.values():
            rolls = _ceil_rolls(total["required_area"], total["roll_area"])
            cost = rolls * total["unit_price_per_roll"]
            total["wallpaper_total_rolls"] = rolls
            total["wallpaper_total_cost"] = cost
            total["room_total_rolls"] = sum(
                room_total["rolls"]
                for totals in room_rolls.values()
                for room_total in totals
                if room_total["key"] == total["key"]
            )
            total["room_total_cost"] = sum(
                room_total["cost"]
                for totals in room_rolls.values()
                for room_total in totals
                if room_total["key"] == total["key"]
            )
            wallpaper_rolls += total["wallpaper_total_rolls"]
            wallpaper_cost += total["wallpaper_total_cost"]
            room_total_rolls += total["room_total_rolls"]
            room_total_cost += total["room_total_cost"]
            wallpaper_rows.append(total)

        return {
            "rows": sorted(wallpaper_rows, key=lambda row: (row["wallpaper_no"], row["wallpaper_name"])),
            "room_rows": room_rolls,
            "total_base_area": sum((row["base_area"] for row in wallpaper_rows), Decimal("0")),
            "total_required_area": sum((row["required_area"] for row in wallpaper_rows), Decimal("0")),
            "wallpaper_total": {"rolls": wallpaper_rolls, "cost": wallpaper_cost},
            "room_total": {"rolls": room_total_rolls, "cost": room_total_cost},
        }


class EstimateDefaultSettings(models.Model):
    """新規案件作成時に使用する初期値設定モデル。"""
    wallpaper_roll_width_m = models.DecimalField("ロール幅(m)", max_digits=5, decimal_places=2, default=Decimal("0.92"))
    wallpaper_roll_length_m = models.DecimalField("ロール長さ(m)", max_digits=5, decimal_places=2, default=Decimal("50"))
    loss_rate_percent = models.DecimalField("ロス率(%)", max_digits=5, decimal_places=1, default=Decimal("8"))
    unit_price_per_roll = models.PositiveIntegerField("1ロール単価", default=11800)
    default_wallpaper = models.ForeignKey(
        Wallpaper,
        verbose_name="デフォルト壁紙",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        """モデルやフォームのメタ情報を定義する。"""
        verbose_name = "初期値設定"
        verbose_name_plural = "初期値設定"

    def __str__(self):
        """画面表示用の文字列表現を返す。

        Returns:
            表示用の文字列。
        """
        return "新規案件の初期値"

    @classmethod
    def load(cls):
        """新規案件の初期値設定を取得する。

        Returns:
            初期値設定インスタンス。
        """
        Wallpaper.ensure_defaults()
        settings, _created = cls.objects.get_or_create(pk=1)
        if settings.default_wallpaper_id is None:
            settings.default_wallpaper = Wallpaper.objects.get(number="001")
            settings.save(update_fields=["default_wallpaper", "updated_at"])
        return settings


class Room(models.Model):
    """案件内の部屋と面別の壁紙情報を表すモデル。"""
    project = models.ForeignKey(Project, related_name="rooms", on_delete=models.CASCADE)
    name = models.CharField("部屋名", max_length=80)
    source_type = models.CharField("部屋追加区分", max_length=16, choices=ROOM_SOURCE_CHOICES, default=ROOM_SOURCE_AI)
    excluded_from_summary = models.BooleanField("集計対象外", default=False)
    perimeter_m = models.DecimalField("周長(m)", max_digits=7, decimal_places=2)
    height_m = models.DecimalField("天井高(m)", max_digits=5, decimal_places=2, default=Decimal("2.4"))
    opening_area_m2 = models.DecimalField("開口部面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    ceiling_area_m2 = models.DecimalField("天井面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    note = models.CharField("備考", max_length=160, blank=True)
    east_surface_area_m2 = models.DecimalField("東壁面 面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    west_surface_area_m2 = models.DecimalField("西壁面 面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    south_surface_area_m2 = models.DecimalField("南壁面 面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    north_surface_area_m2 = models.DecimalField("北壁面 面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    ceiling_surface_area_m2 = models.DecimalField("天井 面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    east_opening_area_m2 = models.DecimalField("東壁面 開口部面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    west_opening_area_m2 = models.DecimalField("西壁面 開口部面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    south_opening_area_m2 = models.DecimalField("南壁面 開口部面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    north_opening_area_m2 = models.DecimalField("北壁面 開口部面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    east_wallpaper_no = models.CharField("東壁面 壁紙No.", max_length=3, default="001")
    east_wallpaper_name = models.CharField("東壁面 壁紙名称", max_length=80, default="標準壁紙")
    east_roll_width_m = models.DecimalField("東壁面 ロール幅(m)", max_digits=5, decimal_places=2, default=Decimal("0.92"))
    east_roll_length_m = models.DecimalField("東壁面 ロール長さ(m)", max_digits=5, decimal_places=2, default=Decimal("50"))
    east_loss_rate_percent = models.DecimalField("東壁面 ロス率(%)", max_digits=5, decimal_places=1, default=Decimal("8"))
    east_unit_price_per_roll = models.PositiveIntegerField("東壁面 1ロール単価", default=11800)
    west_wallpaper_no = models.CharField("西壁面 壁紙No.", max_length=3, default="001")
    west_wallpaper_name = models.CharField("西壁面 壁紙名称", max_length=80, default="標準壁紙")
    west_roll_width_m = models.DecimalField("西壁面 ロール幅(m)", max_digits=5, decimal_places=2, default=Decimal("0.92"))
    west_roll_length_m = models.DecimalField("西壁面 ロール長さ(m)", max_digits=5, decimal_places=2, default=Decimal("50"))
    west_loss_rate_percent = models.DecimalField("西壁面 ロス率(%)", max_digits=5, decimal_places=1, default=Decimal("8"))
    west_unit_price_per_roll = models.PositiveIntegerField("西壁面 1ロール単価", default=11800)
    south_wallpaper_no = models.CharField("南壁面 壁紙No.", max_length=3, default="001")
    south_wallpaper_name = models.CharField("南壁面 壁紙名称", max_length=80, default="標準壁紙")
    south_roll_width_m = models.DecimalField("南壁面 ロール幅(m)", max_digits=5, decimal_places=2, default=Decimal("0.92"))
    south_roll_length_m = models.DecimalField("南壁面 ロール長さ(m)", max_digits=5, decimal_places=2, default=Decimal("50"))
    south_loss_rate_percent = models.DecimalField("南壁面 ロス率(%)", max_digits=5, decimal_places=1, default=Decimal("8"))
    south_unit_price_per_roll = models.PositiveIntegerField("南壁面 1ロール単価", default=11800)
    north_wallpaper_no = models.CharField("北壁面 壁紙No.", max_length=3, default="001")
    north_wallpaper_name = models.CharField("北壁面 壁紙名称", max_length=80, default="標準壁紙")
    north_roll_width_m = models.DecimalField("北壁面 ロール幅(m)", max_digits=5, decimal_places=2, default=Decimal("0.92"))
    north_roll_length_m = models.DecimalField("北壁面 ロール長さ(m)", max_digits=5, decimal_places=2, default=Decimal("50"))
    north_loss_rate_percent = models.DecimalField("北壁面 ロス率(%)", max_digits=5, decimal_places=1, default=Decimal("8"))
    north_unit_price_per_roll = models.PositiveIntegerField("北壁面 1ロール単価", default=11800)
    ceiling_wallpaper_no = models.CharField("天井 壁紙No.", max_length=3, default="001")
    ceiling_wallpaper_name = models.CharField("天井 壁紙名称", max_length=80, default="標準壁紙")
    ceiling_roll_width_m = models.DecimalField("天井 ロール幅(m)", max_digits=5, decimal_places=2, default=Decimal("0.92"))
    ceiling_roll_length_m = models.DecimalField("天井 ロール長さ(m)", max_digits=5, decimal_places=2, default=Decimal("50"))
    ceiling_loss_rate_percent = models.DecimalField("天井 ロス率(%)", max_digits=5, decimal_places=1, default=Decimal("8"))
    ceiling_unit_price_per_roll = models.PositiveIntegerField("天井 1ロール単価", default=11800)

    class Meta:
        """モデルやフォームのメタ情報を定義する。"""
        ordering = ["id"]
        verbose_name = "部屋"
        verbose_name_plural = "部屋"

    def __str__(self):
        """画面表示用の文字列表現を返す。

        Returns:
            表示用の文字列。
        """
        return f"{self.project.name} / {self.name}"

    @property
    def display_name(self):
        """階数を含めた画面表示用の部屋名を返す。

        Returns:
            階数と部屋名を組み合わせた表示名。
        """
        floor = self.display_floor_label
        room_name = self.display_room_name
        if floor:
            return f"{floor} {room_name}".strip()
        return room_name

    @property
    def display_floor_label(self):
        """部屋名や根拠情報から表示用の階数ラベルを返す。

        Returns:
            表示用の階数ラベル。複数階にまたがる場合は空文字。
        """
        name = str(self.name or "").strip()
        if self._is_multi_floor_room(name):
            return ""

        return self._inferred_floor_label()

    @property
    def display_room_name(self):
        """階数表記を除いた表示用の部屋名を返す。

        Returns:
            階数表記を除いた部屋名。
        """
        name = str(self.name or "").strip()
        if self._is_multi_floor_room(name):
            return name

        if self.display_floor_label:
            return self._remove_floor_labels(name) or name
        return name

    def _inferred_floor_label(self):
        """部屋名や備考から階数ラベルを推定する。

        Returns:
            推定した階数ラベル。見つからない場合は空文字。
        """
        source = self._ascii_digits(f"{self.name} {self.note}")
        match = re.search(r"([1-9])\s*(?:F|階)", source, re.IGNORECASE)
        if match:
            return f"{match.group(1)}F"
        return self._floor_label_from_evidence_page(source)

    def _floor_label_from_evidence_page(self, source):
        """根拠ページ番号から階数ラベルを推定する。

        Args:
            source: 階数やページ番号の判定に使う文字列。

        Returns:
            根拠ページから推定した階数ラベル。見つからない場合は空文字。
        """
        page_matches = {
            "1F": self.project.page_1f_plan,
            "2F": self.project.page_2f_plan,
            "3F": self.project.page_3f_plan,
        }
        for floor_label, page_value in page_matches.items():
            page = self._page_number(page_value)
            if page and re.search(rf"(?<!\d){page}\s*(?:P|ページ)", source, re.IGNORECASE):
                return floor_label
        return ""

    def _is_multi_floor_room(self, name):
        """複数階にまたがる部屋名かどうかを返す。

        Args:
            name: 名前。

        Returns:
            吹抜など複数階にまたがる部屋名の場合はTrue。
        """
        return any(keyword in name for keyword in ("吹抜", "吹き抜け"))

    def _remove_floor_labels(self, value):
        """部屋名から階数表記を除去する。

        Args:
            value: 変換または正規化する値。

        Returns:
            階数表記を除去した部屋名。
        """
        normalized = self._ascii_digits(value)
        normalized = re.sub(r"\b[1-9]\s*F\b", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"[1-9]\s*階", "", normalized)
        return re.sub(r"\s+", " ", normalized).strip()

    @staticmethod
    def _ascii_digits(value):
        """全角数字を半角数字へ変換する。

        Args:
            value: 変換または正規化する値。

        Returns:
            全角数字を半角数字に変換した文字列。
        """
        return str(value or "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    @classmethod
    def _page_number(cls, value):
        """ページ指定文字列をページ番号へ変換する。

        Args:
            value: 変換または正規化する値。

        Returns:
            ページ番号。未指定や不正な値の場合はNone。
        """
        normalized = cls._ascii_digits(value).strip()
        if normalized in {"", "-", "ー", "－", "なし", "無し", "0"}:
            return None
        try:
            return int(normalized)
        except ValueError:
            return None

    @property
    def wall_area(self):
        """部屋の壁面積を返す。

        Returns:
            開口部控除後の壁面積。
        """
        if self.has_surface_measurements:
            return sum((self.net_surface_area(field) for field, _label, surface_type in SURFACE_FIELDS if surface_type == "wall"), Decimal("0"))
        area = (self.perimeter_m * self.height_m) - self.opening_area_m2
        return max(area, 0)

    @property
    def has_surface_measurements(self):
        """面別の面積または開口部面積が入力されているかを返す。

        Returns:
            面別入力がある場合はTrue。
        """
        return any(
            getattr(self, f"{field}_surface_area_m2") > 0
            for field, _label, _surface_type in SURFACE_FIELDS
        ) or any(
            getattr(self, f"{field}_opening_area_m2") > 0
            for field, _label, surface_type in SURFACE_FIELDS
            if surface_type == "wall"
        )

    def net_surface_area(self, field):
        """指定した面の開口部控除後面積を返す。

        Args:
            field: 対象の面またはフィールド名。

        Returns:
            指定面の開口部控除後面積。
        """
        surface_area = getattr(self, f"{field}_surface_area_m2")
        if field == "ceiling":
            return max(surface_area, Decimal("0"))
        opening_area = getattr(self, f"{field}_opening_area_m2")
        return max(surface_area - opening_area, Decimal("0"))

    @property
    def wallpaper_area(self):
        """壁紙無しを除いた施工面積を返す。

        Returns:
            集計対象の壁紙施工面積。
        """
        if self.excluded_from_summary:
            return Decimal("0")
        return sum((item["base_area"] for item in self.wallpaper_surface_items() if item["wallpaper_no"] != "000"), Decimal("0"))

    @property
    def total_area(self):
        """ロス率を含めた合計必要面積を返す。

        Returns:
            部屋全体のロス率適用後の必要面積。
        """
        if self.excluded_from_summary:
            return Decimal("0")
        return sum((item["required_area"] for item in self.wallpaper_surface_items() if item["wallpaper_no"] != "000"), Decimal("0"))

    @property
    def rolls_required(self):
        """部屋単位で必要なロール本数を返す。

        Returns:
            部屋内の壁紙別に切り上げた合計ロール本数。
        """
        if self.excluded_from_summary:
            return 0
        groups = {}
        for item in self.wallpaper_surface_items():
            if item["wallpaper_no"] == "000":
                continue
            group = groups.setdefault(item["key"], {"required_area": Decimal("0"), "roll_area": item["roll_area"]})
            group["required_area"] += item["required_area"]
        return sum((_ceil_rolls(group["required_area"], group["roll_area"]) for group in groups.values()), start=0)

    def wallpaper_surface_items(self):
        """部屋の各面ごとの壁紙積算情報を返す。

        Returns:
            面ごとの壁紙、面積、ロール幅、単価などを含む明細一覧。
        """
        derived_wall_face_area = max((self.perimeter_m * self.height_m) - self.opening_area_m2, Decimal("0")) / Decimal("4")
        derived_wall_opening_area = self.opening_area_m2 / Decimal("4")
        use_surface_measurements = self.has_surface_measurements
        items = []
        for field, label, surface_type in SURFACE_FIELDS:
            if use_surface_measurements:
                base_area = self.net_surface_area(field)
                surface_area = getattr(self, f"{field}_surface_area_m2")
                opening_area = Decimal("0") if field == "ceiling" else getattr(self, f"{field}_opening_area_m2")
            else:
                base_area = self.ceiling_area_m2 if field == "ceiling" else derived_wall_face_area
                surface_area = self.ceiling_area_m2 if field == "ceiling" else derived_wall_face_area + derived_wall_opening_area
                opening_area = Decimal("0") if field == "ceiling" else derived_wall_opening_area
            wallpaper_no = getattr(self, f"{field}_wallpaper_no")
            loss_rate = getattr(self, f"{field}_loss_rate_percent")
            roll_width = getattr(self, f"{field}_roll_width_m")
            roll_length = getattr(self, f"{field}_roll_length_m")
            roll_area = roll_width * roll_length
            multiplier = Decimal("1") + (loss_rate / Decimal("100"))
            items.append({
                "surface": field,
                "surface_label": label,
                "surface_type": surface_type,
                "wallpaper_no": wallpaper_no,
                "wallpaper_name": getattr(self, f"{field}_wallpaper_name"),
                "surface_area": surface_area,
                "opening_area": opening_area,
                "base_area": base_area,
                "required_area": Decimal("0") if wallpaper_no == "000" else base_area * multiplier,
                "loss_rate_percent": loss_rate,
                "roll_width_m": roll_width,
                "roll_length_m": roll_length,
                "roll_area": roll_area,
                "unit_price_per_roll": getattr(self, f"{field}_unit_price_per_roll"),
                "key": (
                    wallpaper_no,
                    getattr(self, f"{field}_wallpaper_name"),
                    roll_width,
                    roll_length,
                    loss_rate,
                    getattr(self, f"{field}_unit_price_per_roll"),
                ),
            })
        return items

    def apply_wallpaper_to_all_surfaces(self, wallpaper):
        """指定した壁紙を部屋の全ての面へ適用する。

        Args:
            wallpaper: 適用する壁紙マスタ。
        """
        for field, _label, _surface_type in SURFACE_FIELDS:
            self.apply_wallpaper(field, wallpaper)

    def apply_wallpaper(self, field, wallpaper):
        """指定した面へ壁紙マスタの情報を適用する。

        Args:
            field: 対象の面またはフィールド名。
            wallpaper: 適用する壁紙マスタ。
        """
        setattr(self, f"{field}_wallpaper_no", wallpaper.number)
        setattr(self, f"{field}_wallpaper_name", wallpaper.name)
        setattr(self, f"{field}_roll_width_m", wallpaper.roll_width_m)
        setattr(self, f"{field}_roll_length_m", wallpaper.roll_length_m)
        setattr(self, f"{field}_loss_rate_percent", wallpaper.loss_rate_percent)
        setattr(self, f"{field}_unit_price_per_roll", wallpaper.unit_price_per_roll)

    def set_default_surface_measurements(self):
        """周長・天井高・開口部から各面の初期面積を設定する。"""
        wall_gross_area = self.perimeter_m * self.height_m
        wall_surface_area = wall_gross_area / Decimal("4")
        wall_opening_area = self.opening_area_m2 / Decimal("4")
        for field, _label, surface_type in SURFACE_FIELDS:
            if surface_type == "ceiling":
                setattr(self, f"{field}_surface_area_m2", self.ceiling_area_m2)
            else:
                setattr(self, f"{field}_surface_area_m2", wall_surface_area)
                setattr(self, f"{field}_opening_area_m2", wall_opening_area)

    def sync_totals_from_surface_measurements(self):
        """面別入力値から部屋全体の開口部面積と天井面積を同期する。"""
        self.opening_area_m2 = sum(
            (getattr(self, f"{field}_opening_area_m2") for field, _label, surface_type in SURFACE_FIELDS if surface_type == "wall"),
            Decimal("0"),
        )
        self.ceiling_area_m2 = self.ceiling_surface_area_m2


def _normalize_code(value):
    """壁紙No.や表示順を3桁コードへ正規化する。

    Args:
        value: 変換または正規化する値。

    Returns:
        3桁にゼロ埋めしたコード文字列。
    """
    try:
        number = int(str(value or "0"))
    except ValueError:
        number = 0
    return f"{max(0, min(number, 999)):03d}"


def _ceil_rolls(required_area, roll_area):
    """必要面積からロール本数を切り上げ計算する。

    Args:
        required_area: 必要面積。
        roll_area: 1ロールあたりの面積。

    Returns:
        切り上げ後の必要ロール本数。
    """
    if roll_area <= 0 or required_area <= 0:
        return 0
    return int((required_area / roll_area).to_integral_value(rounding=ROUND_CEILING))


def _empty_wallpaper_total(item):
    """壁紙別集計行の初期値を作成する。

    Args:
        item: 壁紙集計用の面別情報。

    Returns:
        壁紙集計用の初期値辞書。
    """
    return {
        "key": item["key"],
        "wallpaper_no": item["wallpaper_no"],
        "wallpaper_name": item["wallpaper_name"],
        "loss_rate_percent": item["loss_rate_percent"],
        "roll_width_m": item["roll_width_m"],
        "roll_length_m": item["roll_length_m"],
        "roll_area": item["roll_area"],
        "unit_price_per_roll": item["unit_price_per_roll"],
        "base_area": Decimal("0"),
        "required_area": Decimal("0"),
        "wall_area": Decimal("0"),
        "ceiling_area": Decimal("0"),
    }


@receiver(pre_save, sender=Project)
def delete_replaced_project_pdf(sender, instance, **kwargs):
    """案件のPDF差し替え時に未参照の旧PDFを削除する。

    Args:
        sender: Djangoシグナルの送信元モデル。
        instance: Djangoシグナルで渡されるモデルインスタンス。
        kwargs: 追加のキーワード引数。
    """
    if not instance.pk:
        return
    old_path = (
        sender.objects.filter(pk=instance.pk)
        .values_list("drawing_pdf_storage_path", flat=True)
        .first()
    )
    new_path = instance.drawing_pdf_storage_path
    if old_path and old_path != new_path:
        _delete_unreferenced_storage_pdf(old_path, excluding_pk=instance.pk)


@receiver(post_delete, sender=Project)
def delete_project_pdf(sender, instance, **kwargs):
    """案件削除時に未参照のPDFを削除する。

    Args:
        sender: Djangoシグナルの送信元モデル。
        instance: Djangoシグナルで渡されるモデルインスタンス。
        kwargs: 追加のキーワード引数。
    """
    if instance.drawing_pdf_storage_path:
        _delete_unreferenced_storage_pdf(instance.drawing_pdf_storage_path)


def _delete_unreferenced_storage_pdf(object_path, excluding_pk=None):
    """他案件から参照されていないStorage上のPDFを削除する。

    Args:
        object_path: Supabase Storage上のPDFパス。
        excluding_pk: 参照確認から除外する案件ID。
    """
    query = Project.objects.filter(drawing_pdf_storage_path=object_path)
    if excluding_pk:
        query = query.exclude(pk=excluding_pk)
    if query.exists():
        return

    from . import storage

    if not storage.is_configured():
        return

    try:
        storage.delete_pdf(object_path)
    except storage.SupabaseStorageError:
        logger.exception("Could not delete Supabase PDF object: %s", object_path)
