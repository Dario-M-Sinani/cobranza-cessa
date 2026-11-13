"""Cajeros de la Nest (apps/usuarios/cajeros.py): lista y ficha para supervisor/admin, alta doble y
cambios solo admin, siempre con el token de la Nest de quien opera."""
from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient

from apps.usuarios import nest
from apps.usuarios.models import Usuario
from services.cobranzas_banco_client import CajeraCobranzasClient


def _respuesta(status, cuerpo):
    r = Mock(status_code=status, ok=200 <= status < 300, text=str(cuerpo))
    r.json.return_value = cuerpo
    return r


def _usuario_nest(username, rol):
    u = Usuario.objects.create_user(username=username, rol=rol, origen=Usuario.Origen.NEST)
    u.set_unusable_password()
    u.save()
    nest.guardar_sesion(u, {"access_token": f"acc-{username}", "refresh_token": "r"})
    return u


def _api(u):
    c = APIClient()
    c.force_authenticate(user=u)
    return c


@pytest.fixture
def admin(db):
    return _usuario_nest("SISTEMAS1", Usuario.Rol.ADMIN)


@pytest.fixture
def supervisora(db):
    return _usuario_nest("SUPERV", Usuario.Rol.SUPERVISOR)


def _nest(rutas: dict):
    """Simula la Nest: {(metodo, ruta): respuesta}; guarda lo que se pidió."""
    pedidos = []

    def request(self, metodo, ruta, **kwargs):
        pedidos.append((metodo, ruta, kwargs.get("json"), self.usuario.username))
        return rutas[(metodo, ruta)]

    return patch.object(CajeraCobranzasClient, "_request_autenticado", autospec=True, side_effect=request), pedidos


ALTA = {
    "username": "cajera3", "password": "secreta1", "nombre": "Gladys Paredes", "rol": "CAJA",
    "entidad_financiera_id": 1, "hora_inicial": "07:30", "hora_final": "15:30",
    "habilitacion_inicio": "2026-11-13", "habilitacion_fin": "2027-11-13",
    "dias": {"lunes": True, "martes": True, "sabado": True},
}


