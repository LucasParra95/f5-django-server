from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import Cancha, Invitacion, Notificacion, ParticipacionPartido, Partido, Resultado

from .helpers import anotar, crear_partido, crear_usuario

Tipo = Notificacion.Tipo


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.organizador = crear_usuario(nombre='Organizador')
        self.partido = crear_partido(self.organizador, cupo_total=4)  # 2 por equipo

    # --- helpers de requests
    def accion(self, nombre, usuario, datos=None, partido=None):
        partido = partido or self.partido
        self.client.force_authenticate(user=usuario)
        url = reverse('partido-' + nombre.replace('_', '-'), kwargs={'pk': partido.pk})
        return self.client.post(url, datos or {}, format='json')

    def editar(self, usuario, datos, partido=None):
        partido = partido or self.partido
        self.client.force_authenticate(user=usuario)
        url = reverse('partido-detail', kwargs={'pk': partido.pk})
        return self.client.patch(url, datos, format='json')

    # --- helpers de asserts
    def notificados(self, tipo):
        return set(Notificacion.objects.filter(tipo=tipo).values_list('usuario_id', flat=True))

    def estado_de(self, usuario, partido=None):
        return ParticipacionPartido.objects.get(partido=partido or self.partido, usuario=usuario).estado

    def equipo_de(self, usuario, partido=None):
        return ParticipacionPartido.objects.get(partido=partido or self.partido, usuario=usuario).equipo_asignado

    def llenar(self):
        """Equipos completos: A (a1, a2) y B (b1, b2), todos activos"""
        self.a1, self.a2, self.b1, self.b2 = (crear_usuario() for _ in range(4))
        for usuario, equipo in ((self.a1, 'A'), (self.a2, 'A'), (self.b1, 'B'), (self.b2, 'B')):
            anotar(self.partido, usuario, equipo)


