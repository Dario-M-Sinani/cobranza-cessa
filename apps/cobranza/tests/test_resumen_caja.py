from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.clientes.models import Cliente
from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Caja, Deuda
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
def otra_cajera(db):
    return Usuario.objects.create_user(username="cajera2", password="x", rol=Usuario.Rol.CAJERA, activo=True)


@pytest.fixture
def supervisor(db):
    return Usuario.objects.create_user(username="sup1", password="x", rol=Usuario.Rol.SUPERVISOR)


@pytest.fixture
def cliente_deuda(db):
    return Cliente.objects.create(codigo_externo="C-CAJA-1", nombre="Cliente Caja")


@pytest.mark.django_db
class TestResumenCaja:
    def test_qr_y_efectivo_quedan_vinculados_a_la_caja_abierta(self, fake_mc4, cajera, supervisor, cliente_deuda):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)

        deuda_qr = Deuda.objects.create(cliente=cliente_deuda, monto=Decimal("100.00"))
        transaccion = cobranza_services.generar_transaccion_qr(deuda=deuda_qr, usuario=cajera)
        cobranza_services.verificar_pago(transaccion)  # queda PAGADO con el fake

        deuda_efectivo = Deuda.objects.create(cliente=cliente_deuda, monto=Decimal("50.00"))
        cobranza_services.registrar_cobro_efectivo(deuda=deuda_efectivo, usuario=cajera, monto_recibido=Decimal("50.00"))

        transaccion.refresh_from_db()
        assert transaccion.caja == caja

        # El resumen es exclusivo de supervisor/admin (ver test de abajo) --
        # se consulta con supervisor acá para verificar el contenido.
        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.get(f"/api/cajas/{caja.pk}/resumen/")

        assert response.status_code == 200
        assert response.data["monto_total"] == "150.00"
        assert response.data["cantidad_cobros"] == 2
        assert response.data["por_forma_pago"]["qr"] == {"cantidad": 1, "monto": "100.00"}
        assert response.data["por_forma_pago"]["efectivo"] == {"cantidad": 1, "monto": "50.00"}
        assert response.data["facturas_registradas"] == 2
        assert response.data["qr_pendientes_de_confirmar"] == 0

    def test_qr_generado_sin_confirmar_cuenta_como_pendiente(self, fake_mc4, cajera, supervisor, cliente_deuda):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        deuda = Deuda.objects.create(cliente=cliente_deuda, monto=Decimal("80.00"))
        cobranza_services.generar_transaccion_qr(deuda=deuda, usuario=cajera)  # queda pendiente_confirmacion

        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.get(f"/api/cajas/{caja.pk}/resumen/")

        assert response.status_code == 200
        assert response.data["monto_total"] == "0.00"
        assert response.data["qr_pendientes_de_confirmar"] == 1

    def test_sin_caja_abierta_el_cobro_no_queda_vinculado_a_ninguna(self, fake_mc4, cajera, cliente_deuda):
        deuda = Deuda.objects.create(cliente=cliente_deuda, monto=Decimal("40.00"))
        cobro = cobranza_services.registrar_cobro_efectivo(deuda=deuda, usuario=cajera, monto_recibido=Decimal("40.00"))
        assert cobro.caja is None

    def test_cajera_no_puede_ver_resumen_de_caja_ajena(self, cajera, otra_cajera):
        caja_ajena = Caja.abrir(cajero=otra_cajera, usuario=otra_cajera)

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.get(f"/api/cajas/{caja_ajena.pk}/resumen/")
        assert response.status_code == 403

    def test_cajera_no_puede_ver_el_resumen_ni_de_su_propia_caja(self, cajera):
        """A pedido del usuario 2026-09-07: el cajero no debe poder ver el
        total recaudado, ni siquiera de su propio turno."""
        caja = Caja.abrir(cajero=cajera, usuario=cajera)

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.get(f"/api/cajas/{caja.pk}/resumen/")
        assert response.status_code == 403

    def test_supervisor_puede_ver_resumen_de_cualquier_caja(self, supervisor, cajera):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)

        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.get(f"/api/cajas/{caja.pk}/resumen/")
        assert response.status_code == 200