@pytest.mark.django_db
class TestCajeros:
    def test_lista_con_caja_de_hoy(self, supervisora):
        parche, _ = _nest({
            ("get", "/v1/usuarios"): _respuesta(200, [
                {"id": 2, "username": "CAJERA1", "nombre": "MARIA", "rol": "CAJA", "habilitado": True, "entidad_financiera_id": 1, "caja_codigo": 101},
                {"id": 4, "username": "CAJABNB", "nombre": "LUIS", "rol": "CAJA", "habilitado": False, "entidad_financiera_id": 4},
            ]),
            ("get", "/v1/usuarios/caja-aperturada"): _respuesta(200, [{"username": "CAJERA1", "caja_codigo": 101}]),
            ("get", "/v1/entidades-financieras"): _respuesta(200, [{"id": 1, "descripcion": "CESSA "}]),
        })
        with parche:
            r = _api(supervisora).get("/api/cajeros/")
        assert r.status_code == 200, r.data
        assert [(c["username"], c["caja_hoy"], c["habilitado"]) for c in r.data["cajeros"]] == [
            ("CAJERA1", True, True), ("CAJABNB", False, False)]
        assert r.data["entidades"] == [{"id": 1, "descripcion": "CESSA"}]

    def test_ficha_con_horario_legible(self, supervisora):
        parche, _ = _nest({("get", "/v1/usuarios/2"): _respuesta(200, {
            "id": 2, "username": "CAJERA1", "nombre": "MARIA", "rol": "CAJA", "habilitado": True, "entidad_financiera_id": 1,
            "hora_inicial": "80000", "hora_final": "180000", "habilitacion_inicio": "20260101", "habilitacion_fin": "20261231",
            "habilitado_lunes": True, "habilitado_domingo": False})})
        with parche:
            r = _api(supervisora).get("/api/cajeros/2/")
        assert (r.data["hora_inicial"], r.data["hora_final"]) == ("08:00", "18:00")
        assert (r.data["habilitacion_inicio"], r.data["dias"]["lunes"], r.data["dias"]["domingo"]) == ("2026-01-01", True, False)

    def test_alta_doble_en_la_nest_con_el_token_del_admin(self, admin):
        parche, pedidos = _nest({("post", "/v1/usuarios"): _respuesta(201, {"id": 8, "username": "CAJERA3"})})
        with parche:
            r = _api(admin).post("/api/cajeros/", ALTA, format="json")
        assert r.status_code == 201, r.data
        metodo, ruta, cuerpo, quien = pedidos[0]
        assert quien == "SISTEMAS1"
        assert cuerpo["username"] == "CAJERA3" and cuerpo["nombre"] == "GLADYS PAREDES"
        assert (cuerpo["hora_inicial"], cuerpo["habilitacion_fin"]) == ("07:30:00", "20271113")
        assert (cuerpo["habilitado_lunes"], cuerpo["habilitado_sabado"], cuerpo["habilitado_domingo"]) == (True, True, False)

    def test_la_supervisora_no_crea(self, supervisora):
        assert _api(supervisora).post("/api/cajeros/", ALTA, format="json").status_code == 403

    @pytest.mark.parametrize("cambio, mensaje", [
        ({"username": "con espacio"}, "3 a 10 letras"),
        ({"password": "123"}, "al menos 6"),
        ({"habilitacion_fin": "2026-01-01"}, "termina antes"),
        ({"hora_inicial": "8"}, "HH:MM"),
    ])
    def test_validaciones(self, admin, cambio, mensaje):
        r = _api(admin).post("/api/cajeros/", ALTA | cambio, format="json")
        assert r.status_code == 400 and mensaje in r.data["detail"]

    def test_error_de_la_nest_legible(self, admin):
        parche, _ = _nest({("post", "/v1/usuarios"): _respuesta(422, {"error": {"username": ["The username has already been taken."]}})})
        with parche:
            r = _api(admin).post("/api/cajeros/", ALTA, format="json")
        assert r.status_code == 422 and r.data["detail"] == "Username: ya existe."

    def test_deshabilitar_se_refleja_en_el_panel(self, admin):
        cajera = _usuario_nest("CAJERA1", Usuario.Rol.CAJERA)
        parche, pedidos = _nest({("patch", "/v1/usuarios/CAJERA1"): _respuesta(200, {"habilitado": False})})
        with parche:
            r = _api(admin).patch("/api/cajeros/cajera1/", {"habilitado": False}, format="json")
        assert r.status_code == 200, r.data
        assert pedidos[0][2] == {"habilitado": False}
        cajera.refresh_from_db()
        assert cajera.activo is False

    def test_un_admin_local_no_tiene_acceso(self, db):
        local = Usuario.objects.create_user(username="admin_demo", password="x", rol=Usuario.Rol.ADMIN)
        r = _api(local).get("/api/cajeros/")
        assert r.status_code == 400 and "usuario del sistema de cobranzas" in r.data["detail"]


