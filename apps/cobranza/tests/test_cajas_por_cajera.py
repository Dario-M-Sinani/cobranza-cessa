"""Caja propia por cajera vía la Nest de cobranzas (CAJAS_POR_CAJERA): login contra la Nest, caja
del SIIC con agencia y monto inicial, cobro bloqueado sin caja y factura con el token de la cajera."""
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient

from apps.cobranza import services as cobranza_services
from apps.cobranza.models import Caja, CajaOperacionInvalida, Factura
from apps.usuarios import nest
from apps.usuarios.models import SesionNest, Usuario
from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoAuthError, CobranzasBancoRequestError

from .test_cobro_agrupado import _api, deudas  # noqa: F401 -- fixture

NEST = {
    "CAJAS_POR_CAJERA": True,
    "COBRANZAS_BANCO_BASE_URL": "http://nest.test",
    "COBRANZAS_BANCO_CLIENT_ID": "2",
    "COBRANZAS_BANCO_CLIENT_SECRET": "secreto",
}


def _respuesta(status, cuerpo=None):
    r = Mock(status_code=status, ok=200 <= status < 300, text=str(cuerpo))
    r.json.return_value = cuerpo
    return r


def _login(username="cajera1", password="clave", rol="CAJA", habilitado=True, token_status=200):
    token = _respuesta(token_status, {"access_token": "acc-1", "refresh_token": "ref-1"} if token_status == 200
                       else {"error": "invalid_grant"})
    existe = _respuesta(200, {"id": 9, "nombre": "MARIA PEÑA", "username": username.upper(), "rol": rol,
                              "habilitado": habilitado, "entidad_financiera_id": 1})
    with patch.object(nest.requests, "post", return_value=token) as post, patch.object(nest.requests, "get", return_value=existe):
        r = APIClient().post("/api/auth/token/", {"username": username, "password": password}, format="json")
    return r, post


@pytest.fixture
def con_nest(settings):
    for clave, valor in NEST.items():
        setattr(settings, clave, valor)


@pytest.fixture
def cajera_nest(db):
    u = Usuario.objects.create_user(username="CAJERA1", rol=Usuario.Rol.CAJERA, origen=Usuario.Origen.NEST,
                                    entidad_financiera_id=1)
    u.set_unusable_password()
    u.save()
    nest.guardar_sesion(u, {"access_token": "acc-1", "refresh_token": "ref-1"})
    return u


