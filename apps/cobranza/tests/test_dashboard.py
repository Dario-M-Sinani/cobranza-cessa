from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.clientes.models import Cliente
from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Deuda, TransaccionQR
from apps.usuarios.models import Usuario
from services.mc4_client import EstadoPagoMC4, EstadoTransaccionQR, MC4ClientInterface, ResultadoQR


class FakeMC4Client(MC4ClientInterface):
    def generar_qr(self, solicitud):
        return ResultadoQR(
            imagen_qr_base64="ZmFrZQ==", id_qr="qr-1", id_transaccion="op-1",
            fecha_vencimiento=None, banco_destino="BISA", cuenta_destino="0000",
        )

    def inhabilitar_qr(self, alias):
        pass

    def consultar_estado(self, alias):
        return EstadoTransaccionQR(
            alias=alias, estado=EstadoPagoMC4.PAGADO, fecha_procesamiento=None,
            monto=None, numero_orden_originante=None, id_qr=None,
        )


@pytest.fixture
def fake_mc4(monkeypatch):
    cliente = FakeMC4Client()
    monkeypatch.setattr(cobranza_services, "get_mc4_client", lambda: cliente)
    return cliente


@pytest.fixture
def cajera(db):
    return Usuario.objects.create_user(username="cajera1", password="x", rol=Usuario.Rol.CAJERA, activo=True)


@pytest.fixture
def supervisor(db):
    return Usuario.objects.create_user(username="sup1", password="x", rol=Usuario.Rol.SUPERVISOR)


@pytest.fixture
def deuda(db):
    cliente = Cliente.objects.create(codigo_externo="C-300", nombre="Cliente Dashboard")
    return Deuda.objects.create(cliente=cliente, monto=Decimal("100.00"))


@pytest.mark.django_db
class TestDashboardResumen:
    def test_cajera_no_puede_ver_el_dashboard(self, cajera):
        client = APIClient()
        client.force_authenticate(cajera)
        response = client.get("/api/dashboard/resumen/")
        assert response.status_code == 403

    def test_resumen_combina_qr_pagado_y_efectivo(self, fake_mc4, cajera, supervisor, deuda):
        transaccion = cobranza_services.generar_transaccion_qr(deuda=deuda, usuario=cajera)
        cobranza_services.verificar_pago(transaccion)  # queda PAGADO con el fake

        deuda2 = Deuda.objects.create(cliente=deuda.cliente, monto=Decimal("50.00"))
        cobranza_services.registrar_cobro_efectivo(deuda=deuda2, usuario=cajera, monto_recibido=Decimal("50.00"))

        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.get("/api/dashboard/resumen/")

        assert response.status_code == 200
        assert response.data["monto_total"] == "150.00"
        assert response.data["cantidad_cobros"] == 2
        assert response.data["por_forma_pago"]["qr"] == {"cantidad": 1, "monto": "100.00"}
        assert response.data["por_forma_pago"]["efectivo"] == {"cantidad": 1, "monto": "50.00"}
        assert response.data["facturas_registradas"] == 2
        assert response.data["por_cajero"] == [
            {"cajero": "cajera1", "cantidad_cobros": 2, "monto_total": "150.00"}
        ]

    def test_rango_de_fechas_invalido_devuelve_400(self, supervisor):
        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.get("/api/dashboard/resumen/?desde=2026-09-10&hasta=2026-09-01")
        assert response.status_code == 400

    def test_fuera_del_rango_no_cuenta(self, fake_mc4, cajera, supervisor, deuda):
        cobranza_services.registrar_cobro_efectivo(deuda=deuda, usuario=cajera, monto_recibido=deuda.monto)

        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.get("/api/dashboard/resumen/?desde=2020-01-01&hasta=2020-01-02")

        assert response.status_code == 200
        assert response.data["monto_total"] == "0.00"
        assert response.data["cantidad_cobros"] == 0
