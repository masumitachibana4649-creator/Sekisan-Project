import csv
import logging
from pathlib import Path
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import close_old_connections
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from .models import (
    ESTIMATE_METHOD_CHOICES,
    ROOM_TOTAL_METHOD,
    SURFACE_FIELDS,
    WALLPAPER_TOTAL_METHOD,
    EstimateDefaultSettings,
    Project,
    Room,
    Wallpaper,
)
from .pdf_analysis import analyze_wallpaper_pdf

logger = logging.getLogger(__name__)


def dashboard(request):
    projects = Project.objects.prefetch_related("rooms")[:8]
    latest = projects[0] if projects else None
    context = {
        "projects": projects,
        "latest": latest,
    }
    return render(request, "estimator/dashboard.html", context)


def project_create(request):
    defaults = EstimateDefaultSettings.load()
    wallpapers = _selectable_wallpapers()
    default_wallpaper = defaults.default_wallpaper or Wallpaper.objects.get(number="001")
    if request.method == "POST":
        if not request.FILES.get("drawing_pdf"):
            messages.error(request, "図面PDFを選択してください。")
            return redirect("project_create")

        surface_wallpapers = _posted_surface_wallpapers(request.POST, default_wallpaper)
        first_wallpaper = surface_wallpapers["east"]
        project = Project.objects.create(
            name=request.POST.get("name") or "無題の積算",
            client_name=request.POST.get("client_name", ""),
            drawing_pdf=request.FILES.get("drawing_pdf"),
            wallpaper_roll_width_m=first_wallpaper.roll_width_m,
            wallpaper_roll_length_m=first_wallpaper.roll_length_m,
            loss_rate_percent=first_wallpaper.loss_rate_percent,
            unit_price_per_roll=first_wallpaper.unit_price_per_roll,
            page_1f_plan=_page_value(request.POST.get("page_1f_plan"), "ー"),
            page_2f_plan=_page_value(request.POST.get("page_2f_plan"), "ー"),
            page_3f_plan=_page_value(request.POST.get("page_3f_plan"), "ー"),
            page_east_elevation=_page_value(request.POST.get("page_east_elevation"), "ー"),
            page_west_elevation=_page_value(request.POST.get("page_west_elevation"), "ー"),
            page_south_elevation=_page_value(request.POST.get("page_south_elevation"), "ー"),
            page_north_elevation=_page_value(request.POST.get("page_north_elevation"), "ー"),
            page_section=_page_value(request.POST.get("page_section"), "ー"),
            memo=request.POST.get("memo", ""),
        )

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
    project = get_object_or_404(Project.objects.prefetch_related("rooms"), pk=pk)
    rooms = project.rooms.all()
    has_estimated_openings = any(_is_estimated_opening(room) for room in rooms)
    summary = project.wallpaper_summary
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
        },
    )


def project_save_wallpapers(request, pk):
    source_project = get_object_or_404(Project.objects.prefetch_related("rooms"), pk=pk)
    if request.method != "POST":
        return redirect("project_detail", pk=source_project.pk)

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
        for field, _label, _surface_type in SURFACE_FIELDS:
            selected_no = request.POST.get(
                f"room_{source_room_id}_{field}_wallpaper_no",
                getattr(target_room, f"{field}_wallpaper_no"),
            )
            wallpaper = wallpaper_map.get(selected_no)
            if wallpaper:
                target_room.apply_wallpaper(field, wallpaper)
        target_room.save()

    messages.success(request, f"{target_project.name} として壁紙設定を保存しました。")
    return redirect("project_detail", pk=target_project.pk)


