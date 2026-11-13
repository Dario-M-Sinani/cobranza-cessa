"""Cajeros de la Nest de cobranzas (como admin.xhtml de la app Java): todos los usuarios de todas las
entidades (CESSA y bancos). El panel no los guarda: los pide a la Nest con el token de quien mira.

- Listar y ver la ficha: supervisor (COBRANZA) y admin (SISTEMAS).
- Crear, editar, habilitar: solo admin (SISTEMAS). El alta es doble en la Nest, en una sola operación:
  su usuario (Postgres) y el operador del SIIC (OPERCOB), las dos cosas o ninguna.

La Nest valida los roles por su cuenta; acá solo se exige que quien opera haya entrado con su usuario
de la Nest (un usuario local no tiene token allá).
"""
from __future__ import annotations

import re
from datetime import date

from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoError

from .models import Usuario

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
ROLES = {"CAJA": "Cajero", "COBRANZA": "Supervisor de cobranza", "ENTIDAD_FINANCIERA": "Responsable de entidad", "SISTEMAS": "Sistemas"}


class CajerosError(Exception):
    def __init__(self, mensaje: str, status: int = 400):
        super().__init__(mensaje)
        self.status = status


def _cliente(usuario) -> CajeraCobranzasClient:
    if getattr(usuario, "origen", "") != Usuario.Origen.NEST:
        raise CajerosError("Para ver o administrar cajeros entra con tu usuario del sistema de cobranzas.")
    return CajeraCobranzasClient(usuario)


def _llamar(usuario, metodo: str, ruta: str, **kwargs):
    cliente = _cliente(usuario)
    try:
        respuesta = cliente._request_autenticado(metodo, ruta, **kwargs)
    except CobranzasBancoError as exc:
        raise CajerosError(f"Sistema de cobranzas: {exc}", 502) from exc
    if not respuesta.ok:
        status = respuesta.status_code if respuesta.status_code in (400, 403, 404, 422) else 502
        raise CajerosError(_mensaje(respuesta, cliente), status)
    return respuesta.json()


TRADUCCIONES = {
    "has already been taken": "ya existe",
    "field is required": "es obligatorio",
    "may not be greater than": "es demasiado largo",
}


def _mensaje(respuesta, cliente) -> str:
    """Los errores de validación de la Nest vienen como {"campo": ["The ... has already been taken."]}."""
    try:
        error = (respuesta.json() or {}).get("error")
    except ValueError:
        error = None
    if not isinstance(error, dict):
        return cliente._extraer_error(respuesta)
    partes = []
    for campo, mensajes in error.items():
        texto = " ".join(mensajes) if isinstance(mensajes, list) else str(mensajes)
        traducido = next((es for en, es in TRADUCCIONES.items() if en in texto), texto)
        partes.append(f"{campo.replace('_', ' ').capitalize()}: {traducido}.")
    return " ".join(partes)


def _hora(valor) -> str:
    """OPERCOB guarda la hora como número HHMMSS sin el cero inicial ("80000" = 08:00)."""
    texto = re.sub(r"\D", "", str(valor or "")).rjust(6, "0")[-6:]
    return f"{texto[:2]}:{texto[2:4]}"


def _fecha(valor) -> str:
    texto = re.sub(r"\D", "", str(valor or ""))
    return f"{texto[:4]}-{texto[4:6]}-{texto[6:8]}" if len(texto) == 8 else ""


def entidades(usuario) -> list[dict]:
    return [{"id": int(e["id"]), "descripcion": str(e["descripcion"]).strip()} for e in _llamar(usuario, "get", "/v1/entidades-financieras")]


def listar(usuario, entidad_id: int | None = None) -> list[dict]:
    params = {"entidad_financiera_id": entidad_id} if entidad_id else {}
    filas = _llamar(usuario, "get", "/v1/usuarios", params=params)
    try:
        con_caja = {str(c.get("username")) for c in _llamar(usuario, "get", "/v1/usuarios/caja-aperturada")}
    except CajerosError:
        con_caja = set()
    return sorted(
        (
            {
                "id": f["id"],
                "username": f["username"],
                "nombre": f.get("nombre") or "",
                "rol": f["rol"],
                "rol_descripcion": ROLES.get(f["rol"], f["rol"]),
                "habilitado": bool(f.get("habilitado")),
                "entidad_financiera_id": f.get("entidad_financiera_id"),
                "caja_hoy": f["username"] in con_caja,
                "caja_codigo": f.get("caja_codigo") if f["username"] in con_caja else None,
            }
            for f in filas
        ),
        key=lambda f: (f["entidad_financiera_id"] or 0, f["username"]),
    )


