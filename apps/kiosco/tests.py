"""Tests del kiosco. Nunca pegan a SIP ni a api-cobranzas: se monkeypatchean
`get_mc4_client()`, la consulta de deuda y `get_cobranzas_banco_client()`."""
import base64
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.facturacion_externa import services as facturacion_services
from apps.facturacion_externa.models import AvisoSip, SolicitudLiquidacion
from apps.facturacion_externa.tests import FakeCobranzasBancoClientDeTest
from services.mc4_client import EstadoPagoMC4, EstadoTransaccionQR, MC4RequestError, ResultadoQR

from . import services, tasks
from .models import PagoKiosco

TOKEN = "token-de-la-pantalla"
INTERNA = {"HTTP_X_KIOSCO_TOKEN": TOKEN, "HTTP_X_REAL_IP": "10.10.20.5"}


def _item(nro, anio, mes, importe, debito_credito="DEBITO", detalle="CONSUMO"):
    return {
        "codigo_sucursal": "1", "nro_comprobante": str(nro), "nro_suministro": "55", "fecha": f"{anio}{mes:02d}05",
        "tipo": "1", "letra_comprobante": "A", "nro_autorizacion": "9", "anio": anio, "mes": mes,
        "importe": importe, "debito_credito": debito_credito, "detalle": detalle, "nro_cliente": "153896",
    }


# Del más antiguo al más reciente, como los devuelve el SIIC. La conciliación resta.
DEUDA = [
    _item(101, 2026, 8, "40.00"),
    _item(102, 2026, 9, "35.50"),
    _item(103, 2026, 9, "50.00", debito_credito="CREDITO", detalle="CONCILIACION"),
    _item(104, 2026, 10, "60.00"),
]


class FakeSip:
    """SIP de prueba: `pagados` dice qué alias figuran pagados al consultar."""

    def __init__(self):
        self.generados = []
        self.inhabilitados = []
        self.pagados = set()
        self.falla_consulta = False

    def generar_qr(self, solicitud):
        self.generados.append(solicitud)
        return ResultadoQR(
            imagen_qr_base64="iVBORw0KGgo=", id_qr="1", id_transaccion="2",
            fecha_vencimiento=datetime.now(), banco_destino="BISA", cuenta_destino="0",
        )

    def inhabilitar_qr(self, alias):
        self.inhabilitados.append(alias)

    def consultar_estado(self, alias):
        if self.falla_consulta:
            raise MC4RequestError("consultar estado: sin respuesta")
        pagado = alias in self.pagados
        return EstadoTransaccionQR(
            alias=alias, estado=EstadoPagoMC4.PAGADO if pagado else EstadoPagoMC4.PENDIENTE,
            fecha_procesamiento=None, monto=None, numero_orden_originante="ORD-9" if pagado else None, id_qr=None,
            nombre_pagador="JUAN PEREZ" if pagado else "", documento_pagador="1234567" if pagado else "",
        )


@pytest.fixture(autouse=True)
def config(settings, monkeypatch):
    settings.KIOSCO_TOKENS = {"oficina-central": TOKEN}
    settings.KIOSCO_REDES = ["10.0.0.0/8"]
    settings.KIOSCO_MINUTOS_QR = 5
    settings.COBRANZAS_BANCO_DOCUMENTO_ENTE_ID = "5"
    settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_ID = "7"
    settings.SIP_CALLBACK_USERNAME = "sip-user"
    settings.SIP_CALLBACK_PASSWORD = "sip-pass"
    settings.SIP_CALLBACK_REENVIOS = {"CESSA-": "https://laravel.test/no-llamar"}
    settings.ALERTAS_TELEGRAM_BOT_TOKEN = ""
    settings.ALERTAS_EMAIL_DESTINOS = []
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

    deuda = {"body": {"nro_cliente": "153896", "nombre": "PEREZ MAMANI JUAN", "deuda": list(DEUDA)}}

    def consultar(params):
        if params.get("nro_cliente") != "153896":
            return 404, {"error": "No existe el abonado"}
        return 200, deuda["body"]

    monkeypatch.setattr(services, "consultar_cliente_via_cobranzas", consultar)
    monkeypatch.setattr(facturacion_services, "consultar_cliente_via_cobranzas", consultar)
    return deuda


@pytest.fixture
def sip(monkeypatch):
    fake = FakeSip()
    monkeypatch.setattr(services, "get_mc4_client", lambda: fake)
    return fake


@pytest.fixture
def cobranzas(monkeypatch):
    fake = FakeCobranzasBancoClientDeTest()
    monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)
    monkeypatch.setattr(services, "get_cobranzas_banco_client", lambda: fake)
    return fake


def _cliente():
    return APIClient()