@pytest.mark.django_db
class TestCambiarContrasena:
    def test_usuario_nest_se_cambia_en_la_nest(self, supervisora):
        parche, pedidos = _nest({("post", "/v1/cambiar-contrasenia"): _respuesta(200, {})})
        with parche:
            r = _api(supervisora).post("/api/cambiar-contrasena/", {"actual": "local", "nueva": "nueva123"}, format="json")
        assert r.status_code == 200, r.data
        assert pedidos[0][2] == {"password_actual": "local", "password_nuevo": "nueva123"}

    def test_usuario_nest_clave_actual_mala(self, supervisora):
        parche, _ = _nest({("post", "/v1/cambiar-contrasenia"): _respuesta(401, {"error": "El usuario o contraseña son incorrectos"})})
        with parche:
            r = _api(supervisora).post("/api/cambiar-contrasena/", {"actual": "mala", "nueva": "nueva123"}, format="json")
        assert r.status_code == 400 and "actual no es correcta" in r.data["detail"]

    def test_usuario_local_se_cambia_aca(self, db):
        local = Usuario.objects.create_user(username="admin_demo", password="Demo12345!", rol=Usuario.Rol.ADMIN)
        assert _api(local).post("/api/cambiar-contrasena/", {"actual": "mala", "nueva": "Nueva12345!"}, format="json").status_code == 400
        assert _api(local).post("/api/cambiar-contrasena/", {"actual": "Demo12345!", "nueva": "corta"}, format="json").status_code == 400
        r = _api(local).post("/api/cambiar-contrasena/", {"actual": "Demo12345!", "nueva": "Nueva12345!"}, format="json")
        assert r.status_code == 200
        local.refresh_from_db()
        assert local.check_password("Nueva12345!")


@pytest.mark.django_db
class TestTransaccionesDeCajeros:
    def test_lista_con_origen(self, supervisora):
        from apps.cobranza.models import CobroEfectivo, Deuda, Factura
        from apps.clientes.models import Cliente

        cli = Cliente.objects.create(codigo_externo="1", nombre="x")
        deuda = Deuda.objects.create(cliente=cli, monto=1)
        cobro = CobroEfectivo.objects.create(deuda=deuda, usuario=supervisora, monto_snapshot=1, monto_recibido=1, vuelto=0)
        Factura.objects.create(cobro_efectivo=cobro, cobranzas_uuid="u-panel")
        parche, pedidos = _nest({("get", "/v1/transacciones"): _respuesta(200, [
            {"uuid": "u-panel", "lote": 1, "estado": "PAGADA", "fecha_creacion": "2026-11-13 10:00:00"},
            {"uuid": "u-banco", "lote": 2, "estado": "PAGADA", "fecha_creacion": "2026-11-13 11:00:00"},
        ])})
        with parche:
            r = _api(supervisora).get("/api/cajeros/4/transacciones/?desde=2026-11-01&hasta=2026-11-13")
        assert r.status_code == 200, r.data
        assert pedidos[0][2] is None
        assert [(t["uuid"], t["origen"]) for t in r.data["transacciones"]] == [("u-panel", "panel"), ("u-banco", "")]

    def test_rango_maximo(self, supervisora):
        r = _api(supervisora).get("/api/cajeros/4/transacciones/?desde=2026-01-01&hasta=2026-11-13")
        assert r.status_code == 400 and "31 días" in r.data["detail"]

    def test_anula_la_de_un_banco_con_motivo_y_auditoria(self, supervisora):
        from apps.auditoria.models import LogAuditoria

        parche, pedidos = _nest({("post", "/v1/transacciones/u-banco/anular"): _respuesta(200, {"estado": "ANULADA", "lote": 2})})
        with parche:
            r = _api(supervisora).post("/api/transacciones-siic/u-banco/anular/", {"motivo": "cobro duplicado"}, format="json")
        assert r.status_code == 200, r.data
        assert pedidos[0][3] == "SUPERV"
        assert LogAuditoria.objects.filter(accion__contains="cobro duplicado").exists()

    def test_no_anula_pagos_web(self, supervisora):
        from django.utils import timezone

        from apps.facturacion_externa.models import SolicitudLiquidacion

        campos = {f.name for f in SolicitudLiquidacion._meta.get_fields()}
        datos = {"alias": "CESSA-W-1", "cobranzas_uuid": "u-web", "nro_cliente": "1", "monto": 1, "detalle": [], "fecha_pago": timezone.now()}
        SolicitudLiquidacion.objects.create(**{k: v for k, v in datos.items() if k in campos})
        r = _api(supervisora).post("/api/transacciones-siic/u-web/anular/", {"motivo": "cobro duplicado"}, format="json")
        assert r.status_code == 400 and "pago web" in r.data["detail"]