def ficha(usuario, id_nest: int) -> dict:
    f = _llamar(usuario, "get", f"/v1/usuarios/{int(id_nest)}")
    return {
        "id": f["id"],
        "username": f["username"],
        "nombre": f.get("nombre") or "",
        "rol": f["rol"],
        "habilitado": bool(f.get("habilitado")),
        "entidad_financiera_id": f.get("entidad_financiera_id"),
        "hora_inicial": _hora(f.get("hora_inicial")),
        "hora_final": _hora(f.get("hora_final")),
        "habilitacion_inicio": _fecha(f.get("habilitacion_inicio")),
        "habilitacion_fin": _fecha(f.get("habilitacion_fin")),
        "dias": {d: bool(f.get(f"habilitado_{d}")) for d in DIAS},
    }


def _para_nest(datos: dict, alta: bool) -> dict:
    """Del formulario del panel al contrato de la Nest (Lumen): horas H:i:s, fechas Ymd, días sueltos."""
    cuerpo: dict = {}
    for campo in ("nombre", "rol"):
        if campo in datos:
            cuerpo[campo] = str(datos[campo] or "").strip()
    if "nombre" in cuerpo:
        cuerpo["nombre"] = cuerpo["nombre"].upper()
    if datos.get("password"):
        cuerpo["password"] = str(datos["password"])
    if "habilitado" in datos:
        cuerpo["habilitado"] = bool(datos["habilitado"])
    if "entidad_financiera_id" in datos:
        cuerpo["entidad_financiera_id"] = int(datos["entidad_financiera_id"])
    for campo in ("hora_inicial", "hora_final"):
        if datos.get(campo):
            if not re.fullmatch(r"\d{2}:\d{2}", str(datos[campo])):
                raise CajerosError("Las horas van como HH:MM.")
            cuerpo[campo] = f"{datos[campo]}:00"
    for campo in ("habilitacion_inicio", "habilitacion_fin"):
        if datos.get(campo):
            try:
                cuerpo[campo] = date.fromisoformat(str(datos[campo])).strftime("%Y%m%d")
            except ValueError:
                raise CajerosError("Las fechas de vigencia no son válidas.")
    if isinstance(datos.get("dias"), dict):
        for d in DIAS:
            cuerpo[f"habilitado_{d}"] = bool(datos["dias"].get(d))
    if alta:
        username = str(datos.get("username") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9_]{3,10}", username):
            raise CajerosError("El usuario tiene que tener de 3 a 10 letras o números, sin espacios.")
        if len(str(datos.get("password") or "")) < 6:
            raise CajerosError("La contraseña tiene que tener al menos 6 caracteres.")
        cuerpo["username"] = username
        faltan = [c for c in ("nombre", "rol", "hora_inicial", "hora_final", "habilitacion_inicio", "habilitacion_fin",
                              "entidad_financiera_id") if not cuerpo.get(c)]
        if faltan:
            raise CajerosError("Faltan datos: " + ", ".join(c.replace("_", " ") for c in faltan) + ".")
        cuerpo.setdefault("habilitado", True)
        for d in DIAS:
            cuerpo.setdefault(f"habilitado_{d}", False)
    if cuerpo.get("habilitacion_inicio") and cuerpo.get("habilitacion_fin") and cuerpo["habilitacion_fin"] < cuerpo["habilitacion_inicio"]:
        raise CajerosError("La vigencia termina antes de empezar.")
    return cuerpo


def crear(usuario, datos: dict) -> dict:
    return _llamar(usuario, "post", "/v1/usuarios", json=_para_nest(datos, alta=True))


def actualizar(usuario, username: str, datos: dict) -> dict:
    cuerpo = _para_nest(datos, alta=False)
    if not cuerpo:
        raise CajerosError("No hay cambios.")
    respuesta = _llamar(usuario, "patch", f"/v1/usuarios/{username}", json=cuerpo)
    # Si ya entró alguna vez al panel, se refleja acá también (el próximo login lo vuelve a copiar).
    local = Usuario.objects.filter(username=username, origen=Usuario.Origen.NEST).first()
    if local:
        if "habilitado" in cuerpo:
            local.activo = cuerpo["habilitado"]
        if "nombre" in cuerpo:
            local.first_name = cuerpo["nombre"][:150]
        local.save(update_fields=["activo", "first_name"])
    return respuesta


