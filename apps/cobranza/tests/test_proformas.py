"""Proformas (apps/cobranza/proformas.py): clientes de la proforma con su deuda hasta un mes."""
from unittest.mock import Mock, patch

import pytest

from services.cobranzas_banco_client import CajeraCobranzasClient

from .test_cajas_por_cajera import cajera_nest, con_nest  # noqa: F401 -- fixtures
from .test_cobro_agrupado import _api


def _respuesta(status, cuerpo):
    r = Mock(status_code=status, ok=200 <= status < 300, text=str(cuerpo))
    r.json.return_value = cuerpo
    return r


CABECERA = {"numero": "27", "descripcion": "EMPRESA DEMO ", "clientes": [
    {"nro_cliente": "127001", "nombre": "UNO", "direccion": "CALLE 1", "categoria_descripcion": "GENERAL"},
    {"nro_cliente": "127002", "nombre": "DOS", "direccion": "CALLE 2", "categoria_descripcion": "GENERAL"},
]}
DEUDAS = [{"nro_cliente": "127001", "deudas": [{"importe": "100.50"}, {"importe": "-20.00"}]}]


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest")
class TestProformas:
    def test_clientes_con_su_deuda_hasta_el_mes(self, cajera_nest):
        respuestas = {"/v1/proformas/27": _respuesta(200, CABECERA), "/v1/proformas/27/deudas": _respuesta(200, DEUDAS)}
        pedidos = []

        def request(self, metodo, ruta, **kwargs):
            pedidos.append((ruta, kwargs.get("params")))
            return respuestas[ruta]

        with patch.object(CajeraCobranzasClient, "_request_autenticado", autospec=True, side_effect=request):
            r = _api(cajera_nest).get("/api/proformas/27/?anio=2026&mes=8")

        assert r.status_code == 200, r.data
        assert pedidos[1] == ("/v1/proformas/27/deudas", {"anio": 2026, "mes": 8})
        assert r.data["descripcion"] == "EMPRESA DEMO" and r.data["con_deuda"] == 1
        assert [(c["nro_cliente"], c["cantidad"], c["total"]) for c in r.data["clientes"]] == [
            ("127001", 2, "80.50"), ("127002", 0, "0.00")]

    def test_proforma_sin_deuda(self, cajera_nest):
        respuestas = {"/v1/proformas/27": _respuesta(200, CABECERA), "/v1/proformas/27/deudas": _respuesta(404, {"error": "x"})}
        with patch.object(CajeraCobranzasClient, "_request_autenticado", autospec=True,
                          side_effect=lambda self, m, ruta, **k: respuestas[ruta]):
            r = _api(cajera_nest).get("/api/proformas/27/")
        assert r.status_code == 200 and r.data["con_deuda"] == 0

    def test_no_existe(self, cajera_nest):
        with patch.object(CajeraCobranzasClient, "_request_autenticado", return_value=_respuesta(404, {"error": "No existe"})):
            r = _api(cajera_nest).get("/api/proformas/999/")
        assert r.status_code == 404

    def test_mes_invalido(self, cajera_nest):
        assert _api(cajera_nest).get("/api/proformas/27/?mes=13").status_code == 400
