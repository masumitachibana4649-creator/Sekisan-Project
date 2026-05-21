from decimal import Decimal
import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import Project, Room
from .pdf_analysis import analyze_wallpaper_pdf, _parse_ai_analysis_response, _sample_plan_rooms


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

    def test_ai_analysis_response_is_converted_to_analyzed_rooms(self):
        payload = {
            "rooms": [
                {
                    "name": "2F LDK",
                    "perimeter_m": 19.1,
                    "height_m": 2.4,
                    "opening_area_m2": 3.25,
                    "ceiling_area_m2": 22.64,
                    "confidence": 0.82,
                    "evidence": "2F平面図: LDK 13.68帖、C.H 2400",
                }
            ],
            "warnings": ["開口部は一部推定"],
        }

        result = _parse_ai_analysis_response(json.dumps(payload))

        self.assertEqual(len(result["rooms"]), 1)
        room = result["rooms"][0]
        self.assertEqual(room.name, "2F LDK")
        self.assertEqual(room.perimeter_m, Decimal("19.10"))
        self.assertEqual(room.height_m, Decimal("2.40"))
        self.assertEqual(room.opening_area_m2, Decimal("3.25"))
        self.assertEqual(room.ceiling_area_m2, Decimal("22.64"))
        self.assertIn("根拠: 2F平面図", room.note)
        self.assertIn("AI信頼度: 0.82", room.note)
        self.assertEqual(result["warnings"], ["開口部は一部推定"])

    def test_analyze_wallpaper_pdf_uses_ai_extraction_and_keeps_calculation_outside_ai(self):
        extracted_room = _parse_ai_analysis_response(json.dumps({
            "rooms": [
                {
                    "name": "洋室",
                    "perimeter_m": 12,
                    "height_m": 2.4,
                    "opening_area_m2": 1.2,
                    "ceiling_area_m2": 9,
                    "confidence": 0.9,
                    "evidence": "1F平面図: 洋室 3.0x3.0m",
                }
            ],
            "warnings": [],
        }))

        with patch("estimator.pdf_analysis._pdf_page_count", return_value=10), patch(
            "estimator.pdf_analysis._extract_rooms_with_ai",
            return_value=extracted_room,
        ) as extract_rooms:
            result = analyze_wallpaper_pdf("dummy.pdf", {"page_1f_plan": "5"})

        extract_rooms.assert_called_once()
        self.assertEqual(result.rooms[0].name, "洋室")
        self.assertIn("壁紙量とロール本数はシステムの計算式で算出", result.memo)

    def test_project_create_falls_back_when_pdf_analysis_has_unexpected_error(self):
        with patch("estimator.views.analyze_wallpaper_pdf", side_effect=RuntimeError("boom")):
            response = self.client.post(
                reverse("project_create"),
                {
                    "name": "PDFエラー案件",
                    "client_name": "橘工務店",
                    "auto_read_pdf": "on",
                    "drawing_pdf": SimpleUploadedFile("dummy.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
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

        project = Project.objects.get(name="PDFエラー案件")
        self.assertRedirects(response, reverse("project_detail", args=[project.pk]))
        self.assertEqual(project.rooms.count(), 1)

# Create your tests here.
