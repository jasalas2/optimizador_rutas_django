import datetime

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

LAT_VALIDATORS = [MinValueValidator(-90), MaxValueValidator(90)]
LON_VALIDATORS = [MinValueValidator(-180), MaxValueValidator(180)]


class ConfiguracionGeneral(models.Model):
    """Fila única (singleton, siempre pk=1) — reemplaza la barra lateral de
    Streamlit, que guardaba estos mismos valores en la tabla `config`."""
    hora_inicio = models.TimeField(default=datetime.time(8, 0))
    velocidad_kmh = models.PositiveIntegerField(default=40, validators=[MinValueValidator(10), MaxValueValidator(120)])
    tiempo_parada = models.PositiveIntegerField(default=10, validators=[MinValueValidator(1), MaxValueValidator(60)])
    tiempo_descarga = models.PositiveIntegerField(default=30, validators=[MinValueValidator(1), MaxValueValidator(120)])

    usar_almuerzo = models.BooleanField(default=False)
    hora_almuerzo_inicio = models.TimeField(default=datetime.time(12, 0))
    hora_almuerzo_fin = models.TimeField(default=datetime.time(13, 0))

    tope_horas_jornada = models.FloatField(default=8.0, validators=[MinValueValidator(1), MaxValueValidator(24)])

    velocidad_variable_via = models.BooleanField(default=False)
    velocidad_rapida_kmh = models.PositiveIntegerField(default=40, validators=[MinValueValidator(10), MaxValueValidator(150)])

    balancear = models.BooleanField(default=False)

    depot2_lat = models.FloatField(default=9.964356, validators=LAT_VALIDATORS)
    depot2_lon = models.FloatField(default=-84.161528, validators=LON_VALIDATORS)

    @classmethod
    def cargar(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class Camion(models.Model):
    nombre = models.CharField(max_length=200)
    capacidad_kg = models.FloatField(default=1000, validators=[MinValueValidator(1)])
    personas = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    viajes_max = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    plantel_lat = models.FloatField(null=True, blank=True, validators=LAT_VALIDATORS)
    plantel_lon = models.FloatField(null=True, blank=True, validators=LON_VALIDATORS)
    canton_asignado = models.CharField(max_length=200, blank=True, default="")
    distrito_asignado = models.CharField(max_length=200, blank=True, default="")

    def __str__(self):
        return self.nombre


class CamionDisponibilidad(models.Model):
    """Días de la semana en que un camión (identificado por NOMBRE, igual
    que RutaFrecuencia identifica rutas) puede usarse al calcular. Modelo
    aparte —no un campo en Camion— para no perderse cuando se vuelve a
    guardar la tabla de Camiones (que borra y recrea todas las filas)."""
    nombre_camion = models.CharField(max_length=200, unique=True)
    dias = models.CharField(max_length=200, blank=True, default="")

    def __str__(self):
        return self.nombre_camion


class ResultadoCalculo(models.Model):
    """Guarda el último resultado de calcular_rutas_para_puntos() (o la
    combinación de varios, si el modo de cálculo fue por zona), para no
    tener que recalcular al entrar a la pantalla de Resultados. Reemplaza
    st.session_state.resultados.

    Una fila por día de la semana (`dia` en DIAS_SEMANA) más una fila con
    dia="" para el cálculo "Todos" (sin filtrar la flota por disponibilidad
    — es el comportamiento original, de antes de que existiera el filtro
    por día), así conviven varios cálculos guardados a la vez en vez de
    pisarse entre sí."""
    dia = models.CharField(max_length=20, blank=True, default="", unique=True)
    resultado_json = models.JSONField(null=True, blank=True)
    modo_calculo = models.CharField(max_length=100, blank=True, default="")
    calculado_en = models.DateTimeField(null=True, blank=True)

    @classmethod
    def cargar(cls, dia=""):
        obj, _ = cls.objects.get_or_create(dia=dia)
        return obj


class RutaFrecuencia(models.Model):
    """Días de la semana asignados a una ruta YA CALCULADA (identificada por
    su nombre, ej. "Heredia — Camión 2"). Reemplaza
    st.session_state.dias_por_ruta -- en Streamlit se perdía al reiniciar el
    servidor; acá persiste."""
    nombre_ruta = models.CharField(max_length=300, unique=True)
    dias = models.CharField(max_length=200, blank=True, default="")

    def __str__(self):
        return self.nombre_ruta


FRECUENCIA_CHOICES = [("Día", "Día"), ("Semana", "Semana"), ("Mes", "Mes"), ("Año", "Año")]


class CostoInversion(models.Model):
    """Montos grandes (camión, garaje, otros), prorrateados por vida útil."""
    concepto = models.CharField(max_length=200)
    monto = models.FloatField(default=0, validators=[MinValueValidator(0)])
    vida_util_anios = models.FloatField(default=1, validators=[MinValueValidator(0.1)])

    def __str__(self):
        return self.concepto


class CostoRecurrente(models.Model):
    """Gastos periódicos (mantenimiento o administrativa), prorrateados por
    frecuencia. `tipo` separa las dos secciones de Streamlit sin duplicar el
    modelo."""
    TIPO_MANTENIMIENTO = "mantenimiento"
    TIPO_ADMINISTRATIVA = "administrativa"
    TIPO_CHOICES = [(TIPO_MANTENIMIENTO, "Mantenimiento"), (TIPO_ADMINISTRATIVA, "Administrativa")]

    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    concepto = models.CharField(max_length=200)
    monto = models.FloatField(default=0, validators=[MinValueValidator(0)])
    frecuencia = models.CharField(max_length=10, choices=FRECUENCIA_CHOICES, default="Mes")

    def __str__(self):
        return self.concepto


class CostosGenerales(models.Model):
    """Fila única (singleton, siempre pk=1) — insumos de la pestaña Costos
    que no son tablas editables (toneladas, mano de obra, combustible)."""
    ton_actual = models.FloatField(default=0, validators=[MinValueValidator(0)])
    precio_ton_actual = models.FloatField(default=0, validators=[MinValueValidator(0)])
    ton_nuevo_manual = models.FloatField(null=True, blank=True, validators=[MinValueValidator(0)])

    horas_laboradas = models.FloatField(default=8.0, validators=[MinValueValidator(0)])
    precio_hora = models.FloatField(default=0, validators=[MinValueValidator(0)])

    rendimiento = models.FloatField(default=5.0, validators=[MinValueValidator(0.1)])
    precio_litro = models.FloatField(default=0, validators=[MinValueValidator(0)])
    costo_km_extra = models.FloatField(default=0, validators=[MinValueValidator(0)])

    @classmethod
    def cargar(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class RedPropiaGrafo(models.Model):
    """Fila única (singleton, siempre pk=1) — grafo (networkx) construido a
    partir del shapefile de líneas subido por el usuario. No se puede guardar
    un networkx.Graph directamente en JSON, así que se serializa como nodos
    (lon, lat) + aristas (a, b, peso_m); se reconstruye el Graph en memoria
    cada vez que hace falta. Reemplaza st.session_state.red_propia_grafo."""
    nodos_json = models.JSONField(null=True, blank=True)
    aristas_json = models.JSONField(null=True, blank=True)
    n_lineas = models.IntegerField(default=0)
    n_componentes = models.IntegerField(default=0)
    tamano_componentes_json = models.JSONField(default=list, blank=True)

    @classmethod
    def cargar(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class RedPropiaPunto(models.Model):
    """Puntos a recorrer sobre la red propia — tabla independiente de
    `Punto` (la de Puntos/Camiones), igual que en Streamlit."""
    orden = models.PositiveIntegerField(default=0)
    nombre = models.CharField(max_length=200)
    latitud = models.FloatField(null=True, blank=True, validators=LAT_VALIDATORS)
    longitud = models.FloatField(null=True, blank=True, validators=LON_VALIDATORS)

    class Meta:
        ordering = ["orden", "id"]

    def __str__(self):
        return self.nombre


class RedPropiaResultado(models.Model):
    """Fila única (singleton, siempre pk=1) — último resultado calculado
    sobre la red propia."""
    resultado_json = models.JSONField(null=True, blank=True)

    @classmethod
    def cargar(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class TasaViaKgKm(models.Model):
    """Tasa de kg extra por km, una fila fija por tipo de vía OSM (no se
    agregan ni borran filas, igual que en Streamlit)."""
    tipo = models.CharField(max_length=20, unique=True)
    kg_extra_por_km = models.FloatField(default=0, validators=[MinValueValidator(0)])

    def __str__(self):
        return self.tipo


class ViaResultado(models.Model):
    """Fila única (singleton, siempre pk=1) — último resultado de
    'Recolección en vía' (solo lectura sobre Resultados, no toca
    ResultadoCalculo). Reemplaza st.session_state.resultado_via /
    tramos_via_mapa / sumar_a_recoleccion / detalle_progresivo_via."""
    resultados_via_json = models.JSONField(null=True, blank=True)
    tramos_mapa_json = models.JSONField(null=True, blank=True)
    sumar_a_recoleccion = models.BooleanField(default=False)
    detalle_progresivo_json = models.JSONField(null=True, blank=True)

    @classmethod
    def cargar(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class Punto(models.Model):
    nombre = models.CharField(max_length=200)
    direccion = models.CharField(max_length=500, blank=True, default="")
    latitud = models.FloatField(null=True, blank=True, validators=LAT_VALIDATORS)
    longitud = models.FloatField(null=True, blank=True, validators=LON_VALIDATORS)
    peso_kg = models.FloatField(default=0, validators=[MinValueValidator(0)])
    camion_asignado = models.ForeignKey(
        Camion, null=True, blank=True, on_delete=models.SET_NULL, related_name="puntos"
    )
    canton = models.CharField(max_length=200, blank=True, default="")
    distrito = models.CharField(max_length=200, blank=True, default="")

    def __str__(self):
        return self.nombre
