import django.core.serializers.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cobranza', '0006_deuda_items_snapshot_factura_cobranzas_uuid_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='transaccionqr',
            name='items_cobrados',
            field=models.JSONField(blank=True, default=list, encoder=django.core.serializers.json.DjangoJSONEncoder),
        ),
        migrations.AddField(
            model_name='cobroefectivo',
            name='items_cobrados',
            field=models.JSONField(blank=True, default=list, encoder=django.core.serializers.json.DjangoJSONEncoder),
        ),
    ]
