"""Editor de la pantalla del kiosco para el personal que no usa el admin (/kiosco/editor/).

Login propio (sesión de Django), y en la página: el tótem dibujado, donde se arrastran las líneas
entre cuadros para cambiar el alto o el ancho, se cambia qué muestra cada cuadro y se eligen los
tótems que lo usan; al lado, la pantalla de verdad (vista previa) y una pestaña de videos.

Quién entra: superusuario o el grupo «Contenido del kiosco» (manage.py crear_grupo_kiosco), solo
desde la red interna (KIOSCO_REDES)."""
from __future__ import annotations

import logging
import os

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.db.models import Max
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from . import disenos
from .models import DisenoKiosco, Modulo, PantallaKiosco, VideoKiosco
from .permissions import PERMISO_VIDEOS, PuedeEditarPantalla, ip_cliente, ip_permitida, puede_editar

logger = logging.getLogger(__name__)

EXTENSIONES_VIDEO = (".mp4", ".webm", ".m4v")
MAX_VIDEO_MB = 300


# ---------------------------------------------------------------- páginas
class Entrar(auth_views.LoginView):
    template_name = "kiosco/editor_entrar.html"
    redirect_authenticated_user = True

    def dispatch(self, request, *args, **kwargs):
        if not ip_permitida(ip_cliente(request)):
            return HttpResponseForbidden("El editor solo se usa desde la red interna.")
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return reverse("kiosco-editor")


class Salir(auth_views.LogoutView):
    next_page = reverse_lazy("kiosco-editor-entrar")


@ensure_csrf_cookie
def pagina(request):
    if not ip_permitida(ip_cliente(request)):
        return HttpResponseForbidden("El editor solo se usa desde la red interna.")
    if not request.user.is_authenticated:
        return redirect("kiosco-editor-entrar")
    if not puede_editar(request.user):
        return render(request, "kiosco/editor_entrar.html", {"sin_permiso": True}, status=403)
    return render(request, "kiosco/editor.html", {
        "usuario": request.user.get_full_name() or request.user.get_username(),
        "puede_videos": puede_editar(request.user, PERMISO_VIDEOS),
        "max_video_mb": MAX_VIDEO_MB,
        "cookie_csrf": settings.CSRF_COOKIE_NAME,
    })


def ventana_previa(request, pk):
    """La ventana "Abrir en grande": el kiosco real con ese diseño, en un marco del tamaño del tótem."""
    if not ip_permitida(ip_cliente(request)):
        return HttpResponseForbidden("El editor solo se usa desde la red interna.")
    if not puede_editar(request.user):
        return redirect("kiosco-editor-entrar")
    diseno = DisenoKiosco.objects.filter(pk=pk).first()
    if diseno is None:
        return redirect("kiosco-editor")
    return render(request, "admin/kiosco/previa_diseno.html", {
        "diseno": diseno, "ancho": 1080, "alto": 1920,
        "url_pantalla": reverse("kiosco-pantalla") + f"?previa={diseno.pk}&impresion=ninguna",
    })


# ---------------------------------------------------------------- API
class _EditorView(APIView):
    authentication_classes = [SessionAuthentication]  # sesión + CSRF
    permission_classes = [PuedeEditarPantalla]


def _diseno(d: DisenoKiosco) -> dict:
    return {
        "id": d.id,
        "nombre": d.nombre,
        "franjas": [{"modulo": f.modulo, "alto": f.alto, "modulo_2": f.modulo_2, "ancho": f.ancho} for f in d.franjas.all()],
        "pantallas": list(d.pantallas.values_list("nombre", flat=True)),
        "modificado_en": d.modificado_en.isoformat(),
    }


def _video(v: VideoKiosco) -> dict:
    try:
        peso = v.archivo.size
    except (OSError, ValueError):
        peso = None
    return {"id": v.id, "titulo": v.titulo, "orden": v.orden, "activo": v.activo, "con_sonido": v.con_sonido,
            "peso": peso, "url": reverse("kiosco-video", args=[v.id])}


class DatosView(_EditorView):
    """GET /api/kiosco/editor/ -- diseños, tótems y cuadros posibles."""

    def get(self, request):
        return Response({
            "disenos": [_diseno(d) for d in DisenoKiosco.objects.prefetch_related("franjas", "pantallas").order_by("nombre")],
            "pantallas": [
                {"nombre": p.nombre, "diseno": p.diseno_id,
                 "ultimo_contacto": p.ultimo_contacto.isoformat() if p.ultimo_contacto else None}
                for p in PantallaKiosco.objects.order_by("nombre")
            ],
            "modulos": [{"valor": m.value, "nombre": m.label.split(" (")[0]} for m in Modulo],
        })


class DisenosView(_EditorView):
    """POST /api/kiosco/editor/disenos/ {nombre, copiar_de?} -- diseño nuevo (solo el cajero, o copia)."""

    def post(self, request):
        nombre = " ".join(str((request.data or {}).get("nombre") or "").split())[:80]
        if not nombre:
            return Response({"error": "Ponle un nombre al diseño."}, status=status.HTTP_400_BAD_REQUEST)
        if DisenoKiosco.objects.filter(nombre__iexact=nombre).exists():
            return Response({"error": "Ya hay un diseño con ese nombre."}, status=status.HTTP_400_BAD_REQUEST)
        origen = DisenoKiosco.objects.filter(pk=(request.data or {}).get("copiar_de") or 0).first()
        franjas = _diseno(origen)["franjas"] if origen else [{"modulo": "cajero", "alto": 100, "modulo_2": "", "ancho": 100}]
        diseno = disenos.guardar(DisenoKiosco(nombre=nombre), franjas)
        logger.info("kiosco.diseno_creado id=%s nombre=%s por=%s", diseno.id, nombre, request.user)
        return Response(_diseno(diseno), status=status.HTTP_201_CREATED)


