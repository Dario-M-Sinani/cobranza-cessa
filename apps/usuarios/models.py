from django.contrib.auth.models import AbstractUser
from django.db import models


class Usuario(AbstractUser):
    class Rol(models.TextChoices):
        CAJERA = "cajera", "Cajera"
        SUPERVISOR = "supervisor", "Supervisor"
        ADMIN = "admin", "Administrador"

    class Origen(models.TextChoices):
        LOCAL = "local", "Local"
        # Viene de la Nest de cobranzas (CAJAS_POR_CAJERA): la clave, el rol y si está habilitado
        # se manejan allá; acá se copian en cada login.
        NEST = "nest", "Nest de cobranzas"

    rol = models.CharField(max_length=20, choices=Rol.choices, default=Rol.CAJERA)
    # Distinto de `is_active` (que Django usa para permitir/bloquear el login):
    # `activo` marca si el usuario está habilitado operativamente para generar
    # cobros hoy, sin tener que desactivar la cuenta completa.
    activo = models.BooleanField(default=True)
    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.LOCAL)
    # Entidad financiera de la Nest (1 = CESSA; las demás son bancos).
    entidad_financiera_id = models.PositiveIntegerField(null=True, blank=True)

    def __str__(self):
        return self.get_full_name() or self.username


class SesionNest(models.Model):
    """Tokens de la Nest de cobranzas del usuario, cifrados (apps/usuarios/nest.py). Viven en la base
    y no en la sesión del navegador porque la factura de un cobro se manda después, desde Celery, y
    tiene que ir con el token y la caja de la cajera que cobró."""

    usuario = models.OneToOneField(Usuario, on_delete=models.CASCADE, related_name="sesion_nest")
    access_cifrado = models.TextField()
    refresh_cifrado = models.TextField(blank=True, default="")
    actualizada_en = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"SesionNest de {self.usuario}"
