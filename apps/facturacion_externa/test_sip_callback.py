"""Tests del callback único de SIP (sip_callback.py). Nunca pega a SIP ni a cessa-laravel:
se monkeypatchean `get_mc4_client()` y `requests.post`."""
import base64
from datetime import datetime
from decimal import Decimal

import pytest
import requests
from rest_framework.test import APIClient

from apps.clientes.models import Cliente
from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Deuda, Factura, TransaccionQR
from apps.usuarios.models import Usuario
from services.mc4_client import EstadoPagoMC4, EstadoTransaccionQR

from . import sip_callback

URL = "/api/externo/sip/confirmar-pago"
LARAVEL = "https://laravel.test/api/pagos/sip/confirmar-pago"


def _auth(usuario="sip-user", clave="sip-pass"):
    return {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(f"{usuario}:{clave}".encode()).decode()}


@pytest.fixture(autouse=True)
def config_sip(settings):
    settings.SIP_CALLBACK_USERNAME = "sip-user"
    settings.SIP_CALLBACK_PASSWORD = "sip-pass"
    settings.SIP_CALLBACK_REENVIOS = {"CESSA-": LARAVEL}
    settings.API_KEY_CESSA_LARAVEL = "clave-laravel"


class Mc4Pagado:
    def __init__(self):
        self.consultas = 0

    def consultar_estado(self, alias):
        self.consultas += 1
        return EstadoTransaccionQR(
            alias=alias, estado=EstadoPagoMC4.PAGADO, fecha_procesamiento=datetime.now(),
            monto=None, numero_orden_originante="ORD-1", id_qr=None,
        )


class RespuestaFalsa:
    def __init__(self, status_code, cuerpo):
        self.status_code = status_code
        self._cuerpo = cuerpo

    def json(self):
        if isinstance(self._cuerpo, Exception):
            raise self._cuerpo
        return self._cuerpo


@pytest.fixture
def transaccion_pendiente(db):
    cliente = Cliente.objects.create(codigo_externo="C-1", nombre="Cliente")
    deuda = Deuda.objects.create(cliente=cliente, monto=Decimal("50.00"))
    cajera = Usuario.objects.create_user(username="caj", password="x", rol=Usuario.Rol.CAJERA, activo=True)
    transaccion = TransaccionQR.objects.create(deuda=deuda, usuario=cajera, monto_snapshot=Decimal("50.00"))
    transaccion.transicionar_estado(TransaccionQR.Estado.PENDIENTE_CONFIRMACION, usuario=cajera)
    return transaccion


@pytest.mark.django_db
class TestSipCallback:
    def test_sin_auth_o_auth_incorrecto_devuelve_401(self):
        client = APIClient()
        assert client.post(URL, {"alias": "CESSA-WEB-1"}, format="json").status_code == 401
        respuesta = client.post(URL, {"alias": "CESSA-WEB-1"}, format="json", **_auth(clave="mala"))
        assert respuesta.status_code == 401
        assert respuesta.json()["codigo"] == "9999"

    def test_sin_credenciales_configuradas_rechaza_todo(self, settings):
        settings.SIP_CALLBACK_USERNAME = ""
        settings.SIP_CALLBACK_PASSWORD = ""
        respuesta = APIClient().post(URL, {"alias": "x"}, format="json", **_auth("", ""))
        assert respuesta.status_code == 401

    def test_sin_alias_devuelve_9998(self):
        respuesta = APIClient().post(URL, {}, format="json", **_auth())
        assert respuesta.status_code == 400
        assert respuesta.json()["codigo"] == "9998"

    def test_alias_de_ningun_sistema_responde_9999(self):
        respuesta = APIClient().post(URL, {"alias": "OTRO-123"}, format="json", **_auth())
        assert respuesta.status_code == 200
        assert respuesta.json()["codigo"] == "9999"

    def test_qr_del_panel_se_confirma_contra_sip_y_factura(self, monkeypatch, transaccion_pendiente):
        mc4 = Mc4Pagado()
        monkeypatch.setattr(cobranza_services, "get_mc4_client", lambda: mc4)

        alias = f"txqr-{transaccion_pendiente.pk}"
        respuesta = APIClient().post(URL, {"alias": alias}, format="json", **_auth())

        assert respuesta.json()["codigo"] == "0000"
        transaccion_pendiente.refresh_from_db()
        assert transaccion_pendiente.estado == TransaccionQR.Estado.PAGADO
        assert Factura.objects.filter(transaccion_qr=transaccion_pendiente).count() == 1

        # Reintento de SIP: no vuelve a consultar ni duplica la factura.
        respuesta = APIClient().post(URL, {"alias": alias}, format="json", **_auth())
        assert respuesta.json()["codigo"] == "0000"
        assert mc4.consultas == 1
        assert Factura.objects.filter(transaccion_qr=transaccion_pendiente).count() == 1

    def test_qr_del_panel_inexistente_responde_9999(self):
        respuesta = APIClient().post(URL, {"alias": "txqr-999999"}, format="json", **_auth())
        assert respuesta.json()["codigo"] == "9999"

    def test_alias_cessa_se_reenvia_a_laravel_con_api_key(self, monkeypatch):
        llamadas = []

        def post_falso(url, json, headers, timeout):
            llamadas.append((url, json, headers))
            return RespuestaFalsa(200, {"codigo": "0000", "mensaje": "Registro Exitoso"})

        monkeypatch.setattr(sip_callback.requests, "post", post_falso)
        cuerpo = {"alias": "CESSA-WEB-20261022-AB12", "numeroOrdenOriginante": "ORD-9", "monto": 10}
        respuesta = APIClient().post(URL, cuerpo, format="json", **_auth())

        assert respuesta.status_code == 200
        assert respuesta.json() == {"codigo": "0000", "mensaje": "Registro Exitoso"}
        url, enviado, headers = llamadas[0]
        assert url == LARAVEL
        assert enviado == cuerpo
        assert headers["X-Api-Key"] == "clave-laravel"

    def test_laravel_caido_responde_9999_para_que_sip_reintente(self, monkeypatch):
        def post_falso(*args, **kwargs):
            raise requests.ConnectionError("sin ruta")

        monkeypatch.setattr(sip_callback.requests, "post", post_falso)
        respuesta = APIClient().post(URL, {"alias": "CESSA-WEB-1"}, format="json", **_auth())
        assert respuesta.status_code == 200
        assert respuesta.json()["codigo"] == "9999"

    def test_respuesta_no_json_de_laravel_responde_9999(self, monkeypatch):
        monkeypatch.setattr(
            sip_callback.requests, "post", lambda *a, **k: RespuestaFalsa(502, ValueError("html"))
        )
        respuesta = APIClient().post(URL, {"alias": "CESSA-WEB-1"}, format="json", **_auth())
        assert respuesta.status_code == 200
        assert respuesta.json()["codigo"] == "9999"

    def test_rechazo_de_laravel_se_pasa_tal_cual_sin_502(self, monkeypatch):
        monkeypatch.setattr(
            sip_callback.requests, "post",
            lambda *a, **k: RespuestaFalsa(401, {"codigo": "9999", "mensaje": "No autorizado"}),
        )
        respuesta = APIClient().post(URL, {"alias": "CESSA-WEB-1"}, format="json", **_auth())
        assert respuesta.status_code == 200
        assert respuesta.json()["codigo"] == "9999"
