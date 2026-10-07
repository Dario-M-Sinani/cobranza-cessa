import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cobranza', '0007_items_cobrados'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CobroAgrupado',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('monto_total', models.DecimalField(decimal_places=2, max_digits=12)),
                ('monto_recibido', models.DecimalField(decimal_places=2, max_digits=12)),
                ('vuelto', models.DecimalField(decimal_places=2, max_digits=12)),
                ('creado_en', models.DateTimeField(auto_now_add=True)),
                ('caja', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='cobros_agrupados', to='cobranza.caja')),
                ('usuario', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='cobros_agrupados', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-creado_en'],
            },
        ),
        migrations.AddField(
            model_name='cobroefectivo',
            name='grupo',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='cobros', to='cobranza.cobroagrupado'),
        ),
    ]
