"""QR de varios clientes (como "Facturas programadas a pagar" + "PAGO POR QR" de la app Java): un solo QR
de SIP por el total; cada cliente con su TransaccionQR y su factura; se pagan, vencen o anulan juntas;
un solo comprobante."""
from unittest.mock import Mock

import pytest
from django.core.cache import cache

from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Factura, ImpresionComprobante, QRAgrupado, TransaccionQR
from apps.facturacion_externa import sip_callback
from services.mc4_client import EstadoPagoMC4, MC4ClientError

from .test_cobro_agrupado import _api, cajera, deudas  # noqa: F401 -- fixtures

URL = "/api/transacciones-qr/grupo/"


@pytest.fixture(autouse=True)
def cache_limpia():
    cache.clear()
    yield
    cache.clear()


def _mc4(monkeypatch, estado=EstadoPagoMC4.PENDIENTE, falla_generar=False):
    cliente = Mock()
    cliente.generar_qr.return_value = Mock(imagen_qr_base64="iVBOR", id_transaccion="op-77")
    if falla_generar:
        cliente.generar_qr.side_effect = MC4ClientError("SIP caído")
    cliente.consultar_estado.return_value = Mock(estado=estado)
    monkeypatch.setattr(cobranza_services, "get_mc4_client", lambda: cliente)
    return cliente


def _generar(cajera, deudas, cantidades=(None, None)):  # noqa: F811
    items = [{"deuda_id": d.pk, **({"cantidad_comprobantes": c} if c else {})} for d, c in zip(deudas, cantidades)]
    return _api(cajera).post(URL, {"items": items}, format="json")


@pytest.mark.django_db
class TestGenerar:
    def test_un_solo_qr_por_el_total(self, cajera, deudas, monkeypatch):  # noqa: F811
        sip = _mc4(monkeypatch)
        r = _generar(cajera, deudas[:2])
        assert r.status_code == 201, r.data
        grupo = QRAgrupado.objects.get()
        hijas = list(grupo.transacciones.order_by("pk"))
        assert len(hijas) == 2 and all(t.estado == "pendiente_confirmacion" for t in hijas)
        assert grupo.monto_total == sum(t.monto_snapshot for t in hijas) == deudas[0].monto + deudas[1].monto
        solicitud = sip.generar_qr.call_args.args[0]
        assert (solicitud.alias, solicitud.monto) == (f"qrgr-{grupo.pk}", grupo.monto_total)
        assert sip.generar_qr.call_count == 1
        assert r.data["imagen_qr_base64"] == "iVBOR" and r.data["id"] == hijas[0].pk
        assert r.data["grupo_qr"]["monto_total"] == f"{grupo.monto_total:.2f}"
        assert [c["codigo"] for c in r.data["grupo_qr"]["clientes"]] == [d.cliente.codigo_externo for d in deudas[:2]]

    def test_por_cantidad_de_comprobantes(self, cajera, deudas, monkeypatch):  # noqa: F811
        _mc4(monkeypatch)
        r = _generar(cajera, deudas[:2], cantidades=(1, None))
        assert r.status_code == 201, r.data
        primera = TransaccionQR.objects.order_by("pk").first()
        assert len(primera.items_cobrados) == 1

    def test_al_menos_dos_y_sin_repetidos(self, cajera, deudas, monkeypatch):  # noqa: F811
        _mc4(monkeypatch)
        assert _generar(cajera, deudas[:1]).status_code == 400
        assert _generar(cajera, [deudas[0], deudas[0]]).status_code == 400
        assert not TransaccionQR.objects.exists()

    def test_cliente_con_qr_pendiente(self, cajera, deudas, monkeypatch):  # noqa: F811
        _mc4(monkeypatch)
        assert _generar(cajera, deudas[:2]).status_code == 201
        r = _generar(cajera, deudas[:2])
        assert r.status_code == 409
        assert QRAgrupado.objects.count() == 1

    def test_si_sip_falla_no_queda_nada_y_se_puede_reintentar(self, cajera, deudas, monkeypatch):  # noqa: F811
        _mc4(monkeypatch, falla_generar=True)
        assert _generar(cajera, deudas[:2]).status_code == 502
        assert not TransaccionQR.objects.exists() and not QRAgrupado.objects.exists()
        _mc4(monkeypatch)
        assert _generar(cajera, deudas[:2]).status_code == 201


