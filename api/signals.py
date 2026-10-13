from django.db.models.signals import post_save
from django.dispatch import receiver
from api.models import Partido, Invitacion

@receiver(post_save, sender=Partido)
def crear_invitacion_partido(sender, instance, created, **kwargs):
    """Crear invitación automáticamente cuando se crea un Partido"""
    if created:
        Invitacion.objects.create(partido=instance)