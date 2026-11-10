"""Momento cívico del kiosco: todos los días a KIOSCO_HIMNO_HORA (hora del servidor, no la de la
PC de la pantalla) se muestra la bandera, se pide ponerse de pie y suena el himno."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone


def _hora() -> time | None:
    try:
        horas, minutos = (int(x) for x in str(settings.KIOSCO_HIMNO_HORA).strip().split(":"))
        return time(horas, minutos)
    except (TypeError, ValueError):
        return None


def archivo() -> Path | None:
    ruta = Path(settings.KIOSCO_HIMNO_ARCHIVO) if settings.KIOSCO_HIMNO_ARCHIVO else None
    return ruta if ruta and ruta.is_file() else None


def proximo(ahora: datetime | None = None) -> dict | None:
    """{fecha, segundos_para, audio, segundos_sin_audio} del próximo himno, o None si está apagado.
    Si ya pasó la hora de hoy (por más de un minuto), es el de mañana."""
    hora = _hora()
    if hora is None:
        return None
    local = timezone.localtime(ahora or timezone.now())
    momento = local.replace(hour=hora.hour, minute=hora.minute, second=0, microsecond=0)
    if local - momento > timedelta(minutes=1):
        momento += timedelta(days=1)
    return {
        "fecha": momento.date().isoformat(),
        "segundos_para": max(0, int((momento - local).total_seconds())),
        "audio": archivo() is not None,
        "segundos_sin_audio": settings.KIOSCO_HIMNO_SEGUNDOS_SIN_AUDIO,
    }
