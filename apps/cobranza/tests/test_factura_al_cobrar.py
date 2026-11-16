"""La factura del SIIC al cobrar (como la app Java, que la imprimía apenas se pagaba): si todavía no
se registró, se registra en el momento con la fila bloqueada (services.enviar_factura_con_candado)."""
import pytest

from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Factura
from apps.usuarios.models import Usuario

from .test_cobro_agrupado import _api, cajera, deudas  # noqa: F401 -- fixtures


@pytest.fixture(autouse=True)
def siic_falso(settings):
    """Sin el SIIC (ni el simulado del entorno local): el cliente falso del repo."""
    settings.COBRANZAS_BANCO_CLIENT_CLASS = "services.fakes.FakeCobranzasBancoClient"
    settings.CAJAS_POR_CAJERA = False


def _cobrar(usuario, deuda):
    r = _api(usuario).post("/api/cobros-efectivo/", {"deuda_id": deuda.pk, "monto_recibido": "100.00"}, format="json")
    assert r.status_code == 201, r.data
    return Factura.objects.get(cobro_efectivo_id=r.data["id"])


@pytest.mark.django_db
class TestFacturaAlCobrar:
    def test_se_registra_en_el_momento_y_sale_el_pdf(self, cajera, deudas):  # noqa: F811
        factura = _cobrar(cajera, deudas[0])
        assert factura.estado_envio == Factura.EstadoEnvio.PENDIENTE

        r = _api(cajera).get(f"/api/facturas/{factura.pk}/comprobante/")
        assert r.status_code == 200, getattr(r, "data", r.content[:80])
        assert r["Content-Type"] == "application/pdf" and r.content.startswith(b"%PDF")
        factura.refresh_from_db()
        assert factura.estado_envio == Factura.EstadoEnvio.ENVIADO

    def test_si_el_siic_falla_avisa(self, cajera, deudas, monkeypatch):  # noqa: F811
        factura = _cobrar(cajera, deudas[0])

        def falla(f):
            cobranza_services._marcar_error_envio(f, "SIIC caído")
            return f

        monkeypatch.setattr(cobranza_services, "enviar_factura_a_siic", falla)
        r = _api(cajera).get(f"/api/facturas/{factura.pk}/comprobante/")
        assert r.status_code == 409 and "SIIC caído" in r.data["detail"]

    def test_anulada_no_tiene_factura(self, cajera, deudas):  # noqa: F811
        factura = _cobrar(cajera, deudas[0])
        Factura.objects.filter(pk=factura.pk).update(estado_envio=Factura.EstadoEnvio.ANULADO)
        assert _api(cajera).get(f"/api/facturas/{factura.pk}/comprobante/").status_code == 409

    def test_no_la_de_otra_cajera(self, cajera, deudas):  # noqa: F811
        otra = Usuario.objects.create_user(username="caj-fac2", password="x", rol=Usuario.Rol.CAJERA, activo=True)
        factura = _cobrar(otra, deudas[0])
        assert _api(cajera).get(f"/api/facturas/{factura.pk}/comprobante/").status_code == 404

    def test_con_candado_no_se_envia_dos_veces(self, cajera, deudas, monkeypatch):  # noqa: F811
        factura = _cobrar(cajera, deudas[0])
        llamadas = []
        original = cobranza_services.enviar_factura_a_siic
        monkeypatch.setattr(cobranza_services, "enviar_factura_a_siic", lambda f: (llamadas.append(f.pk), original(f))[1])
        cobranza_services.enviar_factura_con_candado(factura.pk)
        cobranza_services.enviar_factura_con_candado(factura.pk)  # ya ENVIADO: no vuelve a pagar
        factura.refresh_from_db()
        assert factura.estado_envio == Factura.EstadoEnvio.ENVIADO
        assert factura.cobranzas_uuid  # una sola transacción en el SIIC

    def test_mis_cobros_trae_el_id_de_la_factura(self, cajera, deudas):  # noqa: F811
        factura = _cobrar(cajera, deudas[0])
        r = _api(cajera).get("/api/comprobantes/")
        assert r.data["comprobantes"][0]["facturas_ids"] == [factura.pk]
