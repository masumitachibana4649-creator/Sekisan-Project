from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("estimator", "0012_update_development_page_range"),
    ]

    operations = [
        migrations.AddField(
            model_name="room",
            name="excluded_from_summary",
            field=models.BooleanField(default=False, verbose_name="集計対象外"),
        ),
        migrations.AddField(
            model_name="room",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("ai", "AI読取"),
                    ("ai_missing", "抽出失敗"),
                    ("manual", "手動追加"),
                ],
                default="ai",
                max_length=16,
                verbose_name="部屋追加区分",
            ),
        ),
    ]
