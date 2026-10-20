from rest_framework import viewsets, status, generics, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from api.models import (
    Usuario, Cancha, Invitacion, Partido, ParticipacionPartido,
    Resultado, EstadisticasPartido, Notificacion
)
from api.serializers import (
    UsuarioSerializer, UsuarioRegistroSerializer, UsuarioPerfilSerializer,
    CanchaSerializer, PartidoListSerializer,
    PartidoDetailSerializer, PartidoCrearSerializer, ParticipacionPartidoSerializer, ResultadoSerializer,
    NotificacionSerializer, TokenObtainPairSerializerCustom
)
from django.db.models import Q
from api.services.notifications import notificar_participantes
# ============================================================================
# PERMISOS PERSONALIZADOS
# ============================================================================
class EsOrganizador(permissions.BasePermission):
    """Permiso para verificar si el usuario es organizador del partido"""
    def has_object_permission(self, request, view, obj):
        return obj.organizador == request.user


class EsUsuarioAutenticado(permissions.BasePermission):
    """Permiso para usuarios autenticados"""
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated


# ============================================================================
# AUTENTICACIÓN
# ============================================================================
class TokenObtainPairViewCustom(generics.GenericAPIView):
    """Vista personalizada de token por email"""
    serializer_class = TokenObtainPairSerializerCustom
    permission_classes = [permissions.AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class RegistroView(generics.CreateAPIView):
    """Endpoint para registrar nuevo usuario"""
    queryset = Usuario.objects.all()
    serializer_class = UsuarioRegistroSerializer
    permission_classes = [permissions.AllowAny]


# ============================================================================
# USUARIO
# ============================================================================
class UsuarioViewSet(viewsets.ModelViewSet):
    """ViewSet para gestionar usuarios"""
    queryset = Usuario.objects.all()
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return UsuarioPerfilSerializer
        return UsuarioSerializer

    @action(detail=False, methods=['get'], permission_classes=[EsUsuarioAutenticado])
    def me(self, request):
        """Obtener perfil del usuario autenticado"""
        serializer = UsuarioPerfilSerializer(request.user)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], permission_classes=[EsUsuarioAutenticado])
    def vincular_fantasma(self, request, pk=None):
        """
        Vincular un usuario fantasma a usuario registrado.
        Busca por email si hay un usuario sin registrar, lo vincula.
        """
        usuario_registrado = self.get_object()
        
        if not usuario_registrado.is_registered:
            return Response(
                {"error": "El usuario debe estar registrado"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Buscar usuario fantasma con mismo email
        usuario_fantasma = Usuario.objects.filter(
            email=usuario_registrado.email,
            is_registered=False
        ).first()

        if not usuario_fantasma:
            return Response(
                {"error": "No hay usuario fantasma con este email"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Reasignar todas las participaciones al usuario registrado
        ParticipacionPartido.objects.filter(usuario=usuario_fantasma).update(
            usuario=usuario_registrado
        )

        usuario_fantasma.delete()

        return Response(
            {"mensaje": "Participaciones vinculadas correctamente"},
            status=status.HTTP_200_OK
        )


# ============================================================================
# CANCHA
# ============================================================================
class CanchaViewSet(viewsets.ModelViewSet):
    """ViewSet para gestionar canchas"""
    queryset = Cancha.objects.all()
    serializer_class = CanchaSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]


# ============================================================================
# PARTIDO
# ============================================================================
class PartidoViewSet(viewsets.ModelViewSet):
    """ViewSet principal para partidos"""
    permission_classes = [permissions.AllowAny]
    filterset_fields = ['organizador', 'cancha', 'periodicidad']
    ordering_fields = ['fecha_hora', 'created_at']
    ordering = ['-fecha_hora']

    def get_queryset(self):
        user = self.request.user
    
        # Si no está autenticado, sin partidos
        if not user.is_authenticated:
            return Partido.objects.none()
        
        # Solo partidos donde el usuario es organizador O participa
        return Partido.objects.filter(
            Q(organizador=user) | 
            Q(participaciones__usuario=user)
        ).distinct().order_by('-fecha_hora')

    def get_serializer_class(self):
        if self.action == 'create':
            return PartidoCrearSerializer
        elif self.action == 'retrieve':
            return PartidoDetailSerializer
        return PartidoListSerializer

    def create(self, request, *args, **kwargs):
        """Crear nuevo partido (solo usuarios registrados)"""
        if not request.user.is_registered:
            return Response(
                {"error": "Solo usuarios registrados pueden crear partidos"},
                status=status.HTTP_403_FORBIDDEN
            )
        return super().create(request, *args, **kwargs)

    @action(detail=True, methods=['post'], permission_classes=[EsUsuarioAutenticado])
    def cambiar_equipo(self, request, pk=None):
        """
        Cambiar asignación de equipo (solo el propio usuario o organizador).
        Body: {"nuevo_equipo": "B", "usuario_id": 1}
        """
        partido = self.get_object()
        usuario_id = request.data.get('usuario_id')
        nuevo_equipo = request.data.get('nuevo_equipo')

        if nuevo_equipo not in ['A', 'B']:
            return Response(
                {"error": "Equipo debe ser 'A' o 'B'"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Obtener participación
        participacion = get_object_or_404(
            ParticipacionPartido,
            partido=partido,
            usuario_id=usuario_id
        )

        # Verificar permisos
        if participacion.usuario != request.user and partido.organizador != request.user:
            return Response(
                {"error": "No tienes permiso para cambiar este equipo"},
                status=status.HTTP_403_FORBIDDEN
            )

        # Validar cupo en nuevo equipo
        if participacion.equipo_asignado != nuevo_equipo:
            cupo_disponible = partido.cupo_disponible(nuevo_equipo)
            if cupo_disponible <= 0:
                return Response(
                    {"error": "No hay cupo disponible en ese equipo"},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # Cambiar equipo
        participacion.equipo_asignado = nuevo_equipo
        participacion.save()

        # Crear notificación si lo cambió el organizador
        if partido.organizador == request.user:
            Notificacion.objects.create(
                usuario=participacion.usuario,
                partido=partido,
                tipo=Notificacion.Tipo.CAMBIO_EQUIPO,
                mensaje=f"Tu equipo ha sido cambiado a Equipo {nuevo_equipo}"
            )

        serializer = ParticipacionPartidoSerializer(participacion)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], permission_classes=[EsOrganizador])
    def banear(self, request, pk=None):
        """
        Banear a un participante del partido.
        Body: {"usuario_id": 1}
        """
        partido = self.get_object()
        self.check_object_permissions(request, partido)

        usuario_id = request.data.get('usuario_id')
        participacion = get_object_or_404(
            ParticipacionPartido,
            partido=partido,
            usuario_id=usuario_id
        )

        participacion.estado = 'baneado'
        participacion.save()

        # Crear notificación
        Notificacion.objects.create(
            usuario=participacion.usuario,
            partido=partido,
            tipo=Notificacion.Tipo.BANEADO,
            mensaje=f"Has sido baneado del partido del {partido.fecha_hora.strftime('%d/%m/%Y')}"
        )

        serializer = ParticipacionPartidoSerializer(participacion)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], permission_classes=[permissions.AllowAny])
    def definir_equipos(self, request, pk=None):
        """
        Definir equipos del partido (solo organizador).
        Body: {"equipos": {"A": [1, 2, 3], "B": [4, 5, 6]}}
        """
        partido = self.get_object()

        if partido.organizador != request.user:
            return Response(
                {"error": "Solo el organizador puede definir equipos"},
                status=status.HTTP_403_FORBIDDEN
            )

        equipos_data = request.data.get('equipos', {})
        
        if not equipos_data.get('A') or not equipos_data.get('B'):
            return Response(
                {"error": "Debes especificar usuarios para ambos equipos"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Actualizar asignaciones
        for equipo, usuario_ids in equipos_data.items():
            for usuario_id in usuario_ids:
                participacion = get_object_or_404(
                    ParticipacionPartido,
                    partido=partido,
                    usuario_id=usuario_id
                )
                participacion.equipo_asignado = equipo
                participacion.save()

        partido.equipos_definidos = True
        partido.save()

        serializer = PartidoDetailSerializer(partido)
        return Response(serializer.data)

    def perform_update(self, serializer):
        partido_anterior = serializer.instance
        horario_anterior = partido_anterior.fecha_hora
        cancha_anterior = partido_anterior.cancha
        cupo_anterior = partido_anterior.cupo_total

        partido = serializer.save()

        cambios = []

        if horario_anterior != partido.fecha_hora:
            cambios.append(
                f"horario de {horario_anterior:%d/%m %H:%M} "
                f"a {partido.fecha_hora:%d/%m %H:%M}"
            )

        if cancha_anterior != partido.cancha:
            cambios.append(
                f"cancha de {cancha_anterior.nombre} a {partido.cancha.nombre}"
            )

        if cupo_anterior != partido.cupo_total:
            cambios.append(
                f"cupo de {cupo_anterior} a {partido.cupo_total} jugadores"
            )

        if cambios:
            notificar_participantes(
                partido,
                tipo=Notificacion.Tipo.PARTIDO_MODIFICADO,
                mensaje=f"Se modificó el partido: {'; '.join(cambios)}",
                excluir_usuario=self.request.user,
            )

# ============================================================================
# RESULTADO Y ESTADÍSTICAS
# ============================================================================
class ResultadoViewSet(viewsets.ModelViewSet):
    """ViewSet para resultados de partidos"""
    queryset = Resultado.objects.all()
    serializer_class = ResultadoSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        """Filtrar resultados del usuario actual (como organizador O participante)"""
        user = self.request.user
        if not user.is_authenticated:
            return Resultado.objects.none()
        
        from django.db.models import Q
        return Resultado.objects.filter(
            Q(partido__organizador=user) | 
            Q(partido__participaciones__usuario=user)
        ).distinct().order_by('-fecha_carga')

    @action(detail=False, methods=['post'])
    def crear_resultado(self, request):
        """
        Crear resultado de un partido.
        Body: {
            "partido_id": 1,
            "equipo_a_goles": 3,
            "equipo_b_goles": 2,
            "estadisticas": [
                {"usuario_id": 1, "goles": 2, "amarillas": 0, "rojas": 0},
                {"usuario_id": 2, "goles": 1, "amarillas": 1, "rojas": 0}
            ]
        }
        """
        partido_id = request.data.get('partido_id')
        equipo_a_goles = request.data.get('equipo_a_goles')
        equipo_b_goles = request.data.get('equipo_b_goles')
        estadisticas_data = request.data.get('estadisticas', [])

        # Obtener partido
        partido = get_object_or_404(Partido, id=partido_id)

        # Verificar permisos
        if partido.organizador != request.user:
            return Response(
                {"error": "Solo el organizador puede cargar resultado"},
                status=status.HTTP_403_FORBIDDEN
            )

        # Verificar equipos definidos
        if not partido.equipos_definidos:
            return Response(
                {"error": "Los equipos deben estar definidos antes de cargar resultado"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Verificar que no existe resultado
        if Resultado.objects.filter(partido=partido).exists():
            return Response(
                {"error": "Este partido ya tiene resultado cargado"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Crear resultado
        resultado = Resultado.objects.create(
            partido=partido,
            equipo_a_goles=equipo_a_goles,
            equipo_b_goles=equipo_b_goles,
            cargado_por=request.user
        )

        # Crear estadísticas por jugador
        for stat in estadisticas_data:
            usuario_id = stat.get('usuario_id')
            usuario = get_object_or_404(Usuario, id=usuario_id)

            EstadisticasPartido.objects.create(
                resultado=resultado,
                usuario=usuario,
                goles=stat.get('goles'),
                tarjetas_amarillas=stat.get('amarillas'),
                tarjetas_rojas=stat.get('rojas')
            )

        # Crear notificaciones
        participantes = ParticipacionPartido.objects.filter(
            partido=partido,
            usuario__is_registered=True
        ).values_list('usuario_id', flat=True)

        for usuario_id in participantes:
            Notificacion.objects.create(
                usuario_id=usuario_id,
                partido=partido,
                tipo=Notificacion.Tipo.RESULTADO_CARGADO,
                mensaje=f"El resultado del partido del dia {partido.fecha} ha sido cargado: {equipo_a_goles} - {equipo_b_goles}"
            )

        serializer = ResultadoSerializer(resultado)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


# ============================================================================
# NOTIFICACIÓN
# ============================================================================
class NotificacionViewSet(viewsets.ReadOnlyModelViewSet):

    """ViewSet para notificaciones del usuario autenticado"""
    serializer_class = NotificacionSerializer
    permission_classes = [EsUsuarioAutenticado]

    def get_queryset(self):
        return Notificacion.objects.filter(usuario=self.request.user)

    @action(detail=True, methods=['patch'])
    def marcar_leida(self, request, pk=None):
        """Marcar notificación como leída"""
        notificacion = self.get_object()
        notificacion.leida = True
        notificacion.save()
        serializer = self.get_serializer(notificacion)
        return Response(serializer.data)

    @action(detail=False, methods=['patch'])
    def marcar_todas_leidas(self, request):
        """Marcar todas las notificaciones como leídas"""
        Notificacion.objects.filter(
            usuario=request.user,
            leida=False
        ).update(leida=True)
        return Response({"mensaje": "Todas las notificaciones marcadas como leídas"})


# ============================================================================
# INVITACIÓN
# ============================================================================
class InvitacionPorCodigoView(APIView):
    """Obtener partido por código de invitación (solo acceso directo)"""
    permission_classes = [permissions.AllowAny]
    
    def get(self, request, codigo):
        invitacion = get_object_or_404(Invitacion, codigo_unico=codigo)
        
        if not invitacion.esta_activa():
            return Response(
                {'detail': 'Esta invitación ha expirado o fue cancelada'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        partido = invitacion.partido
        serializer = PartidoDetailSerializer(partido)
        return Response({'partido': serializer.data})


class AceptarInvitacionView(APIView):
    """Aceptar invitación sin estar autenticado"""
    permission_classes = [permissions.AllowAny]
    
    def post(self, request, partido_id):
        partido = get_object_or_404(Partido, id=partido_id)
        
        codigo = request.data.get('codigo_unico')
        nombre = request.data.get('nombre')
        email = request.data.get('email')
        equipo = request.data.get('equipo', 'A')

        # Validar código de invitación
        try:
            invitacion = Invitacion.objects.get(partido=partido, codigo_unico=codigo)
        except Invitacion.DoesNotExist:
            return Response(
                {'error': 'Código de invitación inválido'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not invitacion.esta_activa():
            return Response(
                {'error': 'La invitación ha expirado o fue cancelada'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Obtener o crear usuario
        if request.user.is_authenticated:
            usuario = request.user
        else:
            if not nombre or not email:
                return Response(
                    {'error': 'Nombre y email requeridos'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            usuario, created = Usuario.objects.get_or_create(
                email=email,
                defaults={'nombre': nombre, 'is_registered': False}
            )

        # Verificar si ya participa
        if ParticipacionPartido.objects.filter(partido=partido, usuario=usuario).exists():
            return Response(
                {'error': 'Ya estás registrado en este partido'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validar equipo
        if equipo not in ['A', 'B']:
            return Response(
                {'error': "Equipo debe ser 'A' o 'B'"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validar cupo
        cupo_disponible_equipo = partido.cupo_disponible(equipo)
        if cupo_disponible_equipo > 0:
            estado = 'activo'
        elif partido.lista_espera_disponible():
            estado = 'en_espera'
        else:
            return Response(
                {'error': 'Sin cupo disponible ni en lista de espera'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Crear participación
        participacion = ParticipacionPartido.objects.create(
            partido=partido,
            usuario=usuario,
            estado=estado,
            equipo_asignado=equipo
        )

        notificar_participantes(
            partido,
            tipo=Notificacion.Tipo.PARTICIPANTE_NUEVO,
            mensaje=f"Se unió un nuevo participante al partido: {usuario.nombre} (Equipo {equipo})",
            excluir_usuario=self.request.user,
        )

        serializer = ParticipacionPartidoSerializer(participacion)
        return Response(serializer.data, status=status.HTTP_201_CREATED)