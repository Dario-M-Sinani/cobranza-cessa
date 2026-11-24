"""Cotización del dólar: la del SIIC (manda) junto a la oficial del BCB (referencia), con alertas y
una fila por día como constancia (apps/cobranza/cotizacion.py, services/tipo_cambio.py)."""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
import requests
from django.utils import timezone
from rest_framework.test import APIClient

from apps.cobranza import cotizacion
from apps.auditoria.models import LogAuditoria
from apps.cobranza.models import CotizacionCambio, CotizacionDolar
from apps.cobranza.tasks import leer_cotizacion_bcb
from apps.usuarios import nest
from apps.usuarios.models import Usuario
from services import tipo_cambio
from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoRequestError
from services.tipo_cambio import TipoCambioBCB, TipoCambioError

PAGINA_BCB = """<html><head><style>.x{color:red}</style></head><body>
<h2>TIPO DE CAMBIO OFICIAL DEL DÓLAR ESTADOUNIDENSE</h2><p>Última cotización</p><p>Bs 11,67/$us</p>
<p>FECHA DE CORTE: VIERNES 13 DE NOVIEMBRE DE 2026</p>
<p>VIGENCIA: DEL SÁBADO 14 DE NOVIEMBRE DE 2026 AL LUNES 16 DE NOVIEMBRE DE 2026</p><a>Más información</a></body></html>"""

OFICIAL = TipoCambioBCB(Decimal("11.67"), "del sábado 14 al lunes 16", "bcb.gob.bo")


