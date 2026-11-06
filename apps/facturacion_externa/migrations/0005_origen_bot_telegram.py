from django.db import migrations, models


def marcar_pagos_del_bot(apps, schema_editor):
    """Los pagos del bot que entraron antes de este cambio quedaron como web y sin banco (el bot
    cobra solo con SIP/BISA)."""
    SolicitudLiquidacion = apps.get_model("facturacion_externa", "SolicitudLiquidacion")
    del_bot = SolicitudLiquidacion.objects.filter(alias__startswith="CESSA-BOT-")
    del_bot.update(origen="bot_telegram")
    del_bot.filter(banco="").update(banco="sip_bisa")


class Migration(migrations.Migration):

    dependencies = [
        ('facturacion_externa', '0004_descartado'),
    ]

    operations = [
        migrations.AlterField(
            model_name='solicitudliquidacion',
            name='origen',
            field=models.CharField(choices=[('cessa_laravel_web', 'cessa-laravel (pago QR web)'), ('bot_telegram', 'bot-telegram (pago QR por Telegram)')], default='cessa_laravel_web', max_length=30),
        ),
        migrations.RunPython(marcar_pagos_del_bot, migrations.RunPython.noop),
    ]
