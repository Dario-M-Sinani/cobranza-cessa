"""Tests de la liquidación de recibos externos (hoy: pagos QR web de
cessa-laravel) contra api-cobranzas-bancos. Nunca pega a la red real: se
monkeypatchea `get_cobranzas_banco_client()` con un doble de prueba, mismo
patrón que `fake_mc4` en apps/cobranza/tests/test_api.py."""
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from services.cobranzas_banco_client import CobranzasBancoError, CobranzasBancoClientInterface

from . import services as facturacion_services
from .models import SolicitudLiquidacion
from .services import liquidar_solicitud


class FakeCobranzasBancoClientDeTest(CobranzasBancoClientInterface):
    def __init__(self, falla_en_pagar=False):
        self.falla_en_pagar = falla_en_pagar
        self.llamadas_crear_transaccion = 0
        self.llamadas_pagar = 0

    def asegurar_caja_abierta(self):
        return None

    def crear_transaccion(self):
        self.llamadas_crear_transaccion += 1
        return "uuid-test"

    def pagar_transaccion(self, uuid, detalle, documento):
        self.llamadas_pagar += 1
        if self.falla_en_pagar:
            raise CobranzasBancoError("api-cobranzas-bancos rechazó el pago")

    def pagar_transaccion_propia(self, uuid, detalle):
        raise NotImplementedError("el gateway externo usa pagar_transaccion, no pagar_transaccion_propia")

    def obtener_comprobante_pdf(self, uuid):
        return b"%PDF-1.4 (test)"

    def obtener_comprobante_json(self, uuid):
        return {"nro_factura": f"F-{uuid}", "cliente_nombre": "Cliente Test"}


def _solicitud(**overrides) -> SolicitudLiquidacion:
    datos = {
        "alias": "CESSA-WEB-TEST-1",
        "nro_cliente": "123",
        "monto": Decimal("150.50"),
        "moneda": "BOB",
        "detalle": [{"importe": 150.5, "debito_credito": "DEBITO", "nro_cliente": "123"}],
        "fecha_pago": timezone.now(),
    }
    datos.update(overrides)
    return SolicitudLiquidacion.objects.create(**datos)


@pytest.fixture(autouse=True)
def _configurar_catalogos_cobranzas(settings):
    # Catálogos GET /v1/entes y GET /v1/bancos -- liquidar_solicitud() exige
    # tenerlos configurados (ver test_falta_configurar_catalogos_queda_en_error).
    settings.COBRANZAS_BANCO_DOCUMENTO_ENTE_ID = "5"
    settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_ID = "7"


