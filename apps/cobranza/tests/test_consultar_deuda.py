from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.usuarios.models import Usuario
from services.deuda_client import ClienteNoEncontradoError, DeudaClientError, ResultadoConsultaDeuda


@pytest.fixture
def cajera(db):
    return Usuario.objects.create_user(username="cajera1", password="x", rol=Usuario.Rol.CAJERA, activo=True)


@pytest.mark.django_db
class TestConsultarDeudaView:
    def test_consulta_exitosa_crea_cliente_y_deuda(self, cajera, monkeypatch):
        resultado = ResultadoConsultaDeuda(codigo_externo_cliente="123", nombre_cliente="Cliente Real", items=[])
        monkeypatch.setattr(
            "apps.cobranza.views.get_deuda_client",
            lambda: type("C", (), {"consultar_deuda": staticmethod(lambda codigo: resultado)})(),
        )

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.post("/api/deudas/consultar/", {"codigo_externo": "123"}, format="json")

        assert response.status_code == 201
        assert response.data["cliente"]["nombre"] == "Cliente Real"
        assert response.data["monto"] == "0.00"

    def test_cliente_no_encontrado_devuelve_404(self, cajera, monkeypatch):
        def _raise(codigo):
            raise ClienteNoEncontradoError(f"No se encontró ningún abonado con el número {codigo}.")

        monkeypatch.setattr(
            "apps.cobranza.views.get_deuda_client",
            lambda: type("C", (), {"consultar_deuda": staticmethod(_raise)})(),
        )

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.post("/api/deudas/consultar/", {"codigo_externo": "999"}, format="json")

        assert response.status_code == 404

    def test_falla_de_conexion_devuelve_502(self, cajera, monkeypatch):
        def _raise(codigo):
            raise DeudaClientError("no se pudo conectar")

        monkeypatch.setattr(
            "apps.cobranza.views.get_deuda_client",
            lambda: type("C", (), {"consultar_deuda": staticmethod(_raise)})(),
        )

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.post("/api/deudas/consultar/", {"codigo_externo": "123"}, format="json")

        assert response.status_code == 502
