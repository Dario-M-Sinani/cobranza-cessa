from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.auditoria.models import LogAuditoria
from apps.cobranza import models as cobranza_models
from apps.cobranza.models import AperturaCajaFueraDeHorario, Caja, CajaOperacionInvalida
from apps.usuarios.models import Usuario


@pytest.fixture
def cajera(db):
    return Usuario.objects.create_user(username="cajera1", password="x", rol=Usuario.Rol.CAJERA, activo=True)


@pytest.fixture
def otra_cajera(db):
    return Usuario.objects.create_user(username="cajera2", password="x", rol=Usuario.Rol.CAJERA, activo=True)


@pytest.fixture
def supervisor(db):
    return Usuario.objects.create_user(username="sup1", password="x", rol=Usuario.Rol.SUPERVISOR)


@pytest.fixture
def admin_user(db):
    return Usuario.objects.create_user(username="admin1", password="x", rol=Usuario.Rol.ADMIN)


@pytest.fixture
def dentro_de_horario(monkeypatch):
    """Fuerza que el reloj esté siempre dentro del horario operativo, para
    no depender de la hora real a la que corren los tests."""
    monkeypatch.setattr(cobranza_models, "dentro_de_horario_operativo", lambda: True)


@pytest.fixture
def fuera_de_horario(monkeypatch):
    monkeypatch.setattr(cobranza_models, "dentro_de_horario_operativo", lambda: False)


@pytest.mark.django_db
class TestCajaModel:
    def test_cajero_abre_su_caja_dentro_de_horario(self, dentro_de_horario, cajera):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)

        assert caja.estado == Caja.Estado.ABIERTA
        assert caja.abierta_por == cajera
        assert LogAuditoria.objects.filter(
            entidad_afectada=f"Caja:{caja.pk}", accion__contains="abierta"
        ).exists()

    def test_no_permite_dos_cajas_abiertas_del_mismo_cajero(self, dentro_de_horario, cajera):
        Caja.abrir(cajero=cajera, usuario=cajera)
        with pytest.raises(CajaOperacionInvalida):
            Caja.abrir(cajero=cajera, usuario=cajera)

    def test_cajero_no_puede_abrir_fuera_de_horario(self, fuera_de_horario, cajera):
        with pytest.raises(CajaOperacionInvalida):
            Caja.abrir(cajero=cajera, usuario=cajera)

    def test_supervisor_puede_abrir_fuera_de_horario_con_motivo(self, fuera_de_horario, cajera, supervisor):
        caja = Caja.abrir(cajero=cajera, usuario=supervisor, motivo="Cierre contable urgente")

        assert caja.estado == Caja.Estado.ABIERTA
        assert AperturaCajaFueraDeHorario.objects.filter(caja=caja, usuario=supervisor).exists()

    def test_supervisor_no_puede_abrir_fuera_de_horario_sin_motivo(self, fuera_de_horario, cajera, supervisor):
        with pytest.raises(CajaOperacionInvalida):
            Caja.abrir(cajero=cajera, usuario=supervisor)

    def test_cajero_cierra_su_propia_caja(self, dentro_de_horario, cajera):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        caja.cerrar(usuario=cajera)
        caja.refresh_from_db()

        assert caja.estado == Caja.Estado.CERRADA
        assert caja.cerrada_por == cajera
        assert caja.cerrada_en is not None

    def test_otro_cajero_no_puede_cerrar_una_caja_ajena(self, dentro_de_horario, cajera, otra_cajera):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        with pytest.raises(CajaOperacionInvalida):
            caja.cerrar(usuario=otra_cajera)

    def test_mismo_cajero_no_puede_reabrir_su_caja_cerrada(self, dentro_de_horario, cajera):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        caja.cerrar(usuario=cajera)

        with pytest.raises(CajaOperacionInvalida):
            caja.reabrir(usuario=cajera)

    def test_cajero_nunca_puede_reabrir_aunque_no_sea_quien_cerro(
        self, dentro_de_horario, cajera, otra_cajera, supervisor
    ):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        caja.cerrar(usuario=cajera)

        with pytest.raises(CajaOperacionInvalida):
            caja.reabrir(usuario=otra_cajera)

    def test_supervisor_reabre_caja_cerrada_por_otro(self, dentro_de_horario, cajera, supervisor):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        caja.cerrar(usuario=cajera)

        caja.reabrir(usuario=supervisor)
        caja.refresh_from_db()

        assert caja.estado == Caja.Estado.ABIERTA
        assert caja.cerrada_en is None
        assert caja.cerrada_por is None

    def test_reapertura_fuera_de_horario_requiere_motivo_y_queda_auditada(
        self, dentro_de_horario, fuera_de_horario, cajera, supervisor, monkeypatch
    ):
        # Abre y cierra dentro de horario, luego fuerza "fuera de horario"
        # solo para la reapertura.
        monkeypatch.setattr(cobranza_models, "dentro_de_horario_operativo", lambda: True)
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        caja.cerrar(usuario=cajera)

        monkeypatch.setattr(cobranza_models, "dentro_de_horario_operativo", lambda: False)
        with pytest.raises(CajaOperacionInvalida):
            caja.reabrir(usuario=supervisor)

        caja.reabrir(usuario=supervisor, motivo="Cajero olvidó cerrar el arqueo")
        assert AperturaCajaFueraDeHorario.objects.filter(caja=caja, usuario=supervisor).exists()

    def test_no_permite_modificar_estado_directamente_con_save(self, dentro_de_horario, cajera):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        caja.estado = Caja.Estado.CERRADA
        with pytest.raises(CajaOperacionInvalida):
            caja.save()


@pytest.mark.django_db
class TestCajaAPI:
    def test_cajera_abre_su_propia_caja(self, dentro_de_horario, cajera):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post("/api/cajas/", {}, format="json")

        assert response.status_code == 201
        assert response.data["estado"] == Caja.Estado.ABIERTA

    def test_cajera_no_puede_abrir_fuera_de_horario(self, fuera_de_horario, cajera):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post("/api/cajas/", {}, format="json")
        assert response.status_code == 400

    def test_cajera_solo_ve_su_propia_caja(self, dentro_de_horario, cajera, otra_cajera, supervisor):
        Caja.abrir(cajero=cajera, usuario=cajera)
        Caja.abrir(cajero=otra_cajera, usuario=otra_cajera)

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.get("/api/cajas/")
        assert response.status_code == 200
        assert len(response.data) == 1

        client.force_authenticate(supervisor)
        response = client.get("/api/cajas/")
        assert len(response.data) == 2

    def test_cajera_cierra_su_caja_por_api(self, dentro_de_horario, cajera):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post(f"/api/cajas/{caja.pk}/cerrar/")
        assert response.status_code == 200
        assert response.data["estado"] == Caja.Estado.CERRADA

    def test_cajera_no_puede_reabrir_caja(self, dentro_de_horario, cajera):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        caja.cerrar(usuario=cajera)

        client = APIClient()
        client.force_authenticate(cajera)
        response = client.post(f"/api/cajas/{caja.pk}/reabrir/")
        assert response.status_code == 403

    def test_supervisor_reabre_caja_por_api(self, dentro_de_horario, cajera, supervisor):
        caja = Caja.abrir(cajero=cajera, usuario=cajera)
        caja.cerrar(usuario=cajera)

        client = APIClient()
        client.force_authenticate(supervisor)
        response = client.post(f"/api/cajas/{caja.pk}/reabrir/", {}, format="json")
        assert response.status_code == 200
        assert response.data["estado"] == Caja.Estado.ABIERTA

    def test_supervisor_abre_caja_de_un_cajero_fuera_de_horario(self, fuera_de_horario, cajera, supervisor):
        client = APIClient()
        client.force_authenticate(supervisor)

        response = client.post(
            "/api/cajas/", {"cajero_id": cajera.pk, "motivo": "Arranque anticipado autorizado"}, format="json"
        )
        assert response.status_code == 201

    def test_solo_supervisor_admin_ve_aperturas_fuera_de_horario(
        self, fuera_de_horario, cajera, supervisor
    ):
        Caja.abrir(cajero=cajera, usuario=supervisor, motivo="Motivo de prueba")

        client = APIClient()
        client.force_authenticate(cajera)
        assert client.get("/api/cajas/aperturas_fuera_de_horario/").status_code == 403

        client.force_authenticate(supervisor)
        response = client.get("/api/cajas/aperturas_fuera_de_horario/")
        assert response.status_code == 200
        assert len(response.data) == 1
