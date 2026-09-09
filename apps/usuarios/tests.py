import pytest
from rest_framework.test import APIClient

from apps.usuarios.models import Usuario


@pytest.fixture
def cajera(db):
    return Usuario.objects.create_user(username="cajera1", password="Original123", rol=Usuario.Rol.CAJERA)


@pytest.fixture
def admin_user(db):
    return Usuario.objects.create_user(username="admin1", password="AdminPass123", rol=Usuario.Rol.ADMIN)


@pytest.fixture
def supervisor(db):
    return Usuario.objects.create_user(username="sup1", password="x", rol=Usuario.Rol.SUPERVISOR)


@pytest.mark.django_db
class TestResetPasswordPorAdmin:
    def test_admin_resetea_password_de_un_cajero(self, admin_user, cajera):
        client = APIClient()
        client.force_authenticate(admin_user)

        response = client.patch(f"/api/usuarios/{cajera.pk}/", {"password": "ResetPorAdmin123"}, format="json")
        assert response.status_code == 200

        respuesta_login = APIClient().post(
            "/api/auth/token/", {"username": "cajera1", "password": "ResetPorAdmin123"}, format="json"
        )
        assert respuesta_login.status_code == 200

    def test_reset_invalida_el_refresh_token_anterior_del_cajero(self, admin_user, cajera):
        login = APIClient().post(
            "/api/auth/token/", {"username": "cajera1", "password": "Original123"}, format="json"
        )
        refresh_viejo = login.data["refresh"]

        client = APIClient()
        client.force_authenticate(admin_user)
        client.patch(f"/api/usuarios/{cajera.pk}/", {"password": "ResetPorAdmin123"}, format="json")

        respuesta_refresh = APIClient().post(
            "/api/auth/token/refresh/", {"refresh": refresh_viejo}, format="json"
        )
        assert respuesta_refresh.status_code == 401

    def test_no_admin_no_puede_resetear_password_de_otro(self, cajera):
        otro_cajero = Usuario.objects.create_user(username="cajera2", password="x", rol=Usuario.Rol.CAJERA)
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.patch(f"/api/usuarios/{otro_cajero.pk}/", {"password": "loquesea123"}, format="json")
        assert response.status_code == 403


@pytest.mark.django_db
class TestResetPasswordCajeroPorSupervisor:
    def test_supervisor_resetea_password_de_un_cajero(self, supervisor, cajera):
        client = APIClient()
        client.force_authenticate(supervisor)

        response = client.post(
            "/api/usuarios/resetear-password-cajero/",
            {"username": "cajera1", "password": "ResetPorSupervisor123"},
            format="json",
        )
        assert response.status_code == 204

        respuesta_login = APIClient().post(
            "/api/auth/token/", {"username": "cajera1", "password": "ResetPorSupervisor123"}, format="json"
        )
        assert respuesta_login.status_code == 200

    def test_supervisor_no_puede_resetear_password_de_otro_supervisor_o_admin(self, supervisor, admin_user):
        client = APIClient()
        client.force_authenticate(supervisor)

        response = client.post(
            "/api/usuarios/resetear-password-cajero/",
            {"username": admin_user.username, "password": "loquesea12345"},
            format="json",
        )
        assert response.status_code == 404

    def test_cajero_no_puede_usar_el_endpoint(self, cajera):
        client = APIClient()
        client.force_authenticate(cajera)

        response = client.post(
            "/api/usuarios/resetear-password-cajero/",
            {"username": "cajera1", "password": "loquesea12345"},
            format="json",
        )
        assert response.status_code == 403

    def test_admin_tambien_puede_usar_este_endpoint(self, admin_user, cajera):
        client = APIClient()
        client.force_authenticate(admin_user)

        response = client.post(
            "/api/usuarios/resetear-password-cajero/",
            {"username": "cajera1", "password": "ResetPorAdmin456"},
            format="json",
        )
        assert response.status_code == 204
