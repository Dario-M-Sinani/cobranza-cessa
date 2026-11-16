"""Cotización del dólar: la del SIIC (manda) junto a la oficial del BCB (referencia), con alertas y
una fila por día como constancia (apps/cobranza/cotizacion.py, services/tipo_cambio.py)."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
import requests
from django.utils import timezone
from rest_framework.test import APIClient

from apps.cobranza import cotizacion
from apps.cobranza.models import CotizacionDolar
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


@pytest.mark.django_db
class TestCotizacionDeHoy:
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