@pytest.mark.django_db
class TestAcceso:
    def test_sin_token_no_atiende(self):
        r = _cliente().get("/api/kiosco/clientes/153896/", HTTP_X_REAL_IP="10.10.20.5")
        assert r.status_code == 403

    def test_desde_internet_no_atiende_aunque_tenga_token(self):
        # test01 publica /api/ a internet: nginx pone la IP pública en X-Real-IP.
        r = _cliente().get("/api/kiosco/clientes/153896/", HTTP_X_KIOSCO_TOKEN=TOKEN, HTTP_X_REAL_IP="200.87.1.10")
        assert r.status_code == 403

    def test_token_de_otra_pantalla_no_sirve(self):
        r = _cliente().get("/api/kiosco/clientes/153896/", HTTP_X_KIOSCO_TOKEN="otro", HTTP_X_REAL_IP="10.10.20.5")
        assert r.status_code == 403

    def test_pagina_solo_red_interna(self):
        assert _cliente().get("/kiosco/", HTTP_X_REAL_IP="200.87.1.10").status_code == 403
        r = _cliente().get("/kiosco/", HTTP_X_REAL_IP="10.10.20.5")
        assert r.status_code == 200
        assert b"Escribe tu n" in r.content


@pytest.mark.django_db
class TestConsulta:
    def test_deuda_y_cantidades_cobrables(self):
        r = _cliente().get("/api/kiosco/clientes/153896/", **INTERNA)
        assert r.status_code == 200
        datos = r.json()
        assert datos["nombre"] == "PEREZ MAMANI JUAN"
        assert [d["periodo"] for d in datos["deuda"]] == ["Agosto 2026", "Septiembre 2026", "Septiembre 2026", "Octubre 2026"]
        assert datos["deuda"][2]["importe"] == "-50.00"
        # Con 3 meses el subtotal es 40 + 35.50 - 50 = 25.50 (> 0, se puede).
        assert datos["cantidades"] == [
            {"cantidad": 1, "monto": "40.00"},
            {"cantidad": 2, "monto": "75.50"},
            {"cantidad": 3, "monto": "25.50"},
            {"cantidad": 4, "monto": "85.50"},
        ]
        assert datos["total"] == "85.50"

    def test_cliente_inexistente(self):
        r = _cliente().get("/api/kiosco/clientes/999/", **INTERNA)
        assert r.status_code == 404

    def test_numero_invalido(self):
        r = _cliente().get("/api/kiosco/clientes/12345678901/", **INTERNA)
        assert r.status_code == 400

    def test_prefijo_con_subtotal_negativo_no_se_ofrece(self, config):
        config["body"]["deuda"] = [_item(1, 2026, 8, "10.00", "CREDITO", "CONCILIACION"), _item(2, 2026, 9, "30.00")]
        datos = _cliente().get("/api/kiosco/clientes/153896/", **INTERNA).json()
        assert datos["cantidades"] == [{"cantidad": 2, "monto": "20.00"}]


@pytest.mark.django_db
class TestGenerarQr:
    def test_cobra_los_mas_antiguos(self, sip):
        r = _cliente().post("/api/kiosco/qr/", {"nro_cliente": "153896", "cantidad": 2}, format="json", **INTERNA)
        assert r.status_code == 201
        datos = r.json()
        assert datos["estado"] == "pendiente"
        assert datos["monto"] == "75.50"
        assert datos["qr"] == "iVBORw0KGgo="
        assert datos["alias"].startswith("CESSA-KIO-")

        pago = PagoKiosco.objects.get(alias=datos["alias"])
        assert pago.kiosco == "oficina-central"
        assert [d["nro_comprobante"] for d in pago.detalle] == ["101", "102"]
        assert pago.glosa == "CESSA 2 comp 153896 B75.50"
        enviado = sip.generados[0]
        assert enviado.monto == Decimal("75.50")
        assert enviado.descripcion == pago.glosa

    def test_cantidad_no_cobrable(self, sip, config):
        config["body"]["deuda"] = [_item(1, 2026, 8, "10.00", "CREDITO", "CONCILIACION"), _item(2, 2026, 9, "30.00")]
        r = _cliente().post("/api/kiosco/qr/", {"nro_cliente": "153896", "cantidad": 1}, format="json", **INTERNA)
        assert r.status_code == 409
        assert not sip.generados

    def test_corte_diario(self, sip, monkeypatch):
        corte = timezone.make_aware(datetime(2026, 11, 9, 23, 59, 10))
        monkeypatch.setattr(services.timezone, "now", lambda: corte)
        r = _cliente().post("/api/kiosco/qr/", {"nro_cliente": "153896", "cantidad": 1}, format="json", **INTERNA)
        assert r.status_code == 409
        assert "23:59" in r.json()["error"]

    def test_falla_sip(self, sip, monkeypatch):
        def falla(_):
            raise MC4RequestError("generar QR: caído")

        monkeypatch.setattr(sip, "generar_qr", falla)
        r = _cliente().post("/api/kiosco/qr/", {"nro_cliente": "153896", "cantidad": 1}, format="json", **INTERNA)
        assert r.status_code == 409
        assert not PagoKiosco.objects.exists()


def _generar(cantidad=2) -> PagoKiosco:
    pago, _ = services.generar_qr("oficina-central", "153896", cantidad)
    return pago


