from api.models import Notificacion, ParticipacionPartido

def notificar_participantes(partido, tipo, mensaje, excluir_usuario=None):
    qs = ParticipacionPartido.objects.filter(
        partido=partido,
        estado__in=['activo', 'en_espera'],
    ).select_related('usuario')

    if excluir_usuario:
        qs = qs.exclude(usuario=excluir_usuario)

    Notificacion.objects.bulk_create([
        Notificacion(
            usuario=p.usuario,
            partido=partido,
            tipo=tipo,
            mensaje=mensaje,
        )
        for p in qs
        if p.usuario.is_registered  # los fantasma no tienen cuenta
    ])