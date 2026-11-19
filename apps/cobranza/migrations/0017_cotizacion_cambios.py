import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cobranza', '0016_caja_sin_cerrar'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='cotizaciondolar',
            name='carga_error',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.CreateModel(
            name='CotizacionCambio',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fecha', models.DateField(db_index=True)),
                ('accion', models.CharField(choices=[('carga_automatica', 'Carga automática (BCB)'), ('carga_manual', 'Carga manual'), ('correccion', 'Corrección')], max_length=20)),
                ('valor_anterior', models.DecimalField(blank=True, decimal_places=4, max_digits=10, null=True)),
                ('valor_nuevo', models.DecimalField(decimal_places=4, max_digits=10)),
                ('origen', models.CharField(blank=True, default='', max_length=255)),
                ('motivo', models.TextField(blank=True, default='')),
                ('tabla', models.CharField(blank=True, default='', max_length=40)),
                ('creado_en', models.DateTimeField(auto_now_add=True)),
                ('usuario', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-creado_en'],
            },
        ),
    ]