@pytest.fixture
def grupo(cajera, deudas, monkeypatch):  # noqa: F811
    _mc4(monkeypatch)
    _generar(cajera, deudas[:2])
    return QRAgrupado.objects.get()


@pytest.mark.django_db
class TestPagoYVencimiento:
    def test_pagado_marca_a_todas_y_factura_cada_una(self, cajera, grupo, monkeypatch):  # noqa: F811
        sip = _mc4(monkeypatch, EstadoPagoMC4.PAGADO)
        primera = grupo.transacciones.order_by("pk").first()
        r = _api(cajera).get(f"/api/transacciones-qr/{primera.pk}/estado/")
        assert r.data["estado"] == "pagado"
        assert set(grupo.transacciones.values_list("estado", flat=True)) == {"pagado"}
        assert Factura.objects.filter(transaccion_qr__grupo_qr=grupo).count() == 2
        assert sip.consultar_estado.call_args.kwargs["alias"] == f"qrgr-{grupo.pk}"

    def test_la_tarea_no_factura_dos_veces(self, grupo, monkeypatch):
        from apps.cobranza.tasks import verificar_transacciones_pendientes

        _mc4(monkeypatch, EstadoPagoMC4.PAGADO)
        verificar_transacciones_pendientes()
        verificar_transacciones_pendientes()
        assert Factura.objects.filter(transaccion_qr__grupo_qr=grupo).count() == 2

    def test_vencido_vence_a_todas(self, grupo, monkeypatch):
        _mc4(monkeypatch, EstadoPagoMC4.EXPIRADO)
        cobranza_services.verificar_pago(grupo.transacciones.order_by("pk").last())
        assert set(grupo.transacciones.values_list("estado", flat=True)) == {"vencido"}
        assert not Factura.objects.exists()

    def test_aviso_de_sip_con_el_alias_del_grupo(self, grupo, monkeypatch):
        from rest_framework.test import APIClient

        _mc4(monkeypatch, EstadoPagoMC4.PAGADO)
        monkeypatch.setattr(sip_callback.SipCallbackView, "authentication_classes", [], raising=False)
        monkeypatch.setattr(sip_callback.SipCallbackView, "permission_classes", [], raising=False)
        vista = sip_callback.SipCallbackView()
        respuesta = vista._confirmar_grupo_en_panel(f"qrgr-{grupo.pk}")
        assert respuesta.data["codigo"] == "0000"
        assert set(grupo.transacciones.values_list("estado", flat=True)) == {"pagado"}
        assert vista._confirmar_grupo_en_panel("qrgr-999999").data["codigo"] == "9999"


@pytest.mark.django_db
class TestAnularQRAgrupado:
    def test_anular_cancela_el_qr_entero(self, cajera, grupo, monkeypatch):  # noqa: F811
        sip = _mc4(monkeypatch, EstadoPagoMC4.INHABILITADO)
        ultima = grupo.transacciones.order_by("pk").last()
        r = _api(cajera).post(f"/api/transacciones-qr/{ultima.pk}/cancelar/")
        assert r.status_code == 200, r.data
        assert set(grupo.transacciones.values_list("estado", flat=True)) == {"cancelado"}
        sip.inhabilitar_qr.assert_called_once_with(f"qrgr-{grupo.pk}")

    def test_si_justo_pago_queda_pagado(self, cajera, grupo, monkeypatch):  # noqa: F811
        _mc4(monkeypatch, EstadoPagoMC4.PAGADO)
        primera = grupo.transacciones.order_by("pk").first()
        r = _api(cajera).post(f"/api/transacciones-qr/{primera.pk}/cancelar/")
        assert r.data["estado"] == "pagado"
        assert Factura.objects.filter(transaccion_qr__grupo_qr=grupo).count() == 2


