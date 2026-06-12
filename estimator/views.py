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
from django.urls import reverse

from .models import (
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
        処理結果。
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
        処理結果。
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
        処理結果。
    """
    return render(request, "estimator/about.html")


@login_required
def project_create(request):
    """PDF図面アップロードから新規積算案件を作成する。

    Args:
        request: HTTPリクエスト。

    Returns:
        処理結果。
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
        処理結果。
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
        処理結果。
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
        処理結果。
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
        処理結果。
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
        処理結果。
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
    """_owned_project_or_403を処理する。

    Args:
        request: HTTPリクエスト。
        queryset: 所有者チェック対象のQuerySetまたはモデル。
        pk: 対象レコードの主キー。

    Returns:
        処理結果。
    """
    if not request.user.is_authenticated:
        return _raise_forbidden()
    project = get_object_or_404(queryset, pk=pk)
    if project.uploaded_by_id != request.user.pk:
        return _raise_forbidden()
    return project


def _raise_forbidden():
    """_raise_forbiddenを処理する。"""
    raise PermissionDenied("この積算データを表示する権限がありません。")


def _create_rooms_from_analysis(
    project,
    analyzed_rooms,
    default_wallpaper=None,
    surface_wallpapers=None,
    missing_rooms=None,
    room_candidates=None,
):
    """_create_rooms_from_analysisを作成する。

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
    """_create_room_from_analysisを作成する。

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
    """_create_empty_roomを作成する。

    Args:
        project: 処理対象の案件。
        name: 名前。
        source_type: 部屋の追加区分。
        note: 備考。
        default_wallpaper: 初期適用する壁紙マスタ。
        surface_wallpapers: 面ごとに適用する壁紙マスタ。
        ceiling_area_m2: 天井面積。

    Returns:
        処理結果。
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
    """_missing_room_namesを処理する。

    Args:
        missing_rooms: 抽出できなかった部屋名の一覧。
        analyzed_rooms: AI解析済みの部屋一覧。

    Returns:
        処理結果。
    """
    return [missing_room["name"] for missing_room in _missing_room_specs(missing_rooms, analyzed_rooms)]


def _missing_room_specs(missing_rooms, analyzed_rooms, room_candidates=None):
    """_missing_room_specsを処理する。

    Args:
        missing_rooms: 抽出できなかった部屋名の一覧。
        analyzed_rooms: AI解析済みの部屋一覧。
        room_candidates: 表ページなどから検出した部屋候補。

    Returns:
        処理結果。
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
    """_missing_room_specs_from_candidatesを処理する。

    Args:
        room_candidates: 表ページなどから検出した部屋候補。
        analyzed_rooms: AI解析済みの部屋一覧。
        missing_rooms: 抽出できなかった部屋名の一覧。

    Returns:
        処理結果。
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
    """_candidate_room_nameを処理する。

    Args:
        candidate: 部屋候補。

    Returns:
        処理結果。
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
        処理結果。
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
        処理結果。
    """
    if candidate.floor:
        return {_normalize_room_name(f"{candidate.floor} {candidate.name}")}
    return _room_match_keys(candidate.name)


def _normalize_room_name(value):
    """部屋名を照合用に正規化する。

    Args:
        value: 変換または正規化する値。

    Returns:
        処理結果。
    """
    return str(value or "").translate(str.maketrans("０１２３４５６７８９", "0123456789")).upper().replace(" ", "")


def _floor_label_from_text(value):
    """文字列から階数ラベルを抽出する。

    Args:
        value: 変換または正規化する値。

    Returns:
        処理結果。
    """
    source = str(value or "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    match = re.search(r"([1-3])\s*(?:F|階)", source, re.IGNORECASE)
    if match:
        return f"{match.group(1)}F"
    return ""


def _create_manual_rooms_from_post(post_data, project, default_wallpaper, wallpaper_map=None):
    """_create_manual_rooms_from_postを作成する。

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
    """_wall_surface_valueを処理する。

    Args:
        wall_surfaces: 1面から4面までの壁面情報。
        field: 対象の面またはフィールド名。

    Returns:
        処理結果。
    """
    face_keys = {
        "east": "face_1",
        "west": "face_2",
        "south": "face_3",
        "north": "face_4",
    }
    return wall_surfaces.get(field) or wall_surfaces.get(face_keys.get(field), {}) or {}


def _wall_surface_area(surface, height_m):
    """_wall_surface_areaを処理する。

    Args:
        surface: AI解析で返された面情報。
        height_m: 天井高。

    Returns:
        処理結果。
    """
    width = surface.get("width_m", Decimal("0"))
    if width > 0:
        return _room_measurement(width * height_m, max_value=Decimal("99999.99"))
    return _room_measurement(surface.get("surface_area_m2", Decimal("0")), max_value=Decimal("99999.99"))


def _room_measurement(value, max_value):
    """_room_measurementを処理する。

    Args:
        value: 変換または正規化する値。
        max_value: DB保存前に許容する最大値。

    Returns:
        処理結果。
    """
    try:
        measurement = Decimal(str(value if value is not None else "0")).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0")
    if not measurement.is_finite() or measurement < 0:
        return Decimal("0")
    return min(measurement, max_value)


def _truncate_text(value, max_length):
    """_truncate_textを処理する。

    Args:
        value: 変換または正規化する値。
        max_length: DB保存前に許容する最大文字数。

    Returns:
        処理結果。
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
    """_save_uploaded_drawing_pdfを保存する。

    Args:
        uploaded_file: アップロードされたPDFファイル。
        user: 権限確認対象のユーザー。

    Returns:
        処理結果。
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
    """_drawing_pdf_object_pathを処理する。

    Args:
        user: 権限確認対象のユーザー。

    Returns:
        処理結果。
    """
    user_id = user.pk if user.is_authenticated else "anonymous"
    return f"{user_id}/{uuid.uuid4()}.pdf"


@contextmanager
def _drawing_pdf_path(project):
    """_drawing_pdf_pathを処理する。

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
        処理結果。
    """
    if not project.has_drawing_pdf:
        messages.error(request, "PDF自動読取はできませんでした。理由: 図面PDFが登録されていません。")
        return False

    started_at = time.monotonic()
    try:
        close_old_connections()
        with _drawing_pdf_path(project) as pdf_path:
            analysis = analyze_wallpaper_pdf(
                pdf_path,
                _project_page_map(project),
                table_pages=_project_table_pages_from_memo(project.memo) if replace_rooms else None,
                allow_visual_table_detection=not replace_rooms,
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
        project.save(update_fields=["memo", "last_calculation_seconds"])
        return True
    except ValueError as exc:
        _save_calculation_seconds(project, started_at)
        messages.error(request, f"PDF自動読取はできませんでした。理由: {exc}")
    except Exception:
        _save_calculation_seconds(project, started_at)
        logger.exception("Unexpected PDF analysis error for project %s", project.pk)
        messages.error(request, "PDF自動読取中に予期しないエラーが発生しました。")
    finally:
        close_old_connections()
    return False


def _elapsed_seconds(started_at):
    return max(0, int(round(time.monotonic() - started_at)))


def _save_calculation_seconds(project, started_at):
    project.last_calculation_seconds = _elapsed_seconds(started_at)
    try:
        project.save(update_fields=["last_calculation_seconds"])
    except Exception:
        logger.exception("Could not save calculation duration for project %s", project.pk)


def _project_table_pages_from_memo(memo):
    """_project_table_pages_from_memoを処理する。

    Args:
        memo: 解析結果の補足メモ。

    Returns:
        処理結果。
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
        処理結果。
    """
    return room.opening_area_m2 > 0 and any(
        marker in room.note for marker in ("推定", "展開図", "平面図", "スケール")
    )


def _join_memo(existing, added):
    """_join_memoを処理する。

    Args:
        existing: 既存メモ。
        added: 追加するメモ。

    Returns:
        処理結果。
    """
    if existing and added:
        return f"{existing}\n{added}"
    return existing or added


def _project_page_map(project):
    """_project_page_mapを処理する。

    Args:
        project: 処理対象の案件。

    Returns:
        処理結果。
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
    """_page_valueを処理する。

    Args:
        value: 変換または正規化する値。
        default: 値が空または不正な場合の既定値。

    Returns:
        処理結果。
    """
    normalized = (value or default).strip()
    return normalized or "ー"


def _page_choices():
    """_page_choicesを処理する。

    Returns:
        処理結果。
    """
    return ["ー", *[str(number) for number in range(100)]]


def _decimal(value, default):
    """_decimalを処理する。

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
    """_atを処理する。

    Args:
        values: 取得対象の値一覧。
        index: 取得する位置。

    Returns:
        処理結果。
    """
    return values[index] if index < len(values) else ""


def _round(value):
    """_roundを処理する。

    Args:
        value: 変換または正規化する値。

    Returns:
        小数第2位で丸めたDecimal値。
    """
    return value.quantize(Decimal("0.01"))


def _selectable_wallpapers():
    """_selectable_wallpapersを処理する。

    Returns:
        選択可能な壁紙QuerySet。
    """
    Wallpaper.ensure_defaults()
    return Wallpaper.objects.filter(is_active=True).order_by("display_order", "number")


def _posted_surface_wallpapers(post_data, default_wallpaper):
    """_posted_surface_wallpapersを処理する。

    Args:
        post_data: POSTされたフォームデータ。
        default_wallpaper: 初期適用する壁紙マスタ。

    Returns:
        処理結果。
    """
    wallpaper_map = {wallpaper.number: wallpaper for wallpaper in _selectable_wallpapers()}
    selected = {}
    for field, _label, _surface_type in SURFACE_FIELDS:
        selected[field] = wallpaper_map.get(post_data.get(f"{field}_wallpaper_no"), default_wallpaper)
    return selected


def _estimate_method(value):
    """_estimate_methodを処理する。

    Args:
        value: 変換または正規化する値。

    Returns:
        処理結果。
    """
    valid = {choice[0] for choice in ESTIMATE_METHOD_CHOICES}
    return value if value in valid else WALLPAPER_TOTAL_METHOD


def _suggested_revision_name(name):
    """_suggested_revision_nameを処理する。

    Args:
        name: 名前。

    Returns:
        処理結果。
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
        処理結果。
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
        memo=project.memo,
    )
    return clone


def _clone_rooms(source_project, target_project):
    """_clone_roomsを複製する。

    Args:
        source_project: 複製元の案件。
        target_project: 複製先の案件。

    Returns:
        処理結果。
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

# Create your views here.
