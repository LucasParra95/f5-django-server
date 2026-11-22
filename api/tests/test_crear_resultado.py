from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import EstadisticasPartido, Notificacion, Resultado

from .helpers import anotar, crear_partido, crear_usuario


class CrearResultadoBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.organizador = crear_usuario(nombre='Organizador')
        # Partido ya jugado y con equipos definidos
        self.partido = crear_partido(self.organizador, cupo_total=10, dias=-1)
        self.partido.equipos_definidos = True
        self.partido.save()

        self.a1 = crear_usuario(nombre='A1')
        self.a2 = crear_usuario(registrado=False, nombre='A2 fantasma')
        self.b1 = crear_usuario(nombre='B1')
        self.baneado = crear_usuario(nombre='Baneado')
        self.espera = crear_usuario(nombre='Espera')
        self.ajeno = crear_usuario(nombre='Ajeno')

        anotar(self.partido, self.organizador, 'B')
        anotar(self.partido, self.a1, 'A')
        anotar(self.partido, self.a2, 'A')
        anotar(self.partido, self.b1, 'B')
        anotar(self.partido, self.baneado, 'B', estado='baneado')
        anotar(self.partido, self.espera, 'A', estado='en_espera')

    def payload(self, **cambios):
        datos = {
            'partido_id': self.partido.id,
            'equipo_a_goles': 3,
            'equipo_b_goles': 2,
            'estadisticas': [
                {'usuario_id': self.a1.id, 'goles': 2, 'amarillas': 1, 'rojas': 0},
                {'usuario_id': self.b1.id, 'goles': 1},
            ],
        }
        datos.update(cambios)
        return datos

    def cargar(self, datos=None, usuario=None):
        self.client.force_authenticate(user=usuario or self.organizador)
        datos = self.payload() if datos is None else datos
        return self.client.post(reverse('resultado-crear-resultado'), datos, format='json')

    def assertNoSeEscribioNada(self):
        self.assertFalse(Resultado.objects.exists())
        self.assertFalse(EstadisticasPartido.objects.exists())
        self.assertFalse(Notificacion.objects.exists())


class CrearResultadoCasoFelizTests(CrearResultadoBase):
    def test_guarda_resultado_y_estadisticas(self):
        r = self.cargar()

        self.assertEqual(r.status_code, 201)
        resultado = Resultado.objects.get(partido=self.partido)
        self.assertEqual((resultado.equipo_a_goles, resultado.equipo_b_goles), (3, 2))
        self.assertEqual(resultado.cargado_por, self.organizador)

        stat_a1 = EstadisticasPartido.objects.get(resultado=resultado, usuario=self.a1)
        self.assertEqual((stat_a1.goles, stat_a1.tarjetas_amarillas, stat_a1.tarjetas_rojas), (2, 1, 0))
        stat_b1 = EstadisticasPartido.objects.get(resultado=resultado, usuario=self.b1)
        self.assertEqual((stat_b1.goles, stat_b1.tarjetas_amarillas, stat_b1.tarjetas_rojas), (1, None, None))

        self.assertEqual(r.data['equipo_ganador'], 'A')
        self.assertEqual(r.data['diferencia_goles'], 1)

    def test_notifica_solo_a_participantes_activos_y_registrados(self):
        self.cargar()

        notificados = set(
            Notificacion.objects.filter(tipo=Notificacion.Tipo.RESULTADO_CARGADO)
            .values_list('usuario_id', flat=True)
        )
        self.assertEqual(notificados, {self.organizador.id, self.a1.id, self.b1.id})
        self.assertIn('3 - 2', Notificacion.objects.first().mensaje)

    def test_acepta_usuario_id_como_string(self):
        datos = self.payload(estadisticas=[{'usuario_id': str(self.a1.id), 'goles': 1}])

        self.assertEqual(self.cargar(datos).status_code, 201)

    def test_estadisticas_son_opcionales(self):
        datos = self.payload()
        del datos['estadisticas']

        r = self.cargar(datos)

        self.assertEqual(r.status_code, 201)
        self.assertFalse(EstadisticasPartido.objects.exists())

    def test_estadistica_con_solo_el_id_del_jugador(self):
        datos = self.payload(estadisticas=[{'usuario_id': self.a1.id}])

        r = self.cargar(datos)

        self.assertEqual(r.status_code, 201)
        stat = EstadisticasPartido.objects.get()
        self.assertEqual((stat.goles, stat.tarjetas_amarillas, stat.tarjetas_rojas), (None, None, None))

    def test_empate_cero_a_cero(self):
        datos = self.payload(equipo_a_goles=0, equipo_b_goles=0, estadisticas=[])

        r = self.cargar(datos)

        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['equipo_ganador'], 'Empate')

    def test_suma_de_goles_igual_a_la_del_equipo_es_valida(self):
        datos = self.payload(
            equipo_a_goles=4,
            equipo_b_goles=0,
            estadisticas=[
                {'usuario_id': self.a1.id, 'goles': 2},
                {'usuario_id': self.a2.id, 'goles': 2},
            ],
        )

        self.assertEqual(self.cargar(datos).status_code, 201)


