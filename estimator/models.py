from django.core.exceptions import ValidationError
from django.db import models
from decimal import Decimal, ROUND_CEILING


WALLPAPER_TOTAL_METHOD = "wallpaper_total"
ROOM_TOTAL_METHOD = "room_total"

ESTIMATE_METHOD_CHOICES = (
    (WALLPAPER_TOTAL_METHOD, "壁紙別合算方式"),
    (ROOM_TOTAL_METHOD, "部屋別積上方式"),
)

SURFACE_FIELDS = (
    ("east", "東壁面", "wall"),
    ("west", "西壁面", "wall"),
    ("south", "南壁面", "wall"),
    ("north", "北壁面", "wall"),
    ("ceiling", "天井", "ceiling"),
)


class Wallpaper(models.Model):
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
        ordering = ["display_order", "number"]
        verbose_name = "壁紙マスタ"
        verbose_name_plural = "壁紙マスタ"

    def __str__(self):
        return f"{self.number}：{self.name}"

    @property
    def roll_area(self):
        return self.roll_width_m * self.roll_length_m

    @property
    def is_none_wallpaper(self):
        return self.number == "000"

    def clean(self):
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
        self.full_clean()
        super().save(*args, **kwargs)

    @classmethod
    def ensure_defaults(cls):
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
    name = models.CharField("案件名", max_length=120)
    client_name = models.CharField("顧客名", max_length=120, blank=True)
    drawing_pdf = models.FileField("図面PDF", upload_to="drawings/", blank=True)
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
        return self.wallpaper_summary["total_base_area"]

    @property
    def total_area(self):
        return self.wallpaper_summary["total_required_area"]

    @property
    def total_rolls(self):
        return self.selected_estimate_totals["rolls"]

    @property
    def total_cost(self):
        return self.selected_estimate_totals["cost"]

    @property
    def selected_estimate_totals(self):
        summary = self.wallpaper_summary
        if self.adopted_estimate_method == ROOM_TOTAL_METHOD:
            return summary["room_total"]
        return summary["wallpaper_total"]

    @property
    def wallpaper_summary(self):
        rooms = list(self.rooms.all())
        by_wallpaper = {}
        room_rolls = {}
        for room in rooms:
            room_groups = {}
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
        verbose_name = "初期値設定"
        verbose_name_plural = "初期値設定"

    def __str__(self):
        return "新規案件の初期値"

    @classmethod
    def load(cls):
        Wallpaper.ensure_defaults()
        settings, _created = cls.objects.get_or_create(pk=1)
        if settings.default_wallpaper_id is None:
            settings.default_wallpaper = Wallpaper.objects.get(number="001")
            settings.save(update_fields=["default_wallpaper", "updated_at"])
        return settings


class Room(models.Model):
    project = models.ForeignKey(Project, related_name="rooms", on_delete=models.CASCADE)
    name = models.CharField("部屋名", max_length=80)
    perimeter_m = models.DecimalField("周長(m)", max_digits=7, decimal_places=2)
    height_m = models.DecimalField("天井高(m)", max_digits=5, decimal_places=2, default=Decimal("2.4"))
    opening_area_m2 = models.DecimalField("開口部面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    ceiling_area_m2 = models.DecimalField("天井面積(m2)", max_digits=7, decimal_places=2, default=Decimal("0"))
    note = models.CharField("備考", max_length=160, blank=True)
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
        return sum((item["base_area"] for item in self.wallpaper_surface_items() if item["wallpaper_no"] != "000"), Decimal("0"))

    @property
    def total_area(self):
        return sum((item["required_area"] for item in self.wallpaper_surface_items() if item["wallpaper_no"] != "000"), Decimal("0"))

    @property
    def rolls_required(self):
        groups = {}
        for item in self.wallpaper_surface_items():
            if item["wallpaper_no"] == "000":
                continue
            group = groups.setdefault(item["key"], {"required_area": Decimal("0"), "roll_area": item["roll_area"]})
            group["required_area"] += item["required_area"]
        return sum((_ceil_rolls(group["required_area"], group["roll_area"]) for group in groups.values()), start=0)

    def wallpaper_surface_items(self):
        wall_face_area = self.wall_area / Decimal("4")
        items = []
        for field, label, surface_type in SURFACE_FIELDS:
            base_area = self.ceiling_area_m2 if field == "ceiling" else wall_face_area
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
        for field, _label, _surface_type in SURFACE_FIELDS:
            self.apply_wallpaper(field, wallpaper)

    def apply_wallpaper(self, field, wallpaper):
        setattr(self, f"{field}_wallpaper_no", wallpaper.number)
        setattr(self, f"{field}_wallpaper_name", wallpaper.name)
        setattr(self, f"{field}_roll_width_m", wallpaper.roll_width_m)
        setattr(self, f"{field}_roll_length_m", wallpaper.roll_length_m)
        setattr(self, f"{field}_loss_rate_percent", wallpaper.loss_rate_percent)
        setattr(self, f"{field}_unit_price_per_roll", wallpaper.unit_price_per_roll)


def _normalize_code(value):
    try:
        number = int(str(value or "0"))
    except ValueError:
        number = 0
    return f"{max(0, min(number, 999)):03d}"


def _ceil_rolls(required_area, roll_area):
    if roll_area <= 0 or required_area <= 0:
        return 0
    return int((required_area / roll_area).to_integral_value(rounding=ROUND_CEILING))


def _empty_wallpaper_total(item):
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
