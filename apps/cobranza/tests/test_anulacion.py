"""Anulación de cobros del panel (apps/cobranza/anulacion.py): solo supervisor/admin, con motivo,
el mismo día; en el SIIC con el usuario de la Nest de quien anula; sale de los totales."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.cobranza import services as cobranza_services
from apps.cobranza.models import CobroEfectivo, Factura
from apps.cobranza.reportes import resumen_caja
from apps.usuarios import nest
from apps.usuarios.models import Usuario
from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoRequestError

from .test_cajas_por_cajera import con_nest  # noqa: F401 -- fixture
from .test_cobro_agrupado import _api, cajera, deudas  # noqa: F401 -- fixtures

MOTIVO = "cobro al cliente equivocado"


@pytest.fixture
def supervisora_nest(db):
    u = Usuario.objects.create_user(username="SUPERV", rol=Usuario.Rol.SUPERVISOR, origen=Usuario.Origen.NEST)
    u.set_unusable_password()
    u.save()
    nest.guardar_sesion(u, {"access_token": "acc-sup", "refresh_token": "ref-sup"})
    return u


def _cobro(deuda, usuario, enviado=True) -> CobroEfectivo:
    cobro = cobranza_services.registrar_cobro_efectivo(deuda, usuario, Decimal("200"))
    if enviado:
        Factura.objects.filter(cobro_efectivo=cobro).update(
            estado_envio=Factura.EstadoEnvio.ENVIADO, cobranzas_uuid="uuid-1", numero_factura="70001"
        )
    return cobro


def _anular(usuario, tipo, objeto_id, motivo=MOTIVO):
    return _api(usuario).post("/api/comprobantes/anular/", {"tipo": tipo, "id": objeto_id, "motivo": motivo}, format="json")


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest")
class TestAnulacion:
    def test_anula_en_el_siic_con_el_usuario_de_quien_anula(self, cajera, deudas, supervisora_nest):  # noqa: F811
        cobro = _cobro(deudas[0], cajera)
        usados = []
        with patch.object(CajeraCobranzasClient, "anular_transaccion", autospec=True,
                          side_effect=lambda self, uuid: usados.append((self.usuario, uuid)) or {}):
            r = _anular(supervisora_nest, "efectivo", cobro.pk)

        assert r.status_code == 200, r.data
        assert usados == [(supervisora_nest, "uuid-1")]
        f = Factura.objects.get(cobro_efectivo=cobro)
        assert (f.estado_envio, f.anulada_por, f.motivo_anulacion) == ("anulado", supervisora_nest, MOTIVO)

    def test_la_cajera_no_anula(self, cajera, deudas):  # noqa: F811
        cobro = _cobro(deudas[0], cajera)
        assert _anular(cajera, "efectivo", cobro.pk).status_code == 403

    def test_motivo_obligatorio(self, cajera, deudas, supervisora_nest):  # noqa: F811
        cobro = _cobro(deudas[0], cajera)
        r = _anular(supervisora_nest, "efectivo", cobro.pk, motivo="  ")
        assert r.status_code == 400 and "motivo" in r.data["detail"]

    def test_un_supervisor_local_no_anula_lo_que_ya_esta_en_el_siic(self, cajera, deudas):  # noqa: F811
        local = Usuario.objects.create_user(username="sup-local", password="x", rol=Usuario.Rol.SUPERVISOR)
        cobro = _cobro(deudas[0], cajera)
        r = _anular(local, "efectivo", cobro.pk)
        assert r.status_code == 400 and "usuario del sistema de cobranzas" in r.data["detail"]

    def test_sin_mandar_al_siic_se_anula_solo_aca_y_ya_no_se_manda(self, cajera, deudas, supervisora_nest):  # noqa: F811
        cobro = _cobro(deudas[0], cajera, enviado=False)
        with patch.object(CajeraCobranzasClient, "anular_transaccion") as anular:
            assert _anular(supervisora_nest, "efectivo", cobro.pk).status_code == 200
        anular.assert_not_called()
        factura = Factura.objects.get(cobro_efectivo=cobro)
        with patch.object(cobranza_services, "_cliente_para") as cliente:
            cobranza_services.enviar_factura_a_siic(factura)
        cliente.assert_not_called()

    def test_si_el_siic_no_anula_queda_como_estaba(self, cajera, deudas, supervisora_nest):  # noqa: F811
        cobro = _cobro(deudas[0], cajera)
        with patch.object(CajeraCobranzasClient, "anular_transaccion", side_effect=CobranzasBancoRequestError("ha expirado")):
            r = _anular(supervisora_nest, "efectivo", cobro.pk)
        assert r.status_code == 400 and "ha expirado" in r.data["detail"]
        assert Factura.objects.get(cobro_efectivo=cobro).estado_envio == "enviado"

    def test_no_dos_veces(self, cajera, deudas, supervisora_nest):  # noqa: F811
        cobro = _cobro(deudas[0], cajera, enviado=False)
        _anular(supervisora_nest, "efectivo", cobro.pk)
        r = _anular(supervisora_nest, "efectivo", cobro.pk)
        assert r.status_code == 400 and "ya está anulado" in r.data["detail"]

    def test_solo_el_mismo_dia(self, cajera, deudas, supervisora_nest):  # noqa: F811
        cobro = _cobro(deudas[0], cajera)
        CobroEfectivo.objects.filter(pk=cobro.pk).update(creado_en=timezone.now() - timedelta(days=1))
        r = _anular(supervisora_nest, "efectivo", cobro.pk)
        assert r.status_code == 400 and "del día" in r.data["detail"]

    def test_un_grupo_se_anula_entero(self, cajera, deudas, supervisora_nest):  # noqa: F811
        grupo = cobranza_services.registrar_cobro_efectivo_agrupado(
            cajera, Decimal("500"), [(deudas[0], None), (deudas[1], None)]
        )
        # Un cobro del grupo no se anula suelto.
        r = _anular(supervisora_nest, "efectivo", grupo.cobros.first().pk)
        assert r.status_code == 400 and "grupo entero" in r.data["detail"]

        assert _anular(supervisora_nest, "grupo", grupo.pk).status_code == 200
        assert set(Factura.objects.filter(cobro_efectivo__grupo=grupo).values_list("estado_envio", flat=True)) == {"anulado"}

    def test_sale_de_los_totales_y_del_listado(self, cajera, deudas, supervisora_nest):  # noqa: F811
        from apps.cobranza.models import Caja

        Caja.abrir(cajero=cajera, usuario=supervisora_nest, motivo="test")
        cobro = _cobro(deudas[0], cajera, enviado=False)
        caja = Caja.objects.get(cajero=cajera)
        assert resumen_caja(caja)["monto_total"] == Decimal("39.70")

        _anular(supervisora_nest, "efectivo", cobro.pk)

        assert resumen_caja(caja)["monto_total"] == Decimal("0")
        fila = next(f for f in _api(supervisora_nest).get("/api/comprobantes/").data["comprobantes"] if f["id"] == cobro.pk)
        assert fila["anulacion"]["motivo"] == MOTIVO and fila["se_puede_anular"] is False
