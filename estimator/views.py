import csv
import logging
import tempfile
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
    ROOM_TOTAL_METHOD,
    SURFACE_FIELDS,
    WALLPAPER_TOTAL_METHOD,
    EstimateDefaultSettings,
    Project,
    Room,
    Wallpaper,
)
from .pdf_analysis import analyze_wallpaper_pdf
from . import storage

logger = logging.getLogger(__name__)


class StaffAwareLoginView(LoginView):
    template_name = "registration/login.html"

    def get_success_url(self):
        if self.request.user.is_staff or self.request.user.is_superuser:
            return "/admin/"
        return self.get_redirect_url() or reverse("dashboard")


def signup(request):
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
    projects = Project.objects.none()
    if request.user.is_authenticated:
        projects = Project.objects.filter(uploaded_by=request.user).prefetch_related("rooms")[:8]
    latest = projects[0] if projects else None
    context = {
        "projects": projects,
        "latest": latest,
    }
    return render(request, "estimator/dashboard.html", context)


@login_required
def project_create(request):
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
            page_1f_development=_page_value(request.POST.get("page_1f_development"), "ー"),
            page_2f_development=_page_value(request.POST.get("page_2f_development"), "ー"),
            page_3f_development=_page_value(request.POST.get("page_3f_development"), "ー"),
            page_1f_ceiling_plan=_page_value(request.POST.get("page_1f_ceiling_plan"), "ー"),
            page_2f_ceiling_plan=_page_value(request.POST.get("page_2f_ceiling_plan"), "ー"),
            page_3f_ceiling_plan=_page_value(request.POST.get("page_3f_ceiling_plan"), "ー"),
            memo=request.POST.get("memo", ""),
            **pdf_fields,
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
    project = _owned_project_or_403(request, Project.objects.prefetch_related("rooms"), pk)
    rooms = project.rooms.all()
    has_estimated_openings = any(_is_estimated_opening(room) for room in rooms)
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
            "edit_mode": edit_mode,
        },
    )


def project_save_wallpapers(request, pk):
    source_project = _owned_project_or_403(request, Project.objects.prefetch_related("rooms"), pk)
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

    messages.success(request, f"{target_project.name} として壁紙設定を保存しました。")
    return redirect("project_detail", pk=target_project.pk)


def project_recalculate(request, pk):
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


def _owned_project_or_403(request, queryset, pk):
    if not request.user.is_authenticated:
        return _raise_forbidden()
    project = get_object_or_404(queryset, pk=pk)
    if project.uploaded_by_id != request.user.pk:
        return _raise_forbidden()
    return project


def _raise_forbidden():
    raise PermissionDenied("この積算データを表示する権限がありません。")


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
        created.set_default_surface_measurements()
        created.save()


def _validate_drawing_pdf(uploaded_file):
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
    user_id = user.pk if user.is_authenticated else "anonymous"
    return f"{user_id}/{uuid.uuid4()}.pdf"


@contextmanager
def _drawing_pdf_path(project):
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
    if not project.has_drawing_pdf:
        messages.error(request, "PDF自動読取はできませんでした。理由: 図面PDFが登録されていません。")
        return False

    try:
        close_old_connections()
        with _drawing_pdf_path(project) as pdf_path:
            analysis = analyze_wallpaper_pdf(pdf_path, _project_page_map(project))
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
        marker in room.note for marker in ("推定", "展開図", "平面図", "スケール")
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
        "page_1f_development": project.page_1f_development,
        "page_2f_development": project.page_2f_development,
        "page_3f_development": project.page_3f_development,
        "page_1f_ceiling_plan": project.page_1f_ceiling_plan,
        "page_2f_ceiling_plan": project.page_2f_ceiling_plan,
        "page_3f_ceiling_plan": project.page_3f_ceiling_plan,
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
        page_1f_development=project.page_1f_development,
        page_2f_development=project.page_2f_development,
        page_3f_development=project.page_3f_development,
        page_1f_ceiling_plan=project.page_1f_ceiling_plan,
        page_2f_ceiling_plan=project.page_2f_ceiling_plan,
        page_3f_ceiling_plan=project.page_3f_ceiling_plan,
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