def cambiar_contrasena(usuario, actual: str, nueva: str) -> None:
    """Con el token del propio usuario: la Nest verifica la actual y corta sus otras sesiones."""
    cliente = _cliente(usuario)
    try:
        respuesta = cliente._request_autenticado(
            "post", "/v1/cambiar-contrasenia", json={"password_actual": actual, "password_nuevo": nueva}
        )
    except CobranzasBancoError as exc:
        raise CajerosError(f"Sistema de cobranzas: {exc}", 502) from exc
    if respuesta.status_code == 401:
        raise CajerosError("La contraseña actual no es correcta.")
    if not respuesta.ok:
        raise CajerosError(_mensaje(respuesta, cliente), 400 if respuesta.status_code in (400, 422) else 502)


# --- Transacciones de un cajero (como transacciones.xhtml de la app Java) -------------------------

def transacciones(usuario, id_nest: int, desde: date, hasta: date) -> list[dict]:
    """Las transacciones de cualquier cajero (también de los bancos) en un rango de fechas."""
    if hasta < desde:
        raise CajerosError("La fecha final es anterior a la inicial.")
    if (hasta - desde).days > 31:
        raise CajerosError("El rango no puede pasar de 31 días.")
    filas = _llamar(
        usuario, "get", "/v1/transacciones",
        params={"usuario_id": int(id_nest), "fecha_inicio": desde.isoformat(), "fecha_fin": hasta.isoformat()},
    )
    origenes = _origenes([f["uuid"] for f in filas])
    return [
        {
            "uuid": f["uuid"],
            "lote": f.get("lote"),
            "caja_codigo": f.get("caja_codigo"),
            "estado": f.get("estado"),
            "total_pagado": f.get("total_pagado"),
            "fecha_creacion": f.get("fecha_creacion"),
            "fecha_pago": f.get("fecha_pago"),
            "fecha_anulacion": f.get("fecha_anulacion"),
            # "panel": cobro de este panel (se anula desde Comprobantes); "web": pago web, del bot o del
            # kiosco (el dinero ya está en el banco: no se anula acá); "": de un banco o de la app Java.
            "origen": origenes.get(f["uuid"], ""),
        }
        for f in filas
    ]


def _origenes(uuids: list[str]) -> dict[str, str]:
    from apps.cobranza.models import Factura
    from apps.facturacion_externa.models import SolicitudLiquidacion

    origen = {u: "web" for u in SolicitudLiquidacion.objects.filter(cobranzas_uuid__in=uuids).values_list("cobranzas_uuid", flat=True)}
    origen.update({u: "panel" for u in Factura.objects.filter(cobranzas_uuid__in=uuids).values_list("cobranzas_uuid", flat=True)})
    return origen


def detalle_transaccion(usuario, uuid: str) -> dict:
    t = _llamar(usuario, "get", f"/v1/transacciones/{uuid}")
    detalle = t.get("detalle") or []
    return {
        "uuid": t.get("uuid"),
        "estado": t.get("estado"),
        "lote": t.get("lote"),
        "total_pagado": t.get("total_pagado"),
        "items": [
            {
                "nro_cliente": str(i.get("nro_cliente") or ""),
                "nro_comprobante": str(i.get("nro_comprobante") or ""),
                "detalle": str(i.get("detalle") or ""),
                "debito_credito": str(i.get("debito_credito") or ""),
                "importe": str(i.get("importe") or "0"),
            }
            for i in (detalle if isinstance(detalle, list) else [])
        ],
    }


def anular_transaccion(usuario, uuid: str, motivo: str) -> dict:
    """Anula en el SIIC una transacción de un cajero de banco (o de la app Java). Las del panel van por
    Comprobantes (apps/cobranza/anulacion.py), que además las saca de los totales."""
    from apps.auditoria.models import LogAuditoria

    motivo = (motivo or "").strip()
    if len(motivo) < 5:
        raise CajerosError("Escribe el motivo de la anulación.")
    origen = _origenes([uuid]).get(uuid)
    if origen == "panel":
        raise CajerosError("Es un cobro hecho en este panel: anúlalo desde Comprobantes.")
    if origen == "web":
        raise CajerosError("Es un pago web, del bot o del kiosco: el dinero ya entró al banco y no se anula desde acá.")
    respuesta = _llamar(usuario, "post", f"/v1/transacciones/{uuid}/anular")
    LogAuditoria.objects.create(
        usuario=usuario,
        accion=f"Transacción del SIIC anulada (lote {respuesta.get('lote', '?')}) -- {motivo}"[:255],
        entidad_afectada=f"Transaccion:{uuid}"[:255],
    )
    return respuesta