@pytest.mark.django_db
class TestPago:
    def test_pantalla_ve_el_pago_y_se_factura(self, sip, cobranzas):
        pago = _generar()
        url = f"/api/kiosco/qr/{pago.alias}/"
        assert _cliente().get(url, **INTERNA).json()["estado"] == "pendiente"

        sip.pagados.add(pago.alias)
        PagoKiosco.objects.filter(pk=pago.pk).update(consultado_sip_en=None)
        datos = _cliente().get(url, **INTERNA).json()
        assert datos["estado"] == "facturado"
        assert datos["nro_factura"] == "F-uuid-test"

        solicitud = SolicitudLiquidacion.objects.get(alias=pago.alias)
        assert solicitud.origen == SolicitudLiquidacion.Origen.KIOSCO
        assert solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO
        assert solicitud.monto == Decimal("75.50")
        assert solicitud.banco == "sip_bisa"
        assert solicitud.numero_orden_originante == "ORD-9"
        assert cobranzas.llamadas_pagar == 1
        aviso = AvisoSip.objects.get(alias=pago.alias)
        assert aviso.datos["nombreCliente"] == "JUAN PEREZ"

        # Más consultas no vuelven a pagar.
        _cliente().get(url, **INTERNA)
        assert cobranzas.llamadas_pagar == 1

    def test_no_consulta_a_sip_en_cada_sondeo(self, sip, monkeypatch):
        pago = _generar()
        consultas = []
        original = sip.consultar_estado
        monkeypatch.setattr(sip, "consultar_estado", lambda alias: consultas.append(alias) or original(alias))
        url = f"/api/kiosco/qr/{pago.alias}/"
        _cliente().get(url, **INTERNA)
        _cliente().get(url, **INTERNA)
        assert len(consultas) == 1

    def test_otra_pantalla_no_ve_el_pago(self, sip, settings):
        pago = _generar()
        settings.KIOSCO_TOKENS = {"oficina-central": TOKEN, "agencia": "token-agencia"}
        r = _cliente().get(f"/api/kiosco/qr/{pago.alias}/", HTTP_X_KIOSCO_TOKEN="token-agencia", HTTP_X_REAL_IP="10.1.1.9")
        assert r.status_code == 404

    def test_vencido_se_da_de_baja(self, sip):
        pago = _generar()
        PagoKiosco.objects.filter(pk=pago.pk).update(vence_en=timezone.now() - timedelta(seconds=1))
        datos = _cliente().get(f"/api/kiosco/qr/{pago.alias}/", **INTERNA).json()
        assert datos["estado"] == "vencido"
        assert sip.inhabilitados == [pago.alias]

    def test_cancelar(self, sip):
        pago = _generar()
        datos = _cliente().post(f"/api/kiosco/qr/{pago.alias}/cancelar/", **INTERNA).json()
        assert datos["estado"] == "cancelado"
        assert sip.inhabilitados == [pago.alias]
        assert not SolicitudLiquidacion.objects.exists()

    def test_cancelar_justo_despues_de_pagar_factura_igual(self, sip, cobranzas):
        pago = _generar()
        sip.pagados.add(pago.alias)
        datos = _cliente().post(f"/api/kiosco/qr/{pago.alias}/cancelar/", **INTERNA).json()
        assert datos["estado"] == "facturado"

    def test_sin_respuesta_de_sip_no_se_cierra(self, sip):
        pago = _generar()
        PagoKiosco.objects.filter(pk=pago.pk).update(vence_en=timezone.now() - timedelta(seconds=1))
        sip.falla_consulta = True
        datos = _cliente().get(f"/api/kiosco/qr/{pago.alias}/", **INTERNA).json()
        assert datos["estado"] == "pendiente"

    def test_error_de_facturacion_se_muestra_como_pago_recibido(self, sip, monkeypatch):
        fake = FakeCobranzasBancoClientDeTest(falla_en_pagar=True)
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: fake)
        pago = _generar()
        sip.pagados.add(pago.alias)
        datos = _cliente().get(f"/api/kiosco/qr/{pago.alias}/", **INTERNA).json()
        assert datos["estado"] == "sin_factura"
        assert SolicitudLiquidacion.objects.get(alias=pago.alias).estado == SolicitudLiquidacion.Estado.ERROR


@pytest.mark.django_db
class TestRecibo:
    def test_sin_pagar_no_hay_recibo(self, sip):
        pago = _generar()
        assert _cliente().get(f"/api/kiosco/qr/{pago.alias}/recibo/", **INTERNA).status_code == 409

    def test_recibo_con_monto_y_meses_aunque_la_factura_no_este(self, sip, monkeypatch):
        # Sin `cobranzas`: el recibo sale de lo guardado en el pago, no del SIIC.
        pago = _generar(cantidad=3)
        sip.pagados.add(pago.alias)
        monkeypatch.setattr(services, "facturar", lambda p: None)
        services.consultar_sip(pago)
        r = _cliente().get(f"/api/kiosco/qr/{pago.alias}/recibo/", **INTERNA)
        assert r.status_code == 200
        datos = r.json()
        assert datos["nro_cliente"] == "153896" and datos["nombre"] == "PEREZ MAMANI JUAN"
        assert datos["total"] == "25.50"
        assert [(m["periodo"], m["importe"]) for m in datos["meses"]] == [
            ("Agosto 2026", "40.00"), ("Septiembre 2026", "35.50"), ("Septiembre 2026", "-50.00"),
        ]
        assert datos["codigo"] == pago.alias and datos["cajero"] == "oficina-central"

    def test_otra_pantalla_no_ve_el_recibo(self, sip, settings):
        pago = _generar()
        settings.KIOSCO_TOKENS = {"oficina-central": TOKEN, "agencia": "token-agencia"}
        r = _cliente().get(f"/api/kiosco/qr/{pago.alias}/recibo/", HTTP_X_KIOSCO_TOKEN="token-agencia", HTTP_X_REAL_IP="10.10.20.5")
        assert r.status_code == 404


