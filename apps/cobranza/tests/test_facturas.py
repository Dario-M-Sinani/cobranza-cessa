from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.clientes.models import Cliente
from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Deuda, Factura, TransaccionQR
from apps.usuarios.models import Usuario
from services.cobranzas_banco_client import CobranzasBancoClientInterface, CobranzasBancoError
from services.mc4_client import EstadoPagoMC4, EstadoTransaccionQR, MC4ClientInterface, ResultadoQR


class FakeMC4Client(MC4ClientInterface):
    """Mismo doble de prueba que ya usa test_api.py -- este módulo no
    comparte fixtures con ese vía conftest (no hay uno en este paquete de
    tests), así que se duplica acá siguiendo la misma convención."""

    def __init__(self):
        self.estado_a_devolver = EstadoPagoMC4.PAGADO

    def generar_qr(self, solicitud):
        return ResultadoQR(
            imagen_qr_base64="ZmFrZQ==", id_qr="qr-1", id_transaccion="op-1",
            fecha_vencimiento=None, banco_destino="BISA", cuenta_destino="0000",
        )

    def inhabilitar_qr(self, alias):
        pass

    def consultar_estado(self, alias):
        return EstadoTransaccionQR(
            alias=alias, estado=self.estado_a_devolver, fecha_procesamiento=None,
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
def cliente(db):
    return Cliente.objects.create(codigo_externo="C-FAC-1", nombre="Cliente Facturas")


def _item_deuda_snapshot(**overrides):
    item = {
        "codigo_sucursal": "01", "nro_comprobante": "1", "nro_suministro": "123",
        "fecha": "20260101", "tipo": "1", "letra_comprobante": "A", "nro_autorizacion": "0",
        "nro_cliente": "C-FAC-1", "anio": 2026, "mes": 1, "importe": "100.00",
        "detalle": "Consumo de energía", "debito_credito": "DEBITO",
    }
    item.update(overrides)
    return item


class FakeCobranzasBancoClientDeTest(CobranzasBancoClientInterface):
    def __init__(self, falla_en_pagar=False):
        self.falla_en_pagar = falla_en_pagar
        self.llamadas_crear_transaccion = 0
        self.llamadas_pagar_propia = 0

    def asegurar_caja_abierta(self):
        return None

    def crear_transaccion(self):
        self.llamadas_crear_transaccion += 1
        return "uuid-panel-test"

    def pagar_transaccion(self, uuid, detalle, documento):
        raise NotImplementedError("el panel usa pagar_transaccion_propia, no pagar_transaccion")

    def pagar_transaccion_propia(self, uuid, detalle):
        self.llamadas_pagar_propia += 1
        if self.falla_en_pagar:
            raise CobranzasBancoError("api-cobranzas-bancos rechazó el pago")

    def obtener_comprobante_pdf(self, uuid):
        return b"%PDF-1.4 (test panel)"

    def obtener_comprobante_json(self, uuid):
        return {"nro_factura": f"F-{uuid}"}


@pytest.mark.django_db
class TestFacturaViewSetAlcancePorRol:
    def test_cajera_ve_su_propia_factura_de_efectivo(self, cajera, cliente):
        """Regresión del bug real corregido 2026-09-07: get_queryset()
        filtraba solo por transaccion_qr__usuario, así que una Factura
        originada en un cobro en efectivo (transaccion_qr=None) nunca
        aparecía para la cajera dueña de ese cobro."""
        deuda = Deuda.objects.create(cliente=cliente, monto=Decimal("100.00"))
        cobranza_services.registrar_cobro_efectivo(deuda=deuda, usuario=cajera, monto_recibido=Decimal("100.00"))

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.get("/api/facturas/")

        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]["cobro_efectivo"] is not None
        assert response.data[0]["transaccion_qr"] is None

    def test_cajera_no_ve_facturas_de_efectivo_ajenas(self, cajera, otra_cajera, cliente):
        deuda = Deuda.objects.create(cliente=cliente, monto=Decimal("100.00"))
        cobranza_services.registrar_cobro_efectivo(deuda=deuda, usuario=otra_cajera, monto_recibido=Decimal("100.00"))

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.get("/api/facturas/")

        assert response.status_code == 200
        assert response.data == []

    def test_supervisor_ve_facturas_de_efectivo_de_cualquier_cajera(self, cajera, supervisor, cliente):
        deuda = Deuda.objects.create(cliente=cliente, monto=Decimal("100.00"))
        cobranza_services.registrar_cobro_efectivo(deuda=deuda, usuario=cajera, monto_recibido=Decimal("100.00"))

        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.get("/api/facturas/")

        assert response.status_code == 200
        assert len(response.data) == 1


@pytest.mark.django_db
class TestEnviarFacturaASiic:
    def _factura_con_snapshot(self, cajera, cliente, **overrides_deuda):
        deuda = Deuda.objects.create(
            cliente=cliente, monto=Decimal("100.00"), items_snapshot=[_item_deuda_snapshot()], **overrides_deuda
        )
        cobro = cobranza_services.registrar_cobro_efectivo(
            deuda=deuda, usuario=cajera, monto_recibido=Decimal("100.00")
        )
        return Factura.objects.get(cobro_efectivo=cobro)

    def test_envio_exitoso(self, cajera, cliente, monkeypatch):
        factura = self._factura_con_snapshot(cajera, cliente)
        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(cobranza_services, "get_cobranzas_banco_client", lambda: fake)

        factura = cobranza_services.enviar_factura_a_siic(factura)

        assert factura.estado_envio == Factura.EstadoEnvio.ENVIADO, factura.error
        assert factura.cobranzas_uuid == "uuid-panel-test"
        assert factura.numero_factura == "F-uuid-panel-test"
        assert bytes(factura.comprobante_pdf) == b"%PDF-1.4 (test panel)"
        assert factura.emitida_en is not None
        assert fake.llamadas_pagar_propia == 1

    def test_sin_items_snapshot_queda_en_error_sin_llamar_al_banco(self, cajera, cliente, monkeypatch):
        deuda = Deuda.objects.create(cliente=cliente, monto=Decimal("100.00"), items_snapshot=[])
        cobro = cobranza_services.registrar_cobro_efectivo(
            deuda=deuda, usuario=cajera, monto_recibido=Decimal("100.00")
        )
        factura = Factura.objects.get(cobro_efectivo=cobro)
        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(cobranza_services, "get_cobranzas_banco_client", lambda: fake)

        factura = cobranza_services.enviar_factura_a_siic(factura)

        assert factura.estado_envio == Factura.EstadoEnvio.ERROR
        assert "items_snapshot" in factura.error
        assert fake.llamadas_crear_transaccion == 0

    def test_fallo_al_pagar_queda_en_error(self, cajera, cliente, monkeypatch):
        factura = self._factura_con_snapshot(cajera, cliente)
        fake = FakeCobranzasBancoClientDeTest(falla_en_pagar=True)
        monkeypatch.setattr(cobranza_services, "get_cobranzas_banco_client", lambda: fake)

        factura = cobranza_services.enviar_factura_a_siic(factura)

        assert factura.estado_envio == Factura.EstadoEnvio.ERROR
        assert factura.intentos == 1
        assert "rechazó el pago" in factura.error

    def test_ya_enviada_es_idempotente(self, cajera, cliente, monkeypatch):
        factura = self._factura_con_snapshot(cajera, cliente)
        factura.estado_envio = Factura.EstadoEnvio.ENVIADO
        factura.cobranzas_uuid = "ya-existente"
        factura.save(update_fields=["estado_envio", "cobranzas_uuid"])

        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(cobranza_services, "get_cobranzas_banco_client", lambda: fake)

        cobranza_services.enviar_factura_a_siic(factura)

        assert fake.llamadas_crear_transaccion == 0
        assert fake.llamadas_pagar_propia == 0

    def test_reintento_reutiliza_uuid_existente(self, cajera, cliente, monkeypatch):
        factura = self._factura_con_snapshot(cajera, cliente)
        factura.estado_envio = Factura.EstadoEnvio.ERROR
        factura.cobranzas_uuid = "uuid-de-intento-previo"
        factura.save(update_fields=["estado_envio", "cobranzas_uuid"])

        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(cobranza_services, "get_cobranzas_banco_client", lambda: fake)

        factura = cobranza_services.enviar_factura_a_siic(factura)

        assert fake.llamadas_crear_transaccion == 0
        assert factura.cobranzas_uuid == "uuid-de-intento-previo"
        assert factura.estado_envio == Factura.EstadoEnvio.ENVIADO

    def test_reintentar_facturacion_envia_de_inmediato(self, fake_mc4, cajera, supervisor, cliente, monkeypatch):
        deuda = Deuda.objects.create(
            cliente=cliente, monto=Decimal("100.00"), items_snapshot=[_item_deuda_snapshot()]
        )
        transaccion = cobranza_services.generar_transaccion_qr(deuda=deuda, usuario=cajera)
        transaccion.transicionar_estado(TransaccionQR.Estado.PAGADO)

        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(cobranza_services, "get_cobranzas_banco_client", lambda: fake)

        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.post(f"/api/transacciones-qr/{transaccion.pk}/reintentar_facturacion/")

        assert response.status_code == 200
        assert response.data["estado_envio"] == "enviado"
        assert fake.llamadas_pagar_propia == 1
