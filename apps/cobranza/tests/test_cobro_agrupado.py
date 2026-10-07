"""Un solo pago en efectivo para varios clientes (services.registrar_cobro_efectivo_agrupado)
y el consumo en kWh que acompaña a la deuda y al historial (services/siic_historial.py)."""
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient

from apps.clientes.models import Cliente
from apps.cobranza.models import Caja, CobroAgrupado, CobroEfectivo, Deuda, Factura, TransaccionQR
from apps.usuarios.models import Usuario
from services import siic_historial


def _item(nro, importe, cliente, anio=2026, mes=1):
    return {
        "codigo_sucursal": "1", "nro_comprobante": str(nro), "nro_suministro": "1", "fecha": "20260101",
        "tipo": "3", "letra_comprobante": " ", "nro_autorizacion": "3", "nro_cliente": cliente, "anio": anio,
        "mes": mes, "importe": str(importe), "detalle": f"Comp {nro}", "debito_credito": "DEBITO",
    }


@pytest.fixture
def cajera(db):
    return Usuario.objects.create_user(username="caj-grupo", password="x", rol=Usuario.Rol.CAJERA, activo=True)


@pytest.fixture
def deudas(db):
    a = Cliente.objects.create(codigo_externo="101194", nombre="Casa")
    b = Cliente.objects.create(codigo_externo="101591", nombre="Negocio")
    return (
        Deuda.objects.create(cliente=a, monto=Decimal("39.70"), items_snapshot=[_item(1, "22.30", "101194"), _item(2, "17.40", "101194")]),
        Deuda.objects.create(cliente=b, monto=Decimal("99.90"), items_snapshot=[_item(3, "99.90", "101591")]),
    )


def _api(usuario):
    c = APIClient()
    c.force_authenticate(user=usuario)
    return c


@pytest.mark.django_db
class TestCobroAgrupado:
    def test_un_pago_varios_clientes_un_vuelto(self, cajera, deudas):
        # Abierta por un supervisor con motivo: no depende de la hora en que corra el test.
        sup = Usuario.objects.create_user(username="sup-caja", password="x", rol=Usuario.Rol.SUPERVISOR)
        Caja.abrir(cajero=cajera, usuario=sup, motivo="test cobro agrupado")
        a, b = deudas
        r = _api(cajera).post(
            "/api/cobros-agrupados/",
            {"monto_recibido": "200.00", "selecciones": [{"deuda_id": a.pk, "cantidad_comprobantes": 1}, {"deuda_id": b.pk}]},
            format="json",
        )

        assert r.status_code == 201, r.data
        assert r.data["monto_total"] == "122.20"
        assert r.data["vuelto"] == "77.80"
        assert len(r.data["cobros"]) == 2
        grupo = CobroAgrupado.objects.get()
        assert grupo.caja is not None
        cobros = {c.deuda.cliente.codigo_externo: c for c in grupo.cobros.all()}
        assert cobros["101194"].monto_snapshot == Decimal("22.30")
        assert [i["nro_comprobante"] for i in cobros["101194"].items_cobrados] == ["1"]
        assert cobros["101591"].vuelto == Decimal("0")
        assert Factura.objects.filter(cobro_efectivo__grupo=grupo).count() == 2

    def test_recibido_insuficiente_no_registra_nada(self, cajera, deudas):
        a, b = deudas
        r = _api(cajera).post(
            "/api/cobros-agrupados/",
            {"monto_recibido": "100.00", "selecciones": [{"deuda_id": a.pk}, {"deuda_id": b.pk}]},
            format="json",
        )
        assert r.status_code == 400
        assert CobroEfectivo.objects.count() == 0 and CobroAgrupado.objects.count() == 0

    def test_una_seleccion_invalida_cancela_todo(self, cajera, deudas):
        a, b = deudas
        r = _api(cajera).post(
            "/api/cobros-agrupados/",
            {"monto_recibido": "500.00", "selecciones": [{"deuda_id": a.pk}, {"deuda_id": b.pk, "cantidad_comprobantes": 9}]},
            format="json",
        )
        assert r.status_code == 400
        assert CobroEfectivo.objects.count() == 0

    def test_mismo_cliente_dos_veces(self, cajera, deudas):
        a, _ = deudas
        otra = Deuda.objects.create(cliente=a.cliente, monto=a.monto, items_snapshot=a.items_snapshot)
        r = _api(cajera).post(
            "/api/cobros-agrupados/",
            {"monto_recibido": "500.00", "selecciones": [{"deuda_id": a.pk}, {"deuda_id": otra.pk}]},
            format="json",
        )
        assert r.status_code == 400

    def test_cliente_con_qr_pendiente_es_409(self, cajera, deudas):
        a, b = deudas
        TransaccionQR.objects.create(
            deuda=b, usuario=cajera, monto_snapshot=b.monto, estado=TransaccionQR.Estado.PENDIENTE_CONFIRMACION
        )
        r = _api(cajera).post(
            "/api/cobros-agrupados/",
            {"monto_recibido": "500.00", "selecciones": [{"deuda_id": a.pk}, {"deuda_id": b.pk}]},
            format="json",
        )
        assert r.status_code == 409
        assert CobroEfectivo.objects.count() == 0

    def test_supervisor_no_cobra(self, deudas):
        sup = Usuario.objects.create_user(username="sup-g", password="x", rol=Usuario.Rol.SUPERVISOR)
        a, _ = deudas
        r = _api(sup).post("/api/cobros-agrupados/", {"monto_recibido": "50", "selecciones": [{"deuda_id": a.pk}]}, format="json")
        assert r.status_code == 403

    def test_cajera_solo_ve_sus_grupos(self, cajera, deudas):
        otra = Usuario.objects.create_user(username="caj-otra", password="x", rol=Usuario.Rol.CAJERA, activo=True)
        a, _ = deudas
        _api(otra).post("/api/cobros-agrupados/", {"monto_recibido": "50", "selecciones": [{"deuda_id": a.pk}]}, format="json")
        assert _api(cajera).get("/api/cobros-agrupados/").data == []


def _resp(status, body):
    r = Mock()
    r.status_code = status
    r.json.return_value = body
    return r


class TestConsumos:
    def test_periodo_de_detalle(self):
        assert siic_historial.periodo_de_detalle("Fact Energia OCTUBRE/2025") == (2025, 10)
        assert siic_historial.periodo_de_detalle("NC. CONCILIACIÓN MAYO/2026") == (2026, 5)
        assert siic_historial.periodo_de_detalle("Varios") is None

    def test_cruza_por_periodo_e_importe(self, settings):
        settings.SIIC_DEUDA_BASE_URL = "http://siic.test"
        settings.SIIC_DEUDA_TOKEN = "tok"
        pendientes = [{"anio": "2026", "mes": "2", "importe": "543.60", "consumo": "403.00",
                       "lectura_anterior": "8138.00", "lectura_actual": "8541.00"}]
        with patch("services.siic_historial.requests.get", side_effect=[_resp(200, pendientes), _resp(404, {"error": "x"})]):
            tabla = siic_historial.consumos("115997")

        assert siic_historial.consumo_de({"anio": 2026, "mes": 2, "importe": "543.60"}, tabla)["consumo_kwh"] == "403.00"
        # Otro importe en el mismo período (ej. una ND) no se confunde con la factura de consumo.
        assert siic_historial.consumo_de({"anio": 2026, "mes": 2, "importe": "1584.60"}, tabla) is None
        assert siic_historial.consumo_de({"importe": "543.60"}, tabla, (2026, 2))["consumo_kwh"] == "403.00"

    def test_sin_siic_configurado_no_rompe(self, settings):
        settings.SIIC_DEUDA_BASE_URL = ""
        assert siic_historial.consumos("1") == {}