@pytest.fixture
def supervisor(db):
    return Usuario.objects.create_user(username="sup-nest", password="x", rol=Usuario.Rol.SUPERVISOR)


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest")
class TestLoginPorNest:
    def test_crea_el_usuario_con_rol_y_guarda_la_sesion_cifrada(self):
        r, post = _login()

        assert r.status_code == 200, r.data
        u = Usuario.objects.get(username="CAJERA1")
        assert (u.origen, u.rol, u.first_name, u.entidad_financiera_id) == ("nest", "cajera", "MARIA PEÑA", 1)
        assert not u.has_usable_password()
        assert post.call_args.kwargs["json"]["grant_type"] == "password"
        assert post.call_args.kwargs["json"]["username"] == "CAJERA1"  # la Nest usa mayúsculas
        sesion = SesionNest.objects.get(usuario=u)
        assert "acc-1" not in sesion.access_cifrado
        assert nest.token_de(u) == "acc-1"

    def test_el_token_del_panel_dice_que_su_caja_es_del_siic(self):
        r, _ = _login()
        from rest_framework_simplejwt.tokens import AccessToken

        datos = AccessToken(r.data["access"])
        assert datos["caja_siic"] is True
        assert datos["nombre"] == "MARIA PEÑA"

    def test_supervisora_y_sistemas(self):
        _login(username="superv", rol="COBRANZA")
        _login(username="sistemas1", rol="SISTEMAS")
        assert Usuario.objects.get(username="SUPERV").rol == Usuario.Rol.SUPERVISOR
        assert Usuario.objects.get(username="SISTEMAS1").rol == Usuario.Rol.ADMIN

    def test_clave_incorrecta(self):
        r, _ = _login(token_status=400)
        assert r.status_code == 401
        assert not Usuario.objects.filter(username="CAJERA1").exists()

    def test_rol_de_banco_todavia_no_entra(self):
        r, _ = _login(username="resp-bnb", rol="ENTIDAD_FINANCIERA")
        assert r.status_code == 401
        assert "todavía no tiene acceso" in str(r.data)

    def test_un_usuario_local_no_pasa_por_la_nest(self):
        Usuario.objects.create_user(username="admin_demo", password="local-123", rol=Usuario.Rol.ADMIN)
        r, post = _login(username="admin_demo", password="local-123")
        assert r.status_code == 200
        post.assert_not_called()

    def test_nest_caida(self):
        import requests

        with patch.object(nest.requests, "post", side_effect=requests.ConnectionError("sin red")):
            r = APIClient().post("/api/auth/token/", {"username": "cajera1", "password": "x"}, format="json")
        assert r.status_code == 401
        assert "No se pudo conectar" in str(r.data)

    def test_apagado_no_consulta_la_nest(self, settings):
        settings.CAJAS_POR_CAJERA = False
        r, post = _login()
        assert r.status_code == 401
        post.assert_not_called()


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest")
class TestCajaDelSiic:
    def test_abre_la_caja_en_el_siic_con_agencia_y_monto(self, cajera_nest, supervisor):
        with patch.object(CajeraCobranzasClient, "aperturar_caja", return_value={"caja_codigo": 101}) as aperturar, \
             patch.object(CajeraCobranzasClient, "caja_del_dia", return_value={
                 "estado": "ABIERTA", "dispositivo_id": 101, "agencia_sigla": "CEN",
                 "agencia_descripcion": "OFICINA CENTRAL", "monto_inicial": "200.00"}):
            caja = Caja.abrir(cajero=cajera_nest, usuario=supervisor, motivo="test", agencia_sigla="CEN",
                              monto_inicial=Decimal("200"))

        aperturar.assert_called_once_with("CEN", Decimal("200"), "test")
        assert (caja.caja_codigo, caja.agencia_sigla, caja.agencia_descripcion, caja.monto_inicial) == (
            "101", "CEN", "OFICINA CENTRAL", Decimal("200.00"))

    def test_sin_agencia_no_abre(self, cajera_nest, supervisor):
        with pytest.raises(CajaOperacionInvalida, match="agencia"):
            Caja.abrir(cajero=cajera_nest, usuario=supervisor, motivo="test")
        assert not Caja.objects.exists()

    def test_si_el_siic_la_rechaza_no_se_abre_aca(self, cajera_nest, supervisor):
        with patch.object(CajeraCobranzasClient, "aperturar_caja",
                          side_effect=CobranzasBancoRequestError("No se puede aperturar porque no existen cajas asignadas")):
            with pytest.raises(CajaOperacionInvalida, match="no existen cajas asignadas"):
                Caja.abrir(cajero=cajera_nest, usuario=supervisor, motivo="test", agencia_sigla="CEN")
        assert not Caja.objects.exists()

    def test_adopta_la_caja_que_el_siic_ya_tiene_abierta(self, cajera_nest, supervisor):
        with patch.object(CajeraCobranzasClient, "aperturar_caja", side_effect=CobranzasBancoRequestError(
                "No puede aperturar caja porque ya cuenta con una caja abierta a las 08:01:00")), \
             patch.object(CajeraCobranzasClient, "caja_del_dia", return_value={
                 "estado": "ABIERTA", "dispositivo_id": 102, "agencia_sigla": "CEN"}):
            caja = Caja.abrir(cajero=cajera_nest, usuario=supervisor, motivo="test", agencia_sigla="CEN")
        assert caja.caja_codigo == "102"

    def test_caja_del_siic_ya_cerrada_hoy(self, cajera_nest, supervisor):
        with patch.object(CajeraCobranzasClient, "aperturar_caja", side_effect=CobranzasBancoRequestError(
                "No puede aperturar caja porque ya cuenta con una caja abierta a las 08:01:00")), \
             patch.object(CajeraCobranzasClient, "caja_del_dia", return_value={"estado": "CERRADA"}):
            with pytest.raises(CajaOperacionInvalida, match="ya está cerrada"):
                Caja.abrir(cajero=cajera_nest, usuario=supervisor, motivo="test", agencia_sigla="CEN")

    def test_usuario_local_abre_como_antes(self, supervisor):
        local = Usuario.objects.create_user(username="caj-local", password="x", rol=Usuario.Rol.CAJERA)
        with patch.object(CajeraCobranzasClient, "aperturar_caja") as aperturar:
            caja = Caja.abrir(cajero=local, usuario=supervisor, motivo="test")
        aperturar.assert_not_called()
        assert caja.caja_codigo == ""

    def test_agencias_de_su_entidad(self, cajera_nest):
        with patch.object(CajeraCobranzasClient, "agencias", return_value=[
                {"sigla_agencia": "CEN", "descripcion": "OFICINA CENTRAL"}, {"sigla_agencia": " ", "descripcion": "?"}]) as ag:
            r = _api(cajera_nest).get("/api/cajas/agencias/")
        ag.assert_called_once_with(1)
        assert r.data == {"requiere_agencia": True, "agencias": [{"sigla": "CEN", "descripcion": "OFICINA CENTRAL"}]}


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest")
class TestCobroConCajaDeLaCajera:
    def test_sin_caja_no_cobra(self, cajera_nest, deudas):  # noqa: F811
        r = _api(cajera_nest).post("/api/cobros-efectivo/", {"deuda_id": deudas[0].pk, "monto_recibido": "100"}, format="json")
        assert r.status_code == 400
        assert "Abre tu caja" in str(r.data)

    def test_la_factura_va_con_el_cliente_de_la_cajera(self, cajera_nest, supervisor, deudas, monkeypatch):  # noqa: F811
        with patch.object(CajeraCobranzasClient, "aperturar_caja", return_value={}), \
             patch.object(CajeraCobranzasClient, "caja_del_dia", return_value={"estado": "ABIERTA", "dispositivo_id": 101}):
            Caja.abrir(cajero=cajera_nest, usuario=supervisor, motivo="test", agencia_sigla="CEN")
        cobro = cobranza_services.registrar_cobro_efectivo(deudas[0], cajera_nest, Decimal("100"))
        compartido = Mock()
        monkeypatch.setattr(cobranza_services, "get_cobranzas_banco_client", lambda: compartido)

        usados = []
        monkeypatch.setattr(cobranza_services, "CajeraCobranzasClient", lambda u: usados.append(u) or Mock(
            crear_transaccion=Mock(return_value="uuid-1"), obtener_comprobante_pdf=Mock(return_value=b"%PDF"),
            obtener_comprobante_json=Mock(return_value={"nro_factura": "70001"})))
        factura = cobranza_services.enviar_factura_a_siic(Factura.objects.get(cobro_efectivo=cobro))

        assert usados == [cajera_nest]
        compartido.crear_transaccion.assert_not_called()
        assert (factura.estado_envio, factura.numero_factura) == ("enviado", "70001")

    def test_cajera_local_sigue_con_el_usuario_compartido(self, deudas, monkeypatch):  # noqa: F811
        local = Usuario.objects.create_user(username="caj-local2", password="x", rol=Usuario.Rol.CAJERA)
        cobro = cobranza_services.registrar_cobro_efectivo(deudas[0], local, Decimal("100"))
        compartido = Mock(crear_transaccion=Mock(return_value="u"), obtener_comprobante_pdf=Mock(return_value=b"%PDF"),
                          obtener_comprobante_json=Mock(return_value={}))
        monkeypatch.setattr(cobranza_services, "get_cobranzas_banco_client", lambda: compartido)
        cobranza_services.enviar_factura_a_siic(Factura.objects.get(cobro_efectivo=cobro))
        compartido.pagar_transaccion_propia.assert_called_once()


