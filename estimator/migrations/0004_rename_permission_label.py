from django.db import migrations


def rename_permission_label(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    Permission.objects.filter(
        content_type__app_label="auth",
        content_type__model="permission",
        name__contains="パーミッション",
    ).update(name="権限を追加できます")

    labels = {
        "add_permission": "権限を追加できます",
        "change_permission": "権限を変更できます",
        "delete_permission": "権限を削除できます",
        "view_permission": "権限を表示できます",
    }
    for codename, name in labels.items():
        Permission.objects.filter(
            content_type__app_label="auth",
            content_type__model="permission",
            codename=codename,
        ).update(name=name)


class Migration(migrations.Migration):
    dependencies = [
        ("estimator", "0003_japanese_admin_labels"),
    ]

    operations = [
        migrations.RunPython(rename_permission_label, migrations.RunPython.noop),
    ]