# ------------------------------------------------------------------ cancelar
class CancelarTests(Base):
    def setUp(self):
        super().setUp()
        self.jugador = crear_usuario(nombre='Jugador')
        self.fantasma = crear_usuario(registrado=False, nombre='Fantasma')
        self.espera = crear_usuario(nombre='Espera')
        self.baneado = crear_usuario(nombre='Baneado')
        anotar(self.partido, self.jugador, 'A')
        anotar(self.partido, self.fantasma, 'B')
        anotar(self.partido, self.espera, 'A', estado='en_espera')
        anotar(self.partido, self.baneado, 'B', estado='baneado')

    def test_cancela_el_partido_y_cierra_la_invitacion(self):
        r = self.accion('cancelar', self.organizador)

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['estado'], 'cancelado')
        self.partido.refresh_from_db()
        self.assertEqual(self.partido.estado, 'cancelado')
        self.assertEqual(Invitacion.objects.get(partido=self.partido).estado, 'cancelada')

    def test_notifica_a_activos_y_en_espera_con_cuenta(self):
        self.accion('cancelar', self.organizador)

        self.assertEqual(self.notificados(Tipo.PARTIDO_CANCELADO), {self.jugador.id, self.espera.id})

    def test_el_organizador_participante_no_se_notifica_a_si_mismo(self):
        anotar(self.partido, self.organizador, 'B')

        self.accion('cancelar', self.organizador)

        self.assertNotIn(self.organizador.id, self.notificados(Tipo.PARTIDO_CANCELADO))

    def test_solo_el_organizador_puede_cancelar(self):
        self.assertEqual(self.accion('cancelar', self.jugador).status_code, 403)
        self.assertEqual(self.accion('cancelar', crear_usuario()).status_code, 404)
        self.client.force_authenticate(user=None)
        r = self.client.post(reverse('partido-cancelar', kwargs={'pk': self.partido.pk}))
        self.assertIn(r.status_code, (401, 403))
        self.partido.refresh_from_db()
        self.assertEqual(self.partido.estado, 'programado')

    def test_no_se_puede_cancelar_dos_veces(self):
        self.assertEqual(self.accion('cancelar', self.organizador).status_code, 200)

        self.assertEqual(self.accion('cancelar', self.organizador).status_code, 400)

    def test_no_se_puede_cancelar_con_resultado(self):
        Resultado.objects.create(
            partido=self.partido, equipo_a_goles=1, equipo_b_goles=0, cargado_por=self.organizador
        )

        self.assertEqual(self.accion('cancelar', self.organizador).status_code, 400)
        self.partido.refresh_from_db()
        self.assertEqual(self.partido.estado, 'programado')

    def test_un_partido_pasado_sin_resultado_se_puede_cancelar(self):
        pasado = crear_partido(self.organizador, dias=-2)

        self.assertEqual(self.accion('cancelar', self.organizador, partido=pasado).status_code, 200)

    def test_la_invitacion_cancelada_no_admite_nuevos_jugadores(self):
        self.accion('cancelar', self.organizador)
        codigo = self.partido.invitacion.codigo_unico

        ver = self.client.get(reverse('invitacion-por-codigo', kwargs={'codigo': codigo}))
        unirse = self.client.post(
            reverse('aceptar-invitacion', kwargs={'partido_id': self.partido.id}),
            {'codigo_unico': codigo, 'nombre': 'Juan', 'email': 'juan@test.com'},
            format='json',
        )

        self.assertEqual(ver.status_code, 400)
        self.assertEqual(unirse.status_code, 400)

    def test_un_partido_cancelado_no_admite_cambios(self):
        self.accion('cancelar', self.organizador)

        editar = self.editar(self.organizador, {'cupo_total': 6})
        definir = self.accion('definir_equipos', self.organizador,
                              {'equipos': {'A': [self.jugador.id], 'B': [self.fantasma.id]}})
        banear = self.accion('banear', self.organizador, {'usuario_id': self.jugador.id})
        cambiar = self.accion('cambiar_equipo', self.jugador,
                              {'usuario_id': self.jugador.id, 'nuevo_equipo': 'B'})

        for r in (editar, definir, banear, cambiar):
            self.assertEqual(r.status_code, 400)

    def test_un_partido_cancelado_no_admite_resultado(self):
        pasado = crear_partido(self.organizador, dias=-1)
        pasado.equipos_definidos = True
        pasado.estado = 'cancelado'
        pasado.save()
        self.client.force_authenticate(user=self.organizador)

        r = self.client.post(
            reverse('resultado-crear-resultado'),
            {'partido_id': pasado.id, 'equipo_a_goles': 1, 'equipo_b_goles': 0},
            format='json',
        )

        self.assertEqual(r.status_code, 400)
        self.assertFalse(Resultado.objects.exists())

    def test_el_partido_cancelado_sigue_visible_para_sus_participantes(self):
        self.accion('cancelar', self.organizador)
        self.client.force_authenticate(user=self.jugador)

        r = self.client.get(reverse('partido-detail', kwargs={'pk': self.partido.pk}))

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['estado'], 'cancelado')


