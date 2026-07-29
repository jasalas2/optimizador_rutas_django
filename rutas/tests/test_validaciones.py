"""
Escenarios de validación de datos: límites de los modelos (Punto, Camion,
ConfiguracionGeneral) y el comportamiento "todo o nada" de las vistas de
guardado (una fila inválida rechaza el lote completo, sin tocar lo que ya
había guardado).

Corre sobre la base de datos de TEST que crea `manage.py test` (efímera, en
memoria) — nunca toca `db.sqlite3` (los datos reales de Puntos/Camiones).

    python manage.py test rutas.tests.test_validaciones
"""
import json

from django.core.exceptions import ValidationError
from django.test import Client, TestCase

from rutas.models import Camion, ConfiguracionGeneral, Punto


class PuntoValidacionesTests(TestCase):
    def test_latitud_fuera_de_rango_rechazada(self):
        p = Punto(nombre="Fuera de CR", latitud=95, longitud=-84, peso_kg=100)
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_longitud_fuera_de_rango_rechazada(self):
        p = Punto(nombre="Fuera de CR", latitud=9.9, longitud=-190, peso_kg=100)
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_peso_negativo_rechazado(self):
        p = Punto(nombre="Peso negativo", latitud=9.9, longitud=-84.1, peso_kg=-50)
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_punto_valido_no_lanza_error(self):
        p = Punto(nombre="Válido", latitud=9.9, longitud=-84.1, peso_kg=100)
        p.full_clean()  # no debería lanzar


class CamionValidacionesTests(TestCase):
    def test_capacidad_cero_rechazada(self):
        c = Camion(nombre="Sin capacidad", capacidad_kg=0, plantel_lat=9.9, plantel_lon=-84.1)
        with self.assertRaises(ValidationError):
            c.full_clean()

    def test_viajes_max_cero_rechazado(self):
        c = Camion(
            nombre="Sin viajes", capacidad_kg=1000, viajes_max=0,
            plantel_lat=9.9, plantel_lon=-84.1,
        )
        with self.assertRaises(ValidationError):
            c.full_clean()

    def test_plantel_fuera_de_rango_rechazado(self):
        c = Camion(nombre="Plantel raro", capacidad_kg=1000, plantel_lat=9.9, plantel_lon=200)
        with self.assertRaises(ValidationError):
            c.full_clean()

    def test_camion_valido_no_lanza_error(self):
        c = Camion(nombre="Válido", capacidad_kg=1000, plantel_lat=9.9, plantel_lon=-84.1)
        c.full_clean()  # no debería lanzar


class ConfiguracionGeneralValidacionesTests(TestCase):
    def test_velocidad_fuera_de_rango_rechazada(self):
        config = ConfiguracionGeneral(velocidad_kmh=500)
        with self.assertRaises(ValidationError):
            config.full_clean()

    def test_tope_horas_jornada_fuera_de_rango_rechazado(self):
        config = ConfiguracionGeneral(tope_horas_jornada=30)
        with self.assertRaises(ValidationError):
            config.full_clean()

    def test_configuracion_por_defecto_es_valida(self):
        ConfiguracionGeneral().full_clean()  # no debería lanzar


class GuardadoAtomicoPuntosTests(TestCase):
    """El endpoint de guardar Puntos reemplaza toda la tabla — pero solo si
    TODAS las filas son válidas. Si una sola fila tiene datos fuera de
    rango, no se guarda nada (ni las filas buenas del mismo lote, ni se
    pierden las que ya estaban)."""

    def setUp(self):
        self.client = Client()
        Punto.objects.create(nombre="Existente", latitud=9.9, longitud=-84.1, peso_kg=100)

    def test_lote_con_una_fila_invalida_no_guarda_nada(self):
        filas = [
            {"Nombre": "Bueno", "Latitud": 9.9, "Longitud": -84.1, "Peso (kg)": 500},
            {"Nombre": "Malo", "Latitud": 999, "Longitud": -84.1, "Peso (kg)": 500},
        ]
        resp = self.client.post(
            "/puntos/guardar/", data=json.dumps({"filas": filas}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("errores", resp.json())
        # La fila "Existente" de antes del POST debe seguir intacta —
        # el rechazo no debe haber borrado nada.
        self.assertEqual(Punto.objects.count(), 1)
        self.assertEqual(Punto.objects.first().nombre, "Existente")

    def test_lote_valido_reemplaza_la_tabla(self):
        filas = [
            {"Nombre": "Nuevo 1", "Latitud": 9.9, "Longitud": -84.1, "Peso (kg)": 500},
            {"Nombre": "Nuevo 2", "Latitud": 10.0, "Longitud": -84.2, "Peso (kg)": 300},
        ]
        resp = self.client.post(
            "/puntos/guardar/", data=json.dumps({"filas": filas}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        nombres = set(Punto.objects.values_list("nombre", flat=True))
        self.assertEqual(nombres, {"Nuevo 1", "Nuevo 2"})  # "Existente" quedó reemplazado


class GuardadoAtomicoCamionesTests(TestCase):
    def setUp(self):
        self.client = Client()
        Camion.objects.create(
            nombre="Existente", capacidad_kg=1000, plantel_lat=9.9, plantel_lon=-84.1,
        )

    def test_lote_con_capacidad_invalida_no_guarda_nada(self):
        filas = [
            {"Nombre": "Bueno", "Capacidad (kg)": 1000, "Plantel Lat": 9.9, "Plantel Lon": -84.1},
            {"Nombre": "Malo", "Capacidad (kg)": -5, "Plantel Lat": 9.9, "Plantel Lon": -84.1},
        ]
        resp = self.client.post(
            "/camiones/guardar/", data=json.dumps({"filas": filas}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(Camion.objects.count(), 1)
        self.assertEqual(Camion.objects.first().nombre, "Existente")
