"""壁紙積算アプリの画面表示、PDF読取、CSV出力を処理するビューを定義する。"""

import csv
import logging
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse

from .models import (
    ANALYSIS_STATUS_FAILED,
    ANALYSIS_STATUS_RUNNING,
    ANALYSIS_STATUS_SUCCEEDED,
    ESTIMATE_METHOD_CHOICES,
    ROOM_SOURCE_AI,
    ROOM_SOURCE_AI_MISSING,
    ROOM_SOURCE_MANUAL,
    ROOM_TOTAL_METHOD,
    SURFACE_FIELDS,
    WALLPAPER_TOTAL_METHOD,
    EstimateDefaultSettings,
    Project,
    Room,
    Wallpaper,
)
from .pdf_analysis import TABLE_PAGE_KEYWORDS, analyze_wallpaper_pdf
from . import storage

logger = logging.getLogger(__name__)


class StaffAwareLoginView(LoginView):
    """ログイン成功後の遷移先を制御するログインビュー。"""
    template_name = "registration/login.html"

    def get_success_url(self):
        """ログイン成功後の遷移先URLを返す。

        Returns:
            ログイン成功後の遷移先URL。
        """
        return self.get_redirect_url() or reverse("dashboard")


def signup(request):
    """一般ユーザーの新規登録を処理する。

    Args:
        request: HTTPリクエスト。

    Returns:
        登録フォーム画面または登録後のリダイレクトレスポンス。
    """
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_staff = False
            user.is_superuser = False
            user.save()
            login(request, user)
            messages.success(request, "ユーザー登録が完了しました。")
            return redirect("dashboard")
    else:
        form = UserCreationForm()

    return render(request, "estimator/signup.html", {"form": form})


def dashboard(request):
    """ログインユーザーの案件一覧を表示する。

    Args:
        request: HTTPリクエスト。

    Returns:
        案件一覧画面のHTTPレスポンス。
    """
    projects = Project.objects.none()
    if request.user.is_authenticated:
        projects = Project.objects.filter(uploaded_by=request.user).prefetch_related("rooms")[:8]
    latest = projects[0] if projects else None
    context = {
        "projects": projects,
        "latest": latest,
    }
    return render(request, "estimator/dashboard.html", context)


def about(request):
    """アプリ説明ページを表示する。

    Args:
        request: HTTPリクエスト。

    Returns:
        アプリ説明画面のHTTPレスポンス。
    """
    return render(request, "estimator/about.html")


@login_required
def project_create(request):
    """PDF図面アップロードから新規積算案件を作成する。

    Args:
        request: HTTPリクエスト。

    Returns:
        案件作成フォーム画面または作成後のリダイレクトレスポンス。
    """
    defaults = EstimateDefaultSettings.load()
    wallpapers = _selectable_wallpapers()
    default_wallpaper = defaults.default_wallpaper or Wallpaper.objects.get(number="001")
    if request.method == "POST":
        uploaded_pdf = request.FILES.get("drawing_pdf")
        if not uploaded_pdf:
            messages.error(request, "図面PDFを選択してください。")
            return redirect("project_create")
        try:
            _validate_drawing_pdf(uploaded_pdf)
            pdf_fields = _save_uploaded_drawing_pdf(uploaded_pdf, request.user)
        except ValidationError as exc:
            messages.error(request, exc.message)
            return redirect("project_create")
        except storage.SupabaseStorageError as exc:
            messages.error(request, f"図面PDFを保存できませんでした。理由: {exc}")
            return redirect("project_create")

        surface_wallpapers = _posted_surface_wallpapers(request.POST, default_wallpaper)
        first_wallpaper = surface_wallpapers["east"]
        try:
            project = Project.objects.create(
                name=request.POST.get("name") or "無題の積算",
                client_name=request.POST.get("client_name", ""),
                wallpaper_roll_width_m=first_wallpaper.roll_width_m,
                wallpaper_roll_length_m=first_wallpaper.roll_length_m,
                loss_rate_percent=first_wallpaper.loss_rate_percent,
                unit_price_per_roll=first_wallpaper.unit_price_per_roll,
                uploaded_by=request.user if request.user.is_authenticated else None,
                page_1f_plan=_page_value(request.POST.get("page_1f_plan"), "ー"),
                page_2f_plan=_page_value(request.POST.get("page_2f_plan"), "ー"),
                page_3f_plan=_page_value(request.POST.get("page_3f_plan"), "ー"),
                page_development_start=_page_value(request.POST.get("page_development_start"), "ー"),
                page_development_end=_page_value(request.POST.get("page_development_end"), "ー"),
                page_1f_ceiling_plan=_page_value(request.POST.get("page_1f_ceiling_plan"), "ー"),
                page_2f_ceiling_plan=_page_value(request.POST.get("page_2f_ceiling_plan"), "ー"),
                page_3f_ceiling_plan=_page_value(request.POST.get("page_3f_ceiling_plan"), "ー"),
                page_floor_area_table=_page_value(request.POST.get("page_floor_area_table"), "ー"),
                page_living_area_table=_page_value(request.POST.get("page_living_area_table"), "ー"),
                page_finish_table=_page_value(request.POST.get("page_finish_table"), "ー"),
                page_internal_finish_table=_page_value(request.POST.get("page_internal_finish_table"), "ー"),
                page_fixture_table_start=_page_value(request.POST.get("page_fixture_table_start"), "ー"),
                page_fixture_table_end=_page_value(request.POST.get("page_fixture_table_end"), "ー"),
                page_other_tables=(request.POST.get("page_other_tables") or "").strip(),
                memo=request.POST.get("memo", ""),
                **pdf_fields,
            )
        except Exception as exc:
            logger.exception("Project creation failed during PDF estimate upload")
            messages.error(request, f"積算データを作成できませんでした。理由: {exc}")
            return redirect("project_create")

        if _read_pdf_into_project(request, project, surface_wallpapers=surface_wallpapers):
            messages.success(request, "PDF図面から部屋情報を読み取り、積算を作成しました。")
        else:
            messages.error(request, "積算が作成できませんでした。")
        return redirect("project_detail", pk=project.pk)

    return render(
        request,
        "estimator/project_form.html",
        {
            "defaults": defaults,
            "wallpapers": wallpapers,
            "default_wallpaper": default_wallpaper,
            "surface_fields": SURFACE_FIELDS,
            "page_choices": _page_choices(),
            "default_page": "ー",
        },
    )