class DisenoView(_EditorView):
    """PUT /api/kiosco/editor/disenos/<id>/ {nombre, franjas} -- guardar; DELETE si ningún tótem lo usa."""

    def put(self, request, pk):
        diseno = DisenoKiosco.objects.filter(pk=pk).first()
        if diseno is None:
            return Response({"error": "Ese diseño ya no existe."}, status=status.HTTP_404_NOT_FOUND)
        datos = request.data if isinstance(request.data, dict) else {}
        nombre = " ".join(str(datos.get("nombre") or diseno.nombre).split())[:80]
        if DisenoKiosco.objects.filter(nombre__iexact=nombre).exclude(pk=pk).exists():
            return Response({"error": "Ya hay otro diseño con ese nombre."}, status=status.HTTP_400_BAD_REQUEST)
        franjas = disenos.limpiar(datos.get("franjas"))
        problemas = disenos.errores(franjas)
        if problemas:
            return Response({"error": " ".join(problemas)}, status=status.HTTP_400_BAD_REQUEST)
        diseno.nombre = nombre
        disenos.guardar(diseno, disenos.proporciones(franjas))
        logger.info("kiosco.diseno_guardado id=%s por=%s", diseno.id, request.user)
        return Response(_diseno(DisenoKiosco.objects.prefetch_related("franjas", "pantallas").get(pk=pk)))

    def delete(self, request, pk):
        diseno = DisenoKiosco.objects.filter(pk=pk).first()
        if diseno is None:
            return Response(status=status.HTTP_204_NO_CONTENT)
        if diseno.pantallas.exists():
            return Response({"error": "Lo usan tótems: primero elige otro diseño para ellos."}, status=status.HTTP_400_BAD_REQUEST)
        diseno.delete()
        logger.info("kiosco.diseno_borrado id=%s por=%s", pk, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PantallaView(_EditorView):
    """POST /api/kiosco/editor/pantallas/<nombre>/ {diseno: id | null} -- qué diseño usa ese tótem."""

    def post(self, request, nombre):
        pantalla = PantallaKiosco.objects.filter(nombre=nombre).first()
        if pantalla is None:
            return Response({"error": "Ese tótem no existe."}, status=status.HTTP_404_NOT_FOUND)
        id_diseno = (request.data or {}).get("diseno")
        diseno = DisenoKiosco.objects.filter(pk=id_diseno).first() if id_diseno else None
        if id_diseno and diseno is None:
            return Response({"error": "Ese diseño ya no existe."}, status=status.HTTP_404_NOT_FOUND)
        pantalla.diseno = diseno
        pantalla.save(update_fields=["diseno"])
        logger.info("kiosco.pantalla_diseno pantalla=%s diseno=%s por=%s", nombre, id_diseno, request.user)
        return Response({"nombre": pantalla.nombre, "diseno": pantalla.diseno_id})


class VideosView(_EditorView):
    """GET lista / POST subir (multipart: titulo, archivo) -- /api/kiosco/editor/videos/."""

    permiso = PERMISO_VIDEOS
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        return Response({"videos": [_video(v) for v in VideoKiosco.objects.all()]})

    def post(self, request):
        archivo = request.FILES.get("archivo")
        titulo = " ".join(str(request.data.get("titulo") or "").split())[:120]
        if not archivo:
            return Response({"error": "Elige el archivo del video."}, status=status.HTTP_400_BAD_REQUEST)
        if os.path.splitext(archivo.name)[1].lower() not in EXTENSIONES_VIDEO:
            return Response({"error": "El video tiene que ser MP4 o WebM."}, status=status.HTTP_400_BAD_REQUEST)
        if archivo.size > MAX_VIDEO_MB * 1048576:
            return Response({"error": f"El video pasa de {MAX_VIDEO_MB} MB."}, status=status.HTTP_400_BAD_REQUEST)
        orden = (VideoKiosco.objects.aggregate(m=Max("orden"))["m"] or 0) + 1
        video = VideoKiosco.objects.create(titulo=titulo or os.path.splitext(archivo.name)[0][:120],
                                           archivo=archivo, orden=orden)
        logger.info("kiosco.video_subido id=%s peso=%s por=%s", video.id, archivo.size, request.user)
        return Response(_video(video), status=status.HTTP_201_CREATED)


class VideoView(_EditorView):
    """PATCH {titulo, activo, con_sonido, orden} / DELETE -- /api/kiosco/editor/videos/<id>/."""

    permiso = PERMISO_VIDEOS

    def patch(self, request, pk):
        video = VideoKiosco.objects.filter(pk=pk).first()
        if video is None:
            return Response({"error": "Ese video ya no existe."}, status=status.HTTP_404_NOT_FOUND)
        datos = request.data if isinstance(request.data, dict) else {}
        if "titulo" in datos:
            video.titulo = " ".join(str(datos["titulo"] or "").split())[:120] or video.titulo
        for campo in ("activo", "con_sonido"):
            if campo in datos:
                setattr(video, campo, bool(datos[campo]))
        if "orden" in datos:
            try:
                video.orden = max(0, int(datos["orden"]))
            except (TypeError, ValueError):
                pass
        video.save()
        return Response(_video(video))

    def delete(self, request, pk):
        video = VideoKiosco.objects.filter(pk=pk).first()
        if video is not None:
            archivo = video.archivo
            video.delete()
            if archivo:
                archivo.delete(save=False)  # que no quede ocupando disco en el servidor
            logger.info("kiosco.video_borrado id=%s por=%s", pk, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)
