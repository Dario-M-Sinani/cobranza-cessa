from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.clientes.models import Cliente
from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Deuda, Factura, TransaccionQR
from apps.usuarios.models import Usuario
from services.mc4_client import EstadoPagoMC4, EstadoTransaccionQR, MC4ClientInterface, ResultadoQR


class FakeMC4Client(MC4ClientInterface):
    """Doble de prueba: nunca golpea la red, se comporta como si MC4/SIP
    respondiera lo que se le indique en `estado_a_devolver`."""

    def __init__(self):
        self.estado_a_devolver = EstadoPagoMC4.PAGADO

    def generar_qr(self, solicitud):
        return ResultadoQR(
            imagen_qr_base64="ZmFrZQ==",
            id_qr="qr-1",
            id_transaccion="op-1",
            fecha_vencimiento=None,
            banco_destino="BISA",
            cuenta_destino="0000",
        )

    def inhabilitar_qr(self, alias):
        pass

    def consultar_estado(self, alias):
        return EstadoTransaccionQR(
            alias=alias,
            estado=self.estado_a_devolver,
            fecha_procesamiento=None,
            monto=None,
            numero_orden_originante=None,
            id_qr=None,
        )


@pytest.fixture
def fake_mc4(monkeypatch):
    cliente = FakeMC4Client()
    monkeypatch.setattr(cobranza_services, "get_mc4_client", lambda: cliente)
    return cliente


@pytest.fixture
def cajera(db):
    return Usuario.objects.create_user(username="cajera1", password="x", rol=Usuario.Rol.CAJERA, activo=True)


@pytest.fixture
def supervisor(db):
    return Usuario.objects.create_user(username="sup1", password="x", rol=Usuario.Rol.SUPERVISOR)


@pytest.fixture
def admin_user(db):
    return Usuario.objects.create_user(username="admin1", password="x", rol=Usuario.Rol.ADMIN)


@pytest.fixture
def deuda(db):
    cliente = Cliente.objects.create(codigo_externo="C-100", nombre="Cliente API")
    return Deuda.objects.create(cliente=cliente, monto=Decimal("80.00"))


@pytest.mark.django_db
class TestGenerarTransaccionQR:
    def test_cajera_genera_qr(self, fake_mc4, cajera, deuda):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post("/api/transacciones-qr/", {"deuda_id": deuda.pk}, format="json")

        assert response.status_code == 201
        assert response.data["estado"] == TransaccionQR.Estado.PENDIENTE_CONFIRMACION

    def test_supervisor_no_puede_generar_qr(self, fake_mc4, supervisor, deuda):
        client = APIClient()
        client.force_authenticate(supervisor)

        response = client.post("/api/transacciones-qr/", {"deuda_id": deuda.pk}, format="json")

        assert response.status_code == 403

    def test_no_permite_generar_qr_con_deuda_en_cero(self, fake_mc4, cajera, deuda):
        deuda.monto = Decimal("0.00")
        deuda.save(update_fields=["monto"])

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.post("/api/transacciones-qr/", {"deuda_id": deuda.pk}, format="json")

        assert response.status_code == 400

    def test_genera_qr_por_adelanto_parcial(self, fake_mc4, cajera, deuda):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post(
            "/api/transacciones-qr/", {"deuda_id": deuda.pk, "monto": "20.00"}, format="json"
        )

        assert response.status_code == 201
        assert response.data["monto_snapshot"] == "20.00"
        assert Decimal(response.data["deuda"]["monto"]) == deuda.monto  # la deuda total no cambia

    def test_rechaza_adelanto_mayor_a_la_deuda(self, fake_mc4, cajera, deuda):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post(
            "/api/transacciones-qr/", {"deuda_id": deuda.pk, "monto": "999.00"}, format="json"
        )
        assert response.status_code == 400

    def test_rechaza_adelanto_en_cero_o_negativo(self, fake_mc4, cajera, deuda):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post("/api/transacciones-qr/", {"deuda_id": deuda.pk, "monto": "0.00"}, format="json")
        assert response.status_code == 400

    def test_no_permite_dos_qr_pendientes_del_mismo_cliente(self, fake_mc4, cajera, deuda):
        client = APIClient()
        client.force_authenticate(cajera)

        primera = client.post("/api/transacciones-qr/", {"deuda_id": deuda.pk}, format="json")
        assert primera.status_code == 201

        segunda_deuda = Deuda.objects.create(cliente=deuda.cliente, monto=deuda.monto)
        segunda = client.post("/api/transacciones-qr/", {"deuda_id": segunda_deuda.pk}, format="json")
        assert segunda.status_code == 409

    def test_cajera_solo_ve_sus_propias_transacciones(self, fake_mc4, cajera, supervisor, deuda):
        otra_cajera = Usuario.objects.create_user(
            username="cajera2", password="x", rol=Usuario.Rol.CAJERA, activo=True
        )
        TransaccionQR.objects.create(deuda=deuda, usuario=otra_cajera, monto_snapshot=deuda.monto)

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.get("/api/transacciones-qr/")
        assert response.status_code == 200
        assert response.data == []

        client.force_authenticate(supervisor)
        response = client.get("/api/transacciones-qr/")
        assert len(response.data) == 1


@pytest.mark.django_db
class TestVerificarPago:
    def test_verificar_pago_marca_pagado_y_crea_factura(self, fake_mc4, cajera, deuda):
        transaccion = cobranza_services.generar_transaccion_qr(deuda=deuda, usuario=cajera)
        fake_mc4.estado_a_devolver = EstadoPagoMC4.PAGADO

        cobranza_services.verificar_pago(transaccion)
        transaccion.refresh_from_db()

        assert transaccion.estado == TransaccionQR.Estado.PAGADO
        factura = Factura.objects.get(transaccion_qr=transaccion)
        assert factura.estado_envio == Factura.EstadoEnvio.PENDIENTE

    def test_reintentar_facturacion_requiere_transaccion_pagada(self, fake_mc4, cajera, supervisor, deuda):
        transaccion = cobranza_services.generar_transaccion_qr(deuda=deuda, usuario=cajera)

        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.post(f"/api/transacciones-qr/{transaccion.pk}/reintentar_facturacion/")
        assert response.status_code == 400

        client.force_authenticate(cajera)
        response = client.post(f"/api/transacciones-qr/{transaccion.pk}/reintentar_facturacion/")
        assert response.status_code == 403


@pytest.mark.django_db
class TestExportarCSV:
    def test_cajera_no_puede_exportar(self, fake_mc4, cajera, deuda):
        cobranza_services.generar_transaccion_qr(deuda=deuda, usuario=cajera)

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.get("/api/transacciones-qr/exportar_csv/")
        assert response.status_code == 403

    def test_supervisor_exporta_csv_con_las_transacciones(self, fake_mc4, cajera, supervisor, deuda):
        cobranza_services.generar_transaccion_qr(deuda=deuda, usuario=cajera)

        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.get("/api/transacciones-qr/exportar_csv/")

        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv"
        contenido = response.content.decode("utf-8")
        assert "cliente" in contenido.splitlines()[0]
        assert deuda.cliente.nombre in contenido


@pytest.mark.django_db
class TestUsuarioViewSet:
    def test_solo_admin_puede_listar_usuarios(self, cajera, admin_user):
        client = APIClient()
        client.force_authenticate(cajera)
        assert client.get("/api/usuarios/").status_code == 403

        client.force_authenticate(admin_user)
        assert client.get("/api/usuarios/").status_code == 200