@pytest.mark.django_db
class TestComprobante:
    def test_antes_de_facturar_no_hay(self, sip):
        pago = _generar()
        r = _cliente().get(f"/api/kiosco/qr/{pago.alias}/comprobante/", **INTERNA)
        assert r.status_code == 409

    def test_datos_del_ticket_con_qr_siat(self, sip, cobranzas, monkeypatch):
        monkeypatch.setattr(cobranzas, "obtener_comprobante_json", lambda uuid: {
            "nro_factura": "1004587", "codigo_autorizacion": "CUF123", "cliente_nombre": "PEREZ MAMANI JUAN",
            "total_pagar": 75.5, "total_pagar_literal": "SETENTA Y CINCO 50/100",
            "detalle": [{"descripcion": "Consumo Agosto", "subtotal": 40}, {"descripcion": "Consumo Septiembre", "subtotal": 35.5}],
        })
        pago = _generar()
        sip.pagados.add(pago.alias)
        services.consultar_sip(pago)
        datos = _cliente().get(f"/api/kiosco/qr/{pago.alias}/comprobante/", **INTERNA).json()
        assert datos["nro_factura"] == "1004587"
        assert datos["cliente_codigo"] == "153896"
        assert [d["descripcion"] for d in datos["detalle"]] == ["Consumo Agosto", "Consumo Septiembre"]
        assert datos["caja"] == "KIOSCO"
        assert datos["qr_siat"].startswith("data:image/svg+xml")

    def test_factura_pdf_para_hojas_carta(self, sip, cobranzas):
        pago = _generar()
        url = f"/api/kiosco/qr/{pago.alias}/factura.pdf"
        assert _cliente().get(url, **INTERNA).status_code == 409
        sip.pagados.add(pago.alias)
        services.consultar_sip(pago)
        r = _cliente().get(url, **INTERNA)
        assert r.status_code == 200
        assert r["Content-Type"] == "application/pdf"
        assert r.content.startswith(b"%PDF")

    def test_sin_comprobante_oficial_usa_lo_del_kiosco(self, sip, cobranzas, monkeypatch):
        pago = _generar()
        sip.pagados.add(pago.alias)
        services.consultar_sip(pago)

        def falla(uuid):
            raise facturacion_services.CobranzasBancoError("caído")

        monkeypatch.setattr(cobranzas, "obtener_comprobante_json", falla)
        datos = _cliente().get(f"/api/kiosco/qr/{pago.alias}/comprobante/", **INTERNA).json()
        assert datos["total_pagar"] == "75.50"
        assert [d["descripcion"] for d in datos["detalle"]] == ["Agosto 2026", "Septiembre 2026"]
        assert datos["qr_siat"] == ""


@pytest.mark.django_db
class TestCorreo:
    @pytest.fixture
    def facturado(self, sip, cobranzas):
        pago = _generar()
        sip.pagados.add(pago.alias)
        services.consultar_sip(pago)
        return pago

    @pytest.fixture
    def contacto_web(self, settings, monkeypatch):
        settings.KIOSCO_CONTACTO_URL = "https://web.test/api/gateway/clientes/{nro_cliente}/contacto"
        settings.API_KEY_CESSA_LARAVEL = "clave-laravel"
        llamadas = []

        def get(url, **kwargs):
            llamadas.append((url, kwargs["headers"]))
            return RespuestaJson({"correo": "juan.perez@gmail.com", "celular": "71234567"})

        monkeypatch.setattr(services.requests, "get", get)
        return llamadas

    def _enviar(self, pago, cuerpo):
        return _cliente().post(f"/api/kiosco/qr/{pago.alias}/enviar-correo/", cuerpo, format="json", **INTERNA)

    def test_correo_escrito_lleva_la_factura_adjunta(self, facturado, mailoutbox):
        r = self._enviar(facturado, {"correo": " Cliente@Ejemplo.com "})
        assert r.status_code == 200
        assert r.json()["enviado_a"] == "c******@ejemplo.com"
        assert len(mailoutbox) == 1
        correo = mailoutbox[0]
        assert correo.to == ["cliente@ejemplo.com"]
        assert correo.subject == "Tu factura CESSA - Cliente N° 153896 (correo automático, no responder)"
        assert "F-uuid-test" in correo.body and "NO RESPONDAS" in correo.body
        nombre, contenido, tipo = correo.attachments[0]
        assert tipo == "application/pdf" and contenido.startswith(b"%PDF")

    def test_correo_registrado_en_la_web_solo_se_ve_enmascarado(self, facturado, contacto_web, mailoutbox):
        r = self._enviar(facturado, {"destino": "registrado"})
        assert r.json()["enviado_a"] == "j*********@gmail.com"
        url, headers = contacto_web[0]
        assert url == "https://web.test/api/gateway/clientes/153896/contacto"
        assert headers["X-Api-Key"] == "clave-laravel"
        assert mailoutbox[0].to == ["juan.perez@gmail.com"]

    def test_sin_correo_registrado(self, facturado, mailoutbox):
        r = self._enviar(facturado, {"destino": "registrado"})
        assert r.status_code == 409
        assert not mailoutbox

    def test_correo_invalido(self, facturado, mailoutbox):
        assert self._enviar(facturado, {"correo": "no-es-correo"}).status_code == 409
        assert not mailoutbox

    def test_antes_de_facturar_no_envia(self, sip, mailoutbox):
        pago = _generar()
        assert self._enviar(pago, {"correo": "a@b.com"}).status_code == 409
        assert not mailoutbox

    def test_queda_registrado_y_se_ve_en_el_panel(self, facturado, contacto_web, mailoutbox, monkeypatch):
        from apps.facturacion_externa.panel import LiquidacionDetalleSerializer

        self._enviar(facturado, {"correo": "otro@ejemplo.com"})
        self._enviar(facturado, {"destino": "registrado"})

        def smtp_caido(*a, **k):
            raise OSError("SMTP caído")

        monkeypatch.setattr("django.core.mail.EmailMessage.send", smtp_caido)
        assert self._enviar(facturado, {"correo": "tercero@ejemplo.com"}).status_code == 409

        envios = list(facturado.envios.values_list("canal", "destino", "registrado", "exitoso"))
        assert envios == [
            ("correo", "otro@ejemplo.com", False, True),
            ("correo", "juan.perez@gmail.com", True, True),
            ("correo", "tercero@ejemplo.com", False, False),
        ]
        facturado.refresh_from_db()
        datos = LiquidacionDetalleSerializer(facturado.solicitud).data
        assert datos["generado_por"] == "Cajero automático (kiosco)"
        assert datos["cajero"]["pantalla"] == "oficina-central"
        assert [e["destino"] for e in datos["cajero"]["envios"]][-1] == "tercero@ejemplo.com"
        assert datos["cajero"]["envios"][-1]["error"] == "SMTP caído"

    def test_tope_de_envios_por_pago(self, facturado, mailoutbox):
        for _ in range(services.MAX_CORREOS_POR_PAGO):
            assert self._enviar(facturado, {"correo": "a@b.com"}).status_code == 200
        assert self._enviar(facturado, {"correo": "a@b.com"}).status_code == 409
        assert len(mailoutbox) == services.MAX_CORREOS_POR_PAGO


