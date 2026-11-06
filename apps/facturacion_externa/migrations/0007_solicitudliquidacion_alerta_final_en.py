from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facturacion_externa', '0006_avisosip_origen_panel'),
    ]

    operations = [
        migrations.AddField(
            model_name='solicitudliquidacion',
            name='alerta_final_en',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
