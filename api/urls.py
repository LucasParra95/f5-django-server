from django.urls import path, include
from rest_framework.routers import DefaultRouter
from api.views import (
    TokenObtainPairViewCustom, RegistroView, UsuarioViewSet, CanchaViewSet,
    PartidoViewSet, ResultadoViewSet, NotificacionViewSet, InvitacionPorCodigoView, AceptarInvitacionView
)

router = DefaultRouter()
router.register(r'usuarios', UsuarioViewSet, basename='usuario')
router.register(r'canchas', CanchaViewSet, basename='cancha')
router.register(r'partidos', PartidoViewSet, basename='partido')
router.register(r'resultados', ResultadoViewSet, basename='resultado')
router.register(r'notificaciones', NotificacionViewSet, basename='notificacion')

urlpatterns = [
    path('', include(router.urls)),
    path('token/', TokenObtainPairViewCustom.as_view(), name='token_obtain_pair'),
    path('register/', RegistroView.as_view(), name='registro'),
    path('invitaciones/<str:codigo>/', InvitacionPorCodigoView.as_view(), name='invitacion-por-codigo'),
    path('partidos/<int:partido_id>/aceptar_invitacion/', AceptarInvitacionView.as_view(), name='aceptar-invitacion'),
]