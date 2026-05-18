from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from .models import Project, Room
from .pdf_analysis import _sample_plan_rooms


class WallpaperEstimateTests(TestCase):
    def test_rolls_are_rounded_up_per_room(self):
        project = Project.objects.create(
            name="テスト案件",
            wallpaper_roll_width_m=Decimal("0.92"),
            wallpaper_roll_length_m=Decimal("50"),
            loss_rate_percent=Decimal("8"),
            unit_price_per_roll=11800,
        )
        room = Room.objects.create(
            project=project,
            name="廊下",
            perimeter_m=Decimal("10.2"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("1.5"),
            ceiling_area_m2=Decimal("6.0"),
        )

        self.assertEqual(room.total_area.quantize(Decimal("0.01")), Decimal("31.30"))
        self.assertEqual(room.rolls_required, 1)

    def test_project_create_view_saves_rooms_and_redirects(self):
        response = self.client.post(
            reverse("project_create"),
            {
                "name": "橘邸",
                "client_name": "橘工務店",
                "wallpaper_roll_width_m": "0.92",
                "wallpaper_roll_length_m": "50",
                "loss_rate_percent": "8",
                "unit_price_per_roll": "11800",
                "room_name": ["LDK"],
                "perimeter_m": ["18"],
                "height_m": ["2.4"],
                "opening_area_m2": ["4.2"],
                "ceiling_area_m2": ["20"],
                "note": [""],
            },
        )

        project = Project.objects.get(name="橘邸")
        self.assertRedirects(response, reverse("project_detail", args=[project.pk]))
        self.assertEqual(project.rooms.count(), 1)
        self.assertEqual(project.total_rolls, 2)

    def test_sample_pdf_analysis_rooms_match_expected_totals(self):
        project = Project.objects.create(
            name="PDF解析テスト",
            wallpaper_roll_width_m=Decimal("0.92"),
            wallpaper_roll_length_m=Decimal("50"),
            loss_rate_percent=Decimal("8"),
            unit_price_per_roll=11800,
        )
        for analyzed_room in _sample_plan_rooms():
            Room.objects.create(
                project=project,
                name=analyzed_room.name,
                perimeter_m=analyzed_room.perimeter_m,
                height_m=analyzed_room.height_m,
                opening_area_m2=analyzed_room.opening_area_m2,
                ceiling_area_m2=analyzed_room.ceiling_area_m2,
                note=analyzed_room.note,
            )

        self.assertEqual(project.rooms.count(), 13)
        self.assertEqual(project.total_area.quantize(Decimal("0.01")), Decimal("472.04"))
        self.assertEqual(project.total_rolls, 16)

    def test_sample_pdf_analysis_uses_only_existing_plan_pages(self):
        project = Project.objects.create(
            name="PDF解析テスト 1Fのみ",
            wallpaper_roll_width_m=Decimal("0.92"),
            wallpaper_roll_length_m=Decimal("50"),
            loss_rate_percent=Decimal("8"),
            unit_price_per_roll=11800,
        )
        page_map = {
            "page_1f_plan": 5,
            "page_2f_plan": None,
            "page_3f_plan": None,
            "page_section": 8,
        }
        for analyzed_room in _sample_plan_rooms(page_map):
            Room.objects.create(
                project=project,
                name=analyzed_room.name,
                perimeter_m=analyzed_room.perimeter_m,
                height_m=analyzed_room.height_m,
                opening_area_m2=analyzed_room.opening_area_m2,
                ceiling_area_m2=analyzed_room.ceiling_area_m2,
                note=analyzed_room.note,
            )

        self.assertEqual(project.rooms.count(), 8)
        self.assertEqual(project.total_area.quantize(Decimal("0.01")), Decimal("284.80"))

# Create your tests here.
