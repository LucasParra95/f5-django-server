from rest_framework import viewsets, status, generics, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import ScopedRateThrottle
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from api.models import (
    Usuario, Cancha, Invitacion, Partido, ParticipacionPartido,
    Resultado, EstadisticasPartido, Notificacion
)
from api.serializers import (
    UsuarioRegistroSerializer, UsuarioPerfilSerializer, UsuarioActualizarSerializer,
    CanchaSerializer, PartidoListSerializer,
    PartidoDetailSerializer, PartidoPublicoSerializer, PartidoEditarSerializer, PartidoCrearSerializer, ParticipacionPartidoSerializer, ResultadoSerializer,
    NotificacionSerializer, TokenObtainPairSerializerCustom
)
from api.services.lista_espera import promover_lista_espera
from api.services.notifications import fecha_local, notificar_participantes


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
# HELPERS
# ============================================================================
def _error(mensaje, codigo=status.HTTP_400_BAD_REQUEST):
    return Response({"error": mensaje}, status=codigo)


def _entero(valor):
    """Convierte a int o devuelve None (evita 500 por ids no numéricos)."""
    if isinstance(valor, bool):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _motivo_bloqueo(partido):
    """Mensaje si el partido ya no admite cambios (cancelado o con resultado), o None."""
    if partido.estado == Partido.Estado.CANCELADO:
        return "El partido fue cancelado"
    if hasattr(partido, 'resultado'):
        return "El partido ya tiene resultado cargado"
    return None


# ============================================================================
# AUTENTICACIÓN
# ============================================================================
class TokenObtainPairViewCustom(generics.GenericAPIView):
    """Vista personalizada de token por email"""
    serializer_class = TokenObtainPairSerializerCustom
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class RegistroView(generics.CreateAPIView):
    """Endpoint para registrar nuevo usuario"""
    queryset = Usuario.objects.all()
    serializer_class = UsuarioRegistroSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'


# ============================================================================
# USUARIO
# ============================================================================
class UsuarioViewSet(viewsets.GenericViewSet):
    """ViewSet de solo lectura: perfil propio (me)"""
    queryset = Usuario.objects.all()
    serializer_class = UsuarioPerfilSerializer
    permission_classes = [EsUsuarioAutenticado]

    @action(detail=False, methods=['get', 'put', 'patch'], permission_classes=[EsUsuarioAutenticado])
    def me(self, request):
        if request.method == 'GET':
            return Response(UsuarioPerfilSerializer(request.user).data)
        serializer = UsuarioActualizarSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UsuarioPerfilSerializer(request.user).data)


# ============================================================================
# CANCHA
# ============================================================================
class CanchaViewSet(viewsets.ModelViewSet):
    """ViewSet para gestionar canchas"""
    queryset = Cancha.objects.filter(activa=True)
    serializer_class = CanchaSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    http_method_names = ['get', 'post', 'head', 'options']

    def perform_create(self, serializer):
        serializer.save(creado_por=self.request.user)


# ============================================================================
# PARTIDO
# ============================================================================
class PartidoViewSet(viewsets.ModelViewSet):
    """ViewSet principal para partidos"""
    permission_classes = [permissions.AllowAny]
    # Sin DELETE: la baja es lógica (acción `cancelar`)
    http_method_names = ['get', 'post', 'put', 'patch', 'head', 'options']

    def get_queryset(self):
        user = self.request.user

        # Si no está autenticado, sin partidos
        if not user.is_authenticated:
            return Partido.objects.none()

        # Solo partidos donde el usuario es organizador O participa
        return Partido.objects.filter(
            Q(organizador=user) |
            Q(participaciones__usuario=user,
              participaciones__estado__in=['activo', 'en_espera'])
        ).distinct().order_by('-fecha_hora')

    def get_serializer_class(self):
        if self.action == 'create':
            return PartidoCrearSerializer
        elif self.action in ('update', 'partial_update'):
            return PartidoEditarSerializer
        return PartidoListSerializer

    def get_permissions(self):
        """
        - create: solo usuarios registrados
        - update/partial_update: solo el organizador (EsOrganizador se evalúa en get_object)
        """
        if self.action == 'create':
            return [EsUsuarioAutenticado()]
        if self.action in ('update', 'partial_update'):
            return [EsUsuarioAutenticado(), EsOrganizador()]
        return super().get_permissions()

    def retrieve(self, request, *args, **kwargs):
        partido = self.get_object()
        serializer_class = (
            PartidoDetailSerializer
            if partido.organizador_id == request.user.id
            else PartidoPublicoSerializer
        )
        serializer = serializer_class(partido, context=self.get_serializer_context())
        return Response(serializer.data)

    def update(self, request, *args, **kwargs):
        """PUT/PATCH: edita y responde con el detalle completo del partido"""
        super().update(request, *args, **kwargs)
        partido = self.get_object()
        return Response(PartidoDetailSerializer(partido, context=self.get_serializer_context()).data)

    def perform_update(self, serializer):
        """Guarda los cambios, avisa a los participantes y reacomoda la lista de espera"""
        partido = serializer.instance
        cancha_id, fecha_hora, cupo_total = partido.cancha_id, partido.fecha_hora, partido.cupo_total

        with transaction.atomic():
            Partido.objects.select_for_update().get(pk=partido.pk)
            partido = serializer.save()

            cambios = []
            if partido.fecha_hora != fecha_hora:
                cambios.append(f"nueva fecha: {fecha_local(partido.fecha_hora)}")
            if partido.cancha_id != cancha_id:
                cambios.append(f"nueva cancha: {partido.cancha.nombre}")
            if partido.cupo_total != cupo_total:
                cambios.append(f"nuevo cupo: {partido.cupo_total} jugadores")

            if cambios:
                notificar_participantes(
                    partido,
                    tipo=Notificacion.Tipo.PARTIDO_MODIFICADO,
                    mensaje=f"El partido fue modificado ({', '.join(cambios)})",
                    excluir_usuario=self.request.user,
                )
            if partido.cupo_total != cupo_total:
                promover_lista_espera(partido)

    @action(detail=True, methods=['post'], permission_classes=[EsUsuarioAutenticado, EsOrganizador])
    def cancelar(self, request, pk=None):
        """
        Baja lógica del partido (solo organizador): lo marca como cancelado, cierra la
        invitación y avisa a los participantes. No se puede cancelar si ya tiene resultado.
        """
        partido = self.get_object()

        with transaction.atomic():
            partido = Partido.objects.select_for_update().get(pk=partido.pk)

            if partido.estado == Partido.Estado.CANCELADO:
                return _error("El partido ya está cancelado")
            if hasattr(partido, 'resultado'):
                return _error("No se puede cancelar un partido con resultado cargado")

            partido.estado = Partido.Estado.CANCELADO
            partido.save(update_fields=['estado', 'updated_at'])
            Invitacion.objects.filter(partido=partido).update(estado='cancelada')

            notificar_participantes(
                partido,
                tipo=Notificacion.Tipo.PARTIDO_CANCELADO,
                mensaje=f"El partido del {fecha_local(partido.fecha_hora)} fue cancelado por el organizador",
                excluir_usuario=request.user,
            )

        return Response(PartidoDetailSerializer(partido, context=self.get_serializer_context()).data)

    @action(detail=True, methods=['post'], permission_classes=[EsUsuarioAutenticado])
    def cambiar_equipo(self, request, pk=None):
        """
        Cambiar asignación de equipo (el propio usuario o el organizador).
        Body: {"nuevo_equipo": "B", "usuario_id": 1}

        - Con los equipos ya definidos, solo el organizador puede cambiarlos.
        - Un jugador activo necesita lugar en el equipo destino; uno en espera puede
          cambiarse igual y sigue en espera (o pasa a activo si hay lugar).
        """
        partido = self.get_object()
        usuario_id = _entero(request.data.get('usuario_id'))
        nuevo_equipo = request.data.get('nuevo_equipo')

        if nuevo_equipo not in ('A', 'B'):
            return _error("Equipo debe ser 'A' o 'B'")
        if usuario_id is None:
            return _error("usuario_id inválido")

        es_organizador = partido.organizador_id == request.user.id
        if usuario_id != request.user.id and not es_organizador:
            return _error("No tienes permiso para cambiar este equipo", status.HTTP_403_FORBIDDEN)

        with transaction.atomic():
            partido = Partido.objects.select_for_update().get(pk=partido.pk)

            motivo = _motivo_bloqueo(partido)
            if motivo:
                return _error(motivo)
            if partido.equipos_definidos and not es_organizador:
                return _error(
                    "Los equipos ya fueron definidos por el organizador",
                    status.HTTP_403_FORBIDDEN,
                )

            participacion = get_object_or_404(
                ParticipacionPartido.objects.select_for_update(),
                partido=partido,
                usuario_id=usuario_id,
            )
            if participacion.estado == 'baneado':
                return _error("El jugador está baneado de este partido")

            if participacion.equipo_asignado != nuevo_equipo:
                if participacion.estado == 'activo' and partido.cupo_disponible(nuevo_equipo) <= 0:
                    return _error("No hay cupo disponible en ese equipo")

                participacion.equipo_asignado = nuevo_equipo
                participacion.save(update_fields=['equipo_asignado'])

                # Avisar solo si lo movió otra persona
                if participacion.usuario_id != request.user.id and participacion.usuario.is_registered:
                    Notificacion.objects.create(
                        usuario=participacion.usuario,
                        partido=partido,
                        tipo=Notificacion.Tipo.CAMBIO_EQUIPO,
                        mensaje=f"Tu equipo ha sido cambiado a Equipo {nuevo_equipo}",
                    )

                # Se liberó un lugar en el equipo anterior (o el en espera ahora tiene lugar)
                promover_lista_espera(partido)

            participacion.refresh_from_db()

        return Response(ParticipacionPartidoSerializer(participacion).data)

    @action(detail=True, methods=['post'], permission_classes=[EsUsuarioAutenticado, EsOrganizador])
    def banear(self, request, pk=None):
        """
        Banear a un participante del partido (solo organizador). Libera su lugar y
        promueve a quien esté primero en la lista de espera de ese equipo.
        Body: {"usuario_id": 1}
        """
        partido = self.get_object()
        usuario_id = _entero(request.data.get('usuario_id'))

        if usuario_id is None:
            return _error("usuario_id inválido")
        if usuario_id == partido.organizador_id:
            return _error("El organizador no puede banearse a sí mismo")

        with transaction.atomic():
            partido = Partido.objects.select_for_update().get(pk=partido.pk)

            motivo = _motivo_bloqueo(partido)
            if motivo:
                return _error(motivo)

            participacion = get_object_or_404(
                ParticipacionPartido.objects.select_for_update(),
                partido=partido,
                usuario_id=usuario_id,
            )
            if participacion.estado == 'baneado':
                return _error("El jugador ya está baneado")

            participacion.estado = 'baneado'
            participacion.save(update_fields=['estado'])

            if participacion.usuario.is_registered:
                Notificacion.objects.create(
                    usuario=participacion.usuario,
                    partido=partido,
                    tipo=Notificacion.Tipo.BANEADO,
                    mensaje=f"Has sido baneado del partido del {fecha_local(partido.fecha_hora, '%d/%m/%Y')}",
                )

            promover_lista_espera(partido)

        return Response(ParticipacionPartidoSerializer(participacion).data)

    @action(detail=True, methods=['post'], permission_classes=[EsUsuarioAutenticado])
    def definir_equipos(self, request, pk=None):
        """
        Definir equipos del partido (solo organizador).
        Body: {"equipos": {"A": [1, 2, 3], "B": [4, 5, 6]}}
        """
        partido = self.get_object()

        if partido.organizador != request.user:
            return _error("Solo el organizador puede definir equipos", status.HTTP_403_FORBIDDEN)

        motivo = _motivo_bloqueo(partido)
        if motivo:
            return _error(motivo)

        equipos_data = request.data.get('equipos')
        if not isinstance(equipos_data, dict) or set(equipos_data.keys()) != {'A', 'B'}:
            return _error("Debes enviar exactamente los equipos 'A' y 'B'")

        if not equipos_data['A'] or not equipos_data['B']:
            return _error("Debes especificar usuarios para ambos equipos")

        try:
            asignaciones = {
                equipo: [int(uid) for uid in ids]
                for equipo, ids in equipos_data.items()
            }
        except (TypeError, ValueError):
            return _error("Los ids de jugadores son inválidos")

        todos = asignaciones['A'] + asignaciones['B']
        if len(todos) != len(set(todos)):
            return _error("Un jugador no puede estar en ambos equipos ni repetido")

        with transaction.atomic():
            activos = set(
                partido.participaciones.select_for_update()
                .filter(estado='activo')
                .values_list('usuario_id', flat=True)
            )
            if not set(todos).issubset(activos):
                return _error("Hay jugadores que no participan activamente del partido")

            for equipo, ids in asignaciones.items():
                partido.participaciones.filter(usuario_id__in=ids).update(equipo_asignado=equipo)

            partido.equipos_definidos = True
            partido.save(update_fields=['equipos_definidos', 'updated_at'])

        return Response(PartidoDetailSerializer(partido).data)


# ============================================================================
# RESULTADO Y ESTADÍSTICAS
# ============================================================================
class ResultadoViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet para resultados de partidos"""
    queryset = Resultado.objects.all()
    serializer_class = ResultadoSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        """Filtrar resultados del usuario actual (como organizador O participante)"""
        user = self.request.user
        if not user.is_authenticated:
            return Resultado.objects.none()

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
        equipo_a_goles = request.data.get('equipo_a_goles')
        equipo_b_goles = request.data.get('equipo_b_goles')
        estadisticas_data = request.data.get('estadisticas', [])

        def error(msg):
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

        def entero_no_negativo(valor):
            return isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0

        try:
            partido_id = int(request.data.get('partido_id'))
        except (TypeError, ValueError):
            return error("partido_id inválido")

        partido = get_object_or_404(Partido, id=partido_id)

        if partido.organizador != request.user:
            return Response(
                {"error": "Solo el organizador puede cargar resultado"},
                status=status.HTTP_403_FORBIDDEN
            )

        if partido.estado == Partido.Estado.CANCELADO:
            return error("El partido fue cancelado")

        if not partido.equipos_definidos:
            return error("Los equipos deben estar definidos antes de cargar resultado")

        if partido.fecha_hora > timezone.now():
            return error("El partido todavía no se jugó")

        if not entero_no_negativo(equipo_a_goles) or not entero_no_negativo(equipo_b_goles):
            return error("Goles inválidos")

        if not isinstance(estadisticas_data, list):
            return error("Estadísticas inválidas")

        # Jugadores activos y su equipo
        equipo_de = dict(
            partido.participaciones.filter(estado='activo')
            .values_list('usuario_id', 'equipo_asignado')
        )

        estadisticas = []
        vistos = set()
        goles_por_equipo = {'A': 0, 'B': 0}

        for stat in estadisticas_data:
            try:
                uid = int(stat.get('usuario_id'))
            except (TypeError, ValueError, AttributeError):
                return error("Estadística con jugador inválido")

            if uid not in equipo_de:
                return error("Hay estadísticas de un jugador que no participa del partido")
            if uid in vistos:
                return error("Hay estadísticas duplicadas para un jugador")
            vistos.add(uid)

            goles = stat.get('goles')
            amarillas = stat.get('amarillas')
            rojas = stat.get('rojas')

            if any(v is not None and not entero_no_negativo(v) for v in (goles, amarillas, rojas)):
                return error("Las estadísticas deben ser enteros mayores o iguales a 0")
            if (amarillas or 0) > 2 or (rojas or 0) > 1:
                return error("Tarjetas fuera de rango")

            if equipo_de[uid] in goles_por_equipo:
                goles_por_equipo[equipo_de[uid]] += goles or 0

            estadisticas.append((uid, goles, amarillas, rojas))

        if goles_por_equipo['A'] > equipo_a_goles or goles_por_equipo['B'] > equipo_b_goles:
            return error("La suma de goles individuales supera los goles del equipo")

        fecha = timezone.localtime(partido.fecha_hora)

        with transaction.atomic():
            # Bloquea el partido y reconfirma que no haya resultado (evita doble carga)
            Partido.objects.select_for_update().get(pk=partido.pk)
            if Resultado.objects.filter(partido=partido).exists():
                return error("Este partido ya tiene resultado cargado")

            resultado = Resultado.objects.create(
                partido=partido,
                equipo_a_goles=equipo_a_goles,
                equipo_b_goles=equipo_b_goles,
                cargado_por=request.user
            )

            EstadisticasPartido.objects.bulk_create([
                EstadisticasPartido(
                    resultado=resultado,
                    usuario_id=uid,
                    goles=goles,
                    tarjetas_amarillas=amarillas,
                    tarjetas_rojas=rojas,
                )
                for uid, goles, amarillas, rojas in estadisticas
            ])

            destinatarios = partido.participaciones.filter(
                estado='activo',
                usuario__is_registered=True
            ).values_list('usuario_id', flat=True)

            Notificacion.objects.bulk_create([
                Notificacion(
                    usuario_id=uid,
                    partido=partido,
                    tipo=Notificacion.Tipo.RESULTADO_CARGADO,
                    mensaje=(
                        f"El resultado del partido del día {fecha:%d/%m/%Y} "
                        f"ha sido cargado: {equipo_a_goles} - {equipo_b_goles}"
                    ),
                )
                for uid in destinatarios
            ])

        return Response(ResultadoSerializer(resultado).data, status=status.HTTP_201_CREATED)


# ============================================================================
# NOTIFICACIÓN
# ============================================================================
class NotificacionViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet para notificaciones del usuario autenticado"""
    serializer_class = NotificacionSerializer
    permission_classes = [EsUsuarioAutenticado]

    def get_queryset(self):
        return Notificacion.objects.filter(usuario=self.request.user)

    @action(detail=False, methods=['get'])
    def no_leidas(self, request):
        """Cantidad total de no leídas (el listado está paginado)"""
        cantidad = self.get_queryset().filter(leida=False).count()
        return Response({'count': cantidad})

    @action(detail=True, methods=['patch'])
    def marcar_leida(self, request, pk=None):
        """Marcar notificación como leída"""
        notificacion = self.get_object()
        notificacion.leida = True
        notificacion.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['patch'])
    def marcar_todas_leidas(self, request):
        """Marcar todas las notificaciones como leídas"""
        Notificacion.objects.filter(
            usuario=request.user,
            leida=False
        ).update(leida=True)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ============================================================================
# INVITACIÓN
# ============================================================================
class InvitacionPorCodigoView(APIView):
    """Obtener partido por código de invitación (solo acceso directo)"""
    permission_classes = [permissions.AllowAny]
    ignorar_token_invalido = True

    def get(self, request, codigo):
        invitacion = get_object_or_404(Invitacion, codigo_unico=codigo)

        if not invitacion.esta_activa():
            return Response(
                {'detail': 'Esta invitación ha expirado o fue cancelada'},
                status=status.HTTP_400_BAD_REQUEST
            )

        partido = invitacion.partido
        serializer = PartidoPublicoSerializer(partido)
        return Response({'partido': serializer.data})


class AceptarInvitacionView(APIView):
    """Aceptar invitación sin estar autenticado"""
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'invitacion'
    ignorar_token_invalido = True

    def post(self, request, partido_id):
        partido = get_object_or_404(Partido, id=partido_id)

        codigo = request.data.get('codigo_unico')
        nombre = request.data.get('nombre')
        email = (request.data.get('email') or '').strip().lower()
        equipo = request.data.get('equipo', 'A')

        def rechazar_antes(mensaje):
            return Response({'error': mensaje}, status=status.HTTP_400_BAD_REQUEST)

        def rechazar(mensaje, codigo_http=status.HTTP_400_BAD_REQUEST):
            # Solo dentro del atomic: deshace lo creado (p. ej. el fantasma)
            transaction.set_rollback(True)
            return Response({'error': mensaje}, status=codigo_http)

        # --- Validaciones que no necesitan transacción ---
        try:
            invitacion = Invitacion.objects.get(partido=partido, codigo_unico=codigo)
        except Invitacion.DoesNotExist:
            return rechazar_antes('Código de invitación inválido')

        if not invitacion.esta_activa():
            return rechazar_antes('La invitación ha expirado o fue cancelada')

        if equipo not in ('A', 'B'):
            return rechazar_antes("Equipo debe ser 'A' o 'B'")

        if hasattr(partido, 'resultado') or (
            partido.periodicidad == 'unico' and partido.fecha_hora <= timezone.now()
        ):
            return rechazar_antes('El partido ya se jugó')

        if not request.user.is_authenticated and (not nombre or not email):
            return rechazar_antes('Nombre y email requeridos')

        # --- Cupo y alta, con el partido bloqueado ---
        with transaction.atomic():
            partido = Partido.objects.select_for_update().get(pk=partido.pk)

            if request.user.is_authenticated:
                usuario = request.user
            else:
                usuario, _ = Usuario.objects.get_or_create(
                    email=email,
                    defaults={'nombre': nombre, 'is_registered': False}
                )
                if usuario.is_registered:
                    return rechazar(
                        'Este email pertenece a una cuenta registrada. Iniciá sesión para unirte.',
                        status.HTTP_403_FORBIDDEN,
                    )

            if ParticipacionPartido.objects.filter(partido=partido, usuario=usuario).exists():
                return rechazar('Ya estás registrado en este partido')

            if partido.cupo_disponible(equipo) > 0:
                estado = 'activo'
            elif partido.lista_espera_disponible():
                estado = 'en_espera'
            else:
                return rechazar('Sin cupo disponible ni en lista de espera')

            participacion = ParticipacionPartido.objects.create(
                partido=partido,
                usuario=usuario,
                estado=estado,
                equipo_asignado=equipo,
            )

            notificar_participantes(
                partido,
                tipo=Notificacion.Tipo.PARTICIPANTE_NUEVO,
                mensaje=f"Se unió un nuevo participante al partido: {usuario.nombre} (Equipo {equipo})",
                excluir_usuario=usuario,
            )

        return Response(
            ParticipacionPartidoSerializer(participacion).data,
            status=status.HTTP_201_CREATED,
        )