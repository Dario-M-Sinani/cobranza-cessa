"""Cobrar con cheque, depósito, tarjeta... (apps/cobranza/formas_pago.py) y el tope de Bs 50.000 en
las cajas de los bancos."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Caja, CobroEfectivo, Factura
from services.cobranzas_banco_client import CajeraCobranzasClient

from .test_cajas_por_cajera import cajera_nest, con_nest, supervisor  # noqa: F401 -- fixtures
from .test_cobro_agrupado import _api, deudas  # noqa: F401 -- fixtures

ENTES = [{"id": 1, "descripcion": "CHEQUE"}, {"id": 3, "descripcion": "DEPOSITO"}, {"id": 7, "descripcion": "EFECTIVO"},
         {"id": 8, "descripcion": "PAGO POR QR"}]
BANCOS = [{"id": 3, "descripcion": "BANCO NACIONAL DE BOLIVIA"}]


@pytest.fixture
def catalogos():
    with patch.object(CajeraCobranzasClient, "catalogo", side_effect=lambda n: ENTES if n == "entes" else BANCOS):
        yield


@pytest.fixture
def con_caja(cajera_nest, supervisor):
    with patch.object(CajeraCobranzasClient, "aperturar_caja", return_value={}), \
         patch.object(CajeraCobranzasClient, "caja_del_dia", return_value={"estado": "ABIERTA", "dispositivo_id": 101}):
        return Caja.abrir(cajero=cajera_nest, usuario=supervisor, motivo="test", agencia_sigla="CEN")


def _fecha(dias: int) -> str:
    return (timezone.localdate() + timedelta(days=dias)).strftime("%Y%m%d")


def _cheque(**cambios):
    return {"ente_id": 1, "banco_id": 3, "numero": "CH-9", "fecha_registro": _fecha(0), "fecha_vencimiento": _fecha(5)} | cambios


def _cobrar(usuario, deuda, documento):
    return _api(usuario).post("/api/cobros-efectivo/", {"deuda_id": deuda.pk, "monto_recibido": "0", "documento": documento}, format="json")


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest", "catalogos")
class TestCobroConDocumento:
    def test_catalogo_sin_efectivo_ni_qr(self, cajera_nest):
        r = _api(cajera_nest).get("/api/cobros-efectivo/formas-pago/")
        assert r.data["disponible"] is True
        assert [e["descripcion"] for e in r.data["entes"]] == ["CHEQUE", "DEPOSITO"]

    def test_cheque_sin_vuelto(self, cajera_nest, con_caja, deudas):  # noqa: F811
        r = _cobrar(cajera_nest, deudas[0], _cheque())
        assert r.status_code == 201, r.data
        assert (r.data["monto_recibido"], r.data["vuelto"]) == ("39.70", "0.00")
        assert r.data["documento"]["ente"] == "CHEQUE" and r.data["documento"]["banco"] == "BANCO NACIONAL DE BOLIVIA"

    @pytest.mark.parametrize("cambios, mensaje", [
        ({"fecha_vencimiento": _fecha(2)}, "3 días o menos"),
        ({"fecha_vencimiento": _fecha(-1), "fecha_registro": _fecha(-10)}, "vencido"),
        ({"fecha_registro": _fecha(0), "fecha_vencimiento": _fecha(40)}, "más de 27 días"),
        ({"fecha_registro": _fecha(10), "fecha_vencimiento": _fecha(5)}, "anterior a su emisión"),
        ({"fecha_vencimiento": "2026-02-31"}, "no es válida"),
        ({"numero": ""}, "número"),
        ({"ente_id": 7}, "no existe en el SIIC"),  # efectivo no se elige como documento
    ])
    def test_reglas(self, cajera_nest, con_caja, deudas, cambios, mensaje):  # noqa: F811
        r = _cobrar(cajera_nest, deudas[0], _cheque(**cambios))
        assert r.status_code == 400
        assert mensaje in r.data["detail"]
        assert not CobroEfectivo.objects.exists()

    def test_deposito_no_pide_fechas(self, cajera_nest, con_caja, deudas):  # noqa: F811
        r = _cobrar(cajera_nest, deudas[0], {"ente_id": 3, "banco_id": 3, "numero": "DEP-1"})
        assert r.status_code == 201, r.data
        assert r.data["documento"]["fecha_vencimiento"] == _fecha(0)

    def test_usuario_local_no_puede(self, deudas, supervisor):  # noqa: F811
        from apps.usuarios.models import Usuario

        local = Usuario.objects.create_user(username="caj-loc-doc", password="x", rol=Usuario.Rol.CAJERA)
        r = _cobrar(local, deudas[0], _cheque())
        assert r.status_code == 400 and "caja del SIIC" in r.data["detail"]

    def test_se_factura_con_el_documento(self, cajera_nest, con_caja, deudas, monkeypatch):  # noqa: F811
        cobro = CobroEfectivo.objects.get(pk=_cobrar(cajera_nest, deudas[0], _cheque()).data["id"])
        cliente = Mock(spec=CajeraCobranzasClient, crear_transaccion=Mock(return_value="u-1"),
                       obtener_comprobante_pdf=Mock(return_value=b"%PDF"), obtener_comprobante_json=Mock(return_value={}))
        monkeypatch.setattr(cobranza_services, "_cliente_para", lambda u: cliente)

        cobranza_services.enviar_factura_a_siic(Factura.objects.get(cobro_efectivo=cobro))

        cliente.pagar_transaccion_propia.assert_not_called()
        documento = cliente.pagar_transaccion.call_args.args[2]
        assert (documento["ente_id"], documento["banco_id"], documento["numero"], documento["importe"]) == (1, 3, "CH-9", 39.7)
        assert documento["fecha_vencimiento"] == int(_fecha(5))

    def test_grupo_con_un_cheque(self, cajera_nest, con_caja, deudas):  # noqa: F811
        r = _api(cajera_nest).post("/api/cobros-agrupados/", {
            "monto_recibido": "0", "selecciones": [{"deuda_id": deudas[0].pk}, {"deuda_id": deudas[1].pk}],
            "documento": _cheque()}, format="json")
        assert r.status_code == 201, r.data
        assert (r.data["monto_recibido"], r.data["vuelto"]) == ("139.60", "0.00")
        assert {c["documento"]["numero"] for c in r.data["cobros"]} == {"CH-9"}


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest")
class TestTopeBancos:
    def test_banco_no_cobra_50000_o_mas(self, cajera_nest, con_caja, deudas):  # noqa: F811
        cajera_nest.entidad_financiera_id = 4
        cajera_nest.save()
        deudas[0].items_snapshot[0]["importe"] = "50000.00"
        deudas[0].monto = Decimal("50017.40")
        deudas[0].save()
        r = _api(cajera_nest).post("/api/cobros-efectivo/", {"deuda_id": deudas[0].pk, "monto_recibido": "60000"}, format="json")
        assert r.status_code == 400 and "oficinas de CESSA" in r.data["detail"]

    def test_cessa_si(self, cajera_nest, con_caja, deudas):  # noqa: F811
        deudas[0].items_snapshot[0]["importe"] = "50000.00"
        deudas[0].monto = Decimal("50017.40")
        deudas[0].save()
        r = _api(cajera_nest).post("/api/cobros-efectivo/", {"deuda_id": deudas[0].pk, "monto_recibido": "60000"}, format="json")
        assert r.status_code == 201, r.data
