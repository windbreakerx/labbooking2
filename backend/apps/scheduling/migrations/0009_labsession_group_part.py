# Generated manually for labbooking2

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0008_alter_labsession_lab_work"),
    ]

    operations = [
        migrations.AddField(
            model_name="labsession",
            name="group_part",
            field=models.CharField(
                choices=[
                    ("ALL", "Вся группа"),
                    ("FIRST", "Первая половина"),
                    ("SECOND", "Вторая половина"),
                ],
                default="ALL",
                max_length=8,
                verbose_name="Часть группы",
            ),
        ),
    ]
