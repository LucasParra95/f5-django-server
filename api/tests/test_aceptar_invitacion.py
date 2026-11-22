from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from api.models import Notificacion, ParticipacionPartido, Usuario

from .helpers import anotar, crear_partido, crear_usuario


class AceptarInvitacionBase(TestCase):
    def setUp(self):
        cache.clear()  # el throttle guarda contadores en la caché
        self.client = APIClient()
        self.organizador = crear_usuario(nombre='Organizador')
        self.partido = crear_partido(self.organizador, cupo_total=4)  # 2 por equipo
        self.codigo = self.partido.invitacion.codigo_unico

    def url(self, partido=None):
        partido = partido or self.partido
        return reverse('aceptar-invitacion', kwargs={'partido_id': partido.id})

    def aceptar(self, partido=None, **datos):
        datos.setdefault('codigo_unico', self.codigo)
        return self.client.post(self.url(partido), datos, format='json')

    def participaciones(self):
        return ParticipacionPartido.objects.filter(partido=self.partido)


class FantasmaSinSesionTests(AceptarInvitacionBase):
    def test_se_anota_y_se_crea_el_fantasma(self):
        r = self.aceptar(nombre='Juan', email='juan@test.com', equipo='B')

        self.assertEqual(r.status_code, 201)
        usuario = Usuario.objects.get(email='juan@test.com')
        self.assertFalse(usuario.is_registered)
        p = self.participaciones().get(usuario=usuario)
        self.assertEqual(p.estado, 'activo')
        self.assertEqual(p.equipo_asignado, 'B')

    def test_equipo_por_defecto_es_A(self):
        r = self.aceptar(nombre='Juan', email='juan@test.com')

        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.participaciones().get().equipo_asignado, 'A')

    def test_normaliza_el_email(self):
        r = self.aceptar(nombre='Juan', email='  JUAN@Test.com ')

        self.assertEqual(r.status_code, 201)
        self.assertTrue(Usuario.objects.filter(email='juan@test.com').exists())

    def test_reutiliza_al_fantasma_existente(self):
        fantasma = crear_usuario(registrado=False, email='juan@test.com')

        r = self.aceptar(nombre='Otro nombre', email='juan@test.com')

        self.assertEqual(r.status_code, 201)
        self.assertEqual(Usuario.objects.filter(email='juan@test.com').count(), 1)
        self.assertTrue(self.participaciones().filter(usuario=fantasma).exists())

    def test_sin_nombre_o_email_devuelve_400(self):
        for datos in ({}, {'nombre': 'Juan'}, {'email': 'juan@test.com'}):
            with self.subTest(datos=datos):
                r = self.aceptar(**datos)
                self.assertEqual(r.status_code, 400)

        self.assertFalse(self.participaciones().exists())
        self.assertFalse(Usuario.objects.filter(email='juan@test.com').exists())

    def test_email_de_cuenta_registrada_devuelve_403(self):
        registrado = crear_usuario(registrado=True, email='ana@test.com')

        r = self.aceptar(nombre='Ana', email='ana@test.com')

        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.participaciones().filter(usuario=registrado).exists())

    def test_duplicado_devuelve_400(self):
        self.aceptar(nombre='Juan', email='juan@test.com')

        r = self.aceptar(nombre='Juan', email='juan@test.com')

        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.participaciones().count(), 1)

    def test_baneado_no_puede_volver_a_anotarse(self):
        baneado = crear_usuario(registrado=False, email='juan@test.com')
        anotar(self.partido, baneado, equipo='A', estado='baneado')

        r = self.aceptar(nombre='Juan', email='juan@test.com')

        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.participaciones().get(usuario=baneado).estado, 'baneado')


class UsuarioRegistradoTests(AceptarInvitacionBase):
    def test_se_anota_con_sesion_sin_enviar_nombre_ni_email(self):
        jugador = crear_usuario(registrado=True)
        self.client.force_authenticate(user=jugador)

        r = self.aceptar(equipo='B')

        self.assertEqual(r.status_code, 201)
        p = self.participaciones().get(usuario=jugador)
        self.assertEqual((p.estado, p.equipo_asignado), ('activo', 'B'))

    def test_ignora_el_email_del_body_si_hay_sesion(self):
        jugador = crear_usuario(registrado=True)
        victima = crear_usuario(registrado=True, email='victima@test.com')
        self.client.force_authenticate(user=jugador)

        r = self.aceptar(nombre='X', email='victima@test.com')

        self.assertEqual(r.status_code, 201)
        self.assertTrue(self.participaciones().filter(usuario=jugador).exists())
        self.assertFalse(self.participaciones().filter(usuario=victima).exists())

    def test_duplicado_devuelve_400(self):
        jugador = crear_usuario(registrado=True)
        self.client.force_authenticate(user=jugador)
        self.aceptar()

        r = self.aceptar()

        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.participaciones().count(), 1)


