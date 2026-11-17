from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("kiosco", "0004_enviofactura"),
    ]

    operations = [
        migrations.CreateModel(
            name="PantallaKiosco",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("nombre", models.CharField(max_length=50, unique=True)),
                ("creada_en", models.DateTimeField(auto_now_add=True)),
                ("ultimo_contacto", models.DateTimeField(blank=True, null=True)),
                ("revisada_en", models.DateTimeField(blank=True, null=True)),
                ("problemas_avisados", models.CharField(blank=True, default="", max_length=500)),
                ("problemas_nuevos", models.CharField(blank=True, default="", max_length=500)),
                ("veces_nuevos", models.PositiveSmallIntegerField(default=0)),
            ],
        ),
    ]