# ------------------------------------------------------------------ editar
class EditarTests(Base):
    def setUp(self):
        super().setUp()
        self.jugador = crear_usuario(nombre='Jugador')
        self.fantasma = crear_usuario(registrado=False, nombre='Fantasma')
        anotar(self.partido, self.jugador, 'A')
        anotar(self.partido, self.fantasma, 'B')

    def test_cambiar_el_cupo_notifica_y_devuelve_el_detalle(self):
        r = self.editar(self.organizador, {'cupo_total': 6})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['cupo_total'], 6)
        self.assertIn('participantes', r.data)
        self.assertEqual(self.notificados(Tipo.PARTIDO_MODIFICADO), {self.jugador.id})
        self.assertIn('nuevo cupo: 6', Notificacion.objects.get(tipo=Tipo.PARTIDO_MODIFICADO).mensaje)

    def test_cambiar_la_fecha_notifica(self):
        nueva = timezone.now() + timedelta(days=5)

        r = self.editar(self.organizador, {'fecha_hora': nueva.isoformat()})

        self.assertEqual(r.status_code, 200)
        self.assertIn('nueva fecha', Notificacion.objects.get(tipo=Tipo.PARTIDO_MODIFICADO).mensaje)

    def test_cambiar_la_cancha_notifica(self):
        otra = Cancha.objects.create(
            nombre='Cancha Nueva', direccion='Calle 9', localidad='Bariloche', provincia='Río Negro'
        )

        r = self.editar(self.organizador, {'cancha': otra.id})

        self.assertEqual(r.status_code, 200)
        self.assertIn('nueva cancha: Cancha Nueva', Notificacion.objects.get(tipo=Tipo.PARTIDO_MODIFICADO).mensaje)

    def test_si_no_cambia_nada_no_notifica(self):
        r = self.editar(self.organizador, {'cupo_total': 4})

        self.assertEqual(r.status_code, 200)
        self.assertFalse(Notificacion.objects.filter(tipo=Tipo.PARTIDO_MODIFICADO).exists())

    def test_no_notifica_al_organizador_aunque_juegue(self):
        anotar(self.partido, self.organizador, 'B')

        self.editar(self.organizador, {'cupo_total': 6})

        self.assertNotIn(self.organizador.id, self.notificados(Tipo.PARTIDO_MODIFICADO))

    def test_solo_el_organizador_puede_editar(self):
        self.assertEqual(self.editar(self.jugador, {'cupo_total': 6}).status_code, 403)
        self.assertEqual(self.editar(crear_usuario(), {'cupo_total': 6}).status_code, 404)
        self.partido.refresh_from_db()
        self.assertEqual(self.partido.cupo_total, 4)

    def test_fecha_pasada_devuelve_400(self):
        r = self.editar(self.organizador, {'fecha_hora': (timezone.now() - timedelta(hours=1)).isoformat()})

        self.assertEqual(r.status_code, 400)

    def test_cupo_fuera_de_rango_devuelve_400(self):
        for cupo in (3, 41):
            with self.subTest(cupo=cupo):
                self.assertEqual(self.editar(self.organizador, {'cupo_total': cupo}).status_code, 400)

    def test_no_se_puede_reducir_el_cupo_si_deja_afuera_a_jugadores_activos(self):
        partido = crear_partido(self.organizador, cupo_total=6)  # 3 por equipo
        for _ in range(3):
            anotar(partido, crear_usuario(), 'A')

        r = self.editar(self.organizador, {'cupo_total': 4}, partido=partido)  # 2 por equipo

        self.assertEqual(r.status_code, 400)

    def test_subir_el_cupo_promueve_a_la_lista_de_espera(self):
        anotar(self.partido, self.organizador, 'A')  # A completo: jugador + organizador
        espera = crear_usuario()
        anotar(self.partido, espera, 'A', estado='en_espera')

        r = self.editar(self.organizador, {'cupo_total': 6})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.estado_de(espera), 'activo')
        self.assertEqual(self.notificados(Tipo.PROMOVIDO), {espera.id})


# ------------------------------------------------------------------ lista de espera
class ListaEsperaTests(Base):
    def setUp(self):
        super().setUp()
        self.llenar()
        self.espera_b = crear_usuario()
        self.espera_a1 = crear_usuario()
        self.espera_a2 = crear_usuario()
        anotar(self.partido, self.espera_b, 'B', estado='en_espera')   # llegó primero, pero B sigue lleno
        anotar(self.partido, self.espera_a1, 'A', estado='en_espera')
        anotar(self.partido, self.espera_a2, 'A', estado='en_espera')

    def test_al_banear_pasa_el_primero_en_espera_de_ese_equipo(self):
        r = self.accion('banear', self.organizador, {'usuario_id': self.a1.id})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.estado_de(self.a1), 'baneado')
        self.assertEqual(self.estado_de(self.espera_a1), 'activo')
        self.assertEqual(self.estado_de(self.espera_a2), 'en_espera')
        self.assertEqual(self.estado_de(self.espera_b), 'en_espera')
        self.assertEqual(self.notificados(Tipo.PROMOVIDO), {self.espera_a1.id})
        self.assertEqual(self.notificados(Tipo.BANEADO), {self.a1.id})

    def test_si_se_liberan_dos_lugares_pasan_los_dos_en_orden(self):
        self.accion('banear', self.organizador, {'usuario_id': self.a1.id})
        self.accion('banear', self.organizador, {'usuario_id': self.a2.id})

        self.assertEqual(self.estado_de(self.espera_a1), 'activo')
        self.assertEqual(self.estado_de(self.espera_a2), 'activo')

    def test_no_pasa_nadie_si_el_lugar_liberado_es_de_otro_equipo(self):
        ParticipacionPartido.objects.filter(usuario=self.espera_b).delete()  # solo quedan en espera de A

        self.accion('banear', self.organizador, {'usuario_id': self.b1.id})

        self.assertEqual(self.estado_de(self.espera_a1), 'en_espera')
        self.assertFalse(Notificacion.objects.filter(tipo=Tipo.PROMOVIDO).exists())

    def test_al_pasarse_de_equipo_se_libera_el_lugar_anterior(self):
        # B tiene un lugar libre: b2 se va
        ParticipacionPartido.objects.filter(usuario=self.b2).delete()

        r = self.accion('cambiar_equipo', self.a1, {'usuario_id': self.a1.id, 'nuevo_equipo': 'B'})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.equipo_de(self.a1), 'B')
        self.assertEqual(self.estado_de(self.espera_a1), 'activo')
        self.assertEqual(self.estado_de(self.espera_b), 'en_espera')  # B quedó lleno de nuevo