def project_detail(request, pk):
    """案件詳細と積算結果を表示する。

    Args:
        request: HTTPリクエスト。
        pk: 対象レコードの主キー。

    Returns:
        案件詳細画面のHTTPレスポンス。
    """
    project = _owned_project_or_403(request, Project.objects.prefetch_related("rooms"), pk)
    rooms = project.rooms.all()
    has_estimated_openings = any(_is_estimated_opening(room) for room in rooms)
    has_ai_missing_rooms = any(room.source_type == ROOM_SOURCE_AI_MISSING for room in rooms)
    has_manual_rooms = any(room.source_type == ROOM_SOURCE_MANUAL for room in rooms)
    summary = project.wallpaper_summary
    edit_mode = request.GET.get("edit") == "1"
    return render(
        request,
        "estimator/project_detail.html",
        {
            "project": project,
            "rooms": rooms,
            "wallpapers": _selectable_wallpapers(),
            "surface_fields": SURFACE_FIELDS,
            "estimate_methods": ESTIMATE_METHOD_CHOICES,
            "summary": summary,
            "suggested_project_name": _suggested_revision_name(project.name),
            "has_estimated_openings": has_estimated_openings,
            "has_ai_missing_rooms": has_ai_missing_rooms,
            "has_manual_rooms": has_manual_rooms,
            "edit_mode": edit_mode,
        },
    )


def project_save_wallpapers(request, pk):
    """壁紙・面積編集内容を保存または別案件として保存する。

    Args:
        request: HTTPリクエスト。
        pk: 対象レコードの主キー。

    Returns:
        保存後の案件詳細画面へのリダイレクトレスポンス。
    """
    source_project = _owned_project_or_403(request, Project.objects.prefetch_related("rooms"), pk)
    if request.method != "POST":
        return redirect("project_detail", pk=source_project.pk)

    apply_changes = request.POST.get("apply_changes") == "1"
    if apply_changes:
        target_project = source_project
        target_project.adopted_estimate_method = _estimate_method(request.POST.get("adopted_estimate_method"))
        target_project.save(update_fields=["adopted_estimate_method", "updated_at"])
        room_map = {room.pk: room for room in target_project.rooms.all()}
    else:
        requested_name = (request.POST.get("save_project_name") or _suggested_revision_name(source_project.name)).strip()
        if not requested_name:
            requested_name = _suggested_revision_name(source_project.name)

        overwrite = requested_name == source_project.name
        if overwrite and request.POST.get("confirm_overwrite") != "1":
            messages.error(request, "元案件名と同じため、上書き確認が必要です。")
            return redirect("project_detail", pk=source_project.pk)

        if overwrite:
            target_project = source_project
            target_project.adopted_estimate_method = _estimate_method(request.POST.get("adopted_estimate_method"))
            target_project.save(update_fields=["adopted_estimate_method", "updated_at"])
            room_map = {room.pk: room for room in target_project.rooms.all()}
        else:
            target_project = _clone_project(source_project, requested_name)
            target_project.adopted_estimate_method = _estimate_method(request.POST.get("adopted_estimate_method"))
            target_project.save(update_fields=["adopted_estimate_method", "updated_at"])
            room_map = _clone_rooms(source_project, target_project)

    wallpaper_map = {wallpaper.number: wallpaper for wallpaper in Wallpaper.objects.all()}
    for source_room_id, target_room in room_map.items():
        target_room.excluded_from_summary = request.POST.get(f"room_{source_room_id}_excluded_from_summary") == "1"
        for field, _label, _surface_type in SURFACE_FIELDS:
            selected_no = request.POST.get(
                f"room_{source_room_id}_{field}_wallpaper_no",
                getattr(target_room, f"{field}_wallpaper_no"),
            )
            wallpaper = wallpaper_map.get(selected_no)
            if wallpaper:
                target_room.apply_wallpaper(field, wallpaper)
            setattr(
                target_room,
                f"{field}_surface_area_m2",
                _decimal(request.POST.get(f"room_{source_room_id}_{field}_surface_area_m2"), getattr(target_room, f"{field}_surface_area_m2")),
            )
            if _surface_type == "wall":
                setattr(
                    target_room,
                    f"{field}_opening_area_m2",
                    _decimal(request.POST.get(f"room_{source_room_id}_{field}_opening_area_m2"), getattr(target_room, f"{field}_opening_area_m2")),
                )
        target_room.sync_totals_from_surface_measurements()
        target_room.save()

    _create_manual_rooms_from_post(
        request.POST,
        target_project,
        default_wallpaper=Wallpaper.objects.get(number="001"),
        wallpaper_map=wallpaper_map,
    )

    if apply_changes:
        messages.success(request, "編集内容を積算に反映しました。")
        return redirect(f"{reverse('project_detail', args=[target_project.pk])}?edit=1")

    messages.success(request, f"{target_project.name} として壁紙設定を保存しました。")
    return redirect("project_detail", pk=target_project.pk)


def project_recalculate(request, pk):
    """既存案件のPDFを再解析して積算を作り直す。

    Args:
        request: HTTPリクエスト。
        pk: 対象レコードの主キー。

    Returns:
        再解析後の案件詳細画面へのリダイレクトレスポンス。
    """
    project = _owned_project_or_403(request, Project, pk)
    if request.method != "POST":
        return redirect("project_detail", pk=project.pk)

    defaults = EstimateDefaultSettings.load()
    default_wallpaper = defaults.default_wallpaper or Wallpaper.objects.get(number="001")
    if _read_pdf_into_project(request, project, replace_rooms=True, default_wallpaper=default_wallpaper):
        messages.success(request, "PDF図面から部屋情報を読み取り、積算を作成しました。")
    else:
        messages.error(request, "積算が作成できませんでした。")
    return redirect("project_detail", pk=project.pk)


