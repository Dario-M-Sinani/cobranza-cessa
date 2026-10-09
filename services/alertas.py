"""Envío de alertas al equipo (Telegram y/o correo). Best effort: nunca lanza ni frena nada.

Configuración (.env), todo opcional; sin nada configurado no se envía nada:
- ALERTAS_TELEGRAM_BOT_TOKEN y ALERTAS_TELEGRAM_CHAT_IDS (separados por coma): un bot de
  Telegram y los chats (personas o grupo) que reciben. El chat tiene que haberle escrito al
  bot (o tenerlo en el grupo) al menos una vez.
- ALERTAS_EMAIL_DESTINOS (separados por coma): usa el correo de Django (EMAIL_HOST, etc.).
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger("alertas")


def hay_canal_configurado() -> bool:
    return bool(
        (settings.ALERTAS_TELEGRAM_BOT_TOKEN and settings.ALERTAS_TELEGRAM_CHAT_IDS) or settings.ALERTAS_EMAIL_DESTINOS
    )


def notificar(asunto: str, texto: str) -> int:
    """Manda la alerta por todos los canales configurados. Devuelve cuántos envíos salieron."""
    enviados = 0
    token = settings.ALERTAS_TELEGRAM_BOT_TOKEN
    for chat_id in settings.ALERTAS_TELEGRAM_CHAT_IDS if token else []:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": f"{asunto}\n\n{texto}", "disable_web_page_preview": True},
                timeout=15,
            )
            if r.ok:
                enviados += 1
            else:
                logger.warning("alerta telegram chat %s: HTTP %s %s", chat_id, r.status_code, r.text[:200])
        except requests.RequestException as exc:
            logger.warning("alerta telegram chat %s: %s", chat_id, exc)

    destinos = settings.ALERTAS_EMAIL_DESTINOS
    if destinos:
        try:
            enviados += send_mail(f"[Cobranza CESSA] {asunto}", texto, None, destinos, fail_silently=False) and len(destinos)
        except Exception as exc:  # noqa: BLE001 -- SMTP mal configurado no debe romper nada
            logger.warning("alerta email: %s", exc)

    if not enviados:
        logger.warning("alerta sin enviar (¿canales configurados?): %s | %s", asunto, texto[:300])
    return enviados
