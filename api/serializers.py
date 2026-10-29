from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.hashers import make_password
from api.models import (
    Usuario, Cancha, Invitacion, Partido, ParticipacionPartido,
    Resultado, EstadisticasPartido, Notificacion
)


# ============================================================================
# USUARIO
# ============================================================================
class UsuarioSerializer(serializers.ModelSerializer):
    """Serializer básico de Usuario para lectura"""
    class Meta:
        model = Usuario
        fields = ['id', 'nombre', 'email', 'is_registered', 'created_at']
        read_only_fields = ['id', 'is_registered', 'created_at']


class UsuarioRegistroSerializer(serializers.ModelSerializer):
    """Serializer para registro de nuevo usuario"""
    password = serializers.CharField(write_only=True, required=True, min_length=6)
    password2 = serializers.CharField(write_only=True, required=True, min_length=6)

    class Meta:
        model = Usuario
        fields = ['nombre', 'email', 'password', 'password2']
        # La unicidad se valida en validate_email: un fantasma puede reclamar su email
        extra_kwargs = {'email': {'validators': []}}

    def validate_email(self, value):
        if Usuario.objects.filter(email=value, is_registered=True).exists():
            raise serializers.ValidationError("Ya existe una cuenta con este email.")
        return value

    def validate(self, data):
        if data['password'] != data['password2']:
            raise serializers.ValidationError({"password": "Las contraseñas no coinciden."})
        return data

    def create(self, validated_data):
        validated_data.pop('password2')
        password = validated_data.pop('password')
        # validate_email descartó los registrados: si el email existe, es un fantasma
        # y se reutiliza la misma fila para conservar su historial de partidos
        user = Usuario.objects.filter(email=validated_data['email']).first()
        if user is None:
            user = Usuario(**validated_data)
        else:
            user.nombre = validated_data['nombre']        
        user.password_hash = make_password(password)
        user.is_registered = True
        user.save()
        return user


class UsuarioPerfilSerializer(serializers.ModelSerializer):
    """Serializer para perfil de usuario con estadísticas"""
    partidos_jugados = serializers.SerializerMethodField()
    partidos_creados = serializers.SerializerMethodField()
    goles_totales = serializers.SerializerMethodField()

    class Meta:
        model = Usuario
        fields = ['id', 'nombre', 'email', 'is_registered', 'created_at', 
                  'partidos_jugados', 'partidos_creados', 'goles_totales']
        read_only_fields = fields

    def get_partidos_jugados(self, obj):
        count = obj.participaciones.filter(estado='activo').count()
        return count

    def get_partidos_creados(self, obj):
        count = obj.partidos_creados.count()
        return count

    def get_goles_totales(self, obj):
        total = 0
        for stat in obj.estadisticas_partidos.all():
            if stat.goles:
                total += stat.goles
        return total


class TokenObtainPairSerializerCustom(serializers.Serializer):
    """Serializer personalizado para login con email"""
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')
        
        if not email or not password:
            raise serializers.ValidationError("Email y password requeridos")
        
        try:
            user = Usuario.objects.get(email=email.lower(), is_registered=True)
        except Usuario.DoesNotExist:
            raise serializers.ValidationError("Email o contraseña incorrectos")
        
        # Verificar contraseña
        from django.contrib.auth.hashers import check_password
        if not check_password(password, user.password_hash):
            raise serializers.ValidationError("Email o contraseña incorrectos")
        
        # Generar tokens
        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken()
        refresh['email'] = user.email
        refresh['nombre'] = user.nombre
        refresh['user_id'] = user.id
        
        return {
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'user': UsuarioSerializer(user).data,
        }

# ============================================================================
# CANCHA
# ============================================================================
class CanchaSerializer(serializers.ModelSerializer):
    """Serializer de Cancha"""
    class Meta:
        model = Cancha
        fields = ['id', 'nombre', 'direccion', 'localidad', 'provincia', 
                  'telefono', 'sitio_web', 'created_at']
        read_only_fields = ['id', 'created_at']


# ============================================================================
# INVITACIÓN
# ============================================================================
class InvitacionSerializer(serializers.ModelSerializer):
    """Serializer de Invitacion"""
    esta_activa = serializers.SerializerMethodField()

    class Meta:
        model = Invitacion
        fields = ['id', 'codigo_unico', 'estado', 'fecha_expiracion', 
                  'esta_activa', 'created_at']
        read_only_fields = ['id', 'codigo_unico', 'created_at']

    def get_esta_activa(self, obj):
        return obj.esta_activa()


# ============================================================================
# RESULTADO Y ESTADÍSTICAS
# ============================================================================
class EstadisticasPartidoSerializer(serializers.ModelSerializer):
    """Serializer de EstadisticasPartido"""
    usuario_nombre = serializers.CharField(source='usuario.nombre', read_only=True)

    class Meta:
        model = EstadisticasPartido
        fields = ['id', 'usuario', 'usuario_nombre', 'goles', 
                  'tarjetas_amarillas', 'tarjetas_rojas', 'created_at']
        read_only_fields = ['id', 'created_at']


class ResultadoSerializer(serializers.ModelSerializer):
    """Serializer de Resultado con estadísticas"""
    equipo_ganador = serializers.SerializerMethodField()
    diferencia_goles = serializers.SerializerMethodField()
    estadisticas = EstadisticasPartidoSerializer(many=True, read_only=True)
    cargado_por_nombre = serializers.CharField(source='cargado_por.nombre', read_only=True)

    class Meta:
        model = Resultado
        fields = ['id', 'equipo_a_goles', 'equipo_b_goles', 'equipo_ganador',
                  'diferencia_goles', 'fecha_carga', 'partido_id', 'cargado_por', 
                  'cargado_por_nombre', 'estadisticas', 'created_at']
        read_only_fields = ['id', 'fecha_carga', 'partido_id', 'created_at']

    def get_equipo_ganador(self, obj):
        return obj.equipo_ganador()

    def get_diferencia_goles(self, obj):
        return obj.diferencia_goles()


# ============================================================================
# PARTICIPACIÓN EN PARTIDO
# ============================================================================
class ParticipacionPartidoSerializer(serializers.ModelSerializer):
    """Serializer de ParticipacionPartido"""
    usuario_nombre = serializers.CharField(source='usuario.nombre', read_only=True)
    usuario_email = serializers.CharField(source='usuario.email', read_only=True)

    class Meta:
        model = ParticipacionPartido
        fields = ['id', 'usuario', 'usuario_nombre', 'usuario_email', 'estado',
                  'equipo_asignado', 'created_at']
        read_only_fields = ['id', 'created_at']


class ParticipacionPartidoPublicaSerializer(ParticipacionPartidoSerializer):
    """Participación sin datos de contacto, para vistas públicas"""
    class Meta(ParticipacionPartidoSerializer.Meta):
        fields = ['id', 'usuario_nombre', 'estado', 'equipo_asignado']
        read_only_fields = fields


class ParticipacionPartidoCrearSerializer(serializers.ModelSerializer):
    """Serializer simplificado para crear/actualizar participación"""
    class Meta:
        model = ParticipacionPartido
        fields = ['equipo_asignado']


# ============================================================================
# PARTIDO
# ============================================================================
class PartidoListSerializer(serializers.ModelSerializer):
    """Serializer simplificado de Partido para listados"""
    organizador_nombre = serializers.CharField(source='organizador.nombre', read_only=True)
    cancha_nombre = serializers.CharField(source='cancha.nombre', read_only=True)
    participantes_count = serializers.SerializerMethodField()
    cupo_disponible = serializers.SerializerMethodField()
    invitacion = InvitacionSerializer(read_only=True)

    class Meta:
        model = Partido
        fields = ['id', 'fecha_hora', 'organizador', 'organizador_nombre',
                  'cancha', 'cancha_nombre', 'cupo_total', 'invitacion', 'periodicidad',
                  'equipos_definidos', 'participantes_count', 'cupo_disponible',
                  'created_at']
        read_only_fields = fields

    def get_participantes_count(self, obj):
        return obj.participaciones.filter(estado='activo').count()

    def get_cupo_disponible(self, obj):
        return obj.cupo_disponible()


class PartidoDetailSerializer(serializers.ModelSerializer):
    """Serializer completo de Partido con relaciones"""
    organizador_nombre = serializers.CharField(source='organizador.nombre', read_only=True)
    cancha_nombre = serializers.CharField(source='cancha.nombre', read_only=True)
    participantes = ParticipacionPartidoSerializer(source='participaciones', many=True, read_only=True)
    resultado = ResultadoSerializer(read_only=True)
    invitacion = InvitacionSerializer(read_only=True)
    participantes_count = serializers.SerializerMethodField()
    cupo_disponible = serializers.SerializerMethodField()
    en_espera_count = serializers.SerializerMethodField()

    class Meta:
        model = Partido
        fields = ['id', 'fecha_hora', 'organizador', 'organizador_nombre',
                  'cancha', 'cancha_nombre', 'cupo_total', 'periodicidad',
                  'equipos_definidos', 'resultado', 'invitacion',
                  'participantes', 'participantes_count', 'cupo_disponible',
                  'en_espera_count', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_participantes_count(self, obj):
        return obj.participaciones.filter(estado='activo').count()

    def get_cupo_disponible(self, obj):
        return obj.cupo_disponible()

    def get_en_espera_count(self, obj):
        return obj.participaciones.filter(estado='en_espera').count()


class PartidoPublicoSerializer(PartidoDetailSerializer):
    """Detalle de partido para quien llega por código de invitación (sin emails)"""
    participantes = ParticipacionPartidoPublicaSerializer(
        source='participaciones', many=True, read_only=True
    )


class PartidoEditarSerializer(serializers.ModelSerializer):
    """Serializer para que el organizador edite fecha, cancha y cupo"""
    class Meta:
        model = Partido
        fields = ['cancha', 'fecha_hora', 'cupo_total']

    def validate_cancha(self, value):
        if value != self.instance.cancha and not value.activa:
            raise serializers.ValidationError("Esta cancha ya no está disponible.")
        return value

    def validate_cupo_total(self, value):
        """El nuevo cupo no puede dejar sin lugar a jugadores activos de un equipo"""
        cupo_equipo = value // 2
        for equipo in ('A', 'B'):
            activos = self.instance.participaciones.filter(
                estado='activo', equipo_asignado=equipo
            ).count()
            if activos > cupo_equipo:
                raise serializers.ValidationError(
                    f"El equipo {equipo} ya tiene {activos} jugadores activos "
                    f"y el nuevo cupo admite {cupo_equipo} por equipo."
                )
        return value


class PartidoCrearSerializer(serializers.ModelSerializer):
    """Serializer para crear nuevo partido"""
    invitacion = InvitacionSerializer(read_only=True)

    class Meta:
        model = Partido
        fields = ['cancha', 'fecha_hora', 'cupo_total', 'invitacion', 'periodicidad']
        read_only_fields = ['invitacion']

    def validate_cancha(self, value):
        if not value.activa:
            raise serializers.ValidationError("Esta cancha ya no está disponible.")
        return value
        
    def create(self, validated_data):
        validated_data['organizador'] = self.context['request'].user
        return super().create(validated_data)


# ============================================================================
# NOTIFICACIÓN
# ============================================================================
class NotificacionSerializer(serializers.ModelSerializer):
    """Serializer de Notificacion"""
    partido_fecha = serializers.CharField(source='partido.fecha_hora', read_only=True)

    class Meta:
        model = Notificacion
        fields = ['id', 'tipo', 'mensaje', 'leida', 'partido', 
                  'partido_fecha', 'created_at']
        read_only_fields = ['id', 'created_at']

    def update(self, instance, validated_data):
        instance.leida = validated_data.get('leida', instance.leida)
        instance.save()
        return instance