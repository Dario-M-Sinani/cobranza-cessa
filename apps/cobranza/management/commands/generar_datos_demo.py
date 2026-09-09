"""Genera usuarios y datos de ejemplo para desarrollo/demo: no reemplaza la
integración real con MC4/SIIC (sigue pendiente, ver README) -- esto solo
puebla la base con transacciones ya `pagado` + `Factura` `enviado`, para
poder probar listados, permisos y la exportación a CSV sin esperar a tener
credenciales reales.

Uso: python manage.py generar_datos_demo [--cantidad 40]
"""
import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.clientes.models import Cliente
from apps.cobranza.models import Deuda, Factura, TransaccionQR
from apps.cobranza.services import crear_factura
from apps.usuarios.models import Usuario
from services.fakes import NOMBRES_DEMO

CONTRASENA_DEMO = "Demo12345!"


class Command(BaseCommand):
    help = "Crea usuarios de prueba (cajera/supervisor/admin) y N transacciones QR ya pagadas y facturadas."

    def add_arguments(self, parser):
        parser.add_argument("--cantidad", type=int, default=40)

    def handle(self, *args, **options):
        cantidad = options["cantidad"]

        cajera = self._usuario_demo("cajera_demo", Usuario.Rol.CAJERA, activo=True)
        self._usuario_demo("supervisor_demo", Usuario.Rol.SUPERVISOR)
        self._usuario_demo("admin_demo", Usuario.Rol.ADMIN, superusuario=True)

        creados = 0
        for i in range(cantidad):
            nombre = NOMBRES_DEMO[i % len(NOMBRES_DEMO)]
            codigo = f"DEMO-{2000 + i}"
            cliente, _ = Cliente.objects.get_or_create(
                codigo_externo=codigo,
                defaults={"nombre": nombre, "nit_ci": str(random.randint(1_000_000, 9_999_999))},
            )

            monto = Decimal(random.randrange(5000, 80000)) / 100  # Bs. 50.00 - 800.00
            deuda = Deuda.objects.create(cliente=cliente, monto=monto)

            transaccion = TransaccionQR.objects.create(deuda=deuda, usuario=cajera, monto_snapshot=monto)
            transaccion.id_operacion_mc4 = f"demo-op-{transaccion.pk}"
            transaccion.save(update_fields=["id_operacion_mc4"])
            transaccion.transicionar_estado(TransaccionQR.Estado.PENDIENTE_CONFIRMACION, usuario=cajera)
            transaccion.transicionar_estado(TransaccionQR.Estado.PAGADO, usuario=cajera)

            factura = crear_factura(transaccion)
            fecha = timezone.now() - timedelta(days=random.randint(0, 30), hours=random.randint(0, 23))
            factura.numero_factura = f"FAC-2026-{i + 1:05d}"
            factura.estado_envio = Factura.EstadoEnvio.ENVIADO
            factura.emitida_en = fecha
            factura.save(update_fields=["numero_factura", "estado_envio", "emitida_en"])

            # auto_now_add no se puede pisar en save(); .update() sí, y así
            # las fechas quedan repartidas en el último mes en vez de todas
            # con el mismo timestamp de "ahora".
            TransaccionQR.objects.filter(pk=transaccion.pk).update(creado_en=fecha, actualizado_en=fecha)

            creados += 1

        self.stdout.write(self.style.SUCCESS(f"Listo: {creados} transacciones pagadas y facturadas de ejemplo."))
        self.stdout.write(f"Credenciales de prueba (todas con contraseña '{CONTRASENA_DEMO}'):")
        self.stdout.write("  cajera_demo / supervisor_demo / admin_demo")

    def _usuario_demo(self, username, rol, activo=False, superusuario=False):
        usuario, creado = Usuario.objects.get_or_create(
            username=username,
            defaults={"rol": rol, "activo": activo, "is_staff": superusuario, "is_superuser": superusuario},
        )
        if creado or not usuario.has_usable_password():
            usuario.set_password(CONTRASENA_DEMO)
            usuario.save()
        return usuario