def project_recalculate(request, pk):
    project = get_object_or_404(Project, pk=pk)
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
    project = get_object_or_404(Project, pk=pk)
    if not project.drawing_pdf:
        raise Http404("PDFが登録されていません。")

    try:
        pdf_file = project.drawing_pdf.open("rb")
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise Http404("PDFファイルが見つかりません。") from exc

    filename = Path(project.drawing_pdf.name).name
    response = FileResponse(pdf_file, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


def project_csv(request, pk):
    project = get_object_or_404(Project.objects.prefetch_related("rooms"), pk=pk)
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
        "部屋名",
        "周長(m)",
        "天井高(m)",
        "開口部(m2)",
        "天井(m2)",
        "東壁面壁紙",
        "西壁面壁紙",
        "南壁面壁紙",
        "北壁面壁紙",
        "天井壁紙",
        "必要面積(m2)",
        "部屋別積上方式ロール本数",
        "備考",
    ])
    for room in project.rooms.all():
        writer.writerow([
            room.name,
            room.perimeter_m,
            room.height_m,
            room.opening_area_m2,
            room.ceiling_area_m2,
            f"{room.east_wallpaper_no}：{room.east_wallpaper_name}",
            f"{room.west_wallpaper_no}：{room.west_wallpaper_name}",
            f"{room.south_wallpaper_no}：{room.south_wallpaper_name}",
            f"{room.north_wallpaper_no}：{room.north_wallpaper_name}",
            f"{room.ceiling_wallpaper_no}：{room.ceiling_wallpaper_name}",
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


def _create_rooms_from_analysis(project, analyzed_rooms, default_wallpaper=None, surface_wallpapers=None):
    default_wallpaper = default_wallpaper or Wallpaper.objects.get(number="001")
    surface_wallpapers = surface_wallpapers or {field: default_wallpaper for field, _label, _type in SURFACE_FIELDS}
    for room in analyzed_rooms:
        created = Room(
            project=project,
            name=room.name,
            perimeter_m=room.perimeter_m,
            height_m=room.height_m,
            opening_area_m2=room.opening_area_m2,
            ceiling_area_m2=room.ceiling_area_m2,
            note=room.note,
        )
        for field, _label, _surface_type in SURFACE_FIELDS:
            created.apply_wallpaper(field, surface_wallpapers.get(field, default_wallpaper))
        created.save()


def _read_pdf_into_project(request, project, replace_rooms=False, default_wallpaper=None, surface_wallpapers=None):
    if not project.drawing_pdf:
        messages.error(request, "PDF自動読取はできませんでした。理由: 図面PDFが登録されていません。")
        return False

    try:
        close_old_connections()
        analysis = analyze_wallpaper_pdf(project.drawing_pdf.path, _project_page_map(project))
        if replace_rooms:
            project.rooms.all().delete()
        _create_rooms_from_analysis(
            project,
            analysis.rooms,
            default_wallpaper=default_wallpaper,
            surface_wallpapers=surface_wallpapers,
        )
        project.memo = _join_memo(project.memo, analysis.memo)
        project.save(update_fields=["memo"])
        return True
    except ValueError as exc:
        messages.error(request, f"PDF自動読取はできませんでした。理由: {exc}")
    except Exception:
        logger.exception("Unexpected PDF analysis error for project %s", project.pk)
        messages.error(request, "PDF自動読取中に予期しないエラーが発生しました。")
    finally:
        close_old_connections()
    return False


def _is_estimated_opening(room):
    return room.opening_area_m2 > 0 and any(
        marker in room.note for marker in ("推定", "立面図", "平面図", "スケール")
    )


def _join_memo(existing, added):
    if existing and added:
        return f"{existing}\n{added}"
    return existing or added


def _project_page_map(project):
    return {
        "page_1f_plan": project.page_1f_plan,
        "page_2f_plan": project.page_2f_plan,
        "page_3f_plan": project.page_3f_plan,
        "page_east_elevation": project.page_east_elevation,
        "page_west_elevation": project.page_west_elevation,
        "page_south_elevation": project.page_south_elevation,
        "page_north_elevation": project.page_north_elevation,
        "page_section": project.page_section,
    }


def _page_value(value, default):
    normalized = (value or default).strip()
    return normalized or "ー"


def _page_choices():
    return ["ー", *[str(number) for number in range(100)]]


def _decimal(value, default):
    try:
        return Decimal(str(value or default))
    except (InvalidOperation, TypeError):
        return Decimal(default)


def _at(values, index):
    return values[index] if index < len(values) else ""


def _round(value):
    return value.quantize(Decimal("0.01"))


def _selectable_wallpapers():
    Wallpaper.ensure_defaults()
    return Wallpaper.objects.filter(is_active=True).order_by("display_order", "number")


def _posted_surface_wallpapers(post_data, default_wallpaper):
    wallpaper_map = {wallpaper.number: wallpaper for wallpaper in _selectable_wallpapers()}
    selected = {}
    for field, _label, _surface_type in SURFACE_FIELDS:
        selected[field] = wallpaper_map.get(post_data.get(f"{field}_wallpaper_no"), default_wallpaper)
    return selected


def _estimate_method(value):
    valid = {choice[0] for choice in ESTIMATE_METHOD_CHOICES}
    return value if value in valid else WALLPAPER_TOTAL_METHOD


def _suggested_revision_name(name):
    base_name = f"{name}修正"
    if not Project.objects.filter(name=base_name).exists():
        return base_name
    number = 2
    while Project.objects.filter(name=f"{base_name}{number}").exists():
        number += 1
    return f"{base_name}{number}"


def _clone_project(project, name):
    clone = Project.objects.create(
        name=name,
        client_name=project.client_name,
        drawing_pdf=project.drawing_pdf,
        wallpaper_roll_width_m=project.wallpaper_roll_width_m,
        wallpaper_roll_length_m=project.wallpaper_roll_length_m,
        loss_rate_percent=project.loss_rate_percent,
        unit_price_per_roll=project.unit_price_per_roll,
        adopted_estimate_method=_estimate_method(project.adopted_estimate_method),
        page_1f_plan=project.page_1f_plan,
        page_2f_plan=project.page_2f_plan,
        page_3f_plan=project.page_3f_plan,
        page_east_elevation=project.page_east_elevation,
        page_west_elevation=project.page_west_elevation,
        page_south_elevation=project.page_south_elevation,
        page_north_elevation=project.page_north_elevation,
        page_section=project.page_section,
        memo=project.memo,
    )
    return clone


def _clone_rooms(source_project, target_project):
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
