from decimal import Decimal

from django.db import migrations


def create_default_settings(apps, schema_editor):
    EstimateDefaultSettings = apps.get_model("estimator", "EstimateDefaultSettings")
    EstimateDefaultSettings.objects.get_or_create(
        pk=1,
        defaults={
            "wallpaper_roll_width_m": Decimal("0.92"),
            "wallpaper_roll_length_m": Decimal("50"),
            "loss_rate_percent": Decimal("8"),
            "unit_price_per_roll": 11800,
        },
    )

    Permission = apps.get_model("auth", "Permission")
    labels = {
        "add_estimatedefaultsettings": "初期値設定を追加できます",
        "change_estimatedefaultsettings": "初期値設定を変更できます",
        "delete_estimatedefaultsettings": "初期値設定を削除できます",
        "view_estimatedefaultsettings": "初期値設定を表示できます",
    }
    for codename, name in labels.items():
        Permission.objects.filter(
            content_type__app_label="estimator",
            content_type__model="estimatedefaultsettings",
            codename=codename,
        ).update(name=name)


class Migration(migrations.Migration):
    dependencies = [
        ("estimator", "0006_estimatedefaultsettings_alter_project_page_1f_plan_and_more"),
    ]

    operations = [
        migrations.RunPython(create_default_settings, migrations.RunPython.noop),
    ]
