"""Rutas del panel (login JWT, supervisor/admin) para ver las liquidaciones web. Las de
cessa-laravel (X-Api-Key) están en urls.py, bajo /api/externo/."""

from rest_framework.routers import SimpleRouter

from .panel import LiquidacionViewSet

router = SimpleRouter()
router.register("liquidaciones", LiquidacionViewSet, basename="liquidacion")

urlpatterns = router.urls
