from decimal import Decimal

import pytest

from apps.auditoria.models import LogAuditoria
from apps.clientes.models import Cliente
from apps.cobranza.models import Deuda, TransaccionQR, TransicionEstadoInvalida
from apps.usuarios.models import Usuario


@pytest.fixture
def usuario(db):
    return Usuario.objects.create_user(username="cajera1", password="x", rol=Usuario.Rol.CAJERA)


@pytest.fixture
def transaccion(db, usuario):
    cliente = Cliente.objects.create(codigo_externo="C-001", nombre="Cliente Test")
    deuda = Deuda.objects.create(cliente=cliente, monto=Decimal("150.00"))
    return TransaccionQR.objects.create(deuda=deuda, usuario=usuario, monto_snapshot=deuda.monto)


@pytest.mark.django_db
class TestTransicionesTransaccionQR:
    def test_transicion_valida_actualiza_estado_y_registra_auditoria(self, transaccion, usuario):
        transaccion.transicionar_estado(TransaccionQR.Estado.PENDIENTE_CONFIRMACION, usuario=usuario)
        transaccion.refresh_from_db()

        assert transaccion.estado == TransaccionQR.Estado.PENDIENTE_CONFIRMACION
        assert LogAuditoria.objects.filter(
            entidad_afectada=f"TransaccionQR:{transaccion.pk}",
            accion__contains="generado -> pendiente_confirmacion",
        ).exists()

    def test_no_permite_pasar_de_pagado_a_generado(self, transaccion, usuario):
        transaccion.transicionar_estado(TransaccionQR.Estado.PENDIENTE_CONFIRMACION, usuario=usuario)
        transaccion.transicionar_estado(TransaccionQR.Estado.PAGADO, usuario=usuario)

        with pytest.raises(TransicionEstadoInvalida):
            transaccion.transicionar_estado(TransaccionQR.Estado.GENERADO, usuario=usuario)

    def test_estados_terminales_no_permiten_ninguna_transicion(self, transaccion, usuario):
        transaccion.transicionar_estado(TransaccionQR.Estado.VENCIDO, usuario=usuario)

        with pytest.raises(TransicionEstadoInvalida):
            transaccion.transicionar_estado(TransaccionQR.Estado.PENDIENTE_CONFIRMACION, usuario=usuario)

    def test_no_permite_modificar_estado_directamente_con_save(self, transaccion):
        transaccion.estado = TransaccionQR.Estado.PAGADO
        with pytest.raises(TransicionEstadoInvalida):
            transaccion.save()
