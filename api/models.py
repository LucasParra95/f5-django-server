from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
import uuid
import secrets

# ============================================================================
# USUARIO
# ============================================================================
class Usuario(models.Model):
    """
    Modelo de Usuario. Puede ser:
    - Registrado: is_registered=True, tiene password_hash
    - Fantasma: is_registered=False, sin password (creado al aceptar invitación)
    """
    id = models.AutoField(primary_key=True)
    nombre = models.CharField(max_length=150)
    email = models.EmailField(unique=True)
    password_hash = models.CharField(max_length=255, null=True, blank=True)
    is_registered = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'  # Campo que usa para login
    REQUIRED_FIELDS = ['nombre']

    @property
    def is_authenticated(self):
        """Propiedad requerida por Django/DRF"""
        return self.is_registered

    @property
    def is_anonymous(self):
        """Propiedad requerida por Django/simplejwt"""
        return False

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['email']),
        ]

    def __str__(self):
        estado = "Registrado" if self.is_registered else "Fantasma"
        return f"{self.nombre} ({self.email}) - {estado}"


# ============================================================================
# CANCHA
# ============================================================================
class Cancha(models.Model):
    """
    Modelo de Cancha de futbol 5.
    """
    id = models.AutoField(primary_key=True)
    nombre = models.CharField(max_length=200)
    direccion = models.CharField(max_length=255)
    localidad = models.CharField(max_length=150)
    provincia = models.CharField(max_length=150)
    telefono = models.CharField(max_length=20, null=True, blank=True)
    sitio_web = models.URLField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['nombre']
        indexes = [
            models.Index(fields=['localidad', 'provincia']),
        ]

    def __str__(self):
        return f"{self.nombre} - {self.localidad}"


# ============================================================================
# INVITACIÓN
# ============================================================================
class Invitacion(models.Model):
    """
    Modelo de Invitación. Puede ser 1:1 (único) o 1:N (periódica).
    El codigo_unico es lo que va en el link.
    """
    ESTADO_CHOICES = [
        ('activa', 'Activa'),
        ('cancelada', 'Cancelada'),
    ]

    id = models.AutoField(primary_key=True)
    partido = models.OneToOneField('Partido', on_delete=models.CASCADE, related_name='invitacion')
    codigo_unico = models.CharField(max_length=32, unique=True, db_index=True)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='activa')
    fecha_expiracion = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.codigo_unico:
            # Generar código único si no existe
            self.codigo_unico = secrets.token_urlsafe(24)
        super().save(*args, **kwargs)

    def esta_activa(self):
        """Verifica si la invitación está activa y no expiró"""
        if self.estado != 'activa':
            return False
        if self.fecha_expiracion and timezone.now() > self.fecha_expiracion:
            return False
        return True

    def __str__(self):
        return f"Invitación {self.codigo_unico} - {self.partido.fecha_hora.strftime('%d/%m/%Y')}"


# ============================================================================
# PARTIDO
# ============================================================================
class Partido(models.Model):
    """
    Modelo de Partido. 
    - Puede ser único o semanal (periodicidad)
    - Tiene un organizador (Usuario)
    - Tiene cupo de jugadores
    - Los equipos se definen antes de cargar resultado (equipos_definidos)
    """
    PERIODICIDAD_CHOICES = [
        ('unico', 'Único'),
        ('semanal', 'Semanal'),
    ]

    id = models.AutoField(primary_key=True)
    organizador = models.ForeignKey(Usuario, on_delete=models.CASCADE, related_name='partidos_creados')
    cancha = models.ForeignKey(Cancha, on_delete=models.SET_NULL, null=True, blank=True)
    fecha_hora = models.DateTimeField()
    cupo_total = models.IntegerField(validators=[MinValueValidator(4)])  # Mínimo 4 jugadores
    periodicidad = models.CharField(max_length=20, choices=PERIODICIDAD_CHOICES, default='unico')
    equipos_definidos = models.BooleanField(default=False)
    # Resultado se accede via: partido_instance.resultado (reverse relation)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-fecha_hora']
        indexes = [
            models.Index(fields=['organizador', 'fecha_hora']),
            models.Index(fields=['fecha_hora']),
        ]

    def cupo_disponible(self, equipo=None):
        """
        Retorna cantidad de lugares disponibles.
        Si equipo es 'A' o 'B', retorna disponibles en ese equipo.
        """
        cupo_equipo = self.cupo_total // 2
        
        if equipo not in ['A', 'B']:
            # Retorna cupo total disponible
            participantes_activos = self.participaciones.filter(estado='activo').count()
            return max(0, self.cupo_total - participantes_activos)
        
        # Retorna cupo disponible en equipo específico
        participantes_equipo = self.participaciones.filter(
            estado='activo',
            equipo_asignado=equipo
        ).count()
        return max(0, cupo_equipo - participantes_equipo)

    def lista_espera_disponible(self):
        """Retorna True si hay lugar en lista de espera"""
        limite_espera = self.cupo_total  # El doble del cupo
        total_participantes = self.participaciones.filter(
            estado__in=['activo', 'en_espera']
        ).count()
        return total_participantes < limite_espera

    def __str__(self):
        return f"Partido {self.fecha_hora.strftime('%d/%m/%Y %H:%M')} - {self.cancha.nombre if self.cancha else 'Sin cancha'}"


# ============================================================================
# PARTICIPACIÓN EN PARTIDO
# ============================================================================
class ParticipacionPartido(models.Model):
    """
    Tabla de relación M2M entre Usuario y Partido.
    Controla: estado (activo/en_espera/baneado), equipo asignado, fecha de aceptación.
    """
    ESTADO_CHOICES = [
        ('activo', 'Activo'),
        ('en_espera', 'En Espera'),
        ('baneado', 'Baneado'),
    ]

    EQUIPO_CHOICES = [
        ('A', 'Equipo A'),
        ('B', 'Equipo B'),
    ]

    id = models.AutoField(primary_key=True)
    partido = models.ForeignKey(Partido, on_delete=models.CASCADE, related_name='participaciones')
    usuario = models.ForeignKey(Usuario, on_delete=models.CASCADE, related_name='participaciones')
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='activo')
    equipo_asignado = models.CharField(max_length=1, choices=EQUIPO_CHOICES, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('partido', 'usuario')
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['partido', 'estado']),
            models.Index(fields=['usuario', 'estado']),
        ]

    def __str__(self):
        return f"{self.usuario.nombre} en {self.partido} - {self.get_estado_display()}"


# ============================================================================
# RESULTADO
# ============================================================================
class Resultado(models.Model):
    """
    Modelo de Resultado del Partido.
    - Un resultado por partido (unique)
    - Registra goles por equipo
    - Los goles por jugador van en EstadisticasPartido
    """
    id = models.AutoField(primary_key=True)
    partido = models.OneToOneField(Partido, on_delete=models.CASCADE, related_name='resultado')
    equipo_a_goles = models.IntegerField(validators=[MinValueValidator(0)])
    equipo_b_goles = models.IntegerField(validators=[MinValueValidator(0)])
    fecha_carga = models.DateTimeField(auto_now_add=True)
    cargado_por = models.ForeignKey(Usuario, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-fecha_carga']

    def diferencia_goles(self):
        """Retorna la diferencia de goles"""
        return abs(self.equipo_a_goles - self.equipo_b_goles)

    def equipo_ganador(self):
        """Retorna 'A', 'B', o 'Empate'"""
        if self.equipo_a_goles > self.equipo_b_goles:
            return 'A'
        elif self.equipo_b_goles > self.equipo_a_goles:
            return 'B'
        else:
            return 'Empate'

    def __str__(self):
        return f"Resultado {self.equipo_a_goles} - {self.equipo_b_goles} ({self.partido.fecha_hora.strftime('%d/%m/%Y')})"


# ============================================================================
# ESTADÍSTICAS POR JUGADOR EN PARTIDO
# ============================================================================
class EstadisticasPartido(models.Model):
    """
    Estadísticas individuales de un jugador en un partido específico.
    Todos los campos son opcionales.
    """
    id = models.AutoField(primary_key=True)
    resultado = models.ForeignKey(Resultado, on_delete=models.CASCADE, related_name='estadisticas')
    usuario = models.ForeignKey(Usuario, on_delete=models.CASCADE, related_name='estadisticas_partidos')
    goles = models.IntegerField(null=True, blank=True, validators=[MinValueValidator(0)])
    tarjetas_amarillas = models.IntegerField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(2)])
    tarjetas_rojas = models.IntegerField(null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(1)])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('resultado', 'usuario')
        ordering = ['-resultado__fecha_carga']
        indexes = [
            models.Index(fields=['usuario', 'resultado']),
        ]

    def __str__(self):
        stats = []
        if self.goles is not None:
            stats.append(f"{self.goles} goles")
        if self.tarjetas_amarillas is not None:
            stats.append(f"{self.tarjetas_amarillas} amarillas")
        if self.tarjetas_rojas is not None:
            stats.append(f"{self.tarjetas_rojas} rojas")
        
        stats_str = ", ".join(stats) if stats else "Sin estadísticas"
        return f"{self.usuario.nombre} - {stats_str}"


# ============================================================================
# NOTIFICACIÓN
# ============================================================================
class Notificacion(models.Model):
    """
    Modelo de Notificaciones. Se generan cuando hay eventos importantes.
    Ej: cambio de equipo, cancelación, etc.
    """
    TIPO_CHOICES = [
        ('cambio_equipo', 'Cambio de Equipo'),
        ('cancelacion_invitacion', 'Cancelación de Invitación'),
        ('resultado_cargado', 'Resultado Cargado'),
        ('baneado', 'Has sido baneado'),
        ('otro', 'Otro'),
    ]

    id = models.AutoField(primary_key=True)
    usuario = models.ForeignKey(Usuario, on_delete=models.CASCADE, related_name='notificaciones')
    partido = models.ForeignKey(Partido, on_delete=models.CASCADE, related_name='notificaciones')
    tipo = models.CharField(max_length=50, choices=TIPO_CHOICES)
    mensaje = models.TextField()
    leida = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['usuario', 'leida']),
        ]

    def __str__(self):
        return f"Notificación para {self.usuario.nombre} - {self.get_tipo_display()}"