@pytest.mark.django_db
class TestLiquidarSolicitud:
    def test_liquidacion_exitosa(self, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = liquidar_solicitud(_solicitud())

        assert solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO, solicitud.error
        assert solicitud.cobranzas_uuid == "uuid-test"
        assert bytes(solicitud.comprobante_pdf) == b"%PDF-1.4 (test)"
        assert solicitud.procesado_en is not None

    def test_liquidacion_fallida_queda_en_error(self, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest(falla_en_pagar=True)
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = liquidar_solicitud(_solicitud())

        assert solicitud.estado == SolicitudLiquidacion.Estado.ERROR
        assert solicitud.intentos == 1
        assert "rechazó el pago" in solicitud.error

    def test_ya_facturada_es_idempotente(self, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = _solicitud(estado=SolicitudLiquidacion.Estado.FACTURADO, cobranzas_uuid="ya-existente")
        liquidar_solicitud(solicitud)

        assert fake.llamadas_crear_transaccion == 0
        assert fake.llamadas_pagar == 0

    def test_falta_configurar_catalogos_queda_en_error(self, monkeypatch, settings):
        settings.COBRANZAS_BANCO_DOCUMENTO_ENTE_ID = ""
        settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_ID = ""
        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = liquidar_solicitud(_solicitud())

        assert solicitud.estado == SolicitudLiquidacion.Estado.ERROR
        assert "Falta configurar" in solicitud.error
        assert fake.llamadas_crear_transaccion == 0  # nunca llega a tocar la API real

    def test_reintento_reutiliza_uuid_existente(self, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = _solicitud(estado=SolicitudLiquidacion.Estado.ERROR, cobranzas_uuid="uuid-de-intento-previo")
        solicitud = liquidar_solicitud(solicitud)

        assert fake.llamadas_crear_transaccion == 0  # no crea una transacción nueva
        assert solicitud.cobranzas_uuid == "uuid-de-intento-previo"
        assert solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO


@pytest.mark.django_db
class TestEndpointLiquidar:
    def _payload(self, **overrides):
        payload = {
            "alias": "CESSA-WEB-API-1",
            "nro_cliente": "123",
            "monto": "150.50",
            "moneda": "BOB",
            "detalle": [{"importe": "150.50", "debito_credito": "DEBITO"}],
            "fecha_pago": timezone.now().isoformat(),
        }
        payload.update(overrides)
        return payload

    def test_sin_api_key_devuelve_403(self, settings):
        settings.API_KEY_CESSA_LARAVEL = "la-key-correcta"
        cliente = APIClient()
        respuesta = cliente.post("/api/externo/recibos-web/liquidar/", self._payload(), format="json")
        assert respuesta.status_code == 403

    def test_con_api_key_invalida_devuelve_403(self, settings):
        settings.API_KEY_CESSA_LARAVEL = "la-key-correcta"
        cliente = APIClient()
        cliente.credentials(HTTP_X_API_KEY="otra-cosa")
        respuesta = cliente.post("/api/externo/recibos-web/liquidar/", self._payload(), format="json")
        assert respuesta.status_code == 403

    def test_con_api_key_valida_liquida_y_devuelve_200(self, settings, monkeypatch):
        settings.API_KEY_CESSA_LARAVEL = "la-key-correcta"
        monkeypatch.setattr(
            facturacion_services, "get_cobranzas_banco_client", lambda: FakeCobranzasBancoClientDeTest()
        )
        cliente = APIClient()
        cliente.credentials(HTTP_X_API_KEY="la-key-correcta")

        respuesta = cliente.post("/api/externo/recibos-web/liquidar/", self._payload(), format="json")

        assert respuesta.status_code == 200
        assert respuesta.data["estado"] == "facturado"
        assert SolicitudLiquidacion.objects.count() == 1

    def test_reenvio_del_mismo_alias_no_duplica(self, settings, monkeypatch):
        settings.API_KEY_CESSA_LARAVEL = "la-key-correcta"
        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)
        cliente = APIClient()
        cliente.credentials(HTTP_X_API_KEY="la-key-correcta")

        payload = self._payload()
        cliente.post("/api/externo/recibos-web/liquidar/", payload, format="json")
        cliente.post("/api/externo/recibos-web/liquidar/", payload, format="json")

        assert SolicitudLiquidacion.objects.count() == 1
        assert fake.llamadas_crear_transaccion == 1  # el reenvío no vuelve a pagar, ya estaba FACTURADO

    def test_consultar_liquidacion_inexistente_devuelve_404(self, settings):
        settings.API_KEY_CESSA_LARAVEL = "la-key-correcta"
        cliente = APIClient()
        cliente.credentials(HTTP_X_API_KEY="la-key-correcta")

        respuesta = cliente.get("/api/externo/recibos-web/no-existe/")

        assert respuesta.status_code == 404


@pytest.mark.django_db
class TestEndpointComprobanteJson:
    def _cliente_autenticado(self, settings):
        settings.API_KEY_CESSA_LARAVEL = "la-key-correcta"
        cliente = APIClient()
        cliente.credentials(HTTP_X_API_KEY="la-key-correcta")
        return cliente

    def test_sin_transaccion_devuelve_404(self, settings):
        cliente = self._cliente_autenticado(settings)
        solicitud = _solicitud(alias="CESSA-WEB-JSON-1")
        assert solicitud.cobranzas_uuid == ""

        respuesta = cliente.get(f"/api/externo/recibos-web/{solicitud.alias}/comprobante-json/")

        assert respuesta.status_code == 404

    def test_con_transaccion_devuelve_el_documento(self, settings, monkeypatch):
        cliente = self._cliente_autenticado(settings)
        solicitud = _solicitud(alias="CESSA-WEB-JSON-2", cobranzas_uuid="uuid-real")

        from . import views as facturacion_views

        monkeypatch.setattr(facturacion_views, "get_cobranzas_banco_client", lambda: FakeCobranzasBancoClientDeTest())

        respuesta = cliente.get(f"/api/externo/recibos-web/{solicitud.alias}/comprobante-json/")

        assert respuesta.status_code == 200
        assert respuesta.data["nro_factura"] == "F-uuid-real"

    def test_error_del_banco_devuelve_502(self, settings, monkeypatch):
        cliente = self._cliente_autenticado(settings)
        solicitud = _solicitud(alias="CESSA-WEB-JSON-3", cobranzas_uuid="uuid-real")

        class ClienteQueFalla(FakeCobranzasBancoClientDeTest):
            def obtener_comprobante_json(self, uuid):
                raise CobranzasBancoError("no se pudo consultar")

        from . import views as facturacion_views

        monkeypatch.setattr(facturacion_views, "get_cobranzas_banco_client", lambda: ClienteQueFalla())

        respuesta = cliente.get(f"/api/externo/recibos-web/{solicitud.alias}/comprobante-json/")

        assert respuesta.status_code == 502
