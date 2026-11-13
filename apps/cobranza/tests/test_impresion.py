"""Comprobante interno de cobro: la cajera lo imprime una sola vez; reimprimir es de
supervisor/admin y queda en Auditoría (POST /api/comprobantes/imprimir/)."""
import pytest

from apps.auditoria.models import LogAuditoria
from apps.cobranza.models import ImpresionComprobante
from apps.usuarios.models import Usuario

from .test_cobro_agrupado import _api, cajera, deudas  # noqa: F401 (fixtures)

URL = "/api/comprobantes/imprimir/"


@pytest.fixture
def supervisor(db):
    return Usuario.objects.create_user(username="sup-imp", password="x", rol=Usuario.Rol.SUPERVISOR)


def _cobrar(usuario, deuda):
    r = _api(usuario).post("/api/cobros-efectivo/", {"deuda_id": deuda.pk, "monto_recibido": "100.00"}, format="json")
    assert r.status_code == 201, r.data
    return r.data["id"]


@pytest.mark.django_db
class TestImpresionComprobante:
    def test_la_cajera_imprime_una_sola_vez(self, cajera, deudas):
        cobro = _cobrar(cajera, deudas[0])

        r = _api(cajera).post(URL, {"tipo": "efectivo", "id": cobro}, format="json")
        assert r.status_code == 200
        assert r.data == {"tipo": "efectivo", "id": cobro, "es_reimpresion": False, "impresiones": 1}

        r = _api(cajera).post(URL, {"tipo": "efectivo", "id": cobro}, format="json")
        assert r.status_code == 403
        assert "supervisor" in r.data["detail"]
        assert ImpresionComprobante.objects.count() == 1

    def test_el_supervisor_reimprime_y_queda_auditado(self, cajera, supervisor, deudas):
        cobro = _cobrar(cajera, deudas[0])
        _api(cajera).post(URL, {"tipo": "efectivo", "id": cobro}, format="json")

        r = _api(supervisor).post(URL, {"tipo": "efectivo", "id": cobro}, format="json")
        assert r.status_code == 200
        assert r.data["es_reimpresion"] is True and r.data["impresiones"] == 2
        log = LogAuditoria.objects.get(accion="Comprobante: reimpresión")
        assert log.usuario == supervisor and log.entidad_afectada == f"Efectivo:{cobro}"

    def test_la_primera_impresion_no_se_audita_como_reimpresion(self, cajera, deudas):
        cobro = _cobrar(cajera, deudas[0])
        _api(cajera).post(URL, {"tipo": "efectivo", "id": cobro}, format="json")
        assert not LogAuditoria.objects.filter(accion="Comprobante: reimpresión").exists()

    def test_un_efectivo_de_un_grupo_cuenta_como_el_grupo(self, cajera, deudas):
        a, b = deudas
        r = _api(cajera).post(
            "/api/cobros-agrupados/",
            {"monto_recibido": "200.00", "selecciones": [{"deuda_id": a.pk}, {"deuda_id": b.pk}]},
            format="json",
        )
        grupo = r.data["id"]
        assert _api(cajera).post(URL, {"tipo": "grupo", "id": grupo}, format="json").status_code == 200

        # Abrir el comprobante de uno de los cobros del grupo no da una segunda impresión.
        un_cobro = r.data["cobros"][0]["id"]
        r = _api(cajera).post(URL, {"tipo": "efectivo", "id": un_cobro}, format="json")
        assert r.status_code == 403

    def test_no_imprime_el_cobro_de_otra_cajera(self, cajera, deudas):
        otra = Usuario.objects.create_user(username="caj-imp", password="x", rol=Usuario.Rol.CAJERA, activo=True)
        cobro = _cobrar(otra, deudas[0])
        assert _api(cajera).post(URL, {"tipo": "efectivo", "id": cobro}, format="json").status_code == 404

    def test_tipo_o_id_invalidos(self, cajera):
        assert _api(cajera).post(URL, {"tipo": "otro", "id": 1}, format="json").status_code == 400
        assert _api(cajera).post(URL, {"tipo": "efectivo", "id": "x"}, format="json").status_code == 400


@pytest.mark.django_db
class TestComprobantesDelDia:
    URL_LISTA = "/api/comprobantes/"

    def test_solo_supervisor_o_admin(self, cajera):
        assert _api(cajera).get(self.URL_LISTA).status_code == 403

    def test_lista_efectivo_y_grupo_con_impresiones(self, cajera, supervisor, deudas):
        a, b = deudas
        individual = _cobrar(cajera, a)
        _api(cajera).post(URL, {"tipo": "efectivo", "id": individual}, format="json")
        r = _api(cajera).post(
            "/api/cobros-agrupados/",
            {"monto_recibido": "200.00", "selecciones": [{"deuda_id": a.pk}, {"deuda_id": b.pk}]},
            format="json",
        )
        grupo = r.data["id"]

        r = _api(supervisor).get(self.URL_LISTA)
        assert r.status_code == 200
        filas = {(f["tipo"], f["id"]): f for f in r.data["comprobantes"]}
        # Los cobros que son parte del grupo no salen sueltos: una fila por grupo.
        assert set(filas) == {("efectivo", individual), ("grupo", grupo)}
        assert filas[("efectivo", individual)]["impresiones"] == 1
        assert filas[("grupo", grupo)]["impresiones"] == 0
        assert [c["codigo"] for c in filas[("grupo", grupo)]["clientes"]] == ["101194", "101591"]
        assert filas[("efectivo", individual)]["numero"] == f"COMP-EF-{individual}"
        # Del más nuevo al más viejo.
        assert r.data["comprobantes"][0]["tipo"] == "grupo"

    def test_otro_dia_vacio_y_fecha_invalida(self, cajera, supervisor, deudas):
        _cobrar(cajera, deudas[0])
        assert _api(supervisor).get(self.URL_LISTA, {"fecha": "2020-01-01"}).data["comprobantes"] == []
        assert _api(supervisor).get(self.URL_LISTA, {"fecha": "ayer"}).status_code == 400
