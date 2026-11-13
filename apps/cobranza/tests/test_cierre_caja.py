"""Cierre de la caja del SIIC (apps/cobranza/cierre.py): arqueo por cortes, documentos, cierre con
facturas al día, totales solo para supervisor/admin, reporte, y el QR de mostrador como documento."""
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from apps.cobranza import cierre
from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Caja, CajaOperacionInvalida, Factura, TransaccionQR
from apps.usuarios.models import Usuario
from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoRequestError

from .test_cajas_por_cajera import cajera_nest, con_nest, supervisor  # noqa: F401 -- fixtures
from .test_cobro_agrupado import _api, deudas  # noqa: F401 -- fixtures

TOTALES = {"efectivo_bs": "100.00", "efectivo_sus": "0.00", "cheques_bs": "0.00", "cheques_sus": "0.00",
           "dividendos_bs": "0.00", "otros_valores": "50.00", "recaudado_bs": "150.00", "diferencia_bs": "0.00"}
FILAS = [
    {"lote": "9001", "indice": "1", "ente_descripcion": "EFECTIVO", "banco_descripcion": "OTRO", "moneda": "B", "numero": "", "importe": "100.00"},
    {"lote": "3", "indice": "1", "ente_descripcion": "PAGO POR QR", "banco_descripcion": "BANCO BISA", "moneda": "B", "numero": "TXQR1", "importe": "50.00"},
]


@pytest.fixture
def caja_siic(cajera_nest, supervisor):
    with patch.object(CajeraCobranzasClient, "aperturar_caja", return_value={}), \
         patch.object(CajeraCobranzasClient, "caja_del_dia", return_value={"estado": "ABIERTA", "dispositivo_id": 101, "agencia_sigla": "CEN"}):
        return Caja.abrir(cajero=cajera_nest, usuario=supervisor, motivo="test", agencia_sigla="CEN")


