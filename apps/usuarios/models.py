from django.contrib.auth.models import AbstractUser
from django.db import models


class Usuario(AbstractUser):
    class Rol(models.TextChoices):
        CAJERA = "cajera", "Cajera"
        SUPERVISOR = "supervisor", "Supervisor"
        ADMIN = "admin", "Administrador"

    rol = models.CharField(max_length=20, choices=Rol.choices, default=Rol.CAJERA)
    # Distinto de `is_active` (que Django usa para permitir/bloquear el login):
    # `activo` marca si el usuario está habilitado operativamente para generar
    # cobros hoy, sin tener que desactivar la cuenta completa.
    activo = models.BooleanField(default=True)

    def __str__(self):
        return self.get_full_name() or self.username
