"""Pantalla "Pagos web" del panel: /api/liquidaciones/ (supervisor/admin, login JWT)."""
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.usuarios.models import Usuario

from . import services as facturacion_services
from .models import SolicitudLiquidacion


def _solicitud(alias, **kw):
    datos = {
        "alias": alias, "nro_cliente": "115997", "monto": Decimal("10.00"), "fecha_pago": timezone.now(),
        "detalle": [{"importe": "10.00", "debito_credito": "DEBITO", "nro_cliente": "115997"}],
    }
    datos.update(kw)
    return SolicitudLiquidacion.objects.create(**datos)


def _api(rol):
    usuario = Usuario.objects.create_user(username=f"u-{rol}", password="x", rol=rol, activo=True)
    c = APIClient()
    c.force_authenticate(user=usuario)
    return c


@pytest.fixture
def sup(db):
    return _api(Usuario.Rol.SUPERVISOR)


@pytest.mark.django_db
class TestLiquidacionesPanel:
    def test_cajera_no_entra(self, db):
        assert _api(Usuario.Rol.CAJERA).get("/api/liquidaciones/").status_code == 403

    def test_lista_y_filtra(self, sup):
        _solicitud("A", estado="error", error="La deuda cambió", banco="bnb")
        _solicitud("B", estado="facturado", nro_cliente="999", comprobante_pdf=b"%PDF")
        todas = sup.get("/api/liquidaciones/").data
        assert [s["alias"] for s in todas] == ["B", "A"]
        assert todas[0]["tiene_pdf"] is True and todas[1]["cantidad_comprobantes"] == 1
        assert [s["alias"] for s in sup.get("/api/liquidaciones/?estado=error").data] == ["A"]
        assert [s["alias"] for s in sup.get("/api/liquidaciones/?cliente=999").data] == ["B"]
        assert [s["alias"] for s in sup.get("/api/liquidaciones/?banco=bnb").data] == ["A"]
        assert "detalle" in sup.get(f"/api/liquidaciones/{todas[1]['id']}/").data

    def test_resumen_del_dia(self, sup):
        _solicitud("A", estado="facturado", monto=Decimal("10.00"))
        _solicitud("B", estado="facturado", monto=Decimal("5.50"))
        _solicitud("C", estado="error")
        r = sup.get("/api/liquidaciones/resumen/").data
        assert r["hoy"]["facturado"] == {"cantidad": 2, "monto": "15.50"}
        assert r["hoy"]["pendiente"]["cantidad"] == 0
        assert r["errores_abiertos"] == 1

    def test_pdf(self, sup):
        s = _solicitud("A", estado="facturado", comprobante_pdf=b"%PDF-1.4")
        r = sup.get(f"/api/liquidaciones/{s.pk}/pdf/")
        assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
        assert sup.get(f"/api/liquidaciones/{_solicitud('B').pk}/pdf/").status_code == 404

    def test_reintentar(self, sup):
        s = _solicitud("A", estado="error", error="fuera de horario")
        def facturar(solicitud):
            solicitud.estado = SolicitudLiquidacion.Estado.FACTURADO
            solicitud.save(update_fields=["estado"])
            return solicitud
        with patch("apps.facturacion_externa.panel.liquidar_solicitud", side_effect=facturar) as liquidar:
            r = sup.post(f"/api/liquidaciones/{s.pk}/reintentar/")
        assert r.status_code == 200 and r.data["estado"] == "facturado"
        liquidar.assert_called_once()

    def test_no_reintenta_una_facturada(self, sup):
        s = _solicitud("A", estado="facturado")
        with patch("apps.facturacion_externa.panel.liquidar_solicitud") as liquidar:
            assert sup.post(f"/api/liquidaciones/{s.pk}/reintentar/").status_code == 409
        liquidar.assert_not_called()

    def test_estado_remoto(self, sup):
        s = _solicitud("A", cobranzas_uuid="u-1")
        respuesta = Mock(ok=True, status_code=200)
        respuesta.json.return_value = {"id": 3903, "estado": "PAGADA", "lote": 6, "caja_codigo": 8197, "extra": "x"}
        cliente = Mock()
        cliente._request_autenticado.return_value = respuesta
        with patch("apps.facturacion_externa.panel.get_cobranzas_banco_client", return_value=cliente):
            r = sup.get(f"/api/liquidaciones/{s.pk}/remoto/")
        assert r.status_code == 200 and r.data["estado"] == "PAGADA" and "extra" not in r.data
        assert sup.get(f"/api/liquidaciones/{_solicitud('B').pk}/remoto/").status_code == 404


@pytest.mark.django_db
class TestDescartar:
    def test_descarta_con_motivo_y_ya_no_se_paga(self, sup, monkeypatch):
        s = _solicitud("A", estado="error", error="La deuda no existe")
        assert sup.post(f"/api/liquidaciones/{s.pk}/descartar/", {"motivo": "x"}).status_code == 400
        r = sup.post(f"/api/liquidaciones/{s.pk}/descartar/", {"motivo": "Prueba vieja de test"})
        assert r.status_code == 200 and r.data["estado"] == "descartado" and r.data["nota_descarte"] == "Prueba vieja de test"
        # cessa-laravel reenvía el mismo recibo: no se vuelve a pagar.
        llamado = []
        monkeypatch.setattr(facturacion_services, "get_cobranzas_banco_client", lambda: llamado.append(1))
        s.refresh_from_db()
        assert facturacion_services.liquidar_solicitud(s).estado == "descartado" and not llamado
        assert sup.post(f"/api/liquidaciones/{s.pk}/reintentar/").status_code == 409
        assert sup.get("/api/liquidaciones/resumen/").data["errores_abiertos"] == 0

    def test_no_descarta_una_facturada(self, sup):
        s = _solicitud("A", estado="facturado")
        assert sup.post(f"/api/liquidaciones/{s.pk}/descartar/", {"motivo": "no corresponde"}).status_code == 409
