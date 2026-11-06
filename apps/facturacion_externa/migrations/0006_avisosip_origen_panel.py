import django.core.serializers.json
from django.db import migrations, models


def marcar_qr_del_panel_web(apps, schema_editor):
    SolicitudLiquidacion = apps.get_model("facturacion_externa", "SolicitudLiquidacion")
    SolicitudLiquidacion.objects.filter(alias__startswith="CESSA-ADMIN-").update(origen="cessa_laravel_panel")


class Migration(migrations.Migration):

    dependencies = [
        ('facturacion_externa', '0005_origen_bot_telegram'),
    ]

    operations = [
        migrations.AlterField(
            model_name='solicitudliquidacion',
            name='origen',
            field=models.CharField(choices=[('cessa_laravel_web', 'cessa-laravel (pago QR web)'), ('cessa_laravel_panel', 'cessa-laravel (QR generado desde el panel)'), ('bot_telegram', 'bot-telegram (pago QR por Telegram)')], default='cessa_laravel_web', max_length=30),
        ),
        migrations.CreateModel(
            name='AvisoSip',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('alias', models.CharField(db_index=True, max_length=100)),
                ('datos', models.JSONField(encoder=django.core.serializers.json.DjangoJSONEncoder)),
                ('recibido_en', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['-recibido_en'],
            },
        ),
        migrations.RunPython(marcar_qr_del_panel_web, migrations.RunPython.noop),
    ]
