from django.db import models


class Cliente(models.Model):
    codigo_externo = models.CharField(max_length=50, unique=True, db_index=True)
    nombre = models.CharField(max_length=255)
    nit_ci = models.CharField(max_length=30, blank=True, default="")

    def __str__(self):
        return f"{self.nombre} ({self.codigo_externo})"
