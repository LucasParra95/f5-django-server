from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from api.models import ParticipacionPartido, Usuario

from .helpers import anotar, crear_partido, crear_usuario


class RegistroTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def registrar(self, **cambios):
        datos = {
            'nombre': 'Ana',
            'email': 'ana@test.com',
            'password': 'secreto123',
            'password2': 'secreto123',
        }
        datos.update(cambios)
        return self.client.post(reverse('registro'), datos, format='json')

    def test_crea_un_usuario_registrado_con_la_clave_hasheada(self):
        r = self.registrar()

        self.assertEqual(r.status_code, 201)
        usuario = Usuario.objects.get(email='ana@test.com')
        self.assertTrue(usuario.is_registered)
        self.assertNotEqual(usuario.password_hash, 'secreto123')
        self.assertTrue(check_password('secreto123', usuario.password_hash))
        self.assertNotIn('password', r.data)

    def test_normaliza_el_email(self):
        r = self.registrar(email='ANA@Test.com')

        self.assertEqual(r.status_code, 201)
        self.assertTrue(Usuario.objects.filter(email='ana@test.com', is_registered=True).exists())

    def test_un_fantasma_reclama_su_cuenta_y_conserva_sus_partidos(self):
        fantasma = crear_usuario(registrado=False, nombre='Ana vieja', email='ana@test.com')
        partido = crear_partido(crear_usuario())
        anotar(partido, fantasma, 'A')

        r = self.registrar()

        self.assertEqual(r.status_code, 201)
        self.assertEqual(Usuario.objects.filter(email='ana@test.com').count(), 1)
        fantasma.refresh_from_db()
        self.assertTrue(fantasma.is_registered)
        self.assertEqual(fantasma.nombre, 'Ana')
        self.assertTrue(ParticipacionPartido.objects.filter(partido=partido, usuario=fantasma).exists())

    def test_email_de_cuenta_registrada_devuelve_400(self):
        crear_usuario(registrado=True, email='ana@test.com')

        for email in ('ana@test.com', 'ANA@test.com'):
            with self.subTest(email=email):
                r = self.registrar(email=email)
                self.assertEqual(r.status_code, 400)
                self.assertIn('email', r.data)

    def test_claves_que_no_coinciden_devuelven_400(self):
        r = self.registrar(password2='distinta123')

        self.assertEqual(r.status_code, 400)
        self.assertIn('password', r.data)
        self.assertFalse(Usuario.objects.exists())

    def test_clave_corta_devuelve_400(self):
        r = self.registrar(password='abc', password2='abc')

        self.assertEqual(r.status_code, 400)
        self.assertFalse(Usuario.objects.exists())

    def test_faltan_campos_devuelve_400(self):
        completos = {
            'nombre': 'Ana',
            'email': 'ana@test.com',
            'password': 'secreto123',
            'password2': 'secreto123',
        }
        for campo in completos:
            with self.subTest(campo=campo):
                datos = {k: v for k, v in completos.items() if k != campo}
                r = self.client.post(reverse('registro'), datos, format='json')
                self.assertEqual(r.status_code, 400)
        self.assertFalse(Usuario.objects.exists())


class LoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.usuario = Usuario.objects.create(
            nombre='Ana',
            email='ana@test.com',
            is_registered=True,
            password_hash=make_password('secreto123'),
        )

    def login(self, email='ana@test.com', password='secreto123'):
        return self.client.post('/api/token/', {'email': email, 'password': password}, format='json')

    def test_login_correcto_devuelve_tokens_y_usuario_sin_la_clave(self):
        r = self.login()

        self.assertEqual(r.status_code, 200)
        for clave in ('access', 'refresh', 'user'):
            self.assertIn(clave, r.data)
        self.assertNotIn('password', r.data)
        self.assertNotIn('secreto123', r.content.decode())
        self.assertEqual(r.data['user']['email'], 'ana@test.com')

    def test_el_email_no_distingue_mayusculas(self):
        self.assertEqual(self.login(email='ANA@Test.com').status_code, 200)

    def test_clave_incorrecta_devuelve_400(self):
        self.assertEqual(self.login(password='mala').status_code, 400)

    def test_email_inexistente_devuelve_400(self):
        self.assertEqual(self.login(email='nadie@test.com').status_code, 400)

    def test_un_fantasma_no_puede_iniciar_sesion(self):
        crear_usuario(registrado=False, email='fantasma@test.com')

        self.assertEqual(self.login(email='fantasma@test.com', password='lo-que-sea').status_code, 400)

    def test_faltan_campos_devuelve_400(self):
        for datos in ({}, {'email': 'ana@test.com'}, {'password': 'secreto123'}):
            with self.subTest(datos=datos):
                self.assertEqual(self.client.post('/api/token/', datos, format='json').status_code, 400)

    def test_el_access_token_autentica_las_requests(self):
        token = self.login().data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        r = self.client.get(reverse('usuario-me'))

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['email'], 'ana@test.com')

    def test_el_refresh_token_entrega_un_access_nuevo(self):
        refresh = self.login().data['refresh']

        r = self.client.post('/api/token/refresh/', {'refresh': refresh}, format='json')

        self.assertEqual(r.status_code, 200)
        self.assertIn('access', r.data)

    def test_login_tiene_limite_de_intentos(self):
        limite = int(ScopedRateThrottle.THROTTLE_RATES['auth'].split('/')[0])

        for _ in range(limite):
            self.assertEqual(self.login(password='mala').status_code, 400)

        self.assertEqual(self.login(password='mala').status_code, 429)


class PerfilTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.usuario = crear_usuario(nombre='Ana', email='ana@test.com')
        self.url = reverse('usuario-me')

    def test_me_devuelve_el_perfil_con_estadisticas(self):
        self.client.force_authenticate(user=self.usuario)

        r = self.client.get(self.url)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['email'], 'ana@test.com')
        for clave in ('partidos_jugados', 'partidos_creados', 'goles_totales'):
            self.assertIn(clave, r.data)

    def test_anonimo_no_accede_a_me(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_fantasma_no_accede_a_me(self):
        self.client.force_authenticate(user=crear_usuario(registrado=False))

        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_put_actualiza_el_nombre(self):
        self.client.force_authenticate(user=self.usuario)

        r = self.client.put(self.url, {'nombre': 'Ana María'}, format='json')

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['nombre'], 'Ana María')
        self.usuario.refresh_from_db()
        self.assertEqual(self.usuario.nombre, 'Ana María')

    def test_solo_se_puede_editar_el_nombre(self):
        self.client.force_authenticate(user=self.usuario)

        r = self.client.patch(
            self.url,
            {'nombre': 'Nuevo', 'email': 'otro@test.com', 'is_registered': False},
            format='json',
        )

        self.assertEqual(r.status_code, 200)
        self.usuario.refresh_from_db()
        self.assertEqual(self.usuario.nombre, 'Nuevo')
        self.assertEqual(self.usuario.email, 'ana@test.com')
        self.assertTrue(self.usuario.is_registered)

    def test_nombre_vacio_devuelve_400(self):
        self.client.force_authenticate(user=self.usuario)

        self.assertEqual(self.client.patch(self.url, {'nombre': ''}, format='json').status_code, 400)

    def test_no_se_puede_leer_el_perfil_de_otro_usuario(self):
        otro = crear_usuario()
        self.client.force_authenticate(user=self.usuario)

        self.assertEqual(self.client.get(f'/api/usuarios/{otro.id}/').status_code, 404)
        self.assertEqual(self.client.get('/api/usuarios/').status_code, 404)
