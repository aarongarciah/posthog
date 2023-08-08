# Generated by Django 3.2.19 on 2023-07-11 19:30

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("posthog", "0335_alter_asyncdeletion_deletion_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="survey",
            name="type",
            field=models.CharField(
                choices=[
                    ("popover", "popover"),
                    ("button", "button"),
                    ("email", "email"),
                    ("full_screen", "full screen"),
                    ("api", "api"),
                ],
                max_length=40,
            ),
        ),
    ]
