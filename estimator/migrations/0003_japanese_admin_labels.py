from django.db import migrations


PERMISSION_ACTION_LABELS = {
    "add": "追加できます",
    "change": "変更できます",
    "delete": "削除できます",
    "view": "表示できます",
}

MODEL_LABELS = {
    ("admin", "logentry"): "ログエントリー",
    ("auth", "group"): "グループ",
    ("auth", "permission"): "権限",
    ("auth", "user"): "ユーザー",
    ("contenttypes", "contenttype"): "コンテンツタイプ",
    ("estimator", "project"): "案件",
    ("estimator", "room"): "部屋",
    ("sessions", "session"): "セッション",
}


def translate_permission_names(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")

    for permission in Permission.objects.select_related("content_type"):
        action = permission.codename.split("_", 1)[0]
        action_label = PERMISSION_ACTION_LABELS.get(action)
        model_label = MODEL_LABELS.get(
            (permission.content_type.app_label, permission.content_type.model),
            permission.content_type.model,
        )
        if action_label:
            permission.name = f"{model_label}を{action_label}"
            permission.save(update_fields=["name"])


class Migration(migrations.Migration):
    dependencies = [
        ("estimator", "0002_alter_project_wallpaper_roll_width_m_and_more"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="project",
            options={
                "ordering": ["-updated_at"],
                "verbose_name": "案件",
                "verbose_name_plural": "案件",
            },
        ),
        migrations.AlterModelOptions(
            name="room",
            options={
                "ordering": ["id"],
                "verbose_name": "部屋",
                "verbose_name_plural": "部屋",
            },
        ),
        migrations.RunPython(translate_permission_names, migrations.RunPython.noop),
    ]
