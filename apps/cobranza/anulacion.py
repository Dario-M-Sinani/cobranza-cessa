"""Anulación de un cobro del panel (como "Anular" en transacciones.xhtml de la app Java).

La hace supervisor/admin, con motivo, solo el mismo día. Si la factura ya está en el SIIC se anula
allá con el usuario de la Nest de quien anula (la Nest no deja anular a una cajera): el SIIC anula el
lote y la deuda vuelve a quedar pendiente. Si todavía no se mandó, se anula solo acá y ya no se manda.
Un cobro de varios clientes se anula entero (cada cliente tiene su propia factura).
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.auditoria.models import LogAuditoria
from apps.usuarios.models import Usuario
from services.cobranzas_banco_client import CajeraCobranzasClient, CobranzasBancoError

from .models import CobroAgrupado, CobroEfectivo, Factura, TransaccionQR


class AnulacionInvalida(Exception):
    pass


def facturas_de(tipo: str, objeto_id: int) -> tuple[list[Factura], object]:
    """Las facturas de un comprobante (`efectivo`, `qr` o `grupo`) y el cobro en sí."""
    if tipo == "efectivo":
        cobro = CobroEfectivo.objects.filter(pk=objeto_id).select_related("factura").first()
        if cobro and cobro.grupo_id:
            raise AnulacionInvalida("Este cobro es parte de un cobro de varios clientes: se anula el grupo entero.")
        cobros = [cobro] if cobro else []
    elif tipo == "qr":
        cobro = TransaccionQR.objects.filter(pk=objeto_id, estado=TransaccionQR.Estado.PAGADO).first()
        cobros = [cobro] if cobro else []
    elif tipo == "grupo":
        cobro = CobroAgrupado.objects.filter(pk=objeto_id).first()
        cobros = list(cobro.cobros.order_by("pk")) if cobro else []
    else:
        raise AnulacionInvalida("Tipo de comprobante inválido.")
    if not cobros:
        raise AnulacionInvalida("No existe ese comprobante.")
    facturas = [Factura.objects.get_or_create(**({"transaccion_qr": c} if tipo == "qr" else {"cobro_efectivo": c}))[0] for c in cobros]
    return facturas, cobro


def anular_comprobante(tipo: str, objeto_id: int, usuario, motivo: str) -> list[Factura]:
    if usuario.rol not in (Usuario.Rol.SUPERVISOR, Usuario.Rol.ADMIN):
        raise AnulacionInvalida("Solo un supervisor o administrador puede anular un cobro.")
    motivo = (motivo or "").strip()
    if len(motivo) < 5:
        raise AnulacionInvalida("Escribe el motivo de la anulación.")

    facturas, cobro = facturas_de(tipo, objeto_id)
    fecha_cobro = timezone.localtime(getattr(cobro, "creado_en", None) or cobro.actualizado_en).date()
    if fecha_cobro != timezone.localdate():
        raise AnulacionInvalida("Solo se pueden anular cobros del día (el SIIC no anula fechas pasadas).")
    pendientes = [f for f in facturas if f.estado_envio != Factura.EstadoEnvio.ANULADO]
    if not pendientes:
        raise AnulacionInvalida("Este cobro ya está anulado.")

    en_siic = [f for f in pendientes if f.estado_envio == Factura.EstadoEnvio.ENVIADO and f.cobranzas_uuid]
    cliente = None
    if en_siic:
        if getattr(usuario, "origen", "") != Usuario.Origen.NEST:
            raise AnulacionInvalida(
                "Este cobro ya está registrado en el SIIC: para anularlo entra con tu usuario del sistema de cobranzas."
            )
        cliente = CajeraCobranzasClient(usuario)

    anuladas = []
    for factura in pendientes:
        with transaction.atomic():
            factura = Factura.objects.select_for_update().get(pk=factura.pk)
            if factura.estado_envio == Factura.EstadoEnvio.ENVIADO and factura.cobranzas_uuid:
                try:
                    cliente.anular_transaccion(factura.cobranzas_uuid)
                except CobranzasBancoError as exc:
                    hechas = f" (ya se anularon {len(anuladas)} de {len(pendientes)})" if anuladas else ""
                    raise AnulacionInvalida(f"El SIIC no anuló la factura {factura.numero_factura or factura.pk}: {exc}{hechas}") from exc
            factura.estado_envio = Factura.EstadoEnvio.ANULADO
            factura.anulada_en = timezone.now()
            factura.anulada_por = usuario
            factura.motivo_anulacion = motivo[:255]
            factura.save(update_fields=["estado_envio", "anulada_en", "anulada_por", "motivo_anulacion"])
            LogAuditoria.objects.create(
                usuario=usuario,
                accion=f"Factura: anulada ({factura.numero_factura or 'sin número'}) -- {motivo}"[:255],
                entidad_afectada=f"Factura:{factura.pk}",
            )
            anuladas.append(factura)
    return anuladas