@pytest.mark.django_db
@pytest.mark.usefixtures("con_nest")
class TestTokenDeLaCajera:
    def test_un_401_renueva_con_el_refresh(self, cajera_nest):
        cliente = CajeraCobranzasClient(cajera_nest)
        respuestas = [_respuesta(401, {}), _respuesta(200, {"estado": "ABIERTA"})]
        nuevo = _respuesta(200, {"access_token": "acc-2", "refresh_token": "ref-2"})
        with patch("services.cobranzas_banco_client.requests.request", side_effect=respuestas) as req, \
             patch.object(nest.requests, "post", return_value=nuevo) as post:
            assert cliente.caja_del_dia() == {"estado": "ABIERTA"}

        assert post.call_args.kwargs["json"] == {"client_id": "2", "client_secret": "secreto", "scope": "",
                                                 "grant_type": "refresh_token", "refresh_token": "ref-1"}
        assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer acc-2"
        assert nest.token_de(cajera_nest) == "acc-2"

    def test_refresh_rechazado_pide_volver_a_entrar(self, cajera_nest):
        cliente = CajeraCobranzasClient(cajera_nest)
        with patch("services.cobranzas_banco_client.requests.request", return_value=_respuesta(401, {})), \
             patch.object(nest.requests, "post", return_value=_respuesta(401, {"error": "invalid_request"})):
            with pytest.raises(CobranzasBancoAuthError, match="vuelva a iniciar sesión"):
                cliente.caja_del_dia()
        assert not SesionNest.objects.filter(usuario=cajera_nest).exists()

    def test_sin_caja_del_siic_no_factura(self, cajera_nest):
        cliente = CajeraCobranzasClient(cajera_nest)
        with patch("services.cobranzas_banco_client.requests.request", return_value=_respuesta(404, {"error": "x"})):
            with pytest.raises(CobranzasBancoRequestError, match="no tiene caja abierta hoy"):
                cliente.asegurar_caja_abierta()
