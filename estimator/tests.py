from decimal import Decimal
import json
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .admin import ProjectAdmin
from .models import ROOM_TOTAL_METHOD, Project, Room, Wallpaper
from .pdf_analysis import AnalyzedRoom, PdfAnalysisResult, analyze_wallpaper_pdf, _parse_ai_analysis_response, _sample_plan_rooms
from .templatetags.estimate_extras import room_note, sentence_breaks


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

    def test_project_create_view_reads_pdf_and_redirects(self):
        analysis = PdfAnalysisResult(
            rooms=[
                AnalyzedRoom("LDK", Decimal("18"), Decimal("2.4"), Decimal("4.2"), Decimal("20"), "推定開口: 立面図から推定")
            ],
            memo="PDF AI読取",
        )
        with patch("estimator.views.analyze_wallpaper_pdf", return_value=analysis):
            response = self.client.post(
                reverse("project_create"),
                {
                    "name": "橘邸",
                    "client_name": "橘工務店",
                    "drawing_pdf": SimpleUploadedFile("dummy.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
                    "wallpaper_roll_width_m": "0.92",
                    "wallpaper_roll_length_m": "50",
                    "loss_rate_percent": "8",
                    "unit_price_per_roll": "11800",
                },
            )

        project = Project.objects.get(name="橘邸")
        self.assertRedirects(response, reverse("project_detail", args=[project.pk]))
        self.assertEqual(project.rooms.count(), 1)
        self.assertEqual(project.total_rolls, 2)

    def test_project_pdf_view_serves_uploaded_pdf(self):
        project = Project.objects.create(
            name="PDF表示テスト",
            drawing_pdf=SimpleUploadedFile(
                "drawing.pdf",
                b"%PDF-1.4\n% test pdf\n",
                content_type="application/pdf",
            ),
        )

        response = self.client.get(reverse("project_pdf", args=[project.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("inline", response["Content-Disposition"])

    def test_result_text_filters_insert_sentence_breaks_and_format_confidence(self):
        self.assertEqual(str(sentence_breaks("一文目です。二文目です。")), "一文目です。<br>二文目です。<br>")
        self.assertEqual(str(sentence_breaks("メモ:施工注意です。")), "メモ<br><br>施工注意です。<br>")
        self.assertEqual(str(sentence_breaks("入力メモです。\nPDF読取説明です。")), "入力メモです。<br><br>PDF読取説明です。<br>")
        self.assertEqual(str(room_note("根拠: 図面。AI信頼度: 0.82")), "根拠: 図面。<br>AI信頼度: 82%")

    def test_project_detail_shows_entered_memo_without_fixed_title(self):
        project = Project.objects.create(name="メモ表示テスト", memo="入力した内容です。\nPDF読取説明です。")

        response = self.client.get(reverse("project_detail", args=[project.pk]))

        self.assertContains(response, "入力した内容です。<br><br>PDF読取説明です。<br>", html=False)
        self.assertNotContains(response, "<strong>メモ</strong>", html=False)

    def test_project_admin_total_columns_have_japanese_labels(self):
        project_admin = ProjectAdmin(Project, AdminSite())

        self.assertEqual(project_admin.total_rolls_display.short_description, "ロール本数")
        self.assertEqual(project_admin.total_cost_display.short_description, "概算金額")

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
        self.assertEqual(project.total_rolls, 11)
        self.assertEqual(project.wallpaper_summary["room_total"]["rolls"], 16)

    def test_multiple_wallpapers_are_grouped_by_wallpaper_and_room(self):
        project = Project.objects.create(name="複数壁紙テスト")
        accent = Wallpaper.objects.create(
            display_order="002",
            number="002",
            name="アクセント",
            roll_width_m=Decimal("0.92"),
            roll_length_m=Decimal("10"),
            loss_rate_percent=Decimal("8"),
            unit_price_per_roll=5000,
        )
        room_a = Room.objects.create(
            project=project,
            name="A部屋",
            perimeter_m=Decimal("20"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("0"),
        )
        room_b = Room.objects.create(
            project=project,
            name="B部屋",
            perimeter_m=Decimal("20"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("0"),
        )
        for room in (room_a, room_b):
            room.apply_wallpaper_to_all_surfaces(accent)
            room.save()

        summary = project.wallpaper_summary

        self.assertEqual(summary["wallpaper_total"]["rolls"], 12)
        self.assertEqual(summary["room_total"]["rolls"], 12)
        self.assertEqual(summary["rows"][0]["wallpaper_no"], "002")

    def test_project_uses_room_total_method_when_selected(self):
        project = Project.objects.create(name="採用方式テスト", adopted_estimate_method=ROOM_TOTAL_METHOD)
        for room_name in ("A部屋", "B部屋"):
            Room.objects.create(
                project=project,
                name=room_name,
                perimeter_m=Decimal("20"),
                height_m=Decimal("2.4"),
                opening_area_m2=Decimal("0"),
                ceiling_area_m2=Decimal("0"),
            )

        self.assertEqual(project.wallpaper_summary["wallpaper_total"]["rolls"], 3)
        self.assertEqual(project.wallpaper_summary["room_total"]["rolls"], 4)
        self.assertEqual(project.total_rolls, 4)

    def test_project_save_wallpapers_creates_revision_with_selected_method(self):
        Wallpaper.ensure_defaults()
        project = Project.objects.create(name="保存元")
        room = Room.objects.create(
            project=project,
            name="LDK",
            perimeter_m=Decimal("18"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("20"),
        )

        response = self.client.post(
            reverse("project_save_wallpapers", args=[project.pk]),
            {
                "save_project_name": "保存元修正",
                "adopted_estimate_method": ROOM_TOTAL_METHOD,
                f"room_{room.pk}_east_wallpaper_no": "000",
                f"room_{room.pk}_west_wallpaper_no": "001",
                f"room_{room.pk}_south_wallpaper_no": "001",
                f"room_{room.pk}_north_wallpaper_no": "001",
                f"room_{room.pk}_ceiling_wallpaper_no": "001",
            },
        )

        revision = Project.objects.get(name="保存元修正")
        revision_room = revision.rooms.get()
        self.assertRedirects(response, reverse("project_detail", args=[revision.pk]))
        self.assertEqual(revision.adopted_estimate_method, ROOM_TOTAL_METHOD)
        self.assertEqual(revision_room.east_wallpaper_no, "000")
        self.assertEqual(project.rooms.get().east_wallpaper_no, "001")

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

    def test_analyze_wallpaper_pdf_rejects_obviously_incomplete_room_extraction(self):
        extracted_rooms = _parse_ai_analysis_response(json.dumps({
            "rooms": [
                {
                    "name": "1階 洋室",
                    "perimeter_m": 12,
                    "height_m": 2.4,
                    "opening_area_m2": 0,
                    "ceiling_area_m2": 10.5,
                    "confidence": 0.9,
                    "evidence": "1F平面図",
                },
                {
                    "name": "2階 LDK",
                    "perimeter_m": 20,
                    "height_m": 2.4,
                    "opening_area_m2": 0,
                    "ceiling_area_m2": 27.37,
                    "confidence": 0.9,
                    "evidence": "2F平面図",
                },
            ],
            "warnings": [],
        }))
        plan_text = "洋室 洋室 洋室 収納 収納 収納 収納 廊下 玄関 トイレ LDK 廊下 洗面所 トイレ 収納 浴室 バルコニー"

        with patch("estimator.pdf_analysis._pdf_page_count", return_value=10), patch(
            "estimator.pdf_analysis._extract_rooms_with_ai",
            return_value=extracted_rooms,
        ), patch("estimator.pdf_analysis._plan_page_text", return_value=plan_text):
            with self.assertRaisesMessage(ValueError, "部屋抽出数が不足"):
                analyze_wallpaper_pdf("dummy.pdf", {"page_1f_plan": "5", "page_2f_plan": "6"})

    def test_project_create_does_not_fall_back_when_pdf_analysis_has_unexpected_error(self):
        with patch("estimator.views.analyze_wallpaper_pdf", side_effect=RuntimeError("boom")):
            response = self.client.post(
                reverse("project_create"),
                {
                    "name": "PDFエラー案件",
                    "client_name": "橘工務店",
                    "drawing_pdf": SimpleUploadedFile("dummy.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
                    "wallpaper_roll_width_m": "0.92",
                    "wallpaper_roll_length_m": "50",
                    "loss_rate_percent": "8",
                    "unit_price_per_roll": "11800",
                },
            )

        project = Project.objects.get(name="PDFエラー案件")
        self.assertRedirects(response, reverse("project_detail", args=[project.pk]))
        self.assertEqual(project.rooms.count(), 0)
        response_messages = list(get_messages(response.wsgi_request))
        self.assertEqual([message.tags for message in response_messages], ["error", "error"])
        self.assertIn("PDF自動読取中に予期しないエラーが発生しました。", str(response_messages[0]))
        self.assertEqual(str(response_messages[1]), "積算が作成できませんでした。")

    def test_project_recalculate_reads_pdf_again(self):
        project = Project.objects.create(name="再計算案件", drawing_pdf="drawings/dummy.pdf")
        Room.objects.create(
            project=project,
            name="古い部屋",
            perimeter_m=Decimal("10"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("5"),
        )
        analysis = PdfAnalysisResult(
            rooms=[
                AnalyzedRoom("新しいLDK", Decimal("18"), Decimal("2.4"), Decimal("0"), Decimal("20"), "PDF再読取")
            ],
            memo="PDF AI読取",
        )

        with patch("estimator.views.analyze_wallpaper_pdf", return_value=analysis):
            response = self.client.post(reverse("project_recalculate", args=[project.pk]))

        self.assertRedirects(response, reverse("project_detail", args=[project.pk]))
        self.assertEqual(list(project.rooms.values_list("name", flat=True)), ["新しいLDK"])

# Create your tests here.