# ------------------------------------------------------------------ banear
class BanearTests(Base):
    def setUp(self):
        super().setUp()
        self.jugador = crear_usuario(nombre='Jugador')
        self.fantasma = crear_usuario(registrado=False, nombre='Fantasma')
        anotar(self.partido, self.jugador, 'A')
        anotar(self.partido, self.fantasma, 'B')

    def test_el_organizador_banea_y_notifica(self):
        r = self.accion('banear', self.organizador, {'usuario_id': self.jugador.id})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['estado'], 'baneado')
        self.assertEqual(self.notificados(Tipo.BANEADO), {self.jugador.id})

    def test_al_banear_a_un_fantasma_no_se_crea_notificacion(self):
        r = self.accion('banear', self.organizador, {'usuario_id': self.fantasma.id})

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.estado_de(self.fantasma), 'baneado')
        self.assertFalse(Notificacion.objects.filter(tipo=Tipo.BANEADO).exists())

    def test_solo_el_organizador_puede_banear(self):
        self.assertEqual(self.accion('banear', self.jugador, {'usuario_id': self.fantasma.id}).status_code, 403)
        self.assertEqual(self.accion('banear', crear_usuario(), {'usuario_id': self.jugador.id}).status_code, 404)
        self.assertEqual(self.estado_de(self.fantasma), 'activo')

    def test_usuario_id_invalido_devuelve_400_y_no_500(self):
        for valor in ('abc', None, [], True):
            with self.subTest(valor=valor):
                r = self.accion('banear', self.organizador, {'usuario_id': valor})
                self.assertEqual(r.status_code, 400)

    def test_jugador_que_no_participa_devuelve_404(self):
        r = self.accion('banear', self.organizador, {'usuario_id': crear_usuario().id})

        self.assertEqual(r.status_code, 404)

    def test_el_organizador_no_puede_banearse(self):
        anotar(self.partido, self.organizador, 'B')

        r = self.accion('banear', self.organizador, {'usuario_id': self.organizador.id})

        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.estado_de(self.organizador), 'activo')

    def test_no_se_puede_banear_dos_veces(self):
        self.accion('banear', self.organizador, {'usuario_id': self.jugador.id})

        r = self.accion('banear', self.organizador, {'usuario_id': self.jugador.id})

        self.assertEqual(r.status_code, 400)
        self.assertEqual(Notificacion.objects.filter(tipo=Tipo.BANEADO).count(), 1)

    def test_con_resultado_no_se_puede_banear(self):
        Resultado.objects.create(
            partido=self.partido, equipo_a_goles=0, equipo_b_goles=0, cargado_por=self.organizador
        )

        r = self.accion('banear', self.organizador, {'usuario_id': self.jugador.id})

        self.assertEqual(r.status_code, 400)

    def test_el_baneado_deja_de_ver_el_partido(self):
        self.accion('banear', self.organizador, {'usuario_id': self.jugador.id})
        self.client.force_authenticate(user=self.jugador)

        r = self.client.get(reverse('partido-detail', kwargs={'pk': self.partido.pk}))

        self.assertEqual(r.status_code, 404)