class CrearResultadoPermisosTests(CrearResultadoBase):
    def test_anonimo_no_puede_cargar(self):
        r = self.client.post(reverse('resultado-crear-resultado'), self.payload(), format='json')

        self.assertIn(r.status_code, (401, 403))
        self.assertNoSeEscribioNada()

    def test_participante_no_organizador_recibe_403(self):
        r = self.cargar(usuario=self.a1)

        self.assertEqual(r.status_code, 403)
        self.assertNoSeEscribioNada()

    def test_partido_inexistente_devuelve_404(self):
        self.assertEqual(self.cargar(self.payload(partido_id=999999)).status_code, 404)


class CrearResultadoValidacionesTests(CrearResultadoBase):
    def test_equipos_no_definidos_devuelve_400(self):
        self.partido.equipos_definidos = False
        self.partido.save()

        self.assertEqual(self.cargar().status_code, 400)
        self.assertNoSeEscribioNada()

    def test_partido_futuro_devuelve_400(self):
        self.partido.fecha_hora = timezone.now() + timedelta(days=1)
        self.partido.save()

        self.assertEqual(self.cargar().status_code, 400)
        self.assertNoSeEscribioNada()

    def test_goles_invalidos_devuelven_400(self):
        for campo in ('equipo_a_goles', 'equipo_b_goles'):
            for valor in (None, -1, 'dos', '3', True, 1.5):
                with self.subTest(campo=campo, valor=valor):
                    self.assertEqual(self.cargar(self.payload(**{campo: valor})).status_code, 400)

        for campo in ('equipo_a_goles', 'equipo_b_goles'):
            with self.subTest(campo=campo, valor='ausente'):
                datos = self.payload()
                del datos[campo]
                self.assertEqual(self.cargar(datos).status_code, 400)
        self.assertNoSeEscribioNada()

    def test_estadisticas_que_no_son_una_lista_devuelven_400(self):
        for valor in ({}, 'abc', 5):
            with self.subTest(valor=valor):
                self.assertEqual(self.cargar(self.payload(estadisticas=valor)).status_code, 400)
        self.assertNoSeEscribioNada()

    def test_estadistica_con_jugador_invalido_devuelve_400(self):
        for estadisticas in ([{}], [{'usuario_id': 'x'}], [{'usuario_id': None}], ['abc']):
            with self.subTest(estadisticas=estadisticas):
                self.assertEqual(self.cargar(self.payload(estadisticas=estadisticas)).status_code, 400)
        self.assertNoSeEscribioNada()

    def test_estadistica_de_jugador_que_no_juega_devuelve_400(self):
        for jugador in (self.ajeno, self.espera, self.baneado):
            with self.subTest(jugador=jugador.nombre):
                datos = self.payload(estadisticas=[{'usuario_id': jugador.id, 'goles': 0}])
                self.assertEqual(self.cargar(datos).status_code, 400)
        self.assertNoSeEscribioNada()

    def test_estadisticas_duplicadas_devuelven_400(self):
        datos = self.payload(estadisticas=[
            {'usuario_id': self.a1.id, 'goles': 1},
            {'usuario_id': self.a1.id, 'goles': 1},
        ])

        self.assertEqual(self.cargar(datos).status_code, 400)
        self.assertNoSeEscribioNada()

    def test_goles_individuales_mayores_a_los_del_equipo_devuelven_400(self):
        casos = [
            # a1 solo ya supera el 1 del equipo A
            self.payload(equipo_a_goles=1, estadisticas=[{'usuario_id': self.a1.id, 'goles': 2}]),
            # a1 + a2 (fantasma) suman 4 y el equipo A hizo 3
            self.payload(estadisticas=[
                {'usuario_id': self.a1.id, 'goles': 2},
                {'usuario_id': self.a2.id, 'goles': 2},
            ]),
            # b1 supera los 2 del equipo B
            self.payload(estadisticas=[{'usuario_id': self.b1.id, 'goles': 3}]),
        ]
        for datos in casos:
            with self.subTest(estadisticas=datos['estadisticas']):
                self.assertEqual(self.cargar(datos).status_code, 400)
        self.assertNoSeEscribioNada()

    def test_valores_invalidos_en_estadisticas_devuelven_400(self):
        casos = [
            ('goles', -1), ('goles', 'x'), ('goles', True), ('goles', 1.5),
            ('amarillas', 3), ('amarillas', -1),
            ('rojas', 2), ('rojas', '1'),
        ]
        for campo, valor in casos:
            with self.subTest(campo=campo, valor=valor):
                datos = self.payload(estadisticas=[{'usuario_id': self.a1.id, campo: valor}])
                self.assertEqual(self.cargar(datos).status_code, 400)
        self.assertNoSeEscribioNada()

    def test_doble_carga_devuelve_400_y_no_duplica_nada(self):
        self.assertEqual(self.cargar().status_code, 201)

        r = self.cargar()

        self.assertEqual(r.status_code, 400)
        self.assertEqual(Resultado.objects.count(), 1)
        self.assertEqual(EstadisticasPartido.objects.count(), 2)
        self.assertEqual(Notificacion.objects.count(), 3)