@pytest.mark.django_db
class TestComprobante:
    @pytest.fixture
    def pagado(self, grupo, monkeypatch):
        _mc4(monkeypatch, EstadoPagoMC4.PAGADO)
        cobranza_services.verificar_pago(grupo.transacciones.order_by("pk").first())
        return grupo

    def test_una_sola_fila_en_comprobantes(self, cajera, pagado):  # noqa: F811
        filas = _api(cajera).get("/api/comprobantes/").data["comprobantes"]
        assert [(f["tipo"], f["id"], f["numero"], len(f["clientes"])) for f in filas] == [("qrgrupo", pagado.pk, f"COMP-QG-{pagado.pk}", 2)]
        assert filas[0]["monto"] == f"{pagado.monto_total:.2f}"

    def test_detalle_con_forma_de_cobro_agrupado(self, cajera, pagado):  # noqa: F811
        r = _api(cajera).get(f"{URL}{pagado.pk}/")
        assert r.status_code == 200, r.data
        assert r.data["monto_total"] == r.data["monto_recibido"] and r.data["vuelto"] == "0.00"
        assert len(r.data["cobros"]) == 2 and r.data["cobros"][0]["deuda"]["cliente"]["codigo_externo"]

    def test_se_imprime_una_vez_como_grupo(self, cajera, pagado):  # noqa: F811
        hija = pagado.transacciones.order_by("pk").first()
        r = _api(cajera).post("/api/comprobantes/imprimir/", {"tipo": "qr", "id": hija.pk}, format="json")
        assert r.status_code == 200 and (r.data["tipo"], r.data["id"]) == ("qrgrupo", pagado.pk)
        assert _api(cajera).post("/api/comprobantes/imprimir/", {"tipo": "qrgrupo", "id": pagado.pk}, format="json").status_code == 403
        assert ImpresionComprobante.objects.filter(tipo="qrgrupo").count() == 1

    def test_no_se_imprime_sin_pagar(self, cajera, grupo):  # noqa: F811
        r = _api(cajera).post("/api/comprobantes/imprimir/", {"tipo": "qrgrupo", "id": grupo.pk}, format="json")
        assert r.status_code == 400


@pytest.mark.django_db
class TestAnulacionDelCobro:
    def test_se_anula_el_qr_entero_y_no_una_parte(self, grupo, monkeypatch):
        from apps.usuarios.models import Usuario

        _mc4(monkeypatch, EstadoPagoMC4.PAGADO)
        cobranza_services.verificar_pago(grupo.transacciones.order_by("pk").first())
        sup = Usuario.objects.create_user(username="sup-qr", password="x", rol=Usuario.Rol.SUPERVISOR)
        hija = grupo.transacciones.order_by("pk").first()
        r = _api(sup).post("/api/comprobantes/anular/", {"tipo": "qr", "id": hija.pk, "motivo": "cobro equivocado"}, format="json")
        assert r.status_code == 400 and "varios clientes" in r.data["detail"]
        r = _api(sup).post("/api/comprobantes/anular/", {"tipo": "qrgrupo", "id": grupo.pk, "motivo": "cobro equivocado"}, format="json")
        assert r.status_code == 200, r.data
        assert r.data["anuladas"] == 2
        assert set(Factura.objects.filter(transaccion_qr__grupo_qr=grupo).values_list("estado_envio", flat=True)) == {"anulado"}


def test_sip_simulado_sin_pago_por_prefijo(settings):
    from services.fakes import FakeMC4Client

    settings.MC4_FAKE_SIN_PAGO = ["qrgr-"]
    sip = FakeMC4Client()
    assert [sip.consultar_estado("qrgr-1").estado for _ in range(3)] == [EstadoPagoMC4.PENDIENTE] * 3
    sip.consultar_estado("txqr-9")
    assert sip.consultar_estado("txqr-9").estado == EstadoPagoMC4.PAGADO
