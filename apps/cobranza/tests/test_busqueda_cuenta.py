"""Búsqueda por N° de cuenta (zona-manzano-correlativo) y datos del suministro en la consulta."""
from unittest.mock import Mock, patch

import pytest

from apps.usuarios.models import Usuario
from services import deuda_client
from services.deuda_client import SiicDeudaClient

from .test_cobro_agrupado import _api

RESPUESTA = {
    "nro_cliente": "153896", "zona": "5", "manzano": "59", "correlativo": "7421", "nombre": "JUAN PEREZ",
    "direccion": "CALLE ESPAÑA 73", "nro_medidor": "123456789", "estado_descripcion": "CORTADO",
    "categoria_descripcion": "DOMICILIARIA", "deuda": [],
}


@pytest.fixture
def cajera(db):
    return Usuario.objects.create_user(username="caj-cuenta", password="x", rol=Usuario.Rol.CAJERA)


def _siic(cuerpo=RESPUESTA, status=200):
    r = Mock(status_code=status)
    r.json.return_value = cuerpo
    return patch.object(deuda_client.requests, "get", return_value=r)


@pytest.mark.django_db
class TestBusquedaPorCuenta:
    @pytest.mark.parametrize("texto", ["05-059-07421", "0505907421", " 05-059-07421 "])
    def test_por_cuenta_manda_zona_manzano_correlativo(self, cajera, settings, texto):
        settings.DEUDA_CLIENT_CLASS = "services.deuda_client.SiicDeudaClient"
        with _siic() as get:
            r = _api(cajera).post("/api/deudas/consultar/", {"codigo_externo": texto}, format="json")

        assert r.status_code == 201, r.data
        assert get.call_args.kwargs["params"] == {"zona": "05", "manzano": "059", "correlativo": "07421", "ver_deuda": "si"}
        # El cliente queda con su número de cliente, no con la cuenta.
        assert r.data["cliente"]["codigo_externo"] == "153896"
        assert r.data["datos_cliente"] == {
            "nro_cuenta": "05-059-07421", "direccion": "CALLE ESPAÑA 73", "nro_medidor": "123456789",
            "categoria": "DOMICILIARIA", "estado": "CORTADO",
        }

    def test_por_numero_de_cliente_sigue_igual(self, cajera, settings):
        settings.DEUDA_CLIENT_CLASS = "services.deuda_client.SiicDeudaClient"
        with _siic() as get:
            r = _api(cajera).post("/api/deudas/consultar/", {"codigo_externo": "153896"}, format="json")
        assert r.status_code == 201
        assert get.call_args.kwargs["params"] == {"nro_cliente": "153896", "ver_deuda": "si"}

    def test_cuenta_inexistente(self, cajera, settings):
        settings.DEUDA_CLIENT_CLASS = "services.deuda_client.SiicDeudaClient"
        with _siic({"error": "No existe el cliente"}, 404):
            r = _api(cajera).post("/api/deudas/consultar/", {"codigo_externo": "99-999-99999"}, format="json")
        assert r.status_code == 404

    def test_fuente_sin_cuentas(self, cajera, settings):
        settings.DEUDA_CLIENT_CLASS = "services.fakes.FakeDeudaClient"
        r = _api(cajera).post("/api/deudas/consultar/", {"codigo_externo": "05-059-07421"}, format="json")
        assert r.status_code == 404 and "no está disponible" in r.data["detail"]

    def test_datos_del_suministro_desde_la_respuesta(self):
        with _siic():
            resultado = SiicDeudaClient(base_url="http://siic", token="t").consultar_deuda("153896")
        assert resultado.datos_cliente["estado"] == "CORTADO"
        assert resultado.datos_cliente["nro_cuenta"] == "05-059-07421"