# ------------------------------------------------------------------ cambiar equipo
class CambiarEquipoTests(Base):
    def setUp(self):
        super().setUp()
        self.jugador = crear_usuario(nombre='Jugador')
        self.otro = crear_usuario(nombre='Otro')
        anotar(self.partido, self.jugador, 'A')
        anotar(self.partido, self.otro, 'B')

    def cambiar(self, quien, usuario_id, equipo):
        return self.accion('cambiar_equipo', quien, {'usuario_id': usuario_id, 'nuevo_equipo': equipo})

    def test_un_jugador_cambia_su_propio_equipo(self):
        r = self.cambiar(self.jugador, self.jugador.id, 'B')

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.equipo_de(self.jugador), 'B')
        self.assertFalse(Notificacion.objects.filter(tipo=Tipo.CAMBIO_EQUIPO).exists())

    def test_un_jugador_no_puede_mover_a_otro(self):
        r = self.cambiar(self.jugador, self.otro.id, 'A')

        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.equipo_de(self.otro), 'B')

    def test_el_organizador_mueve_a_un_jugador_y_este_recibe_aviso(self):
        r = self.cambiar(self.organizador, self.jugador.id, 'B')

        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.notificados(Tipo.CAMBIO_EQUIPO), {self.jugador.id})

    def test_el_organizador_que_se_cambia_a_si_mismo_no_se_notifica(self):
        anotar(self.partido, self.organizador, 'A')

        r = self.cambiar(self.organizador, self.organizador.id, 'B')

        self.assertEqual(r.status_code, 200)
        self.assertFalse(Notificacion.objects.filter(tipo=Tipo.CAMBIO_EQUIPO).exists())

    def test_un_jugador_activo_no_puede_pasar_a_un_equipo_lleno(self):
        anotar(self.partido, crear_usuario(), 'B')  # B completo: otro + este

        r = self.cambiar(self.jugador, self.jugador.id, 'B')

        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.equipo_de(self.jugador), 'A')

    def test_un_jugador_en_espera_puede_cambiar_y_sigue_en_espera(self):
        anotar(self.partido, crear_usuario(), 'A')   # A completo
        anotar(self.partido, crear_usuario(), 'B')   # B completo
        espera = crear_usuario()
        anotar(self.partido, espera, 'A', estado='en_espera')

        r = self.cambiar(espera, espera.id, 'B')

        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.estado_de(espera), self.equipo_de(espera)), ('en_espera', 'B'))

    def test_un_jugador_en_espera_pasa_a_activo_si_el_otro_equipo_tiene_lugar(self):
        anotar(self.partido, crear_usuario(), 'A')   # A completo, B tiene un lugar
        espera = crear_usuario()
        anotar(self.partido, espera, 'A', estado='en_espera')

        self.cambiar(espera, espera.id, 'B')

        self.assertEqual((self.estado_de(espera), self.equipo_de(espera)), ('activo', 'B'))

    def test_datos_invalidos_devuelven_400(self):
        self.assertEqual(self.cambiar(self.jugador, self.jugador.id, 'C').status_code, 400)
        self.assertEqual(self.cambiar(self.jugador, 'abc', 'B').status_code, 400)
        self.assertEqual(self.cambiar(self.jugador, None, 'B').status_code, 400)

    def test_con_equipos_definidos_solo_el_organizador_puede_cambiar(self):
        self.partido.equipos_definidos = True
        self.partido.save()

        self.assertEqual(self.cambiar(self.jugador, self.jugador.id, 'B').status_code, 403)
        self.assertEqual(self.cambiar(self.organizador, self.jugador.id, 'B').status_code, 200)

    def test_con_resultado_nadie_puede_cambiar(self):
        Resultado.objects.create(
            partido=self.partido, equipo_a_goles=0, equipo_b_goles=0, cargado_por=self.organizador
        )

        self.assertEqual(self.cambiar(self.organizador, self.jugador.id, 'B').status_code, 400)

    def test_participante_inexistente_devuelve_404(self):
        self.assertEqual(self.cambiar(self.organizador, crear_usuario().id, 'B').status_code, 404)

    def test_no_se_puede_mover_a_un_baneado(self):
        self.accion('banear', self.organizador, {'usuario_id': self.jugador.id})

        self.assertEqual(self.cambiar(self.organizador, self.jugador.id, 'B').status_code, 400)