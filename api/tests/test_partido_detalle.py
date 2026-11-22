from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .helpers import anotar, crear_partido, crear_usuario


class DetalleBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.organizador = crear_usuario(nombre='Organizador', email='org@test.com')
        self.partido = crear_partido(self.organizador, cupo_total=4)
        self.jugador = crear_usuario(nombre='Jugador', email='jugador@test.com')
        self.fantasma = crear_usuario(registrado=False, nombre='Fantasma', email='fantasma@test.com')
        self.baneado = crear_usuario(nombre='Baneado', email='baneado@test.com')
        anotar(self.partido, self.jugador, 'A')
        anotar(self.partido, self.fantasma, 'B')
        anotar(self.partido, self.baneado, 'B', estado='baneado')
        self.emails = ['org@test.com', 'jugador@test.com', 'fantasma@test.com', 'baneado@test.com']

    def detalle(self, usuario=None, pk=None):
        if usuario is not None:
            self.client.force_authenticate(user=usuario)
        return self.client.get(reverse('partido-detail', kwargs={'pk': pk or self.partido.pk}))


class DetallePartidoTests(DetalleBase):
    def test_organizador_ve_los_emails_de_todos(self):
        r = self.detalle(self.organizador)

        self.assertEqual(r.status_code, 200)
        participantes = r.data['participantes']
        self.assertEqual(len(participantes), 3)
        for p in participantes:
            self.assertIn('usuario_email', p)
        self.assertEqual(
            {p['usuario_email'] for p in participantes},
            {'jugador@test.com', 'fantasma@test.com', 'baneado@test.com'},
        )

    def test_participante_no_ve_ningun_email(self):
        r = self.detalle(self.jugador)

        self.assertEqual(r.status_code, 200)
        for p in r.data['participantes']:
            self.assertNotIn('usuario_email', p)
        cuerpo = r.content.decode()
        for email in self.emails:
            self.assertNotIn(email, cuerpo)

    def test_participante_recibe_el_id_de_usuario(self):
        r = self.detalle(self.jugador)

        ids = {p['usuario'] for p in r.data['participantes']}
        self.assertIn(self.jugador.id, ids)

    def test_participante_en_espera_puede_verlo(self):
        en_espera = crear_usuario()
        anotar(self.partido, en_espera, 'A', estado='en_espera')

        self.assertEqual(self.detalle(en_espera).status_code, 200)

    def test_baneado_recibe_404(self):
        self.assertEqual(self.detalle(self.baneado).status_code, 404)

    def test_usuario_ajeno_recibe_404(self):
        self.assertEqual(self.detalle(crear_usuario()).status_code, 404)

    def test_anonimo_recibe_404(self):
        self.assertEqual(self.detalle().status_code, 404)

    def test_partido_inexistente_recibe_404(self):
        self.assertEqual(self.detalle(self.organizador, pk=999999).status_code, 404)


class ListaPartidosTests(DetalleBase):
    def setUp(self):
        super().setUp()
        self.otro_partido = crear_partido(crear_usuario())
        anotar(self.otro_partido, self.jugador, 'B')
        self.ajeno = crear_partido(crear_usuario())

    def listar(self, usuario=None):
        if usuario is not None:
            self.client.force_authenticate(user=usuario)
        r = self.client.get(reverse('partido-list'))
        self.assertEqual(r.status_code, 200)
        return {p['id'] for p in r.data['results']}

    def test_participante_ve_los_partidos_donde_juega(self):
        self.assertEqual(self.listar(self.jugador), {self.partido.id, self.otro_partido.id})

    def test_organizador_ve_sus_partidos_sin_participar(self):
        self.assertEqual(self.listar(self.organizador), {self.partido.id})

    def test_baneado_no_lo_ve_en_la_lista(self):
        self.assertEqual(self.listar(self.baneado), set())

    def test_anonimo_recibe_lista_vacia(self):
        self.assertEqual(self.listar(), set())


class InvitacionPublicaTests(DetalleBase):
    def url(self, codigo=None):
        codigo = codigo or self.partido.invitacion.codigo_unico
        return reverse('invitacion-por-codigo', kwargs={'codigo': codigo})

    def test_cualquiera_ve_el_partido_sin_emails(self):
        r = self.client.get(self.url())

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['partido']['id'], self.partido.id)
        for p in r.data['partido']['participantes']:
            self.assertNotIn('usuario_email', p)
        cuerpo = r.content.decode()
        for email in self.emails:
            self.assertNotIn(email, cuerpo)

    def test_invitacion_cancelada_devuelve_400(self):
        inv = self.partido.invitacion
        inv.estado = 'cancelada'
        inv.save()

        self.assertEqual(self.client.get(self.url()).status_code, 400)

    def test_codigo_inexistente_devuelve_404(self):
        self.assertEqual(self.client.get(self.url('no-existe')).status_code, 404)