@pytest.mark.django_db
class TestWhatsapp:
    @pytest.fixture(autouse=True)
    def api_wpp(self, settings, monkeypatch):
        settings.KIOSCO_WHATSAPP_URL = "http://wpp.test/api/v1/notifications/text"
        settings.KIOSCO_WHATSAPP_API_KEY = "clave-wpp"
        enviados = []

        def post(url, **kwargs):
            enviados.append((url, kwargs))
            return RespuestaJson({"ok": True, "result": "sent"})

        monkeypatch.setattr(services.requests, "post", post)
        return enviados

    @pytest.fixture
    def facturado(self, sip, cobranzas, monkeypatch):
        monkeypatch.setattr(cobranzas, "obtener_comprobante_json", lambda uuid: {
            "nro_factura": "6164924", "codigo_autorizacion": "CUF123", "total_pagar": 75.5,
        })
        pago = _generar()
        sip.pagados.add(pago.alias)
        services.consultar_sip(pago)
        return pago

    def _enviar(self, pago, cuerpo):
        return _cliente().post(f"/api/kiosco/qr/{pago.alias}/enviar-whatsapp/", cuerpo, format="json", **INTERNA)

    def test_envia_texto_con_factura_y_link_del_siat(self, facturado, api_wpp):
        r = self._enviar(facturado, {"celular": "7123 4567"})
        assert r.status_code == 200
        assert r.json()["enviado_a"] == "7*****67"
        url, kwargs = api_wpp[0]
        assert url == "http://wpp.test/api/v1/notifications/text"
        assert kwargs["headers"]["x-api-key"] == "clave-wpp"
        assert kwargs["json"]["telefono"] == "59171234567"
        texto = kwargs["json"]["mensaje"]
        assert "Factura N°: 6164924" in texto
        assert "siat.impuestos.gob.bo" in texto and "cuf=CUF123" in texto
        assert "no respondas" in texto
        assert kwargs["json"]["id_evento"] == f"{facturado.alias}-wpp-1"
        envio = facturado.envios.get()
        assert (envio.canal, envio.destino, envio.registrado, envio.exitoso) == ("whatsapp", "59171234567", False, True)

    @pytest.mark.parametrize("numero", ["12345678", "7123456", "591812345678", "abc"])
    def test_numero_invalido(self, facturado, api_wpp, numero):
        assert self._enviar(facturado, {"celular": numero}).status_code == 409
        assert not api_wpp

    def test_celular_registrado_en_la_web(self, facturado, api_wpp, settings, monkeypatch):
        settings.KIOSCO_CONTACTO_URL = "https://web.test/api/gateway/clientes/{nro_cliente}/contacto"
        settings.API_KEY_CESSA_LARAVEL = "clave-laravel"
        monkeypatch.setattr(services.requests, "get", lambda url, **k: RespuestaJson({"correo": "a@b.com", "celular": "71234567"}))
        r = self._enviar(facturado, {"destino": "registrado"})
        assert r.json()["enviado_a"] == "7*****67"
        assert api_wpp[0][1]["json"]["telefono"] == "59171234567"

    def test_api_caida(self, facturado, monkeypatch):
        def caida(*a, **k):
            raise services.requests.ConnectionError("sin red")

        monkeypatch.setattr(services.requests, "post", caida)
        assert self._enviar(facturado, {"celular": "71234567"}).status_code == 409
        facturado.refresh_from_db()
        assert facturado.whatsapp_enviados == 0

    def test_sin_configurar(self, facturado, settings):
        settings.KIOSCO_WHATSAPP_URL = ""
        assert self._enviar(facturado, {"celular": "71234567"}).status_code == 409


