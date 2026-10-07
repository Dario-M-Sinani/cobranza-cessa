"""Historial de facturas pagadas y reimpresión (services/siic_historial.py). No pega a la
red: se mockea requests."""
from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient

from apps.auditoria.models import LogAuditoria
from apps.usuarios.models import Usuario

CLAVE = {
    "codigo_sucursal": "1", "nro_comprobante": "1642020", "nro_suministro": "1", "fecha": "20251013",
    "tipo": "3", "letra_comprobante": "            ", "nro_autorizacion": "3", "nro_cliente": "115997",
}


def _resp(status=200, json_body=None, content=b""):
    r = Mock()
    r.status_code = status
    r.json.return_value = json_body
    r.content = content
    return r


@pytest.fixture
def cliente_api(db, settings):
    settings.SIIC_DEUDA_BASE_URL = "http://siic.test"
    settings.SIIC_DEUDA_TOKEN = "tok"
    usuario = Usuario.objects.create_user(username="caj-hist", password="x", rol=Usuario.Rol.CAJERA, activo=True)
    client = APIClient()
    client.force_authenticate(user=usuario)
    return client


@pytest.mark.django_db
class TestFacturasPagadas:
    def test_lista_todas_sin_limite_y_repara_codificacion(self, cliente_api):
        item = {**CLAVE, "importe": "475.40", "pago_fecha": "20260108", "pago_hora": "09:02:18",
                "detalle": "NC. CONCILIACIÃ\x93N"}
        with patch("services.siic_historial.requests.get", return_value=_resp(json_body={"items": [item]})) as get:
            r = cliente_api.get("/api/clientes/115997/facturas-pagadas/")

        assert r.status_code == 200
        assert r.data["items"][0]["detalle"] == "NC. CONCILIACIÓN"
        args, kwargs = get.call_args
        assert args[0] == "http://siic.test/v1/clientes/115997/pagos"
        assert kwargs["params"] == {"limit": -1}
        assert kwargs["headers"] == {"Authorization": "tok"}

    def test_siic_caido_es_502(self, cliente_api):
        with patch("services.siic_historial.requests.get", return_value=_resp(status=500, json_body={})):
            assert cliente_api.get("/api/clientes/115997/facturas-pagadas/").status_code == 502

    def test_codigo_invalido(self, cliente_api):
        assert cliente_api.get("/api/clientes/abc/facturas-pagadas/").status_code == 400

    def test_requiere_login(self, db):
        assert APIClient().get("/api/clientes/115997/facturas-pagadas/").status_code == 401


@pytest.mark.django_db
class TestFacturaPagadaPdf:
    URL = "/api/clientes/115997/facturas-pagadas/pdf/"

    def test_devuelve_el_pdf_y_audita(self, cliente_api):
        with patch("services.siic_historial.requests.post", return_value=_resp(content=b"%PDF-1.4 x")) as post:
            r = cliente_api.post(self.URL, {**CLAVE, "detalle": "ignorado"}, format="json")

        assert r.status_code == 200
        assert r["Content-Type"] == "application/pdf"
        assert r.content == b"%PDF-1.4 x"
        assert post.call_args.kwargs["json"] == {"formato": "pdf", "items": [CLAVE]}
        assert LogAuditoria.objects.filter(accion="Factura SIIC: reimpresión").exists()

    def test_comprobante_de_otro_cliente_se_rechaza(self, cliente_api):
        with patch("services.siic_historial.requests.post") as post:
            r = cliente_api.post(self.URL, {**CLAVE, "nro_cliente": "999"}, format="json")
        assert r.status_code == 400
        post.assert_not_called()

    def test_clave_incompleta(self, cliente_api):
        assert cliente_api.post(self.URL, {"nro_comprobante": "1"}, format="json").status_code == 400

    def test_siic_no_genera_pdf_es_502(self, cliente_api):
        with patch("services.siic_historial.requests.post", return_value=_resp(status=404, content=b"{}")):
            assert cliente_api.post(self.URL, CLAVE, format="json").status_code == 502
