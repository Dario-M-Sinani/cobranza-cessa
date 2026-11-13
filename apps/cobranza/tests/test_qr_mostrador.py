"""QR del mostrador: la tarjeta pregunta por el pago (consulta a SIP en el momento, con tope) y la
anulación inhabilita el QR en SIP antes de cerrarlo (si justo pagó, queda pagado y facturado)."""
from decimal import Decimal
from unittest.mock import Mock

import pytest
from django.core.cache import cache

from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Factura, TransaccionQR
from services.mc4_client import EstadoPagoMC4, MC4ClientError

from .test_cobro_agrupado import _api, cajera, deudas  # noqa: F401 -- fixtures


@pytest.fixture(autouse=True)
def cache_limpia():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def qr(cajera, deudas):  # noqa: F811
    return TransaccionQR.objects.create(
        deuda=deudas[0], usuario=cajera, monto_snapshot=Decimal("22.30"),
        items_cobrados=deudas[0].items_snapshot[:1], estado=TransaccionQR.Estado.PENDIENTE_CONFIRMACION,
    )


def _mc4(monkeypatch, estado=EstadoPagoMC4.PENDIENTE, falla=False):
    cliente = Mock()
    if falla:
        cliente.consultar_estado.side_effect = MC4ClientError("SIP no responde")
    else:
        cliente.consultar_estado.return_value = Mock(estado=estado)
    monkeypatch.setattr(cobranza_services, "get_mc4_client", lambda: cliente)
    return cliente


@pytest.mark.django_db
class TestEstadoDelQR:
    def test_pagado_al_preguntar(self, cajera, qr, monkeypatch):  # noqa: F811
        _mc4(monkeypatch, EstadoPagoMC4.PAGADO)
        r = _api(cajera).get(f"/api/transacciones-qr/{qr.pk}/estado/")
        assert r.status_code == 200
        assert r.data["estado"] == "pagado"
        assert r.data["factura"] is not None and Factura.objects.filter(transaccion_qr=qr).exists()

    def test_sigue_esperando(self, cajera, qr, monkeypatch):  # noqa: F811
        _mc4(monkeypatch, EstadoPagoMC4.PENDIENTE)
        r = _api(cajera).get(f"/api/transacciones-qr/{qr.pk}/estado/")
        assert r.data["estado"] == "pendiente_confirmacion" and r.data["factura"] is None

    def test_a_sip_no_se_le_pregunta_mas_de_una_vez_cada_pocos_segundos(self, cajera, qr, monkeypatch):  # noqa: F811
        cliente = _mc4(monkeypatch)
        for _ in range(4):
            _api(cajera).get(f"/api/transacciones-qr/{qr.pk}/estado/")
        assert cliente.consultar_estado.call_count == 1

    def test_ya_pagado_no_consulta(self, cajera, qr, monkeypatch):  # noqa: F811
        cliente = _mc4(monkeypatch)
        TransaccionQR.objects.get(pk=qr.pk).transicionar_estado(TransaccionQR.Estado.PAGADO)
        r = _api(cajera).get(f"/api/transacciones-qr/{qr.pk}/estado/")
        assert r.data["estado"] == "pagado"
        cliente.consultar_estado.assert_not_called()


@pytest.mark.django_db
class TestAnularQR:
    def test_inhabilita_en_sip_y_anula(self, cajera, qr, monkeypatch):  # noqa: F811
        cliente = _mc4(monkeypatch, EstadoPagoMC4.PENDIENTE)
        r = _api(cajera).post(f"/api/transacciones-qr/{qr.pk}/cancelar/")
        assert r.status_code == 200 and r.data["estado"] == "cancelado"
        cliente.inhabilitar_qr.assert_called_once_with(f"txqr-{qr.pk}")

    def test_si_justo_pago_queda_pagado(self, cajera, qr, monkeypatch):  # noqa: F811
        _mc4(monkeypatch, EstadoPagoMC4.PAGADO)
        r = _api(cajera).post(f"/api/transacciones-qr/{qr.pk}/cancelar/")
        assert r.status_code == 200 and r.data["estado"] == "pagado"
        assert Factura.objects.filter(transaccion_qr=qr).exists()

    def test_sin_respuesta_de_sip_no_se_anula(self, cajera, qr, monkeypatch):  # noqa: F811
        _mc4(monkeypatch, falla=True)
        r = _api(cajera).post(f"/api/transacciones-qr/{qr.pk}/cancelar/")
        assert r.status_code == 400 and "Intenta de nuevo" in r.data["detail"]
        qr.refresh_from_db()
        assert qr.estado == TransaccionQR.Estado.PENDIENTE_CONFIRMACION

    def test_un_qr_pagado_no_se_anula(self, cajera, qr, monkeypatch):  # noqa: F811
        _mc4(monkeypatch)
        TransaccionQR.objects.get(pk=qr.pk).transicionar_estado(TransaccionQR.Estado.PAGADO)
        r = _api(cajera).post(f"/api/transacciones-qr/{qr.pk}/cancelar/")
        assert r.status_code == 400 and "pagado" in r.data["detail"].lower()
