import pytest
from rest_framework.test import APIClient

from apps.auditoria.models import LogAuditoria
from apps.usuarios.models import Usuario


@pytest.fixture
def admin_user(db):
    return Usuario.objects.create_user(username="admin1", password="x", rol=Usuario.Rol.ADMIN)


@pytest.fixture
def supervisor(db):
    return Usuario.objects.create_user(username="sup1", password="x", rol=Usuario.Rol.SUPERVISOR)


@pytest.mark.django_db
class TestLogAuditoriaAPI:
    def test_solo_admin_puede_listar_auditoria(self, admin_user, supervisor):
        LogAuditoria.objects.create(usuario=admin_user, accion="algo", entidad_afectada="Test:1")

        client = APIClient()
        client.force_authenticate(supervisor)
        assert client.get("/api/auditoria/").status_code == 403

        client.force_authenticate(admin_user)
        response = client.get("/api/auditoria/")
        assert response.status_code == 200
        assert len(response.data) == 1
        assert response.data[0]["accion"] == "algo"
