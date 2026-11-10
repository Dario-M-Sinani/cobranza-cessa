import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("kiosco", "0003_pagokiosco_whatsapp_enviados"),
    ]

    operations = [
        migrations.CreateModel(
            name="EnvioFactura",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("canal", models.CharField(choices=[("correo", "Correo"), ("whatsapp", "WhatsApp")], max_length=10)),
                ("destino", models.CharField(max_length=254)),
                ("registrado", models.BooleanField(default=False)),
                ("exitoso", models.BooleanField(default=True)),
                ("error", models.CharField(blank=True, default="", max_length=300)),
                ("enviado_en", models.DateTimeField(auto_now_add=True)),
                ("pago", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="envios", to="kiosco.pagokiosco")),
            ],
            options={"ordering": ["enviado_en"]},
        ),
    ]
