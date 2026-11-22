from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from api.models import ParticipacionPartido, Resultado

from .helpers import anotar, crear_partido, crear_usuario


class DefinirEquiposBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.organizador = crear_usuario(nombre='Organizador')
        self.partido = crear_partido(self.organizador, cupo_total=6)
        self.a1, self.a2, self.b1, self.b2 = (crear_usuario() for _ in range(4))
        # Arrancan en el equipo contrario, para verificar que se reasignan
        anotar(self.partido, self.a1, 'B')
        anotar(self.partido, self.a2, 'B')
        anotar(self.partido, self.b1, 'A')
        anotar(self.partido, self.b2, 'A')

    def url(self):
        return reverse('partido-definir-equipos', kwargs={'pk': self.partido.pk})

    def equipos_validos(self):
        return {'A': [self.a1.id, self.a2.id], 'B': [self.b1.id, self.b2.id]}

    def definir(self, equipos, usuario=None):
        self.client.force_authenticate(user=usuario or self.organizador)
        return self.client.post(self.url(), {'equipos': equipos}, format='json')

    def equipo_de(self, usuario):
        return ParticipacionPartido.objects.get(partido=self.partido, usuario=usuario).equipo_asignado

    def assertSinCambios(self):
        self.partido.refresh_from_db()
        self.assertFalse(self.partido.equipos_definidos)
        self.assertEqual(self.equipo_de(self.a1), 'B')
        self.assertEqual(self.equipo_de(self.b1), 'A')


class DefinirEquiposCasoFelizTests(DefinirEquiposBase):
    def test_asigna_los_equipos_y_marca_equipos_definidos(self):
        r = self.definir(self.equipos_validos())

        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['equipos_definidos'])
        self.partido.refresh_from_db()
        self.assertTrue(self.partido.equipos_definidos)
        self.assertEqual(self.equipo_de(self.a1), 'A')
        self.assertEqual(self.equipo_de(self.a2), 'A')
        self.assertEqual(self.equipo_de(self.b1), 'B')
        self.assertEqual(self.equipo_de(self.b2), 'B')

    def test_acepta_ids_como_string(self):
        r = self.definir({
            'A': [str(self.a1.id), str(self.a2.id)],
            'B': [str(self.b1.id), str(self.b2.id)],
        })

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.equipo_de(self.a1), 'A')

    def test_se_pueden_redefinir_si_no_hay_resultado(self):
        self.assertEqual(self.definir(self.equipos_validos()).status_code, 200)

        r = self.definir({'A': [self.b1.id, self.b2.id], 'B': [self.a1.id, self.a2.id]})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.equipo_de(self.a1), 'B')
        self.assertEqual(self.equipo_de(self.b1), 'A')


class DefinirEquiposPermisosTests(DefinirEquiposBase):
    def test_participante_no_organizador_recibe_403(self):
        r = self.definir(self.equipos_validos(), usuario=self.a1)

        self.assertEqual(r.status_code, 403)
        self.assertSinCambios()

    def test_usuario_ajeno_recibe_404(self):
        r = self.definir(self.equipos_validos(), usuario=crear_usuario())

        self.assertEqual(r.status_code, 404)
        self.assertSinCambios()

    def test_anonimo_no_puede_definir_equipos(self):
        r = self.client.post(self.url(), {'equipos': self.equipos_validos()}, format='json')

        self.assertIn(r.status_code, (401, 403))
        self.assertSinCambios()


class DefinirEquiposValidacionesTests(DefinirEquiposBase):
    def test_estructura_invalida_devuelve_400(self):
        a1, a2, b1 = self.a1.id, self.a2.id, self.b1.id
        casos = [
            None,
            'abc',
            {},
            {'A': [a1]},
            {'B': [b1]},
            {'A': [a1], 'C': [b1]},
            {'A': [a1], 'B': [b1], 'C': [a2]},
        ]
        for equipos in casos:
            with self.subTest(equipos=equipos):
                self.assertEqual(self.definir(equipos).status_code, 400)
        self.assertSinCambios()

    def test_equipo_vacio_devuelve_400(self):
        for equipos in ({'A': [], 'B': [self.b1.id]}, {'A': [self.a1.id], 'B': []}):
            with self.subTest(equipos=equipos):
                self.assertEqual(self.definir(equipos).status_code, 400)
        self.assertSinCambios()

    def test_jugador_repetido_devuelve_400(self):
        casos = [
            {'A': [self.a1.id, self.a1.id], 'B': [self.b1.id]},  # repetido en el mismo equipo
            {'A': [self.a1.id], 'B': [self.a1.id]},              # en ambos equipos
        ]
        for equipos in casos:
            with self.subTest(equipos=equipos):
                self.assertEqual(self.definir(equipos).status_code, 400)
        self.assertSinCambios()

    def test_jugador_que_no_esta_activo_devuelve_400(self):
        en_espera = crear_usuario()
        baneado = crear_usuario()
        anotar(self.partido, en_espera, 'A', estado='en_espera')
        anotar(self.partido, baneado, 'B', estado='baneado')

        casos = [
            {'A': [self.a1.id, en_espera.id], 'B': [self.b1.id]},
            {'A': [self.a1.id], 'B': [baneado.id]},
        ]
        for equipos in casos:
            with self.subTest(equipos=equipos):
                self.assertEqual(self.definir(equipos).status_code, 400)
        self.assertSinCambios()

    def test_jugador_ajeno_al_partido_devuelve_400(self):
        ajeno = crear_usuario()

        r = self.definir({'A': [self.a1.id, ajeno.id], 'B': [self.b1.id]})

        self.assertEqual(r.status_code, 400)
        self.assertSinCambios()

    def test_ids_invalidos_devuelven_400(self):
        b1 = self.b1.id
        casos = [
            {'A': ['abc'], 'B': [b1]},
            {'A': [None], 'B': [b1]},
            {'A': [[self.a1.id]], 'B': [b1]},
            {'A': 5, 'B': [b1]},
        ]
        for equipos in casos:
            with self.subTest(equipos=equipos):
                self.assertEqual(self.definir(equipos).status_code, 400)
        self.assertSinCambios()

    def test_con_resultado_cargado_devuelve_400(self):
        Resultado.objects.create(
            partido=self.partido,
            equipo_a_goles=1,
            equipo_b_goles=0,
            cargado_por=self.organizador,
        )

        r = self.definir(self.equipos_validos())

        self.assertEqual(r.status_code, 400)
        self.assertSinCambios()

    def test_una_validacion_fallida_no_deja_asignaciones_a_medias(self):
        baneado = crear_usuario()
        anotar(self.partido, baneado, 'B', estado='baneado')

        # a1 es válido, pero el baneado en B hace fallar todo el pedido
        r = self.definir({'A': [self.a1.id], 'B': [baneado.id]})

        self.assertEqual(r.status_code, 400)
        self.assertSinCambios()
