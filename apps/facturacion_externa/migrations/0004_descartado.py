from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facturacion_externa', '0003_solicitudliquidacion_alertado_en'),
    ]

    operations = [
        migrations.AddField(
            model_name='solicitudliquidacion',
            name='nota_descarte',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AlterField(
            model_name='solicitudliquidacion',
            name='estado',
            field=models.CharField(choices=[('pendiente', 'Pendiente'), ('facturado', 'Facturado'), ('error', 'Error'), ('descartado', 'Descartado')], default='pendiente', max_length=20),
        ),
    ]