@pytest.fixture
def nest_cierre():
    """La Nest de la cajera, simulada: catálogos, documentos, datos del cierre y cerrar."""
    with patch.object(CajeraCobranzasClient, "entes_cierre", return_value=FILAS), \
         patch.object(CajeraCobranzasClient, "catalogo", side_effect=lambda n: [
             {"id": 1, "descripcion": "CHEQUE"}, {"id": 7, "descripcion": "EFECTIVO"}, {"id": 8, "descripcion": "PAGO POR QR"}]
             if n == "entes" else [{"id": 2, "descripcion": "BANCO BISA"}]), \
         patch.object(CajeraCobranzasClient, "datos_cierre", return_value=TOTALES), \
         patch.object(CajeraCobranzasClient, "actualizar_efectivo", return_value={}) as efectivo, \
         patch.object(CajeraCobranzasClient, "guardar_documento", return_value={"indice": "1"}) as documento, \
         patch.object(CajeraCobranzasClient, "cerrar_caja", return_value={"estado": "CERRADA"}) as cerrar, \
         patch.object(CajeraCobranzasClient, "reporte_cierre", return_value=b"%PDF-cierre") as reporte:
        yield Mock(efectivo=efectivo, documento=documento, cerrar=cerrar, reporte=reporte)


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest")
class TestArqueo:
    def test_la_cajera_no_ve_totales_y_la_supervisora_si(self, caja_siic, cajera_nest, supervisor, nest_cierre):
        r = _api(cajera_nest).get(f"/api/cajas/{caja_siic.pk}/cierre/")
        assert r.status_code == 200, r.data
        assert "totales" not in r.data
        assert [d["ente"] for d in r.data["documentos"]] == ["EFECTIVO", "PAGO POR QR"]
        # Efectivo y QR no se agregan a mano.
        assert [e["descripcion"] for e in r.data["entes"]] == ["CHEQUE"]

        r = _api(supervisor).get(f"/api/cajas/{caja_siic.pk}/cierre/")
        assert r.data["totales"]["recaudado_bs"] == "150.00"

    def test_efectivo_por_cortes(self, caja_siic, cajera_nest, nest_cierre):
        r = _api(cajera_nest).put(f"/api/cajas/{caja_siic.pk}/efectivo/", {"cortes": {"200": 1, "010": 3}}, format="json")

        assert r.status_code == 200, r.data
        assert r.data["efectivo_bs"] == "200.30"
        moneda, cortes = nest_cierre.efectivo.call_args.args
        assert moneda == "B" and cortes["corte_bs_200"] == 1 and cortes["corte_bs_010"] == 3 and cortes["corte_bs_100"] == 0
        caja_siic.refresh_from_db()
        assert caja_siic.arqueo_cortes["200"] == 1

    def test_cortes_negativos(self, caja_siic, cajera_nest, nest_cierre):
        r = _api(cajera_nest).put(f"/api/cajas/{caja_siic.pk}/efectivo/", {"cortes": {"200": -1}}, format="json")
        assert r.status_code == 400
        nest_cierre.efectivo.assert_not_called()

    def test_solo_la_duena_hace_el_arqueo(self, caja_siic, supervisor, nest_cierre):
        r = _api(supervisor).put(f"/api/cajas/{caja_siic.pk}/efectivo/", {"cortes": {"200": 1}}, format="json")
        assert r.status_code == 400
        assert "dueña" in r.data["detail"]

    def test_agrega_un_cheque(self, caja_siic, cajera_nest, nest_cierre):
        r = _api(cajera_nest).post(f"/api/cajas/{caja_siic.pk}/documentos/", {
            "ente_id": 1, "banco_id": 2, "moneda": "B", "numero": "CH-1", "importe": "150,00".replace(",", "."),
            "fecha_registro": "2026-11-13", "fecha_vencimiento": "2026-11-20"}, format="json")

        assert r.status_code == 201, r.data
        cuerpo, modificar = nest_cierre.documento.call_args.args
        assert modificar is False
        assert cuerpo == {"ente_id": 1, "moneda": "B", "banco_id": 2, "importe": 150.0, "numero": "CH-1",
                          "fecha_registro": 20261113, "fecha_vencimiento": 20261120}

    def test_documento_sin_numero(self, caja_siic, cajera_nest, nest_cierre):
        r = _api(cajera_nest).post(f"/api/cajas/{caja_siic.pk}/documentos/",
                                   {"ente_id": 1, "banco_id": 2, "numero": "", "importe": "10"}, format="json")
        assert r.status_code == 400
        nest_cierre.documento.assert_not_called()


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest")
class TestCerrar:
    def test_cierra_en_el_siic_y_guarda_el_arqueo(self, caja_siic, cajera_nest, supervisor, nest_cierre):
        r = _api(cajera_nest).post(f"/api/cajas/{caja_siic.pk}/cerrar/", {"descripcion": "todo bien"}, format="json")

        assert r.status_code == 200, r.data
        nest_cierre.cerrar.assert_called_once_with("todo bien")
        caja_siic.refresh_from_db()
        assert caja_siic.estado == Caja.Estado.CERRADA
        assert caja_siic.cierre_siic["datos"]["diferencia_bs"] == "0.00"
        # La cajera ve sus documentos pero no los totales; la supervisora, todo.
        assert "datos" not in _api(cajera_nest).get(f"/api/cajas/{caja_siic.pk}/cierre/").data["cierre"]
        assert _api(supervisor).get(f"/api/cajas/{caja_siic.pk}/resumen/").data["cierre_siic"]["recaudado_bs"] == "150.00"

    def test_manda_las_facturas_pendientes_antes_de_cerrar(self, caja_siic, cajera_nest, deudas, nest_cierre, monkeypatch):  # noqa: F811
        cobro = cobranza_services.registrar_cobro_efectivo(deudas[0], cajera_nest, Decimal("100"))
        enviadas = []

        def enviar(factura):
            enviadas.append(factura.pk)
            factura.estado_envio = Factura.EstadoEnvio.ENVIADO
            factura.save(update_fields=["estado_envio"])
            return factura

        monkeypatch.setattr(cobranza_services, "enviar_factura_a_siic", enviar)
        r = _api(cajera_nest).post(f"/api/cajas/{caja_siic.pk}/cerrar/", {}, format="json")

        assert r.status_code == 200, r.data
        assert enviadas == [Factura.objects.get(cobro_efectivo=cobro).pk]

    def test_no_cierra_si_una_factura_no_se_pudo_mandar(self, caja_siic, cajera_nest, deudas, nest_cierre, monkeypatch):  # noqa: F811
        cobranza_services.registrar_cobro_efectivo(deudas[0], cajera_nest, Decimal("100"))
        monkeypatch.setattr(cobranza_services, "enviar_factura_a_siic", lambda f: f)
        r = _api(cajera_nest).post(f"/api/cajas/{caja_siic.pk}/cerrar/", {}, format="json")

        assert r.status_code == 400
        assert "todavía no se registraron" in r.data["detail"]
        nest_cierre.cerrar.assert_not_called()
        caja_siic.refresh_from_db()
        assert caja_siic.estado == Caja.Estado.ABIERTA

    def test_no_cierra_con_un_qr_esperando_pago(self, caja_siic, cajera_nest, deudas, nest_cierre):  # noqa: F811
        TransaccionQR.objects.create(deuda=deudas[0], usuario=cajera_nest, caja=caja_siic, monto_snapshot=Decimal("10"))
        r = _api(cajera_nest).post(f"/api/cajas/{caja_siic.pk}/cerrar/", {}, format="json")
        assert r.status_code == 400
        assert "QR esperando" in r.data["detail"]

    def test_si_el_siic_no_cierra_queda_abierta(self, caja_siic, cajera_nest, nest_cierre):
        nest_cierre.cerrar.side_effect = CobranzasBancoRequestError("La caja ya fue cerrada")
        r = _api(cajera_nest).post(f"/api/cajas/{caja_siic.pk}/cerrar/", {}, format="json")
        assert r.status_code == 400
        caja_siic.refresh_from_db()
        assert caja_siic.estado == Caja.Estado.ABIERTA and caja_siic.cierre_siic == {}

    def test_no_se_reabre_una_caja_cerrada_en_el_siic(self, caja_siic, cajera_nest, supervisor, nest_cierre):
        cierre.cerrar_en_siic(caja_siic, cajera_nest)
        with pytest.raises(CajaOperacionInvalida, match="no se puede reabrir"):
            caja_siic.reabrir(usuario=supervisor, motivo="x")

    def test_reporte_solo_supervisora_y_con_la_fecha_de_la_caja(self, caja_siic, cajera_nest, supervisor, nest_cierre):
        assert _api(supervisor).get(f"/api/cajas/{caja_siic.pk}/reporte-cierre/").status_code == 400  # todavía abierta
        cierre.cerrar_en_siic(caja_siic, cajera_nest)

        assert _api(cajera_nest).get(f"/api/cajas/{caja_siic.pk}/reporte-cierre/").status_code == 403
        r = _api(supervisor).get(f"/api/cajas/{caja_siic.pk}/reporte-cierre/")
        assert r.status_code == 200 and r.content == b"%PDF-cierre"
        from django.utils import timezone

        nest_cierre.reporte.assert_called_once_with(timezone.localtime(caja_siic.creada_en).strftime("%Y%m%d"))

    def test_caja_local_cierra_como_siempre(self, supervisor):
        local = Usuario.objects.create_user(username="caj-loc-cierre", password="x", rol=Usuario.Rol.CAJERA)
        caja = Caja.abrir(cajero=local, usuario=supervisor, motivo="test")
        with patch.object(CajeraCobranzasClient, "cerrar_caja") as cerrar:
            r = _api(local).post(f"/api/cajas/{caja.pk}/cerrar/", {}, format="json")
        assert r.status_code == 200
        cerrar.assert_not_called()


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest")
class TestQrDeMostrador:
    def test_va_como_documento_pago_por_qr(self, caja_siic, cajera_nest, deudas, settings, monkeypatch):  # noqa: F811
        settings.COBRANZAS_BANCO_DOCUMENTO_ENTE_ID = "8"
        settings.COBRANZAS_BANCO_DOCUMENTO_BANCO_IDS = {"sip_bisa": "2", "bnb": ""}
        tx = TransaccionQR.objects.create(deuda=deudas[0], usuario=cajera_nest, caja=caja_siic, monto_snapshot=Decimal("22.30"),
                                          items_cobrados=deudas[0].items_snapshot[:1], estado=TransaccionQR.Estado.PAGADO)
        factura = Factura.objects.create(transaccion_qr=tx)
        cliente = Mock(spec=CajeraCobranzasClient, crear_transaccion=Mock(return_value="u-1"),
                       obtener_comprobante_pdf=Mock(return_value=b"%PDF"), obtener_comprobante_json=Mock(return_value={}))
        monkeypatch.setattr(cobranza_services, "_cliente_para", lambda u: cliente)

        cobranza_services.enviar_factura_a_siic(factura)

        cliente.pagar_transaccion_propia.assert_not_called()
        uuid, _, documento = cliente.pagar_transaccion.call_args.args
        assert uuid == "u-1"
        assert (documento["ente_id"], documento["banco_id"], documento["numero"], documento["importe"]) == (8, 2, f"TXQR{tx.pk}", 22.3)