class CupoTests(AceptarInvitacionBase):
    def test_equipo_lleno_pasa_a_lista_de_espera(self):
        anotar(self.partido, crear_usuario(), 'A')
        anotar(self.partido, crear_usuario(), 'A')

        r = self.aceptar(nombre='Juan', email='juan@test.com', equipo='A')

        self.assertEqual(r.status_code, 201)
        p = self.participaciones().get(usuario__email='juan@test.com')
        self.assertEqual((p.estado, p.equipo_asignado), ('en_espera', 'A'))

    def test_el_cupo_se_calcula_por_equipo(self):
        anotar(self.partido, crear_usuario(), 'A')
        anotar(self.partido, crear_usuario(), 'A')

        r = self.aceptar(nombre='Juan', email='juan@test.com', equipo='B')

        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.participaciones().get(usuario__email='juan@test.com').estado, 'activo')

    def test_sin_cupo_ni_lista_de_espera_devuelve_400(self):
        for equipo in ('A', 'A', 'B', 'B'):
            anotar(self.partido, crear_usuario(), equipo)

        r = self.aceptar(nombre='Juan', email='juan@test.com', equipo='A')

        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.participaciones().filter(usuario__email='juan@test.com').exists())

    def test_los_baneados_no_ocupan_cupo(self):
        anotar(self.partido, crear_usuario(), 'A')
        anotar(self.partido, crear_usuario(), 'A', estado='baneado')

        r = self.aceptar(nombre='Juan', email='juan@test.com', equipo='A')

        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.participaciones().get(usuario__email='juan@test.com').estado, 'activo')


class ValidacionesTests(AceptarInvitacionBase):
    def test_equipo_invalido_devuelve_400(self):
        r = self.aceptar(nombre='Juan', email='juan@test.com', equipo='C')

        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.participaciones().exists())

    def test_codigo_invalido_devuelve_400(self):
        r = self.aceptar(codigo_unico='no-existe', nombre='Juan', email='juan@test.com')

        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.participaciones().exists())

    def test_codigo_de_otro_partido_devuelve_400(self):
        otro = crear_partido(self.organizador)

        r = self.aceptar(codigo_unico=otro.invitacion.codigo_unico, nombre='Juan', email='juan@test.com')

        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.participaciones().exists())

    def test_invitacion_cancelada_devuelve_400(self):
        inv = self.partido.invitacion
        inv.estado = 'cancelada'
        inv.save()

        r = self.aceptar(nombre='Juan', email='juan@test.com')

        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.participaciones().exists())

    def test_invitacion_expirada_devuelve_400(self):
        inv = self.partido.invitacion
        inv.fecha_expiracion = timezone.now() - timedelta(hours=1)
        inv.save()

        r = self.aceptar(nombre='Juan', email='juan@test.com')

        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.participaciones().exists())

    def test_partido_inexistente_devuelve_404(self):
        r = self.client.post(
            reverse('aceptar-invitacion', kwargs={'partido_id': 999999}),
            {'codigo_unico': self.codigo, 'nombre': 'Juan', 'email': 'juan@test.com'},
            format='json',
        )

        self.assertEqual(r.status_code, 404)


class NotificacionesTests(AceptarInvitacionBase):
    def test_notifica_a_los_participantes_registrados_menos_al_que_se_une(self):
        participante = crear_usuario(registrado=True)
        fantasma = crear_usuario(registrado=False)
        anotar(self.partido, self.organizador, 'A')
        anotar(self.partido, participante, 'A')
        anotar(self.partido, fantasma, 'B')

        r = self.aceptar(nombre='Juan', email='juan@test.com', equipo='B')

        self.assertEqual(r.status_code, 201)
        nuevas = Notificacion.objects.filter(tipo=Notificacion.Tipo.PARTICIPANTE_NUEVO)
        self.assertEqual(
            set(nuevas.values_list('usuario_id', flat=True)),
            {self.organizador.id, participante.id},
        )
        self.assertIn('Juan', nuevas.first().mensaje)

    def test_sin_otros_participantes_no_genera_notificaciones(self):
        r = self.aceptar(nombre='Juan', email='juan@test.com')

        self.assertEqual(r.status_code, 201)
        self.assertFalse(Notificacion.objects.exists())


class ThrottleTests(AceptarInvitacionBase):
    def test_supera_el_limite_y_devuelve_429(self):
        limite = int(ScopedRateThrottle.THROTTLE_RATES['invitacion'].split('/')[0])
        datos = {'codigo_unico': 'invalido', 'nombre': 'Juan', 'email': 'juan@test.com'}

        for _ in range(limite):
            self.assertEqual(self.client.post(self.url(), datos, format='json').status_code, 400)

        self.assertEqual(self.client.post(self.url(), datos, format='json').status_code, 429)