def test_normalizar_celular():
    assert services.normalizar_celular("71234567") == "59171234567"
    assert services.normalizar_celular("+591 61234567") == "59161234567"
    assert services.normalizar_celular("41234567") is None


PAGINA_INICIO = """<html><body><script data-page="app" type="application/json">{"component":"Home","props":{
"emergencies":[{"type":"emergencia","reason":"CHOQUE DE POSTE","location":"Av. Hernando  Siles","execution_date":"2026-11-09T00:00:00.000000Z","start_time":"14:20:00","finish_time":null,"restored_at":null}],
"outages":[{"type":"programado","reason":"MEJORAS EN BAJA TENSIÓN","location":"Calle Honduras ","execution_date":"2026-11-10T00:00:00.000000Z","start_time":"05:00:00","finish_time":"09:00:00","restored_at":null}]
}}</script></body></html>"""


class RespuestaWeb:
    def __init__(self, texto, status=200):
        self.text = texto
        self.content = texto.encode()
        self.status_code = status
        self.ok = status < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


class RespuestaJson(RespuestaWeb):
    def __init__(self, datos, status=200):
        super().__init__("", status)
        self._datos = datos
        self.content = b"json"

    def json(self):
        return self._datos


@pytest.mark.django_db
class TestAvisos:
    @pytest.fixture(autouse=True)
    def limpiar_cache(self, settings):
        from django.core.cache import cache

        settings.KIOSCO_AVISOS_URL = "https://web.test/"
        settings.KIOSCO_CORTES_API_URL = ""
        settings.KIOSCO_CORTES_API_TOKEN = "token-cortes"
        settings.KIOSCO_CORTES_API_VERIFY = "true"
        cache.clear()

    def _web_y_api(self, monkeypatch, api, web=PAGINA_INICIO):
        from . import avisos

        llamadas = []

        def get(url, **kwargs):
            llamadas.append((url, kwargs))
            if url.startswith("https://api.test"):
                return api() if callable(api) else api
            return RespuestaWeb(web)

        monkeypatch.setattr(avisos.requests, "get", get)
        return llamadas

    def test_programados_de_la_api_y_emergencias_de_la_web(self, settings, monkeypatch):
        from . import avisos

        settings.KIOSCO_CORTES_API_URL = "https://api.test/v1/scheduled-outages"
        monkeypatch.setattr(avisos.timezone, "localdate", lambda: __import__("datetime").date(2026, 11, 10))
        api = RespuestaJson([
            {"id": 3207, "razon": "MANTENIMIENTO", "ubicacion": "Av. Del Maestro ", "fecha_ejecucion": "2026-11-10",
             "hora_inicio": "11:00:00", "hora_fin": "13:00:00"},
            {"id": 3201, "razon": "TRASPASO", "ubicacion": "Calle Honduras", "fecha_ejecucion": "2026-11-10",
             "hora_inicio": "05:00:00", "hora_fin": "09:00:00"},
            {"id": 3100, "razon": "VIEJO", "ubicacion": "X", "fecha_ejecucion": "2026-11-01",
             "hora_inicio": "08:00:00", "hora_fin": "09:00:00"},
        ])
        llamadas = self._web_y_api(monkeypatch, api)
        datos = avisos.avisos()
        assert [(a["tipo"], a["desde"], a["zonas"]) for a in datos] == [
            ("emergencia", "14:20", "Av. Hernando Siles"),
            ("programado", "05:00", "Calle Honduras"),
            ("programado", "11:00", "Av. Del Maestro"),
        ]
        headers = next(k["headers"] for u, k in llamadas if u.startswith("https://api.test"))
        assert headers["Authorization"] == "token-cortes"  # sin "Bearer"

    def test_api_caida_usa_los_programados_de_la_web(self, settings, monkeypatch):
        from . import avisos

        settings.KIOSCO_CORTES_API_URL = "https://api.test/v1/scheduled-outages"

        def caida():
            raise avisos.requests.ConnectionError("sin red")

        self._web_y_api(monkeypatch, caida)
        assert [a["motivo"] for a in avisos.avisos()] == ["CHOQUE DE POSTE", "MEJORAS EN BAJA TENSIÓN"]

    def test_lee_emergencias_y_programados_de_la_web(self, monkeypatch):
        from . import avisos

        monkeypatch.setattr(avisos.requests, "get", lambda *a, **k: RespuestaWeb(PAGINA_INICIO))
        datos = _cliente().get("/api/kiosco/avisos/", **INTERNA).json()["avisos"]
        assert datos == [
            {"tipo": "emergencia", "restablecido": False, "fecha": "2026-11-09", "desde": "14:20", "hasta": "",
             "zonas": "Av. Hernando Siles", "motivo": "CHOQUE DE POSTE"},
            {"tipo": "programado", "restablecido": False, "fecha": "2026-11-10", "desde": "05:00", "hasta": "09:00",
             "zonas": "Calle Honduras", "motivo": "MEJORAS EN BAJA TENSIÓN"},
        ]

    def test_galeria_lista_las_fotos_de_la_web_y_las_sirve(self, monkeypatch):
        from . import galeria

        pagina = PAGINA_INICIO.replace('"outages":', '"galleryHighlights":[{"id":60,"title":"35","url":"/storage/galeria/60.webp"},'
                                       '{"id":99,"title":"ajena","url":"https://otro.test/x.webp"}],"outages":')
        pedidas = []

        def get(url, **kwargs):
            pedidas.append(url)
            if url.endswith(".webp"):
                foto = RespuestaWeb("RIFF-webp")
                foto.headers = {"Content-Type": "image/webp"}
                return foto
            return RespuestaWeb(pagina)

        monkeypatch.setattr(galeria.requests, "get", get)
        monkeypatch.setattr("apps.kiosco.avisos.requests.get", get)
        assert _cliente().get("/api/kiosco/galeria/", **INTERNA).json() == {"fotos": [{"id": 60}]}  # la de otro sitio no

        respuesta = _cliente().get("/api/kiosco/galeria/60/", **INTERNA)
        assert respuesta.status_code == 200 and respuesta["Content-Type"] == "image/webp" and respuesta.content == b"RIFF-webp"
        assert "https://web.test/storage/galeria/60.webp" in pedidas
        _cliente().get("/api/kiosco/galeria/60/", **INTERNA)
        assert pedidas.count("https://web.test/storage/galeria/60.webp") == 1  # queda en caché

        assert _cliente().get("/api/kiosco/galeria/99/", **INTERNA).status_code == 404
        assert _cliente().get("/api/kiosco/galeria/60/", HTTP_X_REAL_IP="10.10.20.5").status_code in (401, 403)

    def test_galeria_rechaza_lo_que_no_es_imagen(self, monkeypatch):
        from . import galeria

        pagina = PAGINA_INICIO.replace('"outages":', '"galleryHighlights":[{"id":61,"url":"/storage/galeria/61.webp"}],"outages":')

        def get(url, **kwargs):
            if url.endswith(".webp"):
                otra = RespuestaWeb("<html>")
                otra.headers = {"Content-Type": "text/html"}
                return otra
            return RespuestaWeb(pagina)

        monkeypatch.setattr("apps.kiosco.avisos.requests.get", get)
        monkeypatch.setattr(galeria.requests, "get", get)
        assert _cliente().get("/api/kiosco/galeria/61/", **INTERNA).status_code == 404

    def test_sin_web_sigue_con_los_ultimos(self, monkeypatch):
        from django.core.cache import cache

        from . import avisos

        monkeypatch.setattr(avisos.requests, "get", lambda *a, **k: RespuestaWeb(PAGINA_INICIO))
        assert len(avisos.avisos()) == 2
        cache.delete(avisos.CLAVE_CACHE)
        monkeypatch.setattr(avisos.requests, "get", lambda *a, **k: RespuestaWeb("", 503))
        assert len(avisos.avisos()) == 2


def _auth_sip():
    return {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(b"sip-user:sip-pass").decode()}


@pytest.mark.django_db
class TestCallback:
    URL = "/api/externo/sip/confirmar-pago"

    def test_aviso_confirma_contra_sip_y_factura(self, sip, cobranzas, monkeypatch):
        import requests

        monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("un pago del kiosco no se reenvía"))
        pago = _generar()
        sip.pagados.add(pago.alias)
        r = _cliente().post(self.URL, {"alias": pago.alias}, format="json", **_auth_sip())
        assert r.json()["codigo"] == "0000"
        assert SolicitudLiquidacion.objects.get(alias=pago.alias).estado == SolicitudLiquidacion.Estado.FACTURADO

    def test_aviso_falso_no_marca_nada(self, sip):
        pago = _generar()
        r = _cliente().post(self.URL, {"alias": pago.alias}, format="json", **_auth_sip())
        assert r.json()["codigo"] == "0000"
        pago.refresh_from_db()
        assert pago.estado == PagoKiosco.Estado.PENDIENTE
        assert not SolicitudLiquidacion.objects.exists()

    def test_alias_desconocido(self, sip):
        r = _cliente().post(self.URL, {"alias": "CESSA-KIO-20261109100000-ZZZZ"}, format="json", **_auth_sip())
        assert r.json()["codigo"] == "9999"


@pytest.mark.django_db
class TestTareaPeriodica:
    def test_detecta_pago_con_la_pantalla_cerrada(self, sip, cobranzas):
        pago = _generar()
        sip.pagados.add(pago.alias)
        tasks.revisar_pagos_kiosco()
        assert SolicitudLiquidacion.objects.get(alias=pago.alias).estado == SolicitudLiquidacion.Estado.FACTURADO

    def test_reintenta_error_comun(self, sip, cobranzas):
        pago = _generar()
        solicitud = SolicitudLiquidacion.objects.create(
            alias=pago.alias, origen=SolicitudLiquidacion.Origen.KIOSCO, nro_cliente="153896", monto=pago.monto,
            detalle=pago.detalle, fecha_pago=timezone.now(), estado=SolicitudLiquidacion.Estado.ERROR,
            intentos=2, error="Timeout de red",
        )
        assert tasks.revisar_pagos_kiosco() == 1
        solicitud.refresh_from_db()
        assert solicitud.estado == SolicitudLiquidacion.Estado.FACTURADO

    @pytest.mark.parametrize("intentos,error", [
        (1, "La deuda cambió desde que se generó el QR: ya no están pendientes N° 101"),
        (5, "Timeout de red"),
    ])
    def test_no_reintenta_definitivos_ni_agotados(self, sip, cobranzas, intentos, error):
        pago = _generar()
        SolicitudLiquidacion.objects.create(
            alias=pago.alias, origen=SolicitudLiquidacion.Origen.KIOSCO, nro_cliente="153896", monto=pago.monto,
            detalle=pago.detalle, fecha_pago=timezone.now(), estado=SolicitudLiquidacion.Estado.ERROR,
            intentos=intentos, error=error,
        )
        assert tasks.revisar_pagos_kiosco() == 0
        assert cobranzas.llamadas_pagar == 0

    def test_caja_cerrada_se_reintenta_aunque_pasen_los_intentos(self, sip, cobranzas):
        pago = _generar()
        SolicitudLiquidacion.objects.create(
            alias=pago.alias, origen=SolicitudLiquidacion.Origen.KIOSCO, nro_cliente="153896", monto=pago.monto,
            detalle=pago.detalle, fecha_pago=timezone.now(), estado=SolicitudLiquidacion.Estado.ERROR,
            intentos=7, error="La caja está fuera de horario",
        )
        assert tasks.revisar_pagos_kiosco() == 1

    def test_no_toca_pagos_de_otros_canales(self, sip, cobranzas):
        SolicitudLiquidacion.objects.create(
            alias="CESSA-WEB-1", nro_cliente="153896", monto=Decimal("40.00"), detalle=[DEUDA[0]],
            fecha_pago=timezone.now(), estado=SolicitudLiquidacion.Estado.ERROR, intentos=1, error="Timeout",
        )
        assert tasks.revisar_pagos_kiosco() == 0


def test_origen_del_alias():
    assert SolicitudLiquidacion.origen_de_alias("CESSA-KIO-20261109101010-AB12") == SolicitudLiquidacion.Origen.KIOSCO


@pytest.mark.django_db
class TestHimno:
    def _a_las(self, monkeypatch, hora, minuto, segundo=0):
        from zoneinfo import ZoneInfo

        from . import himno

        momento = datetime(2026, 11, 10, hora, minuto, segundo, tzinfo=ZoneInfo("America/La_Paz"))
        monkeypatch.setattr(himno.timezone, "now", lambda: momento)

    def test_antes_del_mediodia_es_hoy_en_hora_de_bolivia(self, settings, monkeypatch):
        settings.KIOSCO_HIMNO_HORA = "12:00"
        self._a_las(monkeypatch, 11, 30)
        h = _cliente().get("/api/kiosco/himno/", **INTERNA).json()["himno"]
        assert h["fecha"] == "2026-11-10" and h["segundos_para"] == 30 * 60

    def test_en_el_minuto_del_himno_todavia_es_hoy(self, settings, monkeypatch):
        settings.KIOSCO_HIMNO_HORA = "12:00"
        self._a_las(monkeypatch, 12, 0, 40)
        h = _cliente().get("/api/kiosco/himno/", **INTERNA).json()["himno"]
        assert h["fecha"] == "2026-11-10" and h["segundos_para"] == 0

    def test_pasado_el_mediodia_es_manana(self, settings, monkeypatch):
        settings.KIOSCO_HIMNO_HORA = "12:00"
        self._a_las(monkeypatch, 15, 0)
        h = _cliente().get("/api/kiosco/himno/", **INTERNA).json()["himno"]
        assert h["fecha"] == "2026-11-11" and h["segundos_para"] == 21 * 3600

    def test_apagado(self, settings):
        settings.KIOSCO_HIMNO_HORA = ""
        assert _cliente().get("/api/kiosco/himno/", **INTERNA).json() == {"himno": None}

    def test_audio(self, settings, tmp_path):
        settings.KIOSCO_HIMNO_ARCHIVO = str(tmp_path / "himno.mp3")
        assert _cliente().get("/api/kiosco/himno/audio/", **INTERNA).status_code == 404
        assert _cliente().get("/api/kiosco/himno/", **INTERNA).json()["himno"]["audio"] is False

        (tmp_path / "himno.mp3").write_bytes(b"ID3-audio")
        r = _cliente().get("/api/kiosco/himno/audio/", **INTERNA)
        assert r.status_code == 200 and r["Content-Type"] == "audio/mpeg"
        assert b"".join(r.streaming_content) == b"ID3-audio"
        assert _cliente().get("/api/kiosco/himno/", **INTERNA).json()["himno"]["audio"] is True
        assert _cliente().get("/api/kiosco/himno/audio/", HTTP_X_REAL_IP="10.10.20.5").status_code in (401, 403)
