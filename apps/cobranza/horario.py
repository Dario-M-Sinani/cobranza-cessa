"""Ventana horaria dentro de la cual un cajero puede abrir su Caja sin
intervención de un supervisor/administrador. Configurable vía
CAJA_HORARIO_INICIO/CAJA_HORARIO_FIN (horas 0-23) -- el horario real de
CESSA todavía no está confirmado, ver REQUISITOS_COBRANZA.md pregunta 7."""
from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.utils import timezone


def dentro_de_horario_operativo(momento: datetime | None = None) -> bool:
    momento = momento or timezone.localtime()
    return settings.CAJA_HORARIO_INICIO <= momento.hour < settings.CAJA_HORARIO_FIN