def _api(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


def _nest(username, rol):
    u = Usuario.objects.create_user(username=username, rol=rol, origen=Usuario.Origen.NEST, activo=True)
    nest.guardar_sesion(u, {"access_token": f"acc-{username}", "refresh_token": "r"})
    return u


@pytest.fixture
def supervisora(db):
    return _nest("SUPERV", Usuario.Rol.SUPERVISOR)


def _respuesta(texto="", json=None, status=200):
    r = Mock(status_code=status, text=texto)
    r.json.return_value = json
    r.raise_for_status = Mock(side_effect=None if status < 400 else requests.HTTPError(str(status)))
    return r


class TestLecturaBCB:
    def test_lee_la_pagina_del_bcb(self):
        with patch.object(requests, "get", return_value=_respuesta(PAGINA_BCB)):
            r = tipo_cambio.tipo_cambio_oficial()
        assert (r.valor, r.fuente) == (Decimal("11.67"), "bcb.gob.bo")
        assert r.vigencia.startswith("del sábado 14 de noviembre de 2026 al lunes 16")

    def test_si_el_bcb_falla_usa_factura_bo(self):
        datos = {"ok": True, "datos": {"usd_bob": 11.68, "vigencia_usd": "VIGENCIA: DEL LUNES 16 AL MARTES 17"}}
        respuestas = [requests.ConnectionError("caído"), _respuesta(json=datos)]
        with patch.object(requests, "get", side_effect=respuestas):
            r = tipo_cambio.tipo_cambio_oficial()
        assert (r.valor, r.fuente, r.vigencia) == (Decimal("11.68"), "factura.bo", "del lunes 16 al martes 17")

    def test_formato_desconocido_y_sin_respaldo(self):
        with patch.object(requests, "get", side_effect=[_respuesta("<p>otra cosa</p>"), _respuesta(json={"datos": {}})]):
            with pytest.raises(TipoCambioError, match="formato.*factura.bo no trae"):
                tipo_cambio.tipo_cambio_oficial()

    def test_valor_fuera_de_rango(self):
        with pytest.raises(TipoCambioError):
            tipo_cambio._decimal("0,07")


@pytest.fixture
def sin_api_siic(settings):
    settings.SIIC_DEUDA_BASE_URL = ""


@pytest.mark.django_db
@pytest.mark.usefixtures("sin_api_siic")
class TestCotizacionDeHoy:
    """Sin API SIIC configurada: se lee con el token de la Nest de quien mira."""

    URL = "/api/cotizacion-dolar/"

    def _consultar(self, usuario, siic="11.6700", oficial=OFICIAL):
        siic_mock = (Mock(side_effect=siic) if isinstance(siic, Exception) else Mock(return_value={"cotizacion": siic}))
        with patch.object(CajeraCobranzasClient, "cotizacion_dolar", siic_mock), \
             patch.object(cotizacion, "tipo_cambio_oficial", return_value=oficial):
            return _api(usuario).get(self.URL), siic_mock

    def test_coinciden_sin_alertas_y_queda_la_constancia(self, supervisora):
        r, siic = self._consultar(supervisora)
        assert r.status_code == 200, r.data
        assert (r.data["siic"], r.data["bcb"], r.data["bcb_fuente"], r.data["alertas"]) == ("11.6700", "11.6700", "bcb.gob.bo", [])
        siic.assert_called_once_with(timezone.localdate().strftime("%Y%m%d"))
        fila = CotizacionDolar.objects.get(fecha=timezone.localdate())
        assert fila.siic == Decimal("11.67") and fila.bcb == Decimal("11.67") and fila.siic_leida_en and fila.bcb_leida_en

    def test_sin_cotizacion_en_el_siic_avisa(self, supervisora):
        r, _ = self._consultar(supervisora, siic=None)
        assert [a["tipo"] for a in r.data["alertas"]] == ["sin_siic"]
        assert "no van a poder cerrar" in r.data["alertas"][0]["mensaje"]

    def test_distinta_de_la_oficial_avisa(self, supervisora):
        r, _ = self._consultar(supervisora, siic="11.5000")
        assert [a["tipo"] for a in r.data["alertas"]] == ["diferencia"]

    def test_la_del_siic_no_se_relee_a_cada_rato(self, supervisora):
        self._consultar(supervisora)
        _, siic = self._consultar(supervisora)
        siic.assert_not_called()

    def test_error_del_siic_se_muestra(self, supervisora):
        r, _ = self._consultar(supervisora, siic=CobranzasBancoRequestError("cotización del dólar: Nest caída"))
        assert "Nest caída" in r.data["siic_error"] and r.data["siic"] is None
        assert r.data["alertas"] == []  # no se sabe si falta: no se acusa

    def test_usuario_local_no_consulta_el_siic(self, db):
        local = Usuario.objects.create_user(username="sup-local", password="x", rol=Usuario.Rol.SUPERVISOR)
        r, siic = self._consultar(local)
        siic.assert_not_called()
        assert r.data["siic"] is None and r.data["bcb"] == "11.6700"

    def test_la_cajera_tambien_la_ve(self, db):
        cajera = _nest("CAJERA1", Usuario.Rol.CAJERA)
        r, _ = self._consultar(cajera)
        assert r.status_code == 200 and r.data["siic"] == "11.6700"


@pytest.mark.django_db
@pytest.mark.usefixtures("sin_api_siic")
class TestHistorialYTarea:
    def test_historial_dia_por_dia(self, supervisora):
        hoy = timezone.localdate()
        CotizacionDolar.objects.create(fecha=hoy - timedelta(days=1), siic=Decimal("11.60"), bcb=Decimal("11.60"))
        CotizacionDolar.objects.create(fecha=hoy, siic=None, bcb=Decimal("11.67"), siic_leida_en=timezone.now())
        r = _api(supervisora).get("/api/cotizacion-dolar/historial/", {"desde": (hoy - timedelta(days=5)).isoformat()})
        assert r.status_code == 200
        assert [d["fecha"] for d in r.data["dias"]] == [hoy.isoformat(), (hoy - timedelta(days=1)).isoformat()]
        assert r.data["dias"][0]["alertas"][0]["tipo"] == "sin_siic"

    def test_historial_solo_supervisor_o_admin(self, db):
        cajera = _nest("CAJERA1", Usuario.Rol.CAJERA)
        assert _api(cajera).get("/api/cotizacion-dolar/historial/").status_code == 403

    def test_la_tarea_guarda_la_oficial(self, db):
        with patch.object(cotizacion, "tipo_cambio_oficial", return_value=OFICIAL):
            leer_cotizacion_bcb()
        assert CotizacionDolar.objects.get(fecha=timezone.localdate()).bcb == Decimal("11.67")

    def test_la_tarea_anota_el_error_sin_romper(self, db):
        with patch.object(cotizacion, "tipo_cambio_oficial", side_effect=TipoCambioError("BCB no responde")):
            leer_cotizacion_bcb()
        assert CotizacionDolar.objects.get(fecha=timezone.localdate()).bcb_error == "BCB no responde"


class _Siic:
    """API SIIC falsa: GET/POST/PUT /v1/cajas/cotizacion sobre una "tabla" COTIZA en memoria."""

    def __init__(self, tabla=None, rechazo=None):
        self.tabla = dict(tabla or {})
        self.rechazo = rechazo  # (status, body) para POST/PUT
        self.escrituras = []

    def __call__(self, metodo, url, headers=None, params=None, json=None, **kw):
        assert url == "http://siic.test/v1/cajas/cotizacion" and headers["Authorization"] == "tok"
        if metodo == "get":
            return _respuesta(json={"fecha": params["fecha"], "cotizacion": self.tabla.get(params["fecha"])})
        self.escrituras.append((metodo, json))
        if self.rechazo:
            return _respuesta(json=self.rechazo[1], status=self.rechazo[0])
        f = json["fecha"]
        if metodo == "post":
            insertada = f not in self.tabla
            self.tabla.setdefault(f, f"{Decimal(json['cotizacion']):.4f}")
            return _respuesta(json={"fecha": f, "cotizacion": self.tabla[f], "insertada": insertada, "tabla": "RESTAURA.COTIZA"})
        if Decimal(self.tabla.get(f) or 0) != Decimal(json["anterior"]):
            return _respuesta(json={"error": f"ya no es {json['anterior']}", "code": 409}, status=409)
        self.tabla[f] = f"{Decimal(json['cotizacion']):.4f}"
        return _respuesta(json={"fecha": f, "anterior": json["anterior"], "cotizacion": self.tabla[f], "tabla": "RESTAURA.COTIZA"})


def _ymd(d):
    return d.strftime("%Y%m%d")


MESES_TEXTO = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _texto(d):
    return f"{d.day} de {MESES_TEXTO[d.month - 1]} de {d.year}"


class TestVigencia:
    HOY = date(2026, 11, 13)

    def test_completa_del_bcb(self):
        dias = cotizacion.dias_de_vigencia("del sábado 14 de noviembre de 2026 al lunes 16 de noviembre de 2026", self.HOY)
        assert [d.day for d in dias] == [14, 15, 16]

    def test_de_un_solo_dia(self):
        assert [d.day for d in cotizacion.dias_de_vigencia("martes 17 de noviembre de 2026", self.HOY)] == [17]

    def test_sin_mes_de_factura_bo_y_cruce_de_mes(self):
        assert [d.day for d in cotizacion.dias_de_vigencia("del lunes 16 al martes 17", self.HOY)] == [16, 17]
        fin = date(2026, 11, 30)
        assert [d.isoformat() for d in cotizacion.dias_de_vigencia("del lunes 30 al martes 1", fin)] == ["2026-11-30", "2026-12-01"]

    def test_lo_raro_no_se_carga(self):
        for raro in ["", "próximamente", "del 16 de noviembre de 2026 al 14 de noviembre de 2026",
                     "del 1 de noviembre de 2026 al 30 de noviembre de 2026", "del 14 de brumario de 2026 al 16 de noviembre de 2026"]:
            assert cotizacion.dias_de_vigencia(raro, self.HOY) == []


@pytest.mark.django_db
class TestCargaAutomatica:
    @pytest.fixture(autouse=True)
    def _api(self, settings):
        settings.SIIC_DEUDA_BASE_URL, settings.SIIC_DEUDA_TOKEN = "http://siic.test", "tok"
        settings.COTIZACION_CARGA_AUTOMATICA = True

    def _correr(self, siic, oficial):
        from services import deuda_client
        with patch.object(deuda_client.requests, "request", side_effect=siic), \
             patch.object(cotizacion, "tipo_cambio_oficial", return_value=oficial):
            return leer_cotizacion_bcb()

    def _oficial(self, desde, hasta):
        hoy = timezone.localdate()
        vigencia = f"del {_texto(hoy + timedelta(days=desde))} al {_texto(hoy + timedelta(days=hasta))}"
        return TipoCambioBCB(Decimal("11.75"), vigencia, "bcb.gob.bo")

    def test_carga_cada_dia_de_la_vigencia_que_falte_y_lo_registra(self):
        hoy = timezone.localdate()
        pasado = hoy + timedelta(days=2)
        siic = _Siic({_ymd(hoy + timedelta(days=1)): "11.7000"})  # mañana ya la cargó DTIC: no se toca
        hecho = self._correr(siic, self._oficial(0, 2))
        assert [(m, j["fecha"], j["cotizacion"]) for m, j in siic.escrituras] == [
            ("post", _ymd(hoy), "11.7500"), ("post", _ymd(pasado), "11.7500")]
        assert [h["fecha"] for h in hecho] == [hoy.isoformat(), pasado.isoformat()]
        cambios = list(CotizacionCambio.objects.order_by("fecha"))
        assert [(c.accion, c.usuario, c.valor_anterior, c.valor_nuevo, c.tabla) for c in cambios] == [
            ("carga_automatica", None, None, Decimal("11.75"), "RESTAURA.COTIZA")] * 2
        assert cambios[0].origen.startswith("bcb.gob.bo, vigencia del")
        assert LogAuditoria.objects.filter(usuario=None, accion__contains="Carga automática").count() == 2
        assert CotizacionDolar.objects.get(fecha=hoy).siic == Decimal("11.75")
        assert CotizacionDolar.objects.get(fecha=pasado).bcb == Decimal("11.75")

    def test_la_segunda_vez_no_hace_nada(self):
        siic = _Siic()
        self._correr(siic, self._oficial(0, 0))
        siic.escrituras.clear()
        CotizacionDolar.objects.update(siic_leida_en=None, bcb_leida_en=None)
        assert self._correr(siic, self._oficial(0, 0)) == [] and siic.escrituras == []
        assert CotizacionCambio.objects.count() == 1

    def test_vigencia_que_no_incluye_hoy_no_carga_y_avisa(self):
        siic = _Siic()
        self._correr(siic, self._oficial(-3, -2))
        assert siic.escrituras == []
        assert CotizacionDolar.objects.get(fecha=timezone.localdate()).carga_error.startswith("Vigencia")

    def test_rechazo_del_siic_queda_como_alerta(self):
        siic = _Siic(rechazo=(503, {"error": "La cotización del dólar en ADMINDTA.COTIZA la carga DTIC: esta API todavía no escribe ahí", "code": 503}))
        self._correr(siic, self._oficial(0, 0))
        fila = CotizacionDolar.objects.get(fecha=timezone.localdate())
        assert "DTIC" in fila.carga_error and CotizacionCambio.objects.count() == 0
        assert [a["tipo"] for a in cotizacion.estado(fila)["alertas"]] == ["sin_siic", "carga_fallida"]

    def test_apagada_no_escribe(self, settings):
        settings.COTIZACION_CARGA_AUTOMATICA = False
        siic = _Siic()
        assert self._correr(siic, self._oficial(0, 0)) == [] and siic.escrituras == []


@pytest.mark.django_db
class TestCorreccion:
    @pytest.fixture(autouse=True)
    def _api(self, settings):
        settings.SIIC_DEUDA_BASE_URL, settings.SIIC_DEUDA_TOKEN = "http://siic.test", "tok"
        settings.COTIZACION_EDITABLE = True

    @pytest.fixture
    def sup(self, db):
        return Usuario.objects.create_user(username="sup-local", password="x", rol=Usuario.Rol.SUPERVISOR)

    def test_sin_edicion_no_se_toca_el_siic(self, sup, settings):
        """Producción: solo la carga automática; corregir o cargar a mano se rechaza sin llamar al SIIC."""
        settings.COTIZACION_EDITABLE = False
        siic = _Siic({_ymd(timezone.localdate()): "11.7500"})
        r = self._put(sup, siic, valor="11,70", motivo="intento de corrección")
        assert r.status_code == 403
        assert siic.escrituras == []  # ni POST ni PUT al SIIC
        assert not CotizacionCambio.objects.exists()

    def _put(self, usuario, siic, fecha=None, **datos):
        from services import deuda_client
        fecha = fecha or timezone.localdate()
        with patch.object(deuda_client.requests, "request", side_effect=siic):
            return _api(usuario).put(f"/api/cotizacion-dolar/{fecha.isoformat()}/", datos, format="json")

    def test_corrige_con_el_anterior_y_lo_registra(self, sup):
        hoy = timezone.localdate()
        siic = _Siic({_ymd(hoy): "11.7500"})
        r = self._put(sup, siic, valor="11,70", motivo="BCB corrigió la publicación")
        assert r.status_code == 200, r.data
        assert siic.escrituras == [("put", {"fecha": _ymd(hoy), "anterior": "11.7500", "cotizacion": "11.7000"})]
        assert r.data["siic"] == "11.7000"
        [c] = r.data["cambios"]
        assert (c["accion"], c["valor_anterior"], c["valor_nuevo"], c["usuario"], c["motivo"]) == (
            "correccion", "11.7500", "11.7000", "sup-local", "BCB corrigió la publicación")
        assert LogAuditoria.objects.filter(usuario=sup, accion__contains="motivo: BCB corrigió").count() == 1

    def test_si_falta_la_carga_a_mano(self, sup):
        siic = _Siic()
        r = self._put(sup, siic, valor="11.75", motivo="no publicó el BCB")
        assert r.status_code == 200 and siic.escrituras[0][0] == "post"
        assert r.data["cambios"][0]["accion"] == "carga_manual"

    def test_motivo_obligatorio_mismo_valor_y_ventana(self, sup):
        hoy = timezone.localdate()
        siic = _Siic({_ymd(hoy): "11.7500"})
        assert self._put(sup, siic, valor="11.70", motivo="").status_code == 400
        assert self._put(sup, siic, valor="11.75", motivo="por las dudas").status_code == 400
        assert self._put(sup, siic, valor="11.70", motivo="x" * 6, fecha=hoy - timedelta(days=9)).status_code == 400
        assert siic.escrituras == [] and CotizacionCambio.objects.count() == 0

    def test_rechazo_del_siic(self, sup):
        hoy = timezone.localdate()
        siic = _Siic({_ymd(hoy): "11.7500"}, rechazo=(409, {"error": "La cotización del 20261118 ya no es 11.75 (ahora 11.7200)", "code": 409}))
        r = self._put(sup, siic, valor="11.70", motivo="corrección de prueba")
        assert r.status_code == 409 and "ya no es" in r.data["detail"] and CotizacionCambio.objects.count() == 0

    def test_solo_supervisor_o_admin(self, db):
        cajera = Usuario.objects.create_user(username="caj", password="x", rol=Usuario.Rol.CAJERA)
        assert self._put(cajera, _Siic(), valor="11.70", motivo="no debería").status_code == 403
