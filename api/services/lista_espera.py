from django.db import transaction

from api.models import Notificacion, Usuario
from api.services.notifications import fecha_local


def promover_lista_espera(partido):
    """
    Pasa de `en_espera` a `activo` a quienes tengan lugar en su equipo, por orden de
    llegada (created_at). Cada equipo admite `cupo_total // 2` jugadores activos.
    Notifica a los promovidos que tengan cuenta y devuelve la lista de participaciones.

    Hay que llamarla cada vez que se libera un lugar (baneo, cambio de equipo) o
    cambia el cupo.
    """
    with transaction.atomic():
        cupo_equipo = partido.cupo_total // 2
        activos = {
            equipo: partido.participaciones.filter(estado='activo', equipo_asignado=equipo).count()
            for equipo in ('A', 'B')
        }

        promovidos = []
        en_espera = partido.participaciones.select_for_update().filter(estado='en_espera').order_by('created_at')
        for p in en_espera:
            if p.equipo_asignado in activos and activos[p.equipo_asignado] < cupo_equipo:
                p.estado = 'activo'
                p.save(update_fields=['estado'])
                activos[p.equipo_asignado] += 1
                promovidos.append(p)

        registrados = set(
            Usuario.objects
            .filter(id__in=[p.usuario_id for p in promovidos], is_registered=True)
            .values_list('id', flat=True)
        )
        fecha = fecha_local(partido.fecha_hora)
        Notificacion.objects.bulk_create([
            Notificacion(
                usuario_id=p.usuario_id,
                partido=partido,
                tipo=Notificacion.Tipo.PROMOVIDO,
                mensaje=(
                    f"Se liberó un lugar en el partido del {fecha}: "
                    f"ahora jugás en el Equipo {p.equipo_asignado}"
                ),
            )
            for p in promovidos
            if p.usuario_id in registrados
        ])

    return promovidos