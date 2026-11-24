"""Bancarización: un cobro de Bs 50.000 o más no se hace con QR (sí con cheque, depósito u otro documento)."""
from decimal import Decimal
from unittest.mock import Mock

import pytest

from apps.clientes.models import Cliente
from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Deuda, QRAgrupado, TransaccionQR

from .test_cobro_agrupado import _api, _item, cajera  # noqa: F401 -- fixtures


def _deuda(codigo, importe):
    cliente = Cliente.objects.create(codigo_externo=codigo, nombre=f"Cliente {codigo}")
    return Deuda.objects.create(cliente=cliente, monto=Decimal(importe), items_snapshot=[_item(int(codigo[-2:]), importe, codigo)])


@pytest.fixture
def sip(monkeypatch):
    cliente = Mock()
    cliente.generar_qr.return_value = Mock(imagen_qr_base64="iVBOR", id_transaccion="op-1")
    monkeypatch.setattr(cobranza_services, "get_mc4_client", lambda: cliente)
    return cliente


@pytest.mark.django_db
class TestQRBancarizacion:
    def test_menos_de_50000_se_genera(self, cajera, sip):  # noqa: F811
        deuda = _deuda("300001", "49999.99")
        r = _api(cajera).post("/api/transacciones-qr/", {"deuda_id": deuda.pk}, format="json")
        assert r.status_code == 201, r.data

    @pytest.mark.parametrize("importe", ["50000.00", "65920.70"])
    def test_50000_o_mas_no_se_genera(self, cajera, sip, importe):  # noqa: F811
        deuda = _deuda("300002", importe)
        r = _api(cajera).post("/api/transacciones-qr/", {"deuda_id": deuda.pk}, format="json")
        assert r.status_code == 400
        assert "bancarización" in r.data["detail"]
        assert not TransaccionQR.objects.exists() and sip.generar_qr.call_count == 0

    def test_varios_clientes_cuenta_el_total(self, cajera, sip):  # noqa: F811
        # Cada uno es menos de 50.000, pero el QR sería por el total.
        a, b = _deuda("300003", "30000.00"), _deuda("300004", "20000.00")
        items = [{"deuda_id": a.pk}, {"deuda_id": b.pk}]
        r = _api(cajera).post("/api/transacciones-qr/grupo/", {"items": items}, format="json")
        assert r.status_code == 400 and "bancarización" in r.data["detail"]
        assert not QRAgrupado.objects.exists() and sip.generar_qr.call_count == 0
