from django.db import models
from decimal import Decimal, ROUND_CEILING


class Project(models.Model):
    name = models.CharField("案件名", max_length=120)
    client_name = models.CharField("顧客名", max_length=120, blank=True)
    drawing_pdf = models.FileField("図面PDF", upload_to="drawings/", blank=True)
    wallpaper_roll_width_m = models.DecimalField("ロール幅(m)", max_digits=5, decimal_places=2, default=Decimal("0.92"))
    wallpaper_roll_length_m = models.DecimalField("ロール長さ(m)", max_digits=5, decimal_places=2, default=Decimal("50"))
    loss_rate_percent = models.DecimalField("ロス率(%)", max_digits=5, decimal_places=1, default=Decimal("8"))
    unit_price_per_roll = models.PositiveIntegerField("1ロール単価", default=11800)
    page_1f_plan = models.CharField("1F平面図ページ", max_length=8, default="ー")
    page_2f_plan = models.CharField("2F平面図ページ", max_length=8, default="ー")
    page_3f_plan = models.CharField("3F平面図ページ", max_length=8, default="ー")
    page_east_elevation = models.CharField("東側立面図ページ", max_length=8, default="ー")
    page_west_elevation = models.CharField("西側立面図ページ", max_length=8, default="ー")
    page_south_elevation = models.CharField("南側立面図ページ", max_length=8, default="ー")
    page_north_elevation = models.CharField("北側立面図ページ", max_length=8, default="ー")
    page_section = models.CharField("断面図ページ", max_length=8, default="ー")
    memo = models.TextField("メモ", blank=True)
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "案件"
        verbose_name_plural = "案件"

    def __str__(self):
        return self.name

    @property
    def roll_area(self):
        return self.wallpaper_roll_width_m * self.wallpaper_roll_length_m

    @property
    def subtotal_area(self):
        return sum((room.wallpaper_area for room in self.rooms.all()), start=0)

    @property
    def total_area(self):
        return sum((room.total_area for room in self.rooms.all()), start=0)

    @property
    def total_rolls(self):
        return sum((room.rolls_required for room in self.rooms.all()), start=0)

    @property
    def total_cost(self):
        return self.total_rolls * self.unit_price_per_roll


class EstimateDefaultSettings(models.Model):
    wallpaper_roll_width_m = models.DecimalField("ロール幅(m)", max_digits=5, decimal_places=2, default=Decimal("0.92"))
    wallpaper_roll_length_m = models.DecimalField("ロール長さ(m)", max_digits=5, decimal_places=2, default=Decimal("50"))
    loss_rate_percent = models.DecimalField("ロス率(%)", max_digits=5, decimal_places=1, default=Decimal("8"))
    unit_price_per_roll = models.PositiveIntegerField("1ロール単価", default=11800)
    updated_at = models.DateTimeField("更新日時", auto_now=True)

    class Meta:
        verbose_name = "初期値設定"
        verbose_name_plural = "初期値設定"

    def __str__(self):
        return "新規案件の初期値"

    @classmethod
    def load(cls):
        settings, _created = cls.objects.get_or_create(pk=1)
        return settings


class Room(models.Model):
    project = models.ForeignKey(Project, related_name="rooms", on_delete=models.CASCADE)
    name = models.CharField("部屋名", max_length=80)
    perimeter_m = models.DecimalField("周長(m)", max_digits=7, decimal_places=2)
    height_m = models.DecimalField("天井高(m)", max_digits=5, decimal_places=2, default=Decimal("2.4"))
    opening_area_m2 = models.DecimalField("開口部面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    ceiling_area_m2 = models.DecimalField("天井面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    note = models.CharField("備考", max_length=160, blank=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "部屋"
        verbose_name_plural = "部屋"

    def __str__(self):
        return f"{self.project.name} / {self.name}"

    @property
    def wall_area(self):
        area = (self.perimeter_m * self.height_m) - self.opening_area_m2
        return max(area, 0)

    @property
    def wallpaper_area(self):
        return self.wall_area + self.ceiling_area_m2

    @property
    def total_area(self):
        multiplier = Decimal("1") + (self.project.loss_rate_percent / Decimal("100"))
        return self.wallpaper_area * multiplier

    @property
    def rolls_required(self):
        roll_area = self.project.roll_area
        if roll_area <= 0:
            return 0
        return int((self.total_area / roll_area).to_integral_value(rounding=ROUND_CEILING))