def project_pdf(request, pk):
    """案件に紐づく図面PDFを返す。

    Args:
        request: HTTPリクエスト。
        pk: 対象レコードの主キー。

    Returns:
        図面PDFのレスポンスまたは署名付きURLへのリダイレクト。
    """
    project = _owned_project_or_403(request, Project, pk)
    if not project.has_drawing_pdf:
        raise Http404("PDFが登録されていません。")
    if not project.can_view_drawing_pdf(request.user):
        return HttpResponseForbidden("このPDFを表示する権限がありません。")

    if project.drawing_pdf_storage_path:
        try:
            return HttpResponseRedirect(storage.create_signed_url(project.drawing_pdf_storage_path))
        except storage.SupabaseStorageError as exc:
            raise Http404("PDFの署名付きURLを発行できませんでした。") from exc

    try:
        pdf_file = project.drawing_pdf.open("rb")
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise Http404("PDFファイルが見つかりません。") from exc

    filename = project.drawing_pdf_filename
    response = FileResponse(pdf_file, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


def project_csv(request, pk):
    """案件の積算明細をCSVで出力する。

    Args:
        request: HTTPリクエスト。
        pk: 対象レコードの主キー。

    Returns:
        積算明細CSVのHTTPレスポンス。
    """
    project = _owned_project_or_403(request, Project.objects.prefetch_related("rooms"), pk)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    filename = f"wallpaper_estimate_{project.pk}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("\ufeff")

    writer = csv.writer(response)
    writer.writerow(["案件名", project.name])
    writer.writerow(["顧客名", project.client_name])
    writer.writerow(["採用見積方式", project.get_adopted_estimate_method_display()])
    writer.writerow(["採用正式見積金額(円)", project.total_cost])
    writer.writerow([])
    writer.writerow(["壁紙別集計"])
    writer.writerow([
        "壁紙No.",
        "壁紙名称",
        "壁紙別面積(m2)",
        "壁面面積(m2)",
        "天井面積(m2)",
        "必要面積(m2)",
        "ロス率(%)",
        "壁紙別合算方式ロール本数",
        "壁紙別合算方式金額(円)",
        "部屋別積上方式ロール本数",
        "部屋別積上方式金額(円)",
    ])
    for row in project.wallpaper_summary["rows"]:
        writer.writerow([
            row["wallpaper_no"],
            row["wallpaper_name"],
            _round(row["base_area"]),
            _round(row["wall_area"]),
            _round(row["ceiling_area"]),
            _round(row["required_area"]),
            row["loss_rate_percent"],
            row["wallpaper_total_rolls"],
            row["wallpaper_total_cost"],
            row["room_total_rolls"],
            row["room_total_cost"],
        ])
    writer.writerow([])
    writer.writerow(["部屋別明細"])
    writer.writerow([
        "No.",
        "階",
        "部屋名",
        "周長(m)",
        "天井高(m)",
        "開口部(m2)",
        "天井(m2)",
        "1面壁紙",
        "2面壁紙",
        "3面壁紙",
        "4面壁紙",
        "天井壁紙",
        "集計対象外",
        "必要面積(m2)",
        "部屋別積上方式ロール本数",
        "備考",
    ])
    for index, room in enumerate(project.rooms.all(), start=1):
        writer.writerow([
            index,
            room.display_floor_label,
            room.display_room_name,
            room.perimeter_m,
            room.height_m,
            room.opening_area_m2,
            room.ceiling_area_m2,
            f"{room.east_wallpaper_no}：{room.east_wallpaper_name}",
            f"{room.west_wallpaper_no}：{room.west_wallpaper_name}",
            f"{room.south_wallpaper_no}：{room.south_wallpaper_name}",
            f"{room.north_wallpaper_no}：{room.north_wallpaper_name}",
            f"{room.ceiling_wallpaper_no}：{room.ceiling_wallpaper_name}",
            "対象外" if room.excluded_from_summary else "",
            _round(room.total_area),
            room.rolls_required,
            room.note,
        ])
    writer.writerow([])
    writer.writerow(["合計必要面積(m2)", _round(project.total_area)])
    writer.writerow(["壁紙別合算方式ロール本数", project.wallpaper_summary["wallpaper_total"]["rolls"]])
    writer.writerow(["壁紙別合算方式金額(円)", project.wallpaper_summary["wallpaper_total"]["cost"]])
    writer.writerow(["部屋別積上方式ロール本数", project.wallpaper_summary["room_total"]["rolls"]])
    writer.writerow(["部屋別積上方式金額(円)", project.wallpaper_summary["room_total"]["cost"]])
    writer.writerow(["採用見積方式", project.get_adopted_estimate_method_display()])
    writer.writerow(["採用正式見積金額(円)", project.total_cost])
    return response


def _owned_project_or_403(request, queryset, pk):
    """ログインユーザーが所有する案件を取得する。

    Args:
        request: HTTPリクエスト。
        queryset: 所有者チェック対象のQuerySetまたはモデル。
        pk: 対象レコードの主キー。

    Returns:
        所有者確認済みの案件。
    """
    if not request.user.is_authenticated:
        return _raise_forbidden()
    project = get_object_or_404(queryset, pk=pk)
    if project.uploaded_by_id != request.user.pk:
        return _raise_forbidden()
    return project


def _raise_forbidden():
    """案件閲覧権限がない場合の例外を送出する。"""
    raise PermissionDenied("この積算データを表示する権限がありません。")


def _create_rooms_from_analysis(
    project,
    analyzed_rooms,
    default_wallpaper=None,
    surface_wallpapers=None,
    missing_rooms=None,
    room_candidates=None,
):
    """PDF解析結果と不足候補から案件の部屋レコードを作成する。

    Args:
        project: 処理対象の案件。
        analyzed_rooms: AI解析済みの部屋一覧。
        default_wallpaper: 初期適用する壁紙マスタ。
        surface_wallpapers: 面ごとに適用する壁紙マスタ。
        missing_rooms: 抽出できなかった部屋名の一覧。
        room_candidates: 表ページなどから検出した部屋候補。
    """
    default_wallpaper = default_wallpaper or Wallpaper.objects.get(number="001")
    surface_wallpapers = surface_wallpapers or {field: default_wallpaper for field, _label, _type in SURFACE_FIELDS}
    missing_rooms = _missing_room_specs(missing_rooms or [], analyzed_rooms, room_candidates=room_candidates)
    floor_order = ("1F", "2F", "3F", "")
    for floor in floor_order:
        for room in analyzed_rooms:
            if _floor_label_from_text(f"{room.name} {room.note}") != floor:
                continue
            _create_room_from_analysis(project, room, surface_wallpapers, default_wallpaper)
        for missing_room in missing_rooms:
            if _floor_label_from_text(missing_room["name"]) != floor:
                continue
            _create_empty_room(
                project,
                missing_room["name"],
                ROOM_SOURCE_AI_MISSING,
                missing_room["note"],
                default_wallpaper,
                surface_wallpapers=surface_wallpapers,
                ceiling_area_m2=missing_room["ceiling_area_m2"],
            )


def _create_room_from_analysis(project, room, surface_wallpapers, default_wallpaper):
    """AI解析済みの部屋情報から部屋レコードを作成する。

    Args:
        project: 処理対象の案件。
        room: 処理対象の部屋または解析済み部屋。
        surface_wallpapers: 面ごとに適用する壁紙マスタ。
        default_wallpaper: 初期適用する壁紙マスタ。
    """
    created = Room(
        project=project,
        name=_truncate_text(room.name, Room._meta.get_field("name").max_length),
        source_type=ROOM_SOURCE_AI,
        perimeter_m=_room_measurement(room.perimeter_m, max_value=Decimal("99999.99")),
        height_m=_room_measurement(room.height_m, max_value=Decimal("999.99")),
        opening_area_m2=_room_measurement(room.opening_area_m2, max_value=Decimal("99999.99")),
        ceiling_area_m2=_room_measurement(room.ceiling_area_m2, max_value=Decimal("99999.99")),
        note=_truncate_text(room.note, Room._meta.get_field("note").max_length),
    )
    for field, _label, _surface_type in SURFACE_FIELDS:
        created.apply_wallpaper(field, surface_wallpapers.get(field, default_wallpaper))
    if room.wall_surfaces:
        for field, _label, surface_type in SURFACE_FIELDS:
            if surface_type == "ceiling":
                created.ceiling_surface_area_m2 = created.ceiling_area_m2
                continue
            surface = _wall_surface_value(room.wall_surfaces, field)
            setattr(created, f"{field}_surface_area_m2", _wall_surface_area(surface, created.height_m))
            setattr(
                created,
                f"{field}_opening_area_m2",
                _room_measurement(surface.get("opening_area_m2", Decimal("0")), max_value=Decimal("99999.99")),
            )
        created.sync_totals_from_surface_measurements()
    else:
        created.set_default_surface_measurements()
    created.save()


def _create_empty_room(project, name, source_type, note, default_wallpaper, surface_wallpapers=None, ceiling_area_m2=Decimal("0")):
    """面積未入力の部屋レコードを作成する。

    Args:
        project: 処理対象の案件。
        name: 名前。
        source_type: 部屋の追加区分。
        note: 備考。
        default_wallpaper: 初期適用する壁紙マスタ。
        surface_wallpapers: 面ごとに適用する壁紙マスタ。
        ceiling_area_m2: 天井面積。

    Returns:
        作成した部屋。
    """
    room = Room(
        project=project,
        name=_truncate_text(name, Room._meta.get_field("name").max_length),
        source_type=source_type,
        perimeter_m=Decimal("0"),
        height_m=Decimal("0"),
        opening_area_m2=Decimal("0"),
        ceiling_area_m2=_room_measurement(ceiling_area_m2, max_value=Decimal("99999.99")),
        note=_truncate_text(note, Room._meta.get_field("note").max_length),
    )
    if surface_wallpapers:
        for field, _label, _surface_type in SURFACE_FIELDS:
            room.apply_wallpaper(field, surface_wallpapers.get(field, default_wallpaper))
    else:
        room.apply_wallpaper_to_all_surfaces(default_wallpaper)
    room.set_default_surface_measurements()
    room.save()
    return room


def _missing_room_names(missing_rooms, analyzed_rooms):
    """AI解析で不足している部屋名だけを返す。

    Args:
        missing_rooms: 抽出できなかった部屋名の一覧。
        analyzed_rooms: AI解析済みの部屋一覧。

    Returns:
        不足している部屋名の一覧。
    """
    return [missing_room["name"] for missing_room in _missing_room_specs(missing_rooms, analyzed_rooms)]


def _missing_room_specs(missing_rooms, analyzed_rooms, room_candidates=None):
    """AI解析で不足している部屋の追加用情報を作る。

    Args:
        missing_rooms: 抽出できなかった部屋名の一覧。
        analyzed_rooms: AI解析済みの部屋一覧。
        room_candidates: 表ページなどから検出した部屋候補。

    Returns:
        不足部屋の名前、天井面積、備考を持つ辞書の一覧。
    """
    if room_candidates:
        return _missing_room_specs_from_candidates(room_candidates, analyzed_rooms, missing_rooms)

    extracted = {_normalize_room_name(room.name) for room in analyzed_rooms}
    names = []
    seen = set()
    for room_name in missing_rooms:
        normalized = _normalize_room_name(room_name)
        if not normalized or normalized in extracted or normalized in seen:
            continue
        seen.add(normalized)
        names.append({
            "name": str(room_name).strip(),
            "ceiling_area_m2": Decimal("0"),
            "note": "抽出失敗: 面積、開口部を入力してください",
        })
    return names


def _missing_room_specs_from_candidates(room_candidates, analyzed_rooms, missing_rooms):
    """表ページ候補からAI解析に出ていない部屋の追加用情報を作る。

    Args:
        room_candidates: 表ページなどから検出した部屋候補。
        analyzed_rooms: AI解析済みの部屋一覧。
        missing_rooms: 抽出できなかった部屋名の一覧。

    Returns:
        不足部屋の名前、天井面積、備考を持つ辞書の一覧。
    """
    extracted = set()
    for room in analyzed_rooms:
        extracted.update(_room_match_keys(room.name, room.note))
    specs = []
    seen = set()
    for candidate in room_candidates:
        room_name = _candidate_room_name(candidate)
        normalized = _normalize_room_name(room_name)
        if not normalized or _candidate_match_keys(candidate) & extracted or normalized in seen:
            continue
        seen.add(normalized)
        area = candidate.area_m2 or Decimal("0")
        specs.append({
            "name": room_name,
            "ceiling_area_m2": area,
            "note": f"抽出失敗: 表ページから天井面積 {area}m2 を反映。壁面・開口部を入力してください",
        })
    return specs


def _candidate_room_name(candidate):
    """部屋候補の階数を含む表示名を返す。

    Args:
        candidate: 部屋候補。

    Returns:
        階数付きの部屋候補名。
    """
    if _floor_label_from_text(candidate.name):
        return candidate.name
    return f"{candidate.floor} {candidate.name}".strip()


def _room_match_keys(name, note=""):
    """部屋名の照合キーを作成する。

    Args:
        name: 名前。
        note: 備考。

    Returns:
        部屋名照合に使う正規化済みキーの集合。
    """
    normalized = _normalize_room_name(name)
    keys = {normalized} if normalized else set()
    without_floor = re.sub(r"^[1-3](?:F|階)", "", normalized)
    if without_floor and without_floor == normalized:
        keys.add(without_floor)
    floor = _floor_label_from_text(f"{name} {note}")
    if floor:
        keys.add(_normalize_room_name(f"{floor} {name}"))
    return keys


def _candidate_match_keys(candidate):
    """部屋候補の照合キーを作成する。

    Args:
        candidate: 部屋候補。

    Returns:
        部屋候補照合に使う正規化済みキーの集合。
    """
    if candidate.floor:
        return {_normalize_room_name(f"{candidate.floor} {candidate.name}")}
    return _room_match_keys(candidate.name)


def _normalize_room_name(value):
    """部屋名を照合用に正規化する。

    Args:
        value: 変換または正規化する値。

    Returns:
        照合用に正規化した部屋名。
    """
    return str(value or "").translate(str.maketrans("０１２３４５６７８９", "0123456789")).upper().replace(" ", "")


def _floor_label_from_text(value):
    """文字列から階数ラベルを抽出する。

    Args:
        value: 変換または正規化する値。

    Returns:
        検出した階数ラベル。見つからない場合は空文字。
    """
    source = str(value or "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    match = re.search(r"([1-3])\s*(?:F|階)", source, re.IGNORECASE)
    if match:
        return f"{match.group(1)}F"
    return ""


def _create_manual_rooms_from_post(post_data, project, default_wallpaper, wallpaper_map=None):
    """POSTされた手動追加欄から部屋レコードを作成する。

    Args:
        post_data: POSTされたフォームデータ。
        project: 処理対象の案件。
        default_wallpaper: 初期適用する壁紙マスタ。
        wallpaper_map: 壁紙No.をキーにした壁紙マスタ辞書。
    """
    wallpaper_map = wallpaper_map or {wallpaper.number: wallpaper for wallpaper in Wallpaper.objects.all()}
    floors = post_data.getlist("new_room_floor")
    names = post_data.getlist("new_room_name")
    for index, (floor, name) in enumerate(zip(floors, names)):
        floor = floor if floor in {"1F", "2F", "3F"} else "1F"
        room_name = str(name or "").strip()
        if not room_name:
            continue
        if not _floor_label_from_text(room_name):
            room_name = f"{floor} {room_name}"
        room = _create_empty_room(
            project,
            room_name,
            ROOM_SOURCE_MANUAL,
            "手動追加: 面積、開口部を入力してください",
            default_wallpaper,
        )
        room.excluded_from_summary = _at(post_data.getlist("new_room_excluded_from_summary"), index) == "1"
        for field, _label, surface_type in SURFACE_FIELDS:
            selected_no = _at(post_data.getlist(f"new_room_{field}_wallpaper_no"), index)
            wallpaper = wallpaper_map.get(selected_no)
            if wallpaper:
                room.apply_wallpaper(field, wallpaper)
            setattr(
                room,
                f"{field}_surface_area_m2",
                _decimal(_at(post_data.getlist(f"new_room_{field}_surface_area_m2"), index), Decimal("0")),
            )
            if surface_type == "wall":
                setattr(
                    room,
                    f"{field}_opening_area_m2",
                    _decimal(_at(post_data.getlist(f"new_room_{field}_opening_area_m2"), index), Decimal("0")),
                )
        room.sync_totals_from_surface_measurements()
        room.save()


def _wall_surface_value(wall_surfaces, field):
    """AI解析の面情報から対象フィールドの壁面情報を取り出す。

    Args:
        wall_surfaces: 1面から4面までの壁面情報。
        field: 対象の面またはフィールド名。

    Returns:
        対象面の壁面情報。見つからない場合は空の辞書。
    """
    face_keys = {
        "east": "face_1",
        "west": "face_2",
        "south": "face_3",
        "north": "face_4",
    }
    return wall_surfaces.get(field) or wall_surfaces.get(face_keys.get(field), {}) or {}


def _wall_surface_area(surface, height_m):
    """面幅または面積から保存用の壁面積を算出する。

    Args:
        surface: AI解析で返された面情報。
        height_m: 天井高。

    Returns:
        保存可能な範囲に丸めた壁面積。
    """
    width = surface.get("width_m", Decimal("0"))
    if width > 0:
        return _room_measurement(width * height_m, max_value=Decimal("99999.99"))
    return _room_measurement(surface.get("surface_area_m2", Decimal("0")), max_value=Decimal("99999.99"))


def _room_measurement(value, max_value):
    """部屋寸法値をDecimalへ変換し、保存可能な範囲へ丸める。

    Args:
        value: 変換または正規化する値。
        max_value: DB保存前に許容する最大値。

    Returns:
        0以上かつ最大値以下に正規化したDecimal値。
    """
    try:
        measurement = Decimal(str(value if value is not None else "0")).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0")
    if not measurement.is_finite() or measurement < 0:
        return Decimal("0")
    return min(measurement, max_value)


def _truncate_text(value, max_length):
    """文字列をDB保存可能な長さへ切り詰める。

    Args:
        value: 変換または正規化する値。
        max_length: DB保存前に許容する最大文字数。

    Returns:
        前後空白を除去し、最大文字数内に収めた文字列。
    """
    text = str(value or "").strip()
    if max_length and len(text) > max_length:
        return text[:max_length]
    return text


def _validate_drawing_pdf(uploaded_file):
    """_validate_drawing_pdfを検証する。

    Args:
        uploaded_file: アップロードされたPDFファイル。
    """
    if uploaded_file.size > settings.PDF_MAX_UPLOAD_SIZE:
        raise ValidationError("図面PDFは10MB以下にしてください。")
    if uploaded_file.content_type != "application/pdf":
        raise ValidationError("図面PDFはapplication/pdfのみアップロードできます。")

    current_position = uploaded_file.tell()
    uploaded_file.seek(0)
    header = uploaded_file.read(5)
    uploaded_file.seek(current_position)
    if header != b"%PDF-":
        raise ValidationError("PDFファイルとして認識できません。")


def _save_uploaded_drawing_pdf(uploaded_file, user):
    """アップロードPDFをローカルまたはSupabase Storageへ保存する。

    Args:
        uploaded_file: アップロードされたPDFファイル。
        user: アップロードしたユーザー。

    Returns:
        Project作成時に渡すPDF関連フィールド。
    """
    if not storage.is_configured():
        return {
            "drawing_pdf": uploaded_file,
            "drawing_pdf_original_name": storage.safe_filename(uploaded_file.name),
            "drawing_pdf_content_type": uploaded_file.content_type,
            "drawing_pdf_size": uploaded_file.size,
        }

    object_path = _drawing_pdf_object_path(user)
    storage.upload_pdf(uploaded_file, object_path)
    return {
        "drawing_pdf_storage_path": object_path,
        "drawing_pdf_original_name": storage.safe_filename(uploaded_file.name),
        "drawing_pdf_content_type": uploaded_file.content_type,
        "drawing_pdf_size": uploaded_file.size,
    }


def _drawing_pdf_object_path(user):
    """Storage上でPDFを保存するオブジェクトパスを生成する。

    Args:
        user: アップロードしたユーザー。

    Returns:
        ユーザーIDとUUIDを含むPDFオブジェクトパス。
    """
    user_id = user.pk if user.is_authenticated else "anonymous"
    return f"{user_id}/{uuid.uuid4()}.pdf"


@contextmanager
def _drawing_pdf_path(project):
    """解析用に参照できる図面PDFのローカルパスを一時的に用意する。

    Args:
        project: 処理対象の案件。
    """
    if project.drawing_pdf_storage_path:
        pdf_data = storage.download_pdf(project.drawing_pdf_storage_path)
        temp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        try:
            temp_file.write(pdf_data)
            temp_file.close()
            yield temp_file.name
        finally:
            Path(temp_file.name).unlink(missing_ok=True)
        return

    yield project.drawing_pdf.path


def _read_pdf_into_project(request, project, replace_rooms=False, default_wallpaper=None, surface_wallpapers=None):
    """PDF解析結果を案件の部屋データへ反映する。

    Args:
        request: HTTPリクエスト。
        project: 処理対象の案件。
        replace_rooms: 既存の部屋を置き換える場合はTrue。
        default_wallpaper: 初期適用する壁紙マスタ。
        surface_wallpapers: 面ごとに適用する壁紙マスタ。

    Returns:
        解析と部屋反映に成功した場合はTrue。
    """
    if not project.has_drawing_pdf:
        messages.error(request, "PDF自動読取はできませんでした。理由: 図面PDFが登録されていません。")
        return False

    started_at = time.monotonic()
    project.analysis_status = ANALYSIS_STATUS_RUNNING
    project.analysis_started_at = timezone.now()
    project.analysis_finished_at = None
    project.analysis_error_message = ""
    project.analysis_model = getattr(settings, "OPENAI_PDF_ANALYSIS_MODEL", "") or ""
    project.save(update_fields=[
        "analysis_status",
        "analysis_started_at",
        "analysis_finished_at",
        "analysis_error_message",
        "analysis_model",
    ])
    try:
        close_old_connections()
        explicit_table_pages = _project_explicit_table_pages(project)
        memo_table_pages = _project_table_pages_from_memo(project.memo) if replace_rooms else None
        with _drawing_pdf_path(project) as pdf_path:
            analysis = analyze_wallpaper_pdf(
                pdf_path,
                _project_page_map(project),
                table_pages=explicit_table_pages or memo_table_pages,
                allow_visual_table_detection=not replace_rooms and not explicit_table_pages,
            )
        if replace_rooms:
            project.rooms.all().delete()
        _create_rooms_from_analysis(
            project,
            analysis.rooms,
            default_wallpaper=default_wallpaper,
            surface_wallpapers=surface_wallpapers,
            missing_rooms=analysis.missing_rooms,
            room_candidates=analysis.room_candidates,
        )
        project.memo = _join_memo(project.memo, analysis.memo)
        project.last_calculation_seconds = _elapsed_seconds(started_at)
        project.analysis_status = ANALYSIS_STATUS_SUCCEEDED
        project.analysis_finished_at = timezone.now()
        project.analysis_error_message = ""
        project.save(update_fields=[
            "memo",
            "last_calculation_seconds",
            "analysis_status",
            "analysis_finished_at",
            "analysis_error_message",
        ])
        return True
    except ValueError as exc:
        _mark_analysis_failed(project, started_at, str(exc))
        messages.error(request, f"PDF自動読取はできませんでした。理由: {exc}")
    except Exception as exc:
        _mark_analysis_failed(project, started_at, str(exc))
        logger.exception("Unexpected PDF analysis error for project %s", project.pk)
        messages.error(request, "PDF自動読取中に予期しないエラーが発生しました。")
    finally:
        close_old_connections()
    return False


def _elapsed_seconds(started_at):
    """処理開始時刻からの経過秒数を返す。

    Args:
        started_at: time.monotonicで取得した処理開始時刻。

    Returns:
        0以上に丸めた経過秒数。
    """
    return max(0, int(round(time.monotonic() - started_at)))


def _save_calculation_seconds(project, started_at):
    """案件へ直近の計算時間を保存する。

    Args:
        project: 処理対象の案件。
        started_at: time.monotonicで取得した処理開始時刻。
    """
    project.last_calculation_seconds = _elapsed_seconds(started_at)
    try:
        project.save(update_fields=["last_calculation_seconds"])
    except Exception:
        logger.exception("Could not save calculation duration for project %s", project.pk)


def _mark_analysis_failed(project, started_at, error_message):
    """PDF解析失敗時のステータスとエラー内容を案件へ保存する。

    Args:
        project: 処理対象の案件。
        started_at: time.monotonicで取得した処理開始時刻。
        error_message: 保存するエラーメッセージ。
    """
    project.analysis_status = ANALYSIS_STATUS_FAILED
    project.analysis_finished_at = timezone.now()
    project.analysis_error_message = _truncate_error_message(error_message)
    project.last_calculation_seconds = _elapsed_seconds(started_at)
    try:
        project.save(update_fields=[
            "analysis_status",
            "analysis_finished_at",
            "analysis_error_message",
            "last_calculation_seconds",
        ])
    except Exception:
        logger.exception("Could not save analysis failure for project %s", project.pk)


def _truncate_error_message(value):
    """解析エラーメッセージをDB保存上限内に収める。

    Args:
        value: 変換または正規化する値。

    Returns:
        先頭2000文字までのエラーメッセージ。
    """
    return str(value or "").strip()[:2000]


def _project_explicit_table_pages(project):
    """案件に明示指定された表ページ一覧を返す。

    Args:
        project: 処理対象の案件。

    Returns:
        表種別とページ番号の一覧。指定がない場合はNone。
    """
    pages = []
    pages.extend(_single_table_page("床面積表", project.page_floor_area_table))
    pages.extend(_single_table_page("居室区画面積表", project.page_living_area_table))
    pages.extend(_single_table_page("室内仕上表", project.page_finish_table))
    pages.extend(_single_table_page("内部仕上表", project.page_internal_finish_table))
    pages.extend(_range_table_pages("建具表", project.page_fixture_table_start, project.page_fixture_table_end))
    pages.extend(_range_table_pages("その他表ページ", project.page_other_tables, "ー"))
    return _deduplicate_table_pages(pages) or None


def _single_table_page(label, value):
    """単一指定の表ページをページ一覧形式へ変換する。

    Args:
        label: 表種別ラベル。
        value: ページ指定文字列。

    Returns:
        表種別とページ番号の一覧。
    """
    page = _optional_page_number(value)
    return [(label, page)] if page else []


def _range_table_pages(label, start_value, end_value):
    """範囲または複数指定の表ページをページ一覧形式へ変換する。

    Args:
        label: 表種別ラベル。
        start_value: 開始ページ、単一ページ、または複数ページ指定。
        end_value: 終了ページ指定。

    Returns:
        表種別とページ番号の一覧。
    """
    start_pages = _page_number_list(start_value)
    if not start_pages:
        return []
    end_page = _optional_page_number(end_value)
    if len(start_pages) == 1 and end_page and end_page >= start_pages[0]:
        return [(label, page) for page in range(start_pages[0], end_page + 1)]
    return [(label, page) for page in start_pages]


def _page_number_list(value):
    """ページ指定文字列から有効なページ番号一覧を抽出する。

    Args:
        value: ページ指定文字列。

    Returns:
        1以上のページ番号一覧。
    """
    normalized = str(value or "").strip()
    if normalized in {"", "-", "ー", "－", "なし", "無し", "0"}:
        return []
    numbers = []
    for part in re.split(r"[,\s、]+", normalized):
        if not part:
            continue
        range_match = re.fullmatch(r"(\d+)\s*[-~〜]\s*(\d+)", part)
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if start <= end:
                numbers.extend(range(start, end + 1))
            continue
        if part.isdigit():
            numbers.append(int(part))
    return [number for number in numbers if number > 0]


def _optional_page_number(value):
    """ページ指定文字列から先頭のページ番号を返す。

    Args:
        value: ページ指定文字列。

    Returns:
        先頭のページ番号。指定がない場合はNone。
    """
    numbers = _page_number_list(value)
    return numbers[0] if numbers else None


def _deduplicate_table_pages(table_pages):
    """表種別とページ番号の重複を除いた一覧を返す。

    Args:
        table_pages: 表種別とページ番号の一覧。

    Returns:
        入力順を保った重複除外後の一覧。
    """
    deduplicated = []
    seen = set()
    for label, page in table_pages:
        key = (label, page)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append((label, page))
    return deduplicated


def _project_table_pages_from_memo(memo):
    """解析メモに記録された表ページ一覧を復元する。

    Args:
        memo: 解析結果の補足メモ。

    Returns:
        表種別とページ番号の一覧。見つからない場合はNone。
    """
    if not memo:
        return None
    table_labels = tuple(label for label, _keyword in TABLE_PAGE_KEYWORDS)
    pattern = re.compile(rf"({'|'.join(re.escape(label) for label in table_labels)})=(\d+)P")
    pages = []
    seen = set()
    for label, page in pattern.findall(memo):
        item = (label, int(page))
        if item in seen:
            continue
        seen.add(item)
        pages.append(item)
    return pages or None


def _is_estimated_opening(room):
    """_is_estimated_openingを判定する。

    Args:
        room: 処理対象の部屋または解析済み部屋。

    Returns:
        開口部面積が推定値を含む場合はTrue。
    """
    return room.opening_area_m2 > 0 and any(
        marker in room.note for marker in ("推定", "展開図", "平面図", "スケール")
    )


def _join_memo(existing, added):
    """既存メモと追加メモを改行区切りで連結する。

    Args:
        existing: 既存メモ。
        added: 追加するメモ。

    Returns:
        連結後のメモ。
    """
    if existing and added:
        return f"{existing}\n{added}"
    return existing or added


def _project_page_map(project):
    """案件の図面ページ設定をPDF解析用の辞書へ変換する。

    Args:
        project: 処理対象の案件。

    Returns:
        PDF解析へ渡すページ設定。
    """
    return {
        "page_1f_plan": project.page_1f_plan,
        "page_2f_plan": project.page_2f_plan,
        "page_3f_plan": project.page_3f_plan,
        "page_development_start": project.page_development_start,
        "page_development_end": project.page_development_end,
        "page_1f_ceiling_plan": project.page_1f_ceiling_plan,
        "page_2f_ceiling_plan": project.page_2f_ceiling_plan,
        "page_3f_ceiling_plan": project.page_3f_ceiling_plan,
    }


def _page_value(value, default):
    """フォーム入力されたページ値を保存用文字列へ正規化する。

    Args:
        value: 変換または正規化する値。
        default: 値が空または不正な場合の既定値。

    Returns:
        空の場合は既定値または未指定記号を返すページ値。
    """
    normalized = (value or default).strip()
    return normalized or "ー"


def _page_choices():
    """画面で選択できるページ番号候補を返す。

    Returns:
        未指定記号と0から99までのページ番号候補。
    """
    return ["ー", *[str(number) for number in range(100)]]


def _decimal(value, default):
    """入力値をDecimalへ変換し、失敗時は既定値を返す。

    Args:
        value: 変換または正規化する値。
        default: 値が空または不正な場合の既定値。

    Returns:
        Decimal値。
    """
    try:
        return Decimal(str(value or default))
    except (InvalidOperation, TypeError):
        return Decimal(default)


def _at(values, index):
    """一覧から指定位置の値を安全に取得する。

    Args:
        values: 取得対象の値一覧。
        index: 取得する位置。

    Returns:
        指定位置の値。範囲外の場合は空文字。
    """
    return values[index] if index < len(values) else ""


def _round(value):
    """Decimal値を小数第2位へ丸める。

    Args:
        value: 変換または正規化する値。

    Returns:
        小数第2位で丸めたDecimal値。
    """
    return value.quantize(Decimal("0.01"))


def _selectable_wallpapers():
    """画面で選択できる有効な壁紙マスタを返す。

    Returns:
        選択可能な壁紙QuerySet。
    """
    Wallpaper.ensure_defaults()
    return Wallpaper.objects.filter(is_active=True).order_by("display_order", "number")


def _posted_surface_wallpapers(post_data, default_wallpaper):
    """POSTされた面別壁紙No.から面別の壁紙マスタを返す。

    Args:
        post_data: POSTされたフォームデータ。
        default_wallpaper: 初期適用する壁紙マスタ。

    Returns:
        面フィールド名をキーにした壁紙マスタ辞書。
    """
    wallpaper_map = {wallpaper.number: wallpaper for wallpaper in _selectable_wallpapers()}
    selected = {}
    for field, _label, _surface_type in SURFACE_FIELDS:
        selected[field] = wallpaper_map.get(post_data.get(f"{field}_wallpaper_no"), default_wallpaper)
    return selected


def _estimate_method(value):
    """見積方式の入力値を有効な選択肢へ正規化する。

    Args:
        value: 変換または正規化する値。

    Returns:
        有効な見積方式。無効な場合は壁紙別合算方式。
    """
    valid = {choice[0] for choice in ESTIMATE_METHOD_CHOICES}
    return value if value in valid else WALLPAPER_TOTAL_METHOD


def _suggested_revision_name(name):
    """既存案件名から重複しない修正版の案件名を提案する。

    Args:
        name: 名前。

    Returns:
        重複しない修正版の案件名。
    """
    base_name = f"{name}修正"
    if not Project.objects.filter(name=base_name).exists():
        return base_name
    number = 2
    while Project.objects.filter(name=f"{base_name}{number}").exists():
        number += 1
    return f"{base_name}{number}"


def _clone_project(project, name):
    """_clone_projectを複製する。

    Args:
        project: 処理対象の案件。
        name: 名前。

    Returns:
        複製した案件。
    """
    clone = Project.objects.create(
        name=name,
        client_name=project.client_name,
        drawing_pdf=project.drawing_pdf,
        drawing_pdf_storage_path=project.drawing_pdf_storage_path,
        drawing_pdf_original_name=project.drawing_pdf_original_name,
        drawing_pdf_content_type=project.drawing_pdf_content_type,
        drawing_pdf_size=project.drawing_pdf_size,
        uploaded_by=project.uploaded_by,
        wallpaper_roll_width_m=project.wallpaper_roll_width_m,
        wallpaper_roll_length_m=project.wallpaper_roll_length_m,
        loss_rate_percent=project.loss_rate_percent,
        unit_price_per_roll=project.unit_price_per_roll,
        adopted_estimate_method=_estimate_method(project.adopted_estimate_method),
        page_1f_plan=project.page_1f_plan,
        page_2f_plan=project.page_2f_plan,
        page_3f_plan=project.page_3f_plan,
        page_development_start=project.page_development_start,
        page_development_end=project.page_development_end,
        page_1f_ceiling_plan=project.page_1f_ceiling_plan,
        page_2f_ceiling_plan=project.page_2f_ceiling_plan,
        page_3f_ceiling_plan=project.page_3f_ceiling_plan,
        page_floor_area_table=project.page_floor_area_table,
        page_living_area_table=project.page_living_area_table,
        page_finish_table=project.page_finish_table,
        page_internal_finish_table=project.page_internal_finish_table,
        page_fixture_table_start=project.page_fixture_table_start,
        page_fixture_table_end=project.page_fixture_table_end,
        page_other_tables=project.page_other_tables,
        memo=project.memo,
        analysis_status=project.analysis_status,
        analysis_error_message=project.analysis_error_message,
        analysis_started_at=project.analysis_started_at,
        analysis_finished_at=project.analysis_finished_at,
        analysis_model=project.analysis_model,
        last_calculation_seconds=project.last_calculation_seconds,
    )
    return clone


def _clone_rooms(source_project, target_project):
    """_clone_roomsを複製する。

    Args:
        source_project: 複製元の案件。
        target_project: 複製先の案件。

    Returns:
        複製元の部屋IDをキーにした複製後の部屋辞書。
    """
    room_map = {}
    field_names = [
        field.name
        for field in Room._meta.fields
        if field.name not in ("id", "project")
    ]
    for source_room in source_project.rooms.all():
        values = {field_name: getattr(source_room, field_name) for field_name in field_names}
        target_room = Room.objects.create(project=target_project, **values)
        room_map[source_room.pk] = target_room
    return room_map
