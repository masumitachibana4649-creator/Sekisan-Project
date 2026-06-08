from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .admin import ProjectAdmin, WallpaperAdmin
from .models import ROOM_TOTAL_METHOD, Project, Room, Wallpaper
from .pdf_analysis import (
    AnalyzedRoom,
    PdfAnalysisResult,
    RoomCandidate,
    analyze_wallpaper_pdf,
    _analysis_page_numbers,
    _analysis_prompt,
    _detect_table_pages,
    _expected_room_counts,
    _parse_ai_analysis_response,
    _validate_room_candidates,
    _room_table_candidates_from_text,
    _normalize_room_table_candidates,
    _room_candidate_page_text,
    _sample_plan_rooms,
    _write_selected_pages_pdf,
)
from .views import _create_rooms_from_analysis
from .views import _create_room_from_analysis
from .views import _project_table_pages_from_memo
from .templatetags.estimate_extras import room_note, sentence_breaks


class WallpaperEstimateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="user", password="password")

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

    @override_settings(SUPABASE_URL="", SUPABASE_SECRET_KEY="", SUPABASE_BUCKET="pdfs")
    def test_project_create_view_reads_pdf_and_redirects(self):
        self.client.force_login(self.user)
        analysis = PdfAnalysisResult(
            rooms=[
                AnalyzedRoom(
                    "LDK",
                    Decimal("18"),
                    Decimal("2.4"),
                    Decimal("4.2"),
                    Decimal("20"),
                    "推定開口: 展開図から推定",
                    {
                        "face_1": {"width_m": Decimal("4.17"), "surface_area_m2": Decimal("2.40"), "opening_area_m2": Decimal("1.00")},
                        "face_2": {"width_m": Decimal("4.58"), "surface_area_m2": Decimal("2.40"), "opening_area_m2": Decimal("1.20")},
                        "face_3": {"width_m": Decimal("5.00"), "surface_area_m2": Decimal("2.40"), "opening_area_m2": Decimal("0.80")},
                        "face_4": {"width_m": Decimal("4.25"), "surface_area_m2": Decimal("2.40"), "opening_area_m2": Decimal("1.20")},
                    },
                )
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
        self.assertEqual(project.uploaded_by, self.user)
        self.assertEqual(project.rooms.count(), 1)
        room = project.rooms.get()
        self.assertEqual(room.east_surface_area_m2, Decimal("10.01"))
        self.assertEqual(room.west_surface_area_m2, Decimal("10.99"))
        self.assertEqual(room.south_opening_area_m2, Decimal("0.80"))
        self.assertEqual(room.opening_area_m2, Decimal("4.20"))
        self.assertEqual(project.total_rolls, 2)

    def test_project_create_requires_login(self):
        response = self.client.get(reverse("project_create"))

        self.assertRedirects(response, f"{reverse('login')}?next={reverse('project_create')}")

    def test_dashboard_hides_history_and_start_button_before_login(self):
        Project.objects.create(name="非表示案件", uploaded_by=self.user)

        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, "非表示案件")
        self.assertNotContains(response, "積算を開始")
        self.assertContains(response, "ログインすると積算履歴を確認できます")

    def test_dashboard_shows_only_current_user_projects(self):
        other_user = User.objects.create_user(username="other-user", password="password")
        Project.objects.create(name="自分の案件", uploaded_by=self.user)
        Project.objects.create(name="他人の案件", uploaded_by=other_user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "自分の案件")
        self.assertContains(response, self.user.username)
        self.assertNotContains(response, "他人の案件")

    def test_dashboard_distinguishes_complete_and_failed_estimates(self):
        completed = Project.objects.create(name="積算完了案件", uploaded_by=self.user)
        Room.objects.create(
            project=completed,
            name="LDK",
            perimeter_m=Decimal("18"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("20"),
        )
        failed = Project.objects.create(name="積算失敗案件", uploaded_by=self.user, drawing_pdf="drawings/dummy.pdf")
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "積算完了")
        self.assertContains(response, "必要面積")
        self.assertContains(response, "見積金額")
        self.assertContains(response, "要再計算")
        self.assertContains(response, "積算が作成できませんでした。PDF読取から再計算できます。")
        self.assertNotContains(response, f'action="{reverse("project_recalculate", args=[failed.pk])}"')
        self.assertContains(response, f'href="{reverse("project_detail", args=[failed.pk])}"')
        self.assertContains(response, "結果を見る")

    def test_signup_creates_general_user_and_logs_in(self):
        response = self.client.post(
            reverse("signup"),
            {
                "username": "new-user",
                "password1": "strong-password-123",
                "password2": "strong-password-123",
            },
        )

        user = User.objects.get(username="new-user")
        self.assertRedirects(response, reverse("dashboard"))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_staff_login_redirects_to_admin(self):
        staff = User.objects.create_user(username="staff", password="password", is_staff=True)

        response = self.client.post(reverse("login"), {"username": staff.username, "password": "password"})

        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)

    def test_project_views_reject_other_users(self):
        owner = User.objects.create_user(username="owner-user", password="password")
        project = Project.objects.create(name="他人の積算", uploaded_by=owner, drawing_pdf="drawings/dummy.pdf")
        self.client.force_login(self.user)

        checks = [
            self.client.get(reverse("project_detail", args=[project.pk])),
            self.client.post(reverse("project_save_wallpapers", args=[project.pk])),
            self.client.post(reverse("project_recalculate", args=[project.pk])),
            self.client.get(reverse("project_pdf", args=[project.pk])),
            self.client.get(reverse("project_csv", args=[project.pk])),
        ]

        self.assertTrue(all(response.status_code == 403 for response in checks))

    def test_project_pdf_view_serves_uploaded_pdf(self):
        self.client.force_login(self.user)
        project = Project.objects.create(
            name="PDF表示テスト",
            uploaded_by=self.user,
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

    def test_project_detail_shows_recalculate_button_when_estimate_failed(self):
        self.client.force_login(self.user)
        project = Project.objects.create(
            name="積算失敗案件",
            uploaded_by=self.user,
            drawing_pdf="drawings/dummy.pdf",
        )

        response = self.client.get(reverse("project_detail", args=[project.pk]))

        self.assertContains(response, "積算が作成できませんでした。")
        self.assertContains(response, "再計算")
        self.assertContains(response, f'action="{reverse("project_recalculate", args=[project.pk])}"')
        self.assertContains(response, 'form="project-recalculate-form"')
        self.assertNotContains(response, 'href="' + reverse("project_detail", args=[project.pk]) + '?edit=1"')

    def test_project_detail_action_buttons_follow_estimate_state(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="積算完了案件", uploaded_by=self.user, drawing_pdf="drawings/dummy.pdf")
        Room.objects.create(
            project=project,
            name="トイレ",
            perimeter_m=Decimal("6"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("2"),
            note="根拠: 1F平面図",
        )

        response = self.client.get(reverse("project_detail", args=[project.pk]))
        self.assertContains(response, "編集")
        self.assertNotContains(response, ">再計算</button>")

        response = self.client.get(f'{reverse("project_detail", args=[project.pk])}?edit=1')
        self.assertContains(response, "編集内容を反映")
        self.assertContains(response, "表示に戻る")
        self.assertContains(response, ">再計算</button>")
        self.assertNotContains(response, 'href="' + reverse("project_detail", args=[project.pk]) + '?edit=1"')

    def test_room_columns_split_number_floor_and_name_in_detail_and_csv(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="階表示案件", uploaded_by=self.user)
        Room.objects.create(
            project=project,
            name="トイレ",
            perimeter_m=Decimal("6"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("2"),
            note="根拠: 1F平面図",
        )

        response = self.client.get(reverse("project_detail", args=[project.pk]))
        self.assertContains(response, "<span>No</span><span>階</span><span>部屋名</span>", html=True)
        self.assertContains(response, '<strong class="room-no-cell">1</strong>', html=True)
        self.assertContains(response, '<span class="room-floor-cell">1F</span>', html=True)
        self.assertContains(response, '<strong class="room-name-cell">トイレ</strong>', html=True)

        response = self.client.get(reverse("project_csv", args=[project.pk]))
        self.assertContains(response, "No.,階,部屋名")
        self.assertContains(response, "1,1F,トイレ")

    def test_room_display_name_normalizes_floor_without_duplicates(self):
        project = Project.objects.create(name="階重複案件", uploaded_by=self.user)
        room = Room.objects.create(
            project=project,
            name="トイレ 1F",
            perimeter_m=Decimal("6"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("2"),
        )
        atrium = Room.objects.create(
            project=project,
            name="吹抜",
            perimeter_m=Decimal("8"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("4"),
            note="根拠: 2F平面図",
        )

        self.assertEqual(room.display_name, "1F トイレ")
        self.assertEqual(room.display_floor_label, "1F")
        self.assertEqual(room.display_room_name, "トイレ")
        self.assertEqual(atrium.display_name, "吹抜")
        self.assertEqual(atrium.display_floor_label, "")
        self.assertEqual(atrium.display_room_name, "吹抜")

    def test_estimated_openings_are_blue_in_display_mode(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="推定表示案件", uploaded_by=self.user)
        Room.objects.create(
            project=project,
            name="LDK",
            perimeter_m=Decimal("18"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("1"),
            ceiling_area_m2=Decimal("20"),
            east_surface_area_m2=Decimal("10"),
            east_opening_area_m2=Decimal("1"),
            note="推定開口: 展開図から推定",
        )

        response = self.client.get(reverse("project_detail", args=[project.pk]))

        self.assertContains(response, 'class="room-measure-cell estimated-value">1')

    @override_settings(
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY="sb_secret_test",
        SUPABASE_BUCKET="pdfs",
    )
    def test_project_create_uploads_pdf_to_supabase_storage(self):
        self.client.force_login(self.user)
        analysis = PdfAnalysisResult(
            rooms=[],
            memo="PDF AI読取",
        )
        with patch("estimator.views.uuid.uuid4", return_value="550e8400-e29b-41d4-a716-446655440000"), patch(
            "estimator.views.storage.upload_pdf"
        ) as upload_pdf, patch("estimator.views.analyze_wallpaper_pdf", return_value=analysis), patch(
            "estimator.views.storage.download_pdf", return_value=b"%PDF-1.4\n%%EOF"
        ), patch(
            "estimator.storage.delete_pdf"
        ):
            response = self.client.post(
                reverse("project_create"),
                {
                    "name": "Supabase保存案件",
                    "client_name": "橘工務店",
                    "drawing_pdf": SimpleUploadedFile("drawing.pdf", b"%PDF-1.4\n%%EOF", content_type="application/pdf"),
                },
            )

        project = Project.objects.get(name="Supabase保存案件")
        self.assertRedirects(response, reverse("project_detail", args=[project.pk]))
        self.assertEqual(project.drawing_pdf_storage_path, f"{self.user.pk}/550e8400-e29b-41d4-a716-446655440000.pdf")
        self.assertFalse(project.drawing_pdf)
        upload_pdf.assert_called_once()

    @override_settings(
        SUPABASE_URL="https://example.supabase.co",
        SUPABASE_SECRET_KEY="sb_secret_test",
        SUPABASE_BUCKET="pdfs",
    )
    def test_project_pdf_view_redirects_to_signed_url_for_owner(self):
        owner = User.objects.create_user(username="owner", password="password")
        project = Project.objects.create(
            name="署名URL案件",
            drawing_pdf_storage_path="1/drawing.pdf",
            drawing_pdf_original_name="drawing.pdf",
            uploaded_by=owner,
        )
        self.client.force_login(owner)

        with patch("estimator.views.storage.create_signed_url", return_value="https://example.supabase.co/signed"), patch(
            "estimator.storage.delete_pdf"
        ):
            response = self.client.get(reverse("project_pdf", args=[project.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://example.supabase.co/signed")

    def test_project_pdf_view_rejects_non_owner_for_storage_pdf(self):
        owner = User.objects.create_user(username="owner", password="password")
        other = User.objects.create_user(username="other", password="password")
        project = Project.objects.create(
            name="権限確認案件",
            drawing_pdf_storage_path="1/drawing.pdf",
            uploaded_by=owner,
        )
        self.client.force_login(other)

        response = self.client.get(reverse("project_pdf", args=[project.pk]))

        self.assertEqual(response.status_code, 403)

    def test_result_text_filters_insert_sentence_breaks_and_format_confidence(self):
        self.assertEqual(str(sentence_breaks("一文目です。二文目です。")), "一文目です。<br>二文目です。<br>")
        self.assertEqual(str(sentence_breaks("メモ:施工注意です。")), "メモ<br><br>施工注意です。<br>")
        self.assertEqual(str(sentence_breaks("入力メモです。\nPDF読取説明です。")), "入力メモです。<br><br>PDF読取説明です。<br>")
        self.assertEqual(str(room_note("根拠: 図面。AI信頼度: 0.82")), "根拠: 図面。<br>AI信頼度: 82%")

    def test_project_detail_shows_entered_memo_without_fixed_title(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="メモ表示テスト", memo="入力した内容です。\nPDF読取説明です。", uploaded_by=self.user)

        response = self.client.get(reverse("project_detail", args=[project.pk]))

        self.assertContains(response, "入力した内容です。<br><br>PDF読取説明です。<br>", html=False)
        self.assertNotContains(response, "<strong>メモ</strong>", html=False)

    def test_project_detail_room_detail_labels_include_units(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="単位表示テスト", uploaded_by=self.user)
        Room.objects.create(
            project=project,
            name="LDK",
            perimeter_m=Decimal("18"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("20"),
        )

        response = self.client.get(reverse("project_detail", args=[project.pk]))

        self.assertContains(response, "面積(m2)")
        self.assertContains(response, "開口部(m2)")

    def test_project_admin_total_columns_have_japanese_labels(self):
        project_admin = ProjectAdmin(Project, AdminSite())

        self.assertEqual(project_admin.total_rolls_display.short_description, "ロール本数")
        self.assertEqual(project_admin.total_cost_display.short_description, "概算金額")

    def test_wallpaper_admin_numeric_columns_have_lowercase_meter_labels(self):
        wallpaper_admin = WallpaperAdmin(Wallpaper, AdminSite())

        self.assertEqual(wallpaper_admin.roll_width_display.short_description, "ロール幅(m)")
        self.assertEqual(wallpaper_admin.roll_length_display.short_description, "ロール長さ(m)")

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

    def test_excluded_room_stays_in_detail_but_is_removed_from_summary(self):
        self.client.force_login(self.user)
        project = Project.objects.create(name="対象外テスト", uploaded_by=self.user)
        Room.objects.create(
            project=project,
            name="1F LDK",
            perimeter_m=Decimal("20"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("0"),
        )
        Room.objects.create(
            project=project,
            name="1F 収納",
            excluded_from_summary=True,
            perimeter_m=Decimal("20"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("0"),
        )

        self.assertEqual(project.rooms.count(), 2)
        self.assertEqual(project.total_area.quantize(Decimal("0.01")), Decimal("51.84"))
        self.assertEqual(project.total_rolls, 2)

        response = self.client.get(reverse("project_detail", args=[project.pk]))
        self.assertContains(response, "収納")
        self.assertContains(response, "is-summary-excluded")

        response = self.client.get(reverse("project_csv", args=[project.pk]))
        self.assertContains(response, "1F,収納")
        self.assertContains(response, "対象外")

    def test_pdf_missing_room_candidates_are_added_as_red_zero_rooms(self):
        self.client.force_login(self.user)
        Wallpaper.ensure_defaults()
        project = Project.objects.create(name="不足候補案件", drawing_pdf="drawings/dummy.pdf", uploaded_by=self.user)
        analysis = PdfAnalysisResult(
            rooms=[
                AnalyzedRoom("1F LDK", Decimal("18"), Decimal("2.4"), Decimal("0"), Decimal("20"), "PDF読取"),
                AnalyzedRoom("2F 洋室", Decimal("12"), Decimal("2.4"), Decimal("0"), Decimal("9"), "PDF読取"),
            ],
            memo="PDF AI読取",
            missing_rooms=["1F 収納", "2F トイレ"],
        )

        with patch("estimator.views.analyze_wallpaper_pdf", return_value=analysis):
            response = self.client.post(reverse("project_recalculate", args=[project.pk]))

        self.assertRedirects(response, reverse("project_detail", args=[project.pk]))
        rooms = list(project.rooms.order_by("id"))
        self.assertEqual([room.name for room in rooms], ["1F LDK", "1F 収納", "2F 洋室", "2F トイレ"])
        self.assertEqual(rooms[1].source_type, "ai_missing")
        self.assertEqual(rooms[1].total_area, Decimal("0"))
        response = self.client.get(reverse("project_detail", args=[project.pk]))
        self.assertContains(response, "source-missing-room")
        self.assertContains(response, "赤文字は抽出に失敗した部屋なので編集画面で面積、開口部を入力してください")

    def test_missing_rooms_from_table_candidates_keep_ceiling_area(self):
        project = Project.objects.create(name="表候補面積反映")
        Wallpaper.ensure_defaults()

        _create_rooms_from_analysis(
            project,
            [AnalyzedRoom("1F LDK", Decimal("10"), Decimal("2.4"), Decimal("0"), Decimal("20"), "根拠: 1F平面図")],
            missing_rooms=[],
            room_candidates=[
                RoomCandidate("1F", "LDK", Decimal("20.00"), "居室区画面積表", 22),
                RoomCandidate("1F", "収納 一式", Decimal("3.72"), "居室区画面積表", 22),
                RoomCandidate("2F", "CL 1", Decimal("0.93"), "居室区画面積表", 22),
                RoomCandidate("2F", "CL 2", Decimal("0.93"), "居室区画面積表", 22),
            ],
        )

        rooms = {room.name: room for room in project.rooms.all()}
        self.assertEqual(project.rooms.count(), 4)
        self.assertEqual(rooms["1F 収納 一式"].source_type, "ai_missing")
        self.assertEqual(rooms["1F 収納 一式"].ceiling_area_m2, Decimal("3.72"))
        self.assertEqual(rooms["1F 収納 一式"].ceiling_surface_area_m2, Decimal("3.72"))
        self.assertEqual(rooms["2F CL 1"].ceiling_area_m2, Decimal("0.93"))
        self.assertEqual(rooms["2F CL 2"].ceiling_area_m2, Decimal("0.93"))
        self.assertIn("表ページから天井面積 3.72m2 を反映", rooms["1F 収納 一式"].note)

    def test_pdf_missing_room_candidates_use_selected_surface_wallpapers(self):
        self.client.force_login(self.user)
        Wallpaper.ensure_defaults()
        accent = Wallpaper.objects.create(
            display_order="002",
            number="002",
            name="アクセント",
            roll_width_m=Decimal("0.92"),
            roll_length_m=Decimal("10"),
            loss_rate_percent=Decimal("8"),
            unit_price_per_roll=5000,
        )
        from .views import _create_rooms_from_analysis

        project = Project.objects.create(name="不足候補壁紙案件", uploaded_by=self.user)
        standard = Wallpaper.objects.get(number="001")
        _create_rooms_from_analysis(
            project,
            [AnalyzedRoom("1F LDK", Decimal("18"), Decimal("2.4"), Decimal("0"), Decimal("20"), "PDF読取")],
            default_wallpaper=standard,
            surface_wallpapers={
                "east": accent,
                "west": standard,
                "south": standard,
                "north": standard,
                "ceiling": accent,
            },
            missing_rooms=["1F 収納"],
        )

        missing_room = project.rooms.get(name="1F 収納")
        self.assertEqual(missing_room.source_type, "ai_missing")
        self.assertEqual(missing_room.east_wallpaper_no, "002")
        self.assertEqual(missing_room.east_wallpaper_name, "アクセント")
        self.assertEqual(missing_room.west_wallpaper_no, "001")
        self.assertEqual(missing_room.ceiling_wallpaper_no, "002")

    def test_manual_room_add_post_creates_green_zero_room(self):
        self.client.force_login(self.user)
        Wallpaper.ensure_defaults()
        project = Project.objects.create(name="手動追加案件", uploaded_by=self.user)
        Room.objects.create(
            project=project,
            name="1F LDK",
            perimeter_m=Decimal("18"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("20"),
        )

        response = self.client.post(
            reverse("project_save_wallpapers", args=[project.pk]),
            {
                "apply_changes": "1",
                "new_room_floor": ["2F"],
                "new_room_name": ["納戸"],
                "new_room_excluded_from_summary": ["1"],
                "new_room_east_surface_area_m2": ["3.50"],
                "new_room_west_surface_area_m2": ["4.50"],
                "new_room_south_surface_area_m2": ["5.50"],
                "new_room_north_surface_area_m2": ["6.50"],
                "new_room_ceiling_surface_area_m2": ["7.50"],
                "new_room_east_opening_area_m2": ["0.10"],
                "new_room_west_opening_area_m2": ["0.20"],
                "new_room_south_opening_area_m2": ["0.30"],
                "new_room_north_opening_area_m2": ["0.40"],
            },
        )

        self.assertRedirects(response, f"{reverse('project_detail', args=[project.pk])}?edit=1")
        added = project.rooms.get(name="2F 納戸")
        self.assertEqual(added.source_type, "manual")
        self.assertTrue(added.excluded_from_summary)
        self.assertEqual(added.east_surface_area_m2, Decimal("3.50"))
        self.assertEqual(added.ceiling_surface_area_m2, Decimal("7.50"))
        self.assertEqual(added.opening_area_m2, Decimal("1.00"))
        self.assertEqual(added.total_area, Decimal("0"))
        response = self.client.get(f"{reverse('project_detail', args=[project.pk])}?edit=1")
        self.assertContains(response, "source-manual-room")
        self.assertContains(response, "緑文字は追加した部屋なので編集画面で面積、開口部を入力してください")

    def test_project_save_wallpapers_creates_revision_with_selected_method(self):
        self.client.force_login(self.user)
        Wallpaper.ensure_defaults()
        project = Project.objects.create(name="保存元", uploaded_by=self.user)
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
                f"room_{room.pk}_east_surface_area_m2": "12.00",
                f"room_{room.pk}_west_surface_area_m2": "10.00",
                f"room_{room.pk}_south_surface_area_m2": "9.00",
                f"room_{room.pk}_north_surface_area_m2": "8.00",
                f"room_{room.pk}_ceiling_surface_area_m2": "20.00",
                f"room_{room.pk}_east_opening_area_m2": "1.00",
                f"room_{room.pk}_west_opening_area_m2": "0.50",
                f"room_{room.pk}_south_opening_area_m2": "0.25",
                f"room_{room.pk}_north_opening_area_m2": "0.00",
            },
        )

        revision = Project.objects.get(name="保存元修正")
        revision_room = revision.rooms.get()
        self.assertRedirects(response, reverse("project_detail", args=[revision.pk]))
        self.assertEqual(revision.adopted_estimate_method, ROOM_TOTAL_METHOD)
        self.assertEqual(revision_room.east_wallpaper_no, "000")
        self.assertEqual(revision_room.east_surface_area_m2, Decimal("12.00"))
        self.assertEqual(revision_room.opening_area_m2, Decimal("1.75"))
        self.assertEqual(revision_room.ceiling_area_m2, Decimal("20.00"))
        self.assertEqual(project.rooms.get().east_wallpaper_no, "001")

    def test_project_apply_changes_updates_same_project_and_returns_to_edit_mode(self):
        self.client.force_login(self.user)
        Wallpaper.ensure_defaults()
        project = Project.objects.create(name="反映元", uploaded_by=self.user)
        room = Room.objects.create(
            project=project,
            name="LDK",
            perimeter_m=Decimal("18"),
            height_m=Decimal("2.4"),
            opening_area_m2=Decimal("0"),
            ceiling_area_m2=Decimal("20"),
        )
        original_rolls = project.total_rolls

        response = self.client.post(
            reverse("project_save_wallpapers", args=[project.pk]),
            {
                "apply_changes": "1",
                "save_project_name": "別案件名は使わない",
                "adopted_estimate_method": ROOM_TOTAL_METHOD,
                f"room_{room.pk}_east_wallpaper_no": "001",
                f"room_{room.pk}_west_wallpaper_no": "001",
                f"room_{room.pk}_south_wallpaper_no": "001",
                f"room_{room.pk}_north_wallpaper_no": "001",
                f"room_{room.pk}_ceiling_wallpaper_no": "001",
                f"room_{room.pk}_east_surface_area_m2": "80.00",
                f"room_{room.pk}_west_surface_area_m2": "10.00",
                f"room_{room.pk}_south_surface_area_m2": "9.00",
                f"room_{room.pk}_north_surface_area_m2": "8.00",
                f"room_{room.pk}_ceiling_surface_area_m2": "20.00",
                f"room_{room.pk}_east_opening_area_m2": "1.00",
                f"room_{room.pk}_west_opening_area_m2": "0.50",
                f"room_{room.pk}_south_opening_area_m2": "0.25",
                f"room_{room.pk}_north_opening_area_m2": "0.00",
            },
        )

        project.refresh_from_db()
        room.refresh_from_db()
        self.assertRedirects(response, f"{reverse('project_detail', args=[project.pk])}?edit=1", fetch_redirect_response=False)
        self.assertEqual(Project.objects.count(), 1)
        self.assertEqual(project.adopted_estimate_method, ROOM_TOTAL_METHOD)
        self.assertEqual(room.east_wallpaper_no, "001")
        self.assertEqual(room.east_surface_area_m2, Decimal("80.00"))
        self.assertEqual(room.opening_area_m2, Decimal("1.75"))
        self.assertGreater(project.total_rolls, original_rolls)

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
            "page_development_start": 8,
            "page_development_end": 8,
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
                    "wall_surfaces": {
                        "face_1": {"width_m": 3.375, "surface_area_m2": 8.1, "opening_area_m2": 1.0},
                        "face_2": {"width_m": 3.416, "surface_area_m2": 8.2, "opening_area_m2": 0.5},
                        "face_3": {"width_m": 3.458, "surface_area_m2": 8.3, "opening_area_m2": 1.25},
                        "face_4": {"width_m": 3.5, "surface_area_m2": 8.4, "opening_area_m2": 0.5},
                    },
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
        self.assertEqual(room.wall_surfaces["east"]["surface_area_m2"], Decimal("8.10"))
        self.assertEqual(room.wall_surfaces["south"]["opening_area_m2"], Decimal("1.25"))
        self.assertIn("根拠: 2F平面図", room.note)
        self.assertIn("AI信頼度: 0.82", room.note)
        self.assertEqual(result["warnings"], ["開口部は一部推定"])

    def test_expected_room_counts_includes_legacy_house_labels_without_double_counting(self):
        plan_text = "和室 和室 台所 食堂 洗面所 脱衣 便所 押入 物入 納戸 子供室 主寝室 ホール"

        counts = _expected_room_counts(plan_text)

        self.assertEqual(counts["和室"], 2)
        self.assertEqual(counts["台所"], 1)
        self.assertEqual(counts["食堂"], 1)
        self.assertEqual(counts["洗面所"], 2)
        self.assertEqual(counts["トイレ"], 1)
        self.assertEqual(counts["収納"], 3)
        self.assertEqual(counts["洋室"], 2)
        self.assertEqual(counts["廊下"], 1)

    def test_room_candidate_text_includes_ceiling_plan_pages(self):
        class FakePage:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                return self.text

        class FakeReader:
            pages = [
                FakePage("1F平面図 LDK"),
                FakePage("1F天井伏図 洗面所"),
                FakePage("展開図"),
            ]

            def __init__(self, _path):
                pass

        with patch("pypdf.PdfReader", FakeReader):
            text = _room_candidate_page_text(
                "dummy.pdf",
                {"page_1f_plan": 1, "page_1f_ceiling_plan": 2, "page_development_start": 3},
            )

        self.assertIn("LDK", text)
        self.assertIn("洗面所", text)
        self.assertNotIn("展開図", text)

    def test_room_candidate_text_deduplicates_same_page_numbers(self):
        class FakePage:
            def __init__(self, text):
                self.text = text
                self.read_count = 0

            def extract_text(self):
                self.read_count += 1
                return self.text

        shared_page = FakePage("1F平面図 LDK")

        class FakeReader:
            pages = [shared_page]

            def __init__(self, _path):
                pass

        with patch("pypdf.PdfReader", FakeReader):
            text = _room_candidate_page_text(
                "dummy.pdf",
                {"page_1f_plan": 1, "page_1f_ceiling_plan": 1},
            )

        self.assertEqual(text.count("LDK"), 1)
        self.assertEqual(shared_page.read_count, 1)

    def test_analysis_prompt_keeps_rooms_when_development_drawings_are_incomplete(self):
        prompt = _analysis_prompt(
            {
                "page_1f_plan": 5,
                "page_2f_plan": 5,
                "page_development_start": 16,
                "page_development_end": 22,
            },
            expected_counts={"和室": 2, "収納": 3, "台所": 1},
            table_pages=[("居室区画面積表", 22)],
            room_candidates=[
                RoomCandidate("1F", "LDK", Decimal("33.12"), "居室区画面積表", 22),
                RoomCandidate("2F", "主寝室", Decimal("12.42"), "居室区画面積表", 22),
            ],
        )

        self.assertIn("展開図が読み取りやすい和室A/Bなど一部の部屋だけで回答を終えず", prompt)
        self.assertIn("展開図未確認のため平面図から推定", prompt)
        self.assertIn("居室区画面積表: 22ページ", prompt)
        self.assertIn("1F LDK: 33.12m2", prompt)
        self.assertIn("表ページ候補を優先", prompt)
        self.assertIn("和室: 約2件", prompt)

    def test_living_area_table_candidates_extract_floor_room_and_area(self):
        text = """
        居室区画面積表
        LDK 33.122
        階段室 1.656
        ﾊﾟﾝﾄﾘｰ 1.219
        UB 3.312
        PS 0.229
        廊下 7.451
        主寝室 12.420
        洋室2 8.797
        洋室1 8.797
        ﾌｧﾐﾘｰｸﾛｰｾﾞｯﾄ 6.625
        ﾄｲﾚ 1.449
        CL 0.931
        CL 0.931
        凡例
        """

        candidates = [
            candidate
            for candidate in _room_table_candidates_from_text(text, "居室区画面積表", 22)
            if candidate.name != "UB"
        ]

        self.assertEqual(len(candidates), 12)
        self.assertEqual(candidates[0], RoomCandidate("1F", "LDK", Decimal("33.12"), "居室区画面積表", 22))
        self.assertEqual(candidates[3].name, "PS")
        self.assertEqual(candidates[4].floor, "2F")
        self.assertEqual(candidates[7].name, "洋室1")
        self.assertEqual(candidates[8].name, "ファミリークローゼット")
        self.assertEqual([candidate.name for candidate in candidates[-2:]], ["CL", "CL"])

    def test_room_table_candidates_aggregate_storage_and_keep_duplicate_cl(self):
        candidates = _normalize_room_table_candidates([
            RoomCandidate("1F", "収納", Decimal("2.48"), "居室区画面積表", 22),
            RoomCandidate("1F", "収納", Decimal("0.83"), "居室区画面積表", 22),
            RoomCandidate("1F", "収納", Decimal("0.41"), "居室区画面積表", 22),
            RoomCandidate("2F", "CL", Decimal("0.93"), "居室区画面積表", 22),
            RoomCandidate("2F", "CL", Decimal("0.93"), "居室区画面積表", 22),
        ])

        self.assertEqual(
            candidates,
            [
                RoomCandidate("2F", "CL 1", Decimal("0.93"), "居室区画面積表", 22),
                RoomCandidate("2F", "CL 2", Decimal("0.93"), "居室区画面積表", 22),
                RoomCandidate("1F", "収納 一式", Decimal("3.72"), "居室区画面積表", 22),
            ],
        )

    def test_analysis_page_numbers_include_detected_table_pages(self):
        pages = _analysis_page_numbers(
            {
                "page_1f_plan": 9,
                "page_2f_plan": 10,
                "page_development_start": 11,
                "page_development_end": 12,
                "page_1f_ceiling_plan": 6,
            },
            additional_pages=[14, 22, 6],
        )

        self.assertEqual(pages, [9, 10, 6, 11, 12, 14, 22])

    def test_detect_table_pages_includes_finish_and_fixture_tables(self):
        class FakePage:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                return self.text

        class FakeReader:
            pages = [
                FakePage("室内仕上表 壁 クロス 天井"),
                FakePage("内部仕上表 壁 天井 仕上"),
                FakePage("建具表 開口 寸法"),
                FakePage("床面積表 単位 ㎡"),
            ]

            def __init__(self, _path):
                pass

        with patch("pypdf.PdfReader", FakeReader):
            pages = _detect_table_pages("dummy.pdf")

        self.assertEqual(pages, [("室内仕上表", 1), ("内部仕上表", 2), ("建具表", 3)])

    def test_detect_table_pages_includes_garbled_finish_and_fixture_tables_without_ai(self):
        class FakePage:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                return self.text

        class FakeReader:
            pages = [
                FakePage("έΠΧϧ൘ ̥ɾ̗ Լ԰ ্ද ̍֊চ໘ੵ ̎֊চ໘ੵ Ԇচ໘ੵ"),
                FakePage("਺ྔ ࣜ ঢ়ɹੇ๏ ੇ๏ ࢠ ෺ ߟ ਺ྔ ࣜ ঢ়ɹੇ๏"),
            ]

            def __init__(self, _path):
                pass

        with patch("pypdf.PdfReader", FakeReader):
            pages = _detect_table_pages("dummy.pdf")

        self.assertEqual(pages, [("室内仕上表", 1), ("建具表", 2)])

    @override_settings(OPENAI_API_KEY="test-key", OPENAI_VISUAL_TABLE_PAGE_DETECTION="true")
    def test_detect_table_pages_uses_visual_ai_when_text_is_garbled(self):
        class FakePage:
            def extract_text(self):
                return "4 & , * ɹɹ   "

        class FakeReader:
            pages = [FakePage(), FakePage(), FakePage()]

            def __init__(self, _path):
                pass

        class FakeFiles:
            def create(self, file, purpose):
                return SimpleNamespace(id="file-1")

            def delete(self, file_id):
                return None

        class FakeResponses:
            def create(self, **kwargs):
                return SimpleNamespace(output_text=json.dumps({
                    "table_pages": [
                        {"label": "床面積表", "page": 2, "confidence": 0.91},
                        {"label": "建具表", "page": 3, "confidence": 0.81},
                    ]
                }))

        class FakeOpenAI:
            def __init__(self, api_key):
                self.files = FakeFiles()
                self.responses = FakeResponses()

        with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf_file, patch("pypdf.PdfReader", FakeReader), patch(
            "openai.OpenAI",
            FakeOpenAI,
        ):
            pages = _detect_table_pages(pdf_file.name)

        self.assertEqual(pages, [("床面積表", 2), ("建具表", 3)])

    def test_room_candidate_validation_respects_floor_for_same_room_name(self):
        rooms = [
            AnalyzedRoom("1F トイレ", Decimal("4"), Decimal("2.4"), Decimal("0"), Decimal("1.4"), "根拠: 1F平面図"),
        ]
        candidates = [
            RoomCandidate("1F", "トイレ", Decimal("1.45"), "居室区画面積表", 22),
            RoomCandidate("2F", "トイレ", Decimal("1.45"), "居室区画面積表", 22),
        ]

        warnings = _validate_room_candidates(rooms, [], candidates)

        self.assertIn("2F トイレ", warnings[0])
        self.assertNotIn("1F トイレ、", warnings[0])

    def test_room_candidate_validation_treats_same_floor_storage_bundle_as_displayed(self):
        rooms = [
            AnalyzedRoom("1F 収納 一式", Decimal("0"), Decimal("2.4"), Decimal("0"), Decimal("2.48"), "根拠: 1F 収納群"),
        ]
        candidates = [
            RoomCandidate("1F", "収納", Decimal("2.48"), "居室区画面積表", 22),
            RoomCandidate("1F", "収納", Decimal("0.83"), "居室区画面積表", 22),
            RoomCandidate("2F", "CL", Decimal("0.93"), "居室区画面積表", 22),
        ]

        warnings = _validate_room_candidates(rooms, [], candidates)

        self.assertNotIn("1F 収納", warnings[0])
        self.assertIn("2F CL", warnings[0])

    def test_create_room_from_analysis_clamps_ai_values_to_db_limits(self):
        project = Project.objects.create(name="AI値丸め")
        wallpaper = Wallpaper.objects.get(number="001")
        room = AnalyzedRoom(
            name="1F " + ("長い部屋名" * 30),
            perimeter_m=Decimal("123456789.12"),
            height_m=Decimal("12345.67"),
            opening_area_m2=Decimal("-5"),
            ceiling_area_m2=Decimal("123456789.12"),
            note="根拠: " + ("長い備考" * 80),
            wall_surfaces={
                "face_1": {"width_m": Decimal("999999.99"), "surface_area_m2": Decimal("999999.99"), "opening_area_m2": Decimal("999999.99")},
                "face_2": {"width_m": Decimal("0"), "surface_area_m2": Decimal("123456789.12"), "opening_area_m2": Decimal("-1")},
                "face_3": {"width_m": Decimal("0"), "surface_area_m2": Decimal("-1"), "opening_area_m2": Decimal("0")},
                "face_4": {"width_m": Decimal("0"), "surface_area_m2": Decimal("1"), "opening_area_m2": Decimal("0")},
            },
        )

        _create_room_from_analysis(project, room, {}, wallpaper)

        created = project.rooms.get()
        self.assertLessEqual(len(created.name), 80)
        self.assertLessEqual(len(created.note), 160)
        self.assertEqual(created.perimeter_m, Decimal("99999.99"))
        self.assertEqual(created.height_m, Decimal("999.99"))
        self.assertEqual(created.opening_area_m2, Decimal("99999.99"))
        self.assertEqual(created.ceiling_area_m2, Decimal("99999.99"))

    def test_project_table_pages_from_memo_reuses_detected_tables(self):
        memo = (
            "PDF AI読取: 1F平面図=5P、2F平面図=5P、"
            "室内仕上表=4P、建具表=10P、建具表=11P、建具表=10P。"
        )

        self.assertEqual(
            _project_table_pages_from_memo(memo),
            [("室内仕上表", 4), ("建具表", 10), ("建具表", 11)],
        )

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

    def test_analyze_wallpaper_pdf_warns_on_obviously_incomplete_room_extraction(self):
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
            "missing_rooms": ["1階 収納", "2階 トイレ", "2階 LDK"],
            "warnings": [],
        }))
        plan_text = "洋室 洋室 洋室 収納 収納 収納 収納 廊下 玄関 トイレ LDK 廊下 洗面所 トイレ 収納 浴室 バルコニー"

        with patch("estimator.pdf_analysis._pdf_page_count", return_value=10), patch(
            "estimator.pdf_analysis._extract_rooms_with_ai",
            return_value=extracted_rooms,
        ), patch("estimator.pdf_analysis._room_candidate_page_text", return_value=plan_text):
            result = analyze_wallpaper_pdf("dummy.pdf", {"page_1f_plan": "5", "page_2f_plan": "6"})

        self.assertEqual(len(result.rooms), 2)
        self.assertIn("部屋抽出数が不足", result.memo)
        self.assertIn("件数内訳: AI抽出=2件、抽出失敗追加=2件、表示合計=4件", result.memo)
        self.assertIn("AI抽出は2件、抽出失敗追加は2件、表示合計は4件", result.memo)
        self.assertIn("未抽出候補", result.memo)

    def test_analyze_wallpaper_pdf_warns_instead_of_failing_when_only_secondary_spaces_are_missing(self):
        extracted_rooms = _parse_ai_analysis_response(json.dumps({
            "rooms": [
                {
                    "name": "1階 LDK",
                    "perimeter_m": 20,
                    "height_m": 2.4,
                    "opening_area_m2": 0,
                    "ceiling_area_m2": 30,
                    "confidence": 0.9,
                    "evidence": "1F平面図",
                },
                {
                    "name": "1階 玄関",
                    "perimeter_m": 8,
                    "height_m": 2.4,
                    "opening_area_m2": 0,
                    "ceiling_area_m2": 4,
                    "confidence": 0.9,
                    "evidence": "1F平面図",
                },
                {
                    "name": "2階 洋室1",
                    "perimeter_m": 12,
                    "height_m": 2.4,
                    "opening_area_m2": 0,
                    "ceiling_area_m2": 9,
                    "confidence": 0.9,
                    "evidence": "2F平面図",
                },
                {
                    "name": "2階 洋室2",
                    "perimeter_m": 12,
                    "height_m": 2.4,
                    "opening_area_m2": 0,
                    "ceiling_area_m2": 9,
                    "confidence": 0.9,
                    "evidence": "2F平面図",
                },
            ],
            "warnings": [],
        }))
        plan_text = "LDK 洋室1 洋室2 収納 収納 収納 玄関"

        with patch("estimator.pdf_analysis._pdf_page_count", return_value=12), patch(
            "estimator.pdf_analysis._extract_rooms_with_ai",
            return_value=extracted_rooms,
        ), patch("estimator.pdf_analysis._room_candidate_page_text", return_value=plan_text):
            result = analyze_wallpaper_pdf("dummy.pdf", {"page_1f_plan": "9", "page_2f_plan": "10"})

        self.assertEqual(len(result.rooms), 4)
        self.assertIn("補助空間候補", result.memo)

    def test_selected_pages_pdf_contains_only_requested_unique_pages(self):
        from pypdf import PdfReader, PdfWriter

        source_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        source_file.close()
        selected_path = None
        try:
            writer = PdfWriter()
            for _index in range(5):
                writer.add_blank_page(width=72, height=72)
            with open(source_file.name, "wb") as pdf_file:
                writer.write(pdf_file)

            selected_path = _write_selected_pages_pdf(
                source_file.name,
                {
                    "page_1f_plan": 2,
                    "page_development_start": 3,
                    "page_development_end": 5,
                    "page_1f_ceiling_plan": 2,
                    "page_3f_plan": None,
                },
            )

            self.assertEqual(len(PdfReader(selected_path).pages), 4)
        finally:
            Path(source_file.name).unlink(missing_ok=True)
            if selected_path:
                Path(selected_path).unlink(missing_ok=True)

    @override_settings(SUPABASE_URL="", SUPABASE_SECRET_KEY="", SUPABASE_BUCKET="pdfs")
    def test_project_create_does_not_fall_back_when_pdf_analysis_has_unexpected_error(self):
        self.client.force_login(self.user)
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
        self.client.force_login(self.user)
        project = Project.objects.create(name="再計算案件", drawing_pdf="drawings/dummy.pdf", uploaded_by=self.user)
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
