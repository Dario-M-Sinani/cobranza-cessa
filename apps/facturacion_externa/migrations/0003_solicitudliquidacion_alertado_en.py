from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facturacion_externa', '0002_solicitudliquidacion_banco'),
    ]

    operations = [
        migrations.AddField(
            model_name='solicitudliquidacion',
            name='alertado_en',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
