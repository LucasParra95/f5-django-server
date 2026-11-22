from datetime import timedelta
from itertools import count

from django.utils import timezone

from api.models import Cancha, ParticipacionPartido, Partido, Usuario

_secuencia = count(1)


def crear_usuario(registrado=True, nombre=None, email=None):
    n = next(_secuencia)
    return Usuario.objects.create(
        nombre=nombre or f'Jugador {n}',
        email=email or f'jugador{n}@test.com',
        is_registered=registrado,
    )


def crear_partido(organizador, cupo_total=4, dias=1):
    """Crea un partido futuro. El signal post_save genera su Invitacion."""
    cancha = Cancha.objects.create(
        nombre='Cancha Test',
        direccion='Calle 123',
        localidad='Bariloche',
        provincia='Río Negro',
    )
    return Partido.objects.create(
        organizador=organizador,
        cancha=cancha,
        fecha_hora=timezone.now() + timedelta(days=dias),
        cupo_total=cupo_total,
    )


def anotar(partido, usuario, equipo='A', estado='activo'):
    return ParticipacionPartido.objects.create(
        partido=partido,
        usuario=usuario,
        estado=estado,
        equipo_asignado=equipo,
    )
