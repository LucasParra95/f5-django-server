from django.utils import timezone

from api.models import Notificacion, ParticipacionPartido


def fecha_local(fecha, formato='%d/%m/%Y %H:%M'):
    """Formatea un datetime en la zona horaria del proyecto (settings.TIME_ZONE)."""
    return timezone.localtime(fecha).strftime(formato)


def notificar_participantes(partido, tipo, mensaje, excluir_usuario=None):
    """Notifica a los participantes activos y en espera que tengan cuenta."""
    qs = ParticipacionPartido.objects.filter(
        partido=partido,
        estado__in=['activo', 'en_espera'],
        usuario__is_registered=True,  # los fantasma no tienen cuenta
    )

    if excluir_usuario:
        qs = qs.exclude(usuario=excluir_usuario)

    Notificacion.objects.bulk_create([
        Notificacion(
            usuario_id=p.usuario_id,
            partido=partido,
            tipo=tipo,
            mensaje=mensaje,
        )
        for p in qs
    ])