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


def _item(nro, importe, mes):
    from services.deuda_client import ItemDeuda

    return ItemDeuda(
        codigo_sucursal="1", nro_comprobante=nro, nro_suministro="1", fecha="20260912", tipo="3",
        letra_comprobante="", nro_autorizacion="3", nro_cliente="100001", anio=2026, mes=mes,
        importe=Decimal(importe), detalle=f"Fact Energia {mes}", debito_credito="DEBITO",
    )


@pytest.mark.django_db
class TestCobradoSinRegistrar:
    """Un cobro del panel que el SIIC todavía no registró (la factura se manda al imprimir o con la
    tarea de cada minuto) no se vuelve a ofrecer si la cajera consulta al cliente en ese rato."""

    def _consultar(self, cajera, monkeypatch):
        resultado = ResultadoConsultaDeuda(
            codigo_externo_cliente="100001", nombre_cliente="Patricia",
            items=[_item("3552309", "227.60", 9), _item("3707711", "91.30", 10)],
        )
        monkeypatch.setattr(
            "apps.cobranza.views.get_deuda_client",
            lambda: type("C", (), {"consultar_deuda": staticmethod(lambda codigo: resultado)})(),
        )
        monkeypatch.setattr("apps.cobranza.views.siic_historial.consumos", lambda codigo: {})
        client = APIClient()
        client.force_authenticate(cajera)
        return client.post("/api/deudas/consultar/", {"codigo_externo": "100001"}, format="json")

    def _cobro(self, cajera, estado):
        from dataclasses import asdict

        from apps.clientes.models import Cliente
        from apps.cobranza.models import CobroEfectivo, Deuda, Factura

        cliente, _ = Cliente.objects.get_or_create(codigo_externo="100001", defaults={"nombre": "Patricia"})
        septiembre = {**asdict(_item("3552309", "227.60", 9)), "importe": "227.60"}
        deuda = Deuda.objects.create(cliente=cliente, monto=Decimal("318.90"), items_snapshot=[septiembre])
        cobro = CobroEfectivo.objects.create(
            deuda=deuda, usuario=cajera, monto_snapshot=Decimal("227.60"), items_cobrados=[septiembre],
            monto_recibido=Decimal("227.60"), vuelto=Decimal("0"),
        )
        Factura.objects.create(cobro_efectivo=cobro, estado_envio=estado)

    def test_no_ofrece_lo_cobrado_que_falta_registrar(self, cajera, monkeypatch):
        self._cobro(cajera, "pendiente")
        response = self._consultar(cajera, monkeypatch)
        assert response.status_code == 201
        assert [i["nro_comprobante"] for i in response.data["items"]] == ["3707711"]
        assert response.data["monto"] == "91.30"
        assert response.data["datos_cliente"]["cobrados_sin_registrar"] == 1

    def test_ya_registrado_o_anulado_no_se_descuenta(self, cajera, monkeypatch):
        self._cobro(cajera, "enviado")  # el SIIC ya no lo devolvería; si lo devuelve, manda el SIIC
        self._cobro(cajera, "anulado")
        response = self._consultar(cajera, monkeypatch)
        assert len(response.data["items"]) == 2
        assert response.data["monto"] == "318.90"
