from decimal import Decimal

import pytest
from django.db import IntegrityError
from rest_framework.test import APIClient

from apps.clientes.models import Cliente
from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Caja, CobroEfectivo, Deuda, Factura, TransaccionQR
from apps.usuarios.models import Usuario


@pytest.fixture
def cajera(db):
    return Usuario.objects.create_user(username="cajera1", password="x", rol=Usuario.Rol.CAJERA, activo=True)


@pytest.fixture
def supervisor(db):
    return Usuario.objects.create_user(username="sup1", password="x", rol=Usuario.Rol.SUPERVISOR)


@pytest.fixture
def deuda(db):
    cliente = Cliente.objects.create(codigo_externo="C-200", nombre="Cliente Efectivo")
    return Deuda.objects.create(cliente=cliente, monto=Decimal("100.00"))


@pytest.mark.django_db
class TestRegistrarCobroEfectivoService:
    def test_registra_cobro_y_crea_factura(self, cajera, deuda):
        cobro = cobranza_services.registrar_cobro_efectivo(
            deuda=deuda, usuario=cajera, monto_recibido=Decimal("150.00")
        )

        assert cobro.monto_snapshot == Decimal("100.00")
        assert cobro.vuelto == Decimal("50.00")
        assert cobro.caja is None  # cajera no tenía caja abierta
        factura = Factura.objects.get(cobro_efectivo=cobro)
        assert factura.estado_envio == Factura.EstadoEnvio.PENDIENTE
        assert factura.transaccion_qr is None

    def test_vincula_la_caja_abierta_del_cajero(self, cajera, deuda):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        cobro = cobranza_services.registrar_cobro_efectivo(
            deuda=deuda, usuario=cajera, monto_recibido=Decimal("100.00")
        )
        assert cobro.caja == caja
        assert cobro.vuelto == Decimal("0.00")

    def test_rechaza_monto_insuficiente(self, cajera, deuda):
        with pytest.raises(cobranza_services.MontoRecibidoInsuficienteError):
            cobranza_services.registrar_cobro_efectivo(
                deuda=deuda, usuario=cajera, monto_recibido=Decimal("50.00")
            )

    def test_rechaza_si_ya_hay_qr_pendiente_del_mismo_cliente(self, cajera, deuda):
        TransaccionQR.objects.create(deuda=deuda, usuario=cajera, monto_snapshot=deuda.monto)

        with pytest.raises(cobranza_services.TransaccionEnCursoError):
            cobranza_services.registrar_cobro_efectivo(
                deuda=deuda, usuario=cajera, monto_recibido=Decimal("100.00")
            )

    def test_factura_no_puede_tener_ambos_origenes_ni_ninguno(self, cajera, deuda):
        transaccion = TransaccionQR.objects.create(deuda=deuda, usuario=cajera, monto_snapshot=deuda.monto)
        cobro = CobroEfectivo.objects.create(
            deuda=deuda, usuario=cajera, monto_snapshot=deuda.monto,
            monto_recibido=deuda.monto, vuelto=Decimal("0.00"),
        )

        with pytest.raises(IntegrityError):
            Factura.objects.create(transaccion_qr=transaccion, cobro_efectivo=cobro)


@pytest.mark.django_db
class TestCobroEfectivoAPI:
    def test_cajera_registra_cobro_efectivo(self, cajera, deuda):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post(
            "/api/cobros-efectivo/", {"deuda_id": deuda.pk, "monto_recibido": "120.00"}, format="json"
        )

        assert response.status_code == 201
        assert response.data["vuelto"] == "20.00"
        assert response.data["factura"] is not None

    def test_supervisor_no_puede_registrar_cobro_efectivo(self, supervisor, deuda):
        client = APIClient()
        client.force_authenticate(supervisor)

        response = client.post(
            "/api/cobros-efectivo/", {"deuda_id": deuda.pk, "monto_recibido": "120.00"}, format="json"
        )
        assert response.status_code == 403

    def test_monto_insuficiente_devuelve_400(self, cajera, deuda):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post(
            "/api/cobros-efectivo/", {"deuda_id": deuda.pk, "monto_recibido": "10.00"}, format="json"
        )
        assert response.status_code == 400

    def test_no_permite_cobrar_deuda_en_cero(self, cajera, deuda):
        deuda.monto = Decimal("0.00")
        deuda.save(update_fields=["monto"])

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.post(
            "/api/cobros-efectivo/", {"deuda_id": deuda.pk, "monto_recibido": "0.00"}, format="json"
        )
        assert response.status_code == 400

    def test_cobra_un_adelanto_parcial_en_efectivo(self, cajera, deuda):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post(
            "/api/cobros-efectivo/",
            {"deuda_id": deuda.pk, "monto_a_cobrar": "30.00", "monto_recibido": "30.00"},
            format="json",
        )

        assert response.status_code == 201
        assert response.data["monto_snapshot"] == "30.00"
        assert response.data["vuelto"] == "0.00"
        assert Decimal(response.data["deuda"]["monto"]) == deuda.monto  # la deuda total no cambia

    def test_adelanto_en_efectivo_exige_cubrir_el_adelanto_no_la_deuda_total(self, cajera, deuda):
        client = APIClient()
        client.force_authenticate(cajera)

        # Cubre el adelanto (30) pero no la deuda total (100) -- debe alcanzar.
        response = client.post(
            "/api/cobros-efectivo/",
            {"deuda_id": deuda.pk, "monto_a_cobrar": "30.00", "monto_recibido": "30.00"},
            format="json",
        )
        assert response.status_code == 201

    def test_rechaza_adelanto_mayor_a_la_deuda(self, cajera, deuda):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post(
            "/api/cobros-efectivo/",
            {"deuda_id": deuda.pk, "monto_a_cobrar": "999.00", "monto_recibido": "999.00"},
            format="json",
        )
        assert response.status_code == 400

    def test_cajera_solo_ve_sus_propios_cobros(self, cajera, supervisor, deuda):
        otra_cajera = Usuario.objects.create_user(
            username="cajera2", password="x", rol=Usuario.Rol.CAJERA, activo=True
        )
        cobranza_services.registrar_cobro_efectivo(deuda=deuda, usuario=otra_cajera, monto_recibido=deuda.monto)

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.get("/api/cobros-efectivo/")
        assert response.status_code == 200
        assert response.data == []

        client.force_authenticate(supervisor)
        response = client.get("/api/cobros-efectivo/")
        assert len(response.data) == 1
