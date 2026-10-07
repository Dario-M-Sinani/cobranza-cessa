"""Cobro por comprobantes: SIIC cobra comprobantes enteros, del más antiguo al más
nuevo (ver services.resolver_seleccion). La consulta de deuda ya los trae en ese
orden, así que el panel cobra un prefijo de la lista."""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.clientes.models import Cliente
from apps.cobranza import services as cobranza_services
from apps.cobranza.models import CobroEfectivo, Deuda, Factura
from apps.cobranza.services import MontoInvalidoError, resolver_seleccion
from apps.usuarios.models import Usuario
from services.cobranzas_banco_client import CobranzasBancoClientInterface


def _item(nro, importe, debito_credito="DEBITO", **extra):
    return {
        "codigo_sucursal": "1", "nro_comprobante": str(nro), "nro_suministro": "1", "fecha": "20260101",
        "tipo": "31" if debito_credito == "CREDITO" else "3", "letra_comprobante": " ", "nro_autorizacion": "3",
        "nro_cliente": "115997", "anio": 2026, "mes": 1, "importe": str(importe), "detalle": f"Comp {nro}",
        "debito_credito": debito_credito, **extra,
    }


# Mismo patrón que el cliente 115997 en test: facturas y una NC en el medio.
ITEMS = [_item(1, "543.60"), _item(2, "1584.60"), _item(3, "-514.90", "CREDITO"), _item(4, "269.10")]
TOTAL = Decimal("1882.40")


@pytest.fixture
def cajera(db):
    return Usuario.objects.create_user(username="cajera-sel", password="x", rol=Usuario.Rol.CAJERA, activo=True)


@pytest.fixture
def deuda(db):
    cliente = Cliente.objects.create(codigo_externo="115997", nombre="Cliente con comprobantes")
    return Deuda.objects.create(cliente=cliente, monto=TOTAL, items_snapshot=ITEMS)


@pytest.mark.django_db
class TestResolverSeleccion:
    def test_sin_seleccion_cobra_todo(self, deuda):
        monto, items = resolver_seleccion(deuda)
        assert monto == TOTAL
        assert items == ITEMS

    def test_cantidad_cobra_los_mas_antiguos(self, deuda):
        monto, items = resolver_seleccion(deuda, cantidad=2)
        assert monto == Decimal("2128.20")
        assert [i["nro_comprobante"] for i in items] == ["1", "2"]

    def test_la_nota_de_credito_resta_en_su_lugar(self, deuda):
        monto, items = resolver_seleccion(deuda, cantidad=3)
        assert monto == Decimal("1613.30")
        assert len(items) == 3

    def test_monto_que_coincide_con_un_prefijo(self, deuda):
        monto, items = resolver_seleccion(deuda, monto=Decimal("2128.20"))
        assert monto == Decimal("2128.20")
        assert len(items) == 2

    def test_monto_arbitrario_se_rechaza_con_los_posibles(self, deuda):
        with pytest.raises(MontoInvalidoError) as exc:
            resolver_seleccion(deuda, monto=Decimal("50.00"))
        assert "543.60" in str(exc.value) and "2128.20" in str(exc.value)

    @pytest.mark.parametrize("cantidad", [0, 5])
    def test_cantidad_fuera_de_rango(self, deuda, cantidad):
        with pytest.raises(MontoInvalidoError):
            resolver_seleccion(deuda, cantidad=cantidad)

    def test_seleccion_que_no_deja_saldo_se_rechaza(self, db):
        cliente = Cliente.objects.create(codigo_externo="9", nombre="Solo NC primero")
        deuda = Deuda.objects.create(
            cliente=cliente, monto=Decimal("50.00"), items_snapshot=[_item(1, "-20.00", "CREDITO"), _item(2, "70.00")]
        )
        with pytest.raises(MontoInvalidoError):
            resolver_seleccion(deuda, cantidad=1)
        assert resolver_seleccion(deuda, cantidad=2)[0] == Decimal("50.00")


class _FakeBanco(CobranzasBancoClientInterface):
    def __init__(self):
        self.detalle = None

    def asegurar_caja_abierta(self):
        return None

    def crear_transaccion(self):
        return "uuid-sel"

    def pagar_transaccion(self, uuid, detalle, documento):
        raise NotImplementedError

    def pagar_transaccion_propia(self, uuid, detalle):
        self.detalle = detalle

    def obtener_comprobante_pdf(self, uuid):
        return b"%PDF"

    def obtener_comprobante_json(self, uuid):
        return {"nro_factura": "1"}


@pytest.mark.django_db
class TestCobroPorComprobantes:
    def test_efectivo_por_cantidad_por_api(self, cajera, deuda):
        client = APIClient()
        client.force_authenticate(user=cajera)

        response = client.post(
            "/api/cobros-efectivo/",
            {"deuda_id": deuda.pk, "cantidad_comprobantes": 1, "monto_recibido": "600.00"},
            format="json",
        )

        assert response.status_code == 201, response.data
        assert response.data["monto_snapshot"] == "543.60"
        assert response.data["vuelto"] == "56.40"
        assert [i["nro_comprobante"] for i in response.data["items_cobrados"]] == ["1"]

    def test_factura_paga_solo_los_comprobantes_cobrados(self, cajera, deuda, monkeypatch):
        """Regresión: un cobro parcial mandaba a pagar en SIIC TODA la deuda."""
        cobro = cobranza_services.registrar_cobro_efectivo(
            deuda=deuda, usuario=cajera, monto_recibido=Decimal("2200.00"), cantidad_comprobantes=2
        )
        fake = _FakeBanco()
        monkeypatch.setattr(cobranza_services, "get_cobranzas_banco_client", lambda: fake)

        factura = cobranza_services.enviar_factura_a_siic(Factura.objects.get(cobro_efectivo=cobro))

        assert factura.estado_envio == Factura.EstadoEnvio.ENVIADO, factura.error
        assert [d["nro_comprobante"] for d in fake.detalle] == ["1", "2"]

    def test_cobro_viejo_sin_items_cobrados_paga_la_deuda_completa(self, cajera, deuda, monkeypatch):
        cobro = CobroEfectivo.objects.create(
            deuda=deuda, usuario=cajera, monto_snapshot=TOTAL, monto_recibido=TOTAL, vuelto=Decimal("0")
        )
        factura = Factura.objects.create(cobro_efectivo=cobro)
        fake = _FakeBanco()
        monkeypatch.setattr(cobranza_services, "get_cobranzas_banco_client", lambda: fake)

        cobranza_services.enviar_factura_a_siic(factura)

        assert len(fake.detalle) == 4

    def test_la_deuda_serializada_trae_los_comprobantes_en_orden(self, deuda):
        from apps.cobranza.serializers import DeudaSerializer

        assert [i["nro_comprobante"] for i in DeudaSerializer(deuda).data["items"]] == ["1", "2", "3", "4"]
