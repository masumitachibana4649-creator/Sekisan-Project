import csv
import logging
from pathlib import Path
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import close_old_connections
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from .models import EstimateDefaultSettings, Project, Room
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
    if request.method == "POST":
        auto_read_pdf = request.POST.get("auto_read_pdf") == "on"
        project = Project.objects.create(
            name=request.POST.get("name") or "無題の積算",
            client_name=request.POST.get("client_name", ""),
            drawing_pdf=request.FILES.get("drawing_pdf"),
            wallpaper_roll_width_m=_decimal(request.POST.get("wallpaper_roll_width_m"), defaults.wallpaper_roll_width_m),
            wallpaper_roll_length_m=_decimal(request.POST.get("wallpaper_roll_length_m"), defaults.wallpaper_roll_length_m),
            loss_rate_percent=_decimal(request.POST.get("loss_rate_percent"), defaults.loss_rate_percent),
            unit_price_per_roll=int(request.POST.get("unit_price_per_roll") or defaults.unit_price_per_roll),
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

        if auto_read_pdf and project.drawing_pdf:
            try:
                close_old_connections()
                analysis = analyze_wallpaper_pdf(project.drawing_pdf.path, _project_page_map(project))
                close_old_connections()
                _create_rooms_from_analysis(project, analysis.rooms)
                project.memo = _join_memo(project.memo, analysis.memo)
                project.save(update_fields=["memo"])
                messages.success(request, "PDF図面から部屋情報を読み取り、積算を作成しました。")
                return redirect("project_detail", pk=project.pk)
            except ValueError as exc:
                messages.warning(request, f"PDF自動読取はできませんでした。手入力の部屋情報で計算します。理由: {exc}")
            except Exception:
                logger.exception("Unexpected PDF analysis error for project %s", project.pk)
                messages.warning(
                    request,
                    "PDF自動読取中に予期しないエラーが発生しました。手入力の部屋情報で計算します。",
                )

        names = request.POST.getlist("room_name")
        perimeters = request.POST.getlist("perimeter_m")
        heights = request.POST.getlist("height_m")
        openings = request.POST.getlist("opening_area_m2")
        ceilings = request.POST.getlist("ceiling_area_m2")
        notes = request.POST.getlist("note")

        created_rooms = 0
        for index, room_name in enumerate(names):
            perimeter = _decimal(_at(perimeters, index), "0")
            if not room_name and perimeter == 0:
                continue
            Room.objects.create(
                project=project,
                name=room_name or f"部屋{index + 1}",
                perimeter_m=perimeter,
                height_m=_decimal(_at(heights, index), "2.4"),
                opening_area_m2=_decimal(_at(openings, index), "0"),
                ceiling_area_m2=_decimal(_at(ceilings, index), "0"),
                note=_at(notes, index),
            )
            created_rooms += 1

        if created_rooms == 0:
            Room.objects.create(
                project=project,
                name="LDK",
                perimeter_m=Decimal("18.00"),
                height_m=Decimal("2.40"),
                opening_area_m2=Decimal("4.20"),
                ceiling_area_m2=Decimal("20.00"),
                note="サンプル値",
            )
            messages.info(request, "部屋入力が空だったため、サンプル行で積算しました。")
        else:
            messages.success(request, "積算を作成しました。")
        return redirect("project_detail", pk=project.pk)

    return render(
        request,
        "estimator/project_form.html",
        {
            "defaults": defaults,
            "page_choices": _page_choices(),
            "default_page": "ー",
        },
    )


def project_detail(request, pk):
    project = get_object_or_404(Project.objects.prefetch_related("rooms"), pk=pk)
    rooms = project.rooms.all()
    return render(request, "estimator/project_detail.html", {"project": project, "rooms": rooms})


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
    writer.writerow(["ロール幅(m)", project.wallpaper_roll_width_m])
    writer.writerow(["ロール長さ(m)", project.wallpaper_roll_length_m])
    writer.writerow(["ロス率(%)", project.loss_rate_percent])
    writer.writerow([])
    writer.writerow(["部屋名", "周長(m)", "天井高(m)", "開口部(m2)", "天井(m2)", "必要面積(m2)", "ロール本数", "備考"])
    for room in project.rooms.all():
        writer.writerow([
            room.name,
            room.perimeter_m,
            room.height_m,
            room.opening_area_m2,
            room.ceiling_area_m2,
            _round(room.total_area),
            room.rolls_required,
            room.note,
        ])
    writer.writerow([])
    writer.writerow(["合計必要面積(m2)", _round(project.total_area)])
    writer.writerow(["合計ロール本数", project.total_rolls])
    writer.writerow(["概算金額(円)", project.total_cost])
    return response


def _create_rooms_from_analysis(project, analyzed_rooms):
    for room in analyzed_rooms:
        Room.objects.create(
            project=project,
            name=room.name,
            perimeter_m=room.perimeter_m,
            height_m=room.height_m,
            opening_area_m2=room.opening_area_m2,
            ceiling_area_m2=room.ceiling_area_m2,
            note=room.note,
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

# Create your views here.
