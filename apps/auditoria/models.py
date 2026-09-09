from django.conf import settings
from django.db import models


class LogAuditoria(models.Model):
    # Nulo cuando la transición la dispara un proceso del sistema (ej. la
    # tarea Celery que confirma pagos) y no la acción directa de un usuario.
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="logs_auditoria",
    )
    accion = models.CharField(max_length=255)
    entidad_afectada = models.CharField(max_length=255)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-creado_en"]
        verbose_name = "Log de auditoría"
        verbose_name_plural = "Logs de auditoría"

    def __str__(self):
        return f"{self.creado_en:%Y-%m-%d %H:%M} {self.accion}"
