"""Cierre de caja en el SIIC (CAJAS_POR_CAJERA), como cierreCaja.xhtml de la app Java de cobranzas.

La cajera, con su usuario de la Nest:
1. Cuenta el efectivo por cortes de billetes y monedas (Bs) → `actualizar-efectivo`.
2. Revisa los documentos del día (cheques, QR...) y agrega los que recibió a mano → `entes-cierre-caja`.
3. Cierra → `cerrar`. Antes, el panel manda al SIIC las facturas de esa caja que falten: con la caja
   cerrada, la Nest ya no deja pagar en ella y esa factura quedaría colgada.

El recaudado y la diferencia los ve solo supervisor/admin (pedido del 2026-09-07: la cajera no ve
el total de su turno). Lo mismo el reporte de cierre en PDF.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

from apps.auditoria.models import LogAuditoria
from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoError

from .models import Caja, CajaOperacionInvalida, Factura, TransaccionQR

CORTES_BS = [
    ("200", Decimal("200")), ("100", Decimal("100")), ("50", Decimal("50")), ("20", Decimal("20")),
    ("10", Decimal("10")), ("5", Decimal("5")), ("2", Decimal("2")), ("1", Decimal("1")),
    ("050", Decimal("0.5")), ("020", Decimal("0.2")), ("010", Decimal("0.1")),
]
# Entes que no se agregan a mano: el efectivo sale de los cortes y el QR lo registra cada cobro.
ENTES_AUTOMATICOS = {"EFECTIVO", "DOLARES", "PAGO POR QR"}


class CierreInvalido(CajaOperacionInvalida):
    pass


def _cliente(caja: Caja) -> CajeraCobranzasClient:
    if not caja.caja_codigo:
        raise CierreInvalido("Esta caja no es una caja del SIIC: se cierra sin arqueo.")
    return CajeraCobranzasClient(caja.cajero)


def _abierta_de(caja: Caja, usuario) -> None:
    if caja.estado != Caja.Estado.ABIERTA:
        raise CierreInvalido("La caja ya está cerrada.")
    if usuario.pk != caja.cajero_id:
        raise CierreInvalido("Solo la cajera dueña de la caja puede hacer su arqueo y cerrarla.")


def _siic(funcion, *args, **kwargs):
    try:
        return funcion(*args, **kwargs)
    except CobranzasBancoError as exc:
        raise CierreInvalido(f"SIIC: {exc}") from exc


def facturas_pendientes(caja: Caja):
    return Factura.objects.filter(Q(cobro_efectivo__caja=caja) | Q(transaccion_qr__caja=caja)).exclude(
        estado_envio__in=[Factura.EstadoEnvio.ENVIADO, Factura.EstadoEnvio.ANULADO]
    )


def qr_sin_confirmar(caja: Caja):
    return TransaccionQR.objects.filter(
        caja=caja, estado__in=[TransaccionQR.Estado.GENERADO, TransaccionQR.Estado.PENDIENTE_CONFIRMACION]
    )


def _documento(fila: dict) -> dict:
    return {
        "lote": str(fila.get("lote") or ""),
        "indice": str(fila.get("indice") or ""),
        "ente": str(fila.get("ente_descripcion") or ""),
        "banco": str(fila.get("banco_descripcion") or ""),
        "moneda": str(fila.get("moneda") or "B"),
        "numero": str(fila.get("numero") or "").strip(),
        "importe": str(fila.get("importe") or "0"),
        "fecha_registro": str(fila.get("fecha_registro") or ""),
        "fecha_vencimiento": str(fila.get("fecha_vencimiento") or ""),
        # Solo los que agregó la cajera (lote 9000) se corrigen desde acá.
        "editable": str(fila.get("lote")) == "9000",
    }


def estado_cierre(caja: Caja, ver_totales: bool) -> dict:
    """Todo lo que necesita la pantalla de cierre. Con la caja cerrada, lo que quedó guardado al cerrar."""
    base = {
        "caja_id": caja.pk,
        "estado": caja.estado,
        "cortes": caja.arqueo_cortes,
        "facturas_pendientes": facturas_pendientes(caja).count(),
        "qr_sin_confirmar": qr_sin_confirmar(caja).count(),
    }
    if caja.estado == Caja.Estado.CERRADA:
        cerrado = dict(caja.cierre_siic)
        if not ver_totales:
            cerrado.pop("datos", None)
        return base | {"documentos": cerrado.get("documentos", []), "cierre": cerrado}

    cliente = _cliente(caja)
    documentos = [_documento(f) for f in _siic(cliente.entes_cierre)]
    entes = [e for e in _siic(cliente.catalogo, "entes") if str(e.get("descripcion", "")).upper() not in ENTES_AUTOMATICOS]
    datos = {
        "documentos": documentos,
        "entes": [{"id": e["id"], "descripcion": e["descripcion"]} for e in entes],
        "bancos": [{"id": b["id"], "descripcion": b["descripcion"]} for b in _siic(cliente.catalogo, "bancos")],
    }
    if ver_totales:
        datos["totales"] = _siic(cliente.datos_cierre)
    return base | datos


def declarar_efectivo(caja: Caja, usuario, cortes: dict) -> dict:
    _abierta_de(caja, usuario)
    limpios = {}
    for corte, _ in CORTES_BS:
        valor = cortes.get(corte, 0)
        try:
            cantidad = int(str(valor).strip() or 0)
        except ValueError:
            raise CierreInvalido(f"La cantidad de billetes/monedas de {corte} no es un número entero.")
        if cantidad < 0:
            raise CierreInvalido("Las cantidades no pueden ser negativas.")
        limpios[f"corte_bs_{corte}"] = cantidad
    _siic(_cliente(caja).actualizar_efectivo, "B", limpios)
    caja.arqueo_cortes = {k.removeprefix("corte_bs_"): v for k, v in limpios.items()}
    caja.save(update_fields=["arqueo_cortes"])
    total = sum((Decimal(caja.arqueo_cortes[c]) * v for c, v in CORTES_BS), Decimal("0"))
    LogAuditoria.objects.create(usuario=usuario, accion=f"Caja: efectivo declarado Bs {total:.2f}", entidad_afectada=f"Caja:{caja.pk}")
    return {"cortes": caja.arqueo_cortes, "efectivo_bs": f"{total:.2f}"}


def guardar_documento(caja: Caja, usuario, datos: dict) -> dict:
    """Agrega (o corrige, con `indice`) un documento recibido a mano: cheque, depósito, tarjeta..."""
    _abierta_de(caja, usuario)
    try:
        importe = Decimal(str(datos.get("importe") or "0"))
    except InvalidOperation:
        raise CierreInvalido("El importe no es válido.")
    numero = str(datos.get("numero") or "").strip()
    if importe < 1:
        raise CierreInvalido("El importe tiene que ser de al menos Bs 1.")
    if not numero or len(numero) > 20:
        raise CierreInvalido("El número del documento es obligatorio (hasta 20 caracteres).")
    cuerpo = {
        "ente_id": int(datos.get("ente_id") or 0),
        "moneda": "D" if datos.get("moneda") == "D" else "B",
        "banco_id": int(datos.get("banco_id") or 0),
        "importe": float(importe),
        "numero": numero,
    }
    for campo in ("fecha_registro", "fecha_vencimiento"):  # AAAAMMDD (cheques)
        valor = str(datos.get(campo) or "").replace("-", "")
        if valor:
            cuerpo[campo] = int(valor)
    modificar = bool(datos.get("indice"))
    if modificar:
        cuerpo["indice"] = int(datos["indice"])
    respuesta = _siic(_cliente(caja).guardar_documento, cuerpo, modificar)
    LogAuditoria.objects.create(
        usuario=usuario,
        accion=f"Caja: documento {'corregido' if modificar else 'agregado'} al cierre ({numero}, Bs {importe:.2f})",
        entidad_afectada=f"Caja:{caja.pk}",
    )
    return respuesta


def cerrar_en_siic(caja: Caja, usuario, descripcion: str = "") -> Caja:
    from .services import enviar_factura_a_siic

    _abierta_de(caja, usuario)
    cliente = _cliente(caja)
    if qr_sin_confirmar(caja).exists():
        raise CierreInvalido(
            "Hay QR esperando el pago del cliente en esta caja. Espera la confirmación o anúlalos antes de cerrar."
        )
    for factura in facturas_pendientes(caja):
        enviar_factura_a_siic(factura)
    pendientes = facturas_pendientes(caja).count()
    if pendientes:
        raise CierreInvalido(
            f"{pendientes} cobro(s) de esta caja todavía no se registraron en el SIIC. Revisa Transacciones o "
            "avisa a tu supervisora: con la caja cerrada ya no se podrían registrar."
        )

    documentos = [_documento(f) for f in _siic(cliente.entes_cierre)]
    datos = _siic(cliente.datos_cierre)
    _siic(cliente.cerrar_caja, descripcion)
    caja.cierre_siic = {
        "datos": datos,
        "documentos": documentos,
        "descripcion": descripcion[:255],
        "cerrada_en_siic": timezone.now().isoformat(),
    }
    caja.save(update_fields=["cierre_siic"])
    LogAuditoria.objects.create(
        usuario=usuario,
        accion=f"Caja: cerrada en el SIIC (caja {caja.caja_codigo}, diferencia Bs {datos.get('diferencia_bs', '?')})",
        entidad_afectada=f"Caja:{caja.pk}",
    )
    return caja.cerrar(usuario=usuario)


def reporte_cierre(caja: Caja) -> bytes:
    if caja.estado != Caja.Estado.CERRADA or not caja.cierre_siic:
        raise CierreInvalido("El reporte de cierre sale cuando la caja ya se cerró en el SIIC.")
    # La fecha de la caja del SIIC es la de su apertura (creada_en: una reapertura mueve abierta_en).
    fecha = timezone.localtime(caja.creada_en).strftime("%Y%m%d")
    return _siic(_cliente(caja).reporte_cierre, fecha)
