"""Tests de la liquidación de recibos externos (hoy: pagos QR web de
cessa-laravel) contra api-cobranzas-bancos. Nunca pega a la red real: se
monkeypatchea `get_cobranzas_banco_client()` con un doble de prueba, mismo
patrón que `fake_mc4` en apps/cobranza/tests/test_api.py."""
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from services.cobranzas_banco_client import CobranzasBancoError, CobranzasBancoClientInterface, CobranzasBancoRequestError

from . import services as facturacion_services
from .models import SolicitudLiquidacion
from .services import banco_id_para, liquidar_solicitud, numero_documento_para


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

    def test_transaccion_de_otro_dia_expirada_crea_una_nueva(self, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest()
        pagados = []

        def pagar(uuid, detalle, documento):
            fake.llamadas_pagar += 1
            if uuid == "uuid-de-ayer":
                raise CobranzasBancoRequestError("La transacción ha expirado, ésta ha sido creada en fecha y hora ...")
            pagados.append(uuid)

        fake.pagar_transaccion = pagar
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = _solicitud(estado=SolicitudLiquidacion.Estado.ERROR, cobranzas_uuid="uuid-de-ayer")
        solicitud = liquidar_solicitud(solicitud)

        assert solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO, solicitud.error
        assert fake.llamadas_crear_transaccion == 1
        assert pagados == ["uuid-test"]
        assert solicitud.cobranzas_uuid == "uuid-test"

    def _fake_con_transaccion_de_ayer(self, estado_de_ayer):
        fake = FakeCobranzasBancoClientDeTest()
        fake.pagados = []

        def pagar(uuid, detalle, documento):
            fake.llamadas_pagar += 1
            if uuid == "uuid-de-ayer":
                raise CobranzasBancoRequestError("La transacción ha expirado, ésta ha sido creada en fecha y hora ...")
            fake.pagados.append(uuid)

        fake.pagar_transaccion = pagar
        fake.obtener_estado_transaccion = lambda uuid: estado_de_ayer
        return fake

    def test_expirada_pero_ya_pagada_ayer_no_crea_otra_y_trae_el_comprobante(self, monkeypatch):
        fake = self._fake_con_transaccion_de_ayer("PAGADA")
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = liquidar_solicitud(
            _solicitud(estado=SolicitudLiquidacion.Estado.ERROR, cobranzas_uuid="uuid-de-ayer")
        )

        assert solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO, solicitud.error
        assert fake.llamadas_crear_transaccion == 0
        assert fake.pagados == []
        assert solicitud.cobranzas_uuid == "uuid-de-ayer"

    def test_expirada_en_transaccion_queda_en_error_sin_crear_otra(self, monkeypatch):
        fake = self._fake_con_transaccion_de_ayer("EN_TRANSACCION")
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = liquidar_solicitud(
            _solicitud(estado=SolicitudLiquidacion.Estado.ERROR, cobranzas_uuid="uuid-de-ayer")
        )

        assert solicitud.estado == SolicitudLiquidacion.Estado.ERROR
        assert "EN_TRANSACCION" in solicitud.error
        assert fake.llamadas_crear_transaccion == 0
        assert solicitud.cobranzas_uuid == "uuid-de-ayer"

    def test_ya_pagada_por_otro_medio_informa_el_rechazo_real(self, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest()

        def pagar(uuid, detalle, documento):
            raise CobranzasBancoRequestError('pagar transacción: {"error_mensaje": "La deuda ya ha sido pagada con anterioridad"}')

        fake.pagar_transaccion = pagar
        fake.obtener_estado_transaccion = lambda uuid: "FALLIDA"
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = liquidar_solicitud(_solicitud())

        assert solicitud.estado == SolicitudLiquidacion.Estado.ERROR
        assert "ya figura pagado por otro medio" in solicitud.error

    def test_ya_pagada_por_esta_misma_transaccion_sigue_al_comprobante(self, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest()

        def pagar(uuid, detalle, documento):
            raise CobranzasBancoRequestError("La transacción no se puede completar porque ya ha sido pagada.")

        fake.pagar_transaccion = pagar
        fake.obtener_estado_transaccion = lambda uuid: "PAGADA"
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = liquidar_solicitud(_solicitud(cobranzas_uuid="uuid-previo"))

        assert solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO, solicitud.error

    def _fake_con_transaccion_fallida(self, segundo_intento=None):
        """Transacción "uuid-fallida" quedó FALLIDA en un intento anterior; la nueva ("uuid-test")
        hace lo que diga `segundo_intento` (None = paga bien)."""
        fake = FakeCobranzasBancoClientDeTest()
        fake.pagados = []

        def pagar(uuid, detalle, documento):
            fake.llamadas_pagar += 1
            if uuid == "uuid-fallida":
                raise CobranzasBancoRequestError(
                    "pagar transacción: La transacción no se puede completar porque ha fallado previamente. "
                    "Por favor, revise el detalle de la transacción para más información."
                )
            if segundo_intento:
                raise segundo_intento
            fake.pagados.append(uuid)

        fake.pagar_transaccion = pagar
        fake.obtener_estado_transaccion = lambda uuid: "FALLIDA"
        return fake

    def test_transaccion_fallida_crea_una_nueva_y_factura(self, monkeypatch):
        fake = self._fake_con_transaccion_fallida()
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = liquidar_solicitud(
            _solicitud(estado=SolicitudLiquidacion.Estado.ERROR, cobranzas_uuid="uuid-fallida")
        )

        assert solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO, solicitud.error
        assert fake.llamadas_crear_transaccion == 1
        assert fake.pagados == ["uuid-test"]
        assert solicitud.cobranzas_uuid == "uuid-test"

    def test_transaccion_fallida_muestra_el_motivo_real_del_rechazo(self, monkeypatch):
        # Antes el error guardado era siempre "ha fallado previamente"; ahora es el motivo real.
        fake = self._fake_con_transaccion_fallida(
            CobranzasBancoRequestError('pagar transacción: {"error_mensaje": "La deuda ya ha sido pagada con anterioridad"}')
        )
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = liquidar_solicitud(
            _solicitud(estado=SolicitudLiquidacion.Estado.ERROR, cobranzas_uuid="uuid-fallida")
        )

        assert solicitud.estado == SolicitudLiquidacion.Estado.ERROR
        assert "ya figura pagado por otro medio" in solicitud.error
        assert "fallado previamente" not in solicitud.error
        assert solicitud.cobranzas_uuid == "uuid-test"

    def test_transaccion_fallida_con_otro_rechazo_lo_informa_tal_cual(self, monkeypatch):
        fake = self._fake_con_transaccion_fallida(
            CobranzasBancoRequestError("pagar transacción: El operador no puede aperturar caja fuera de horario")
        )
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        solicitud = liquidar_solicitud(
            _solicitud(estado=SolicitudLiquidacion.Estado.ERROR, cobranzas_uuid="uuid-fallida")
        )

        assert solicitud.estado == SolicitudLiquidacion.Estado.ERROR
        assert "fuera de horario" in solicitud.error

    def test_documento_manda_numero_corto_y_fecha_registro(self, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest()
        documentos = []
        fake.pagar_transaccion = lambda uuid, detalle, documento: documentos.append(documento)
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)

        liquidar_solicitud(_solicitud(alias="CESSA-SIM-20260922125619-MSGC"))

        assert len(documentos[0]["numero"]) <= 20
        assert documentos[0]["fecha_registro"] == documentos[0]["fecha"]


@pytest.mark.django_db
class TestNumeroDocumento:
    def test_usa_numero_de_orden_si_entra(self):
        assert numero_documento_para(_solicitud(numero_orden_originante="ORD-123")) == "ORD-123"

    def test_usa_alias_si_no_hay_orden_y_entra(self):
        assert numero_documento_para(_solicitud(alias="CESSA-WEB-1")) == "CESSA-WEB-1"

    def test_alias_largo_se_deriva_estable_de_20(self):
        solicitud = _solicitud(alias="CESSA-SIM-20260922125619-MSGC", numero_orden_originante="X" * 30)
        numero = numero_documento_para(solicitud)
        assert len(numero) == 20
        assert numero.startswith("CW")
        assert numero == numero_documento_para(solicitud)  # mismo en cada reintento
        otro = _solicitud(alias="CESSA-SIM-20260922145238-E2BM")
        assert numero_documento_para(otro) != numero


@pytest.mark.django_db
class TestBancoIdPorBanco:
    """Lo pagado por BNB no debe quedar en el SIIC como BISA: el banco_id sale del banco del QR."""

    @pytest.fixture(autouse=True)
    def _ids(self, settings):
        settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_IDS = {"sip_bisa": "4", "bnb": "5"}

    def test_bnb_usa_su_banco_id(self):
        assert banco_id_para(_solicitud(banco="bnb")) == "5"

    def test_bisa_usa_su_banco_id(self):
        assert banco_id_para(_solicitud(banco="sip_bisa")) == "4"

    def test_sin_banco_o_desconocido_usa_el_de_siempre(self, settings):
        assert banco_id_para(_solicitud()) == "7"
        assert banco_id_para(_solicitud(alias="CESSA-WEB-TEST-2", banco="otro")) == "7"
        settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_IDS = {"sip_bisa": "", "bnb": ""}
        assert banco_id_para(_solicitud(alias="CESSA-WEB-TEST-3", banco="bnb")) == "7"

    def test_el_documento_enviado_lleva_el_banco_id_del_qr(self, monkeypatch):
        documentos = []

        class Fake(FakeCobranzasBancoClientDeTest):
            def pagar_transaccion(self, uuid, detalle, documento):
                documentos.append(documento)

        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: Fake())
        liquidar_solicitud(_solicitud(banco="bnb"))
        assert documentos[0]["banco_id"] == 5


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
        assert respuesta.data["estado"] == "FACTURADO"
        assert SolicitudLiquidacion.objects.count() == 1

    def test_rechazo_de_negocio_devuelve_200_con_estado_error(self, settings, monkeypatch):
        # No 502: Cloudflare reemplaza los 502 del origen por su propia página y el motivo se pierde.
        settings.API_KEY_CESSA_LARAVEL = "la-key-correcta"
        monkeypatch.setattr(
            facturacion_services, "get_cobranzas_banco_client",
            lambda: FakeCobranzasBancoClientDeTest(falla_en_pagar=True),
        )
        cliente = APIClient()
        cliente.credentials(HTTP_X_API_KEY="la-key-correcta")

        respuesta = cliente.post("/api/externo/recibos-web/liquidar/", self._payload(), format="json")

        assert respuesta.status_code == 200
        assert respuesta.data["estado"] == "ERROR"
        assert "rechazó el pago" in respuesta.data["error"]

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


@pytest.mark.django_db
class TestConsultaClienteExterna:
    """GET /api/externo/consulta/cliente/: deuda para cessa-laravel leída como
    banco vía api-cobranzas (mismo SIIC donde se paga)."""

    URL = "/api/externo/consulta/cliente/"

    @pytest.fixture(autouse=True)
    def _api_key(self, settings):
        settings.API_KEY_CESSA_LARAVEL = "la-key-correcta"

    def _cliente(self, key="la-key-correcta"):
        cliente = APIClient()
        if key:
            cliente.credentials(HTTP_X_API_KEY=key)
        return cliente

    def test_sin_api_key_devuelve_403(self):
        assert self._cliente(key=None).get(self.URL, {"nro_cliente": "115997"}).status_code == 403

    def test_reenvia_solo_parametros_conocidos_y_devuelve_el_body_tal_cual(self, monkeypatch):
        recibidos = {}

        def fake(params):
            recibidos.update(params)
            return 200, {"nro_cliente": "115997", "nombre": "X", "deuda": [], "deuda_total": "0"}

        monkeypatch.setattr("apps.facturacion_externa.views.consultar_cliente_via_cobranzas", fake)
        respuesta = self._cliente().get(self.URL, {"nro_cliente": "115997", "ver_deuda": "si", "otro": "1"})

        assert respuesta.status_code == 200
        assert respuesta.json()["nro_cliente"] == "115997"
        assert recibidos == {"nro_cliente": "115997", "ver_deuda": "si"}

    def test_no_existe_reenvia_404_con_error(self, monkeypatch):
        monkeypatch.setattr(
            "apps.facturacion_externa.views.consultar_cliente_via_cobranzas",
            lambda params: (404, {"error": "No existe ningún cliente con los datos introducidos", "code": 404}),
        )
        respuesta = self._cliente().get(self.URL, {"nro_cliente": "999999"})
        assert respuesta.status_code == 404
        assert "error" in respuesta.json()

    def test_api_cobranzas_caida_devuelve_503_no_502(self, monkeypatch):
        from services.deuda_client import DeudaClientError

        def falla(params):
            raise DeudaClientError("timeout")

        monkeypatch.setattr("apps.facturacion_externa.views.consultar_cliente_via_cobranzas", falla)
        respuesta = self._cliente().get(self.URL, {"nro_cliente": "115997"})
        assert respuesta.status_code == 503

    def test_sin_nro_cliente_devuelve_400(self):
        assert self._cliente().get(self.URL).status_code == 400


class TestEndpointsFacturasCliente:
    """"Tus últimas facturas" de cessa-laravel, del mismo SIIC que la deuda."""

    @pytest.fixture(autouse=True)
    def _api_key(self, settings):
        settings.API_KEY_CESSA_LARAVEL = "la-key-correcta"

    def _cliente(self, key="la-key-correcta"):
        cliente = APIClient()
        if key:
            cliente.credentials(HTTP_X_API_KEY=key)
        return cliente

    def test_pagos_sin_api_key_403(self):
        assert self._cliente(key=None).get("/api/externo/consulta/clientes/179185/pagos/").status_code == 403

    def test_pagos_reenvia_el_body_y_acota_el_limit(self, monkeypatch):
        recibidos = {}

        def fake(nro, limit):
            recibidos.update(nro=nro, limit=limit)
            return 200, {"items": [{"detalle": "Fact Energia OCTUBRE/2023"}]}

        monkeypatch.setattr("apps.facturacion_externa.views.pagos_cliente_siic", fake)
        r = self._cliente().get("/api/externo/consulta/clientes/179185/pagos/", {"limit": "999"})
        assert r.status_code == 200
        assert r.json()["items"][0]["detalle"] == "Fact Energia OCTUBRE/2023"
        assert recibidos == {"nro": 179185, "limit": 50}

    def test_pagos_siic_caido_503(self, monkeypatch):
        from services.deuda_client import DeudaClientError

        def falla(nro, limit):
            raise DeudaClientError("timeout")

        monkeypatch.setattr("apps.facturacion_externa.views.pagos_cliente_siic", falla)
        assert self._cliente().get("/api/externo/consulta/clientes/179185/pagos/").status_code == 503

    def test_pagos_nro_no_numerico_404(self):
        assert self._cliente().get("/api/externo/consulta/clientes/abc/pagos/").status_code == 404

    def test_pdf_devuelve_el_pdf(self, monkeypatch):
        monkeypatch.setattr("apps.facturacion_externa.views.comprobante_pdf_siic", lambda item: b"%PDF-1.4 x")
        r = self._cliente().post("/api/externo/consulta/comprobantes/pdf/", {"item": {"comprobante": "1"}}, format="json")
        assert r.status_code == 200
        assert r["Content-Type"] == "application/pdf"
        assert r.content.startswith(b"%PDF")

    def test_pdf_sin_item_400_y_no_generado_404(self, monkeypatch):
        assert self._cliente().post("/api/externo/consulta/comprobantes/pdf/", {}, format="json").status_code == 400
        monkeypatch.setattr("apps.facturacion_externa.views.comprobante_pdf_siic", lambda item: None)
        r = self._cliente().post("/api/externo/consulta/comprobantes/pdf/", {"item": {"x": 1}}, format="json")
        assert r.status_code == 404

    def test_pdf_sin_api_key_403(self):
        r = self._cliente(key=None).post("/api/externo/consulta/comprobantes/pdf/", {"item": {"x": 1}}, format="json")
        assert r.status_code == 403


@pytest.mark.django_db
class TestVerLiquidaciones:
    def test_filtra_por_cliente_y_muestra_error_y_detalle(self):
        from io import StringIO

        from django.core.management import call_command

        _solicitud(alias="A-1", nro_cliente="115997", estado=SolicitudLiquidacion.Estado.ERROR, error="La deuda no existe")
        _solicitud(alias="A-2", nro_cliente="999")
        salida = StringIO()

        call_command("ver_liquidaciones", "--cliente", "115997", "--detalle", stdout=salida)

        texto = salida.getvalue()
        assert "A-1" in texto and "A-2" not in texto
        assert "La deuda no existe" in texto

    def test_sin_resultados(self):
        from io import StringIO

        from django.core.management import call_command

        salida = StringIO()
        call_command("ver_liquidaciones", "--cliente", "1", stdout=salida)
        assert "Sin liquidaciones" in salida.getvalue()


@pytest.fixture(autouse=True)
def _sin_verificacion_de_deuda_por_defecto(monkeypatch, request):
    """Los tests de liquidación no pegan a la red: la re-consulta de la deuda falla como si no
    hubiera red, y en ese caso liquidar_solicitud() sigue (decide el SIIC al pagar). Los tests
    de TestVerificacionDeuda la reemplazan."""
    from services.deuda_client import DeudaClientError

    def sin_red(params):
        raise DeudaClientError("sin red en tests")

    monkeypatch.setattr(facturacion_services, "consultar_cliente_via_cobranzas", sin_red)


def _pend(nro, importe, detalle="Fact", fecha="20260211"):
    return {
        "codigo_sucursal": "1", "nro_comprobante": str(nro), "nro_suministro": "1", "fecha": fecha, "tipo": "3",
        "letra_comprobante": "            ", "nro_autorizacion": "3", "nro_cliente": "115997",
        "importe": importe, "detalle": detalle, "debito_credito": "DEBITO",
    }


class _FakeConEstado(FakeCobranzasBancoClientDeTest):
    def __init__(self, estado="", **kw):
        super().__init__(**kw)
        self.estado = estado

    def obtener_estado_transaccion(self, uuid):
        return self.estado


@pytest.mark.django_db
class TestVerificacionDeuda:
    def _con_deuda_actual(self, monkeypatch, pendientes, status=200):
        body = {"nro_cliente": "115997", "deuda": pendientes} if status == 200 else {"error": "No existe", "code": 404}
        monkeypatch.setattr(facturacion_services, "consultar_cliente_via_cobranzas", lambda params: (status, body))

    def _liquidar(self, monkeypatch, detalle, fake=None, **overrides):
        fake = fake or FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)
        # Monto coherente con el detalle (si no, lo frena antes el control de monto).
        monto = sum(
            (-abs(Decimal(d["importe"])) if d["debito_credito"] == "CREDITO" else abs(Decimal(d["importe"])))
            for d in detalle
        )
        solicitud = _solicitud(nro_cliente="115997", detalle=detalle, monto=monto, **overrides)
        return liquidar_solicitud(solicitud), fake

    def test_deuda_igual_paga(self, monkeypatch):
        self._con_deuda_actual(monkeypatch, [_pend(1, "10.00"), _pend(2, "20.00")])
        # Con letra sin espacios y fecha con guiones: se normalizan.
        s, fake = self._liquidar(monkeypatch, [{**_pend(1, "10.00"), "letra_comprobante": "", "fecha": "2026-02-11"}])
        assert s.estado == SolicitudLiquidacion.Estado.FACTURADO, s.error
        assert fake.llamadas_pagar == 1

    def test_comprobante_ya_no_pendiente_no_crea_transaccion(self, monkeypatch):
        """Caso real 2026-10-06 (115997): NC de prod que no existe en el SIIC donde se paga."""
        self._con_deuda_actual(monkeypatch, [_pend(1, "10.00")])
        nc = {**_pend(20253, "384.90", "NC. CONCILIACIÓN JULIO/2026"), "debito_credito": "CREDITO", "tipo": "31"}
        s, fake = self._liquidar(monkeypatch, [_pend(1, "10.00"), nc])
        assert s.estado == SolicitudLiquidacion.Estado.ERROR
        assert s.error.startswith(facturacion_services.MENSAJE_DEUDA_CAMBIADA)
        assert "20253" in s.error and "NC. CONCILIACIÓN JULIO/2026" in s.error
        assert fake.llamadas_crear_transaccion == 0 and fake.llamadas_pagar == 0

    def test_importe_distinto(self, monkeypatch):
        self._con_deuda_actual(monkeypatch, [_pend(1, "12.00")])
        s, fake = self._liquidar(monkeypatch, [_pend(1, "10.00")])
        assert s.estado == SolicitudLiquidacion.Estado.ERROR and "cambió el importe" in s.error
        assert fake.llamadas_pagar == 0

    def test_hay_uno_mas_antiguo_que_no_se_paga(self, monkeypatch):
        self._con_deuda_actual(monkeypatch, [_pend(0, "5.00", "Fact NUEVA"), _pend(1, "10.00")])
        s, fake = self._liquidar(monkeypatch, [_pend(1, "10.00")])
        assert s.estado == SolicitudLiquidacion.Estado.ERROR and "más antiguos" in s.error and "N° 0" in s.error
        assert fake.llamadas_pagar == 0

    def test_cliente_inexistente(self, monkeypatch):
        self._con_deuda_actual(monkeypatch, [], status=404)
        s, _ = self._liquidar(monkeypatch, [_pend(1, "10.00")])
        assert s.estado == SolicitudLiquidacion.Estado.ERROR and "no figura" in s.error

    def test_reintento_con_transaccion_ya_pagada_no_verifica(self, monkeypatch):
        """Pagó en un intento anterior y falló al traer el comprobante: la deuda ya no figura
        pendiente justamente por este pago; no hay que bloquearlo."""
        self._con_deuda_actual(monkeypatch, [])  # ya no hay nada pendiente
        s, fake = self._liquidar(
            monkeypatch, [_pend(1, "10.00")], fake=_FakeConEstado(estado="PAGADA"), cobranzas_uuid="uuid-previo",
        )
        assert s.estado == SolicitudLiquidacion.Estado.FACTURADO, s.error


@pytest.mark.django_db
class TestMontoContraComprobantes:
    def test_monto_distinto_no_paga(self, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)
        s = liquidar_solicitud(_solicitud(monto=Decimal("200.00")))  # el detalle suma 150.50
        assert s.estado == SolicitudLiquidacion.Estado.ERROR
        assert "no coincide" in s.error and "150.50" in s.error
        assert fake.llamadas_crear_transaccion == 0

    def test_la_nota_de_credito_resta(self, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest()
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)
        detalle = [
            {"importe": "100.00", "debito_credito": "DEBITO", "nro_cliente": "123"},
            {"importe": "30.00", "debito_credito": "CREDITO", "nro_cliente": "123"},
        ]
        s = liquidar_solicitud(_solicitud(monto=Decimal("70.00"), detalle=detalle))
        assert s.estado == SolicitudLiquidacion.Estado.FACTURADO, s.error
