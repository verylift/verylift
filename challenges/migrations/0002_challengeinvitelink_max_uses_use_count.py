from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("challenges", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="challengeinvitelink",
            name="max_uses",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="challengeinvitelink",
            name="use_count",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
