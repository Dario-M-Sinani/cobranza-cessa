"""Crea (o actualiza) el grupo «Contenido del kiosco»: quien está en él entra solo al editor de la
pantalla (/kiosco/editor/) para cambiar el diseño y los videos. No es staff: no ve el admin.

    manage.py crear_grupo_kiosco                    # crea/actualiza el grupo
    manage.py crear_grupo_kiosco --usuario mperez   # y agrega ese usuario al grupo
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError

GRUPO = "Contenido del kiosco"
PERMISOS = [
    f"{accion}_{modelo}"
    for modelo in ("disenokiosco", "franjadiseno", "videokiosco")
    for accion in ("view", "add", "change", "delete")
] + ["view_pantallakiosco", "change_pantallakiosco"]


class Command(BaseCommand):
    help = "Crea el grupo «Contenido del kiosco» (editor de la pantalla) y opcionalmente agrega un usuario."

    def add_arguments(self, parser):
        parser.add_argument("--usuario", help="Nombre de usuario a agregar al grupo.")

    def handle(self, *args, **opciones):
        grupo, creado = Group.objects.get_or_create(name=GRUPO)
        permisos = list(Permission.objects.filter(content_type__app_label="kiosco", codename__in=PERMISOS))
        faltan = set(PERMISOS) - {p.codename for p in permisos}
        if faltan:
            raise CommandError(f"Faltan permisos (¿falta migrar?): {', '.join(sorted(faltan))}")
        grupo.permissions.set(permisos)
        self.stdout.write(f"Grupo «{GRUPO}» {'creado' if creado else 'actualizado'} con {len(permisos)} permisos.")
        if opciones.get("usuario"):
            usuario = get_user_model().objects.filter(username=opciones["usuario"]).first()
            if usuario is None:
                raise CommandError(f"No existe el usuario «{opciones['usuario']}».")
            usuario.groups.add(grupo)
            self.stdout.write(f"«{usuario.username}» agregado: entra en /kiosco/editor/.")
