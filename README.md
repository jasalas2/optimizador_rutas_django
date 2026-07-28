# Optimizador de Rutas de Recolección — V9 (Django)

Aplicación web que calcula rutas óptimas de recolección para una flota de
camiones, con horarios estimados (incluyendo descarga, almuerzo y tope de
jornada), restricciones de capacidad, múltiples viajes por camión, cálculo
por Cantón/Distrito, frecuencia semanal por ruta, costo real por tonelada,
mapa interactivo, y exportación a formatos GIS y de navegación.

Es la migración a Django de la [v8 en Streamlit](https://github.com/jasalas2/optimizador_rutas)
— la lógica de cálculo (VRP con OR-Tools, modelo de tiempo, exportaciones,
red propia) se movió tal cual a `core/optimizador.py`, sin reescribirla.

---

## Metodología

El núcleo del sistema es un problema de ruteo de vehículos (VRP) resuelto
con OR-Tools (Google). Cada camión se modela con su propia capacidad, su
propio plantel (de dónde sale y a dónde vuelve cada día), y la posibilidad
de hacer varios viajes en el mismo día si se llena antes de terminar. Las
distancias entre puntos se calculan sobre la red vial real mediante OSRM,
en lugar de asumir línea recta.

Sobre esa base, el costo de operar las rutas se construye desde cinco
categorías (inversión, mano de obra, combustible, mantenimiento y
administrativa), cada una prorrateada a un equivalente diario, y se compara
contra lo que cuesta el modelo de recolección actual.

## Páginas

| Página | Qué hace |
|---|---|
| **Puntos** | Carga de paradas: nombre, dirección, coordenadas, peso (generación DIARIA, kg/día), cantón, distrito, camión asignado (opcional). Geocodifica direcciones, importa CSV, avisa si algún punto pesa más de lo que cualquier camión puede levantar en un solo viaje. |
| **Camiones** | Flota: capacidad, personas, viajes máximos por día, plantel (obligatorio), y Cantón/Distrito asignado (opcional, para restringir en qué zonas puede trabajar). |
| **Configuración** | Hora de inicio, velocidad, tiempos de parada/descarga, almuerzo, tope de jornada, planta de descarga. |
| **Calcular** | Modo de cálculo (todos los puntos juntos / por Distrito / por Cantón / mixto), mapa con selector de rutas y filtro por día de la semana, alerta si algún camión excede el tope de jornada, y "Frecuencia por ruta". |
| **Costos** | Comparación modelo actual vs. modelo nuevo; estructura completa de costos; costo real por tonelada calculado automáticamente. |
| **Exportar** | CSV, GeoJSON, Shapefile, GPX, KML, y links directos a Google Maps y Waze. |
| **Red propia** *(Beta)* | Rutea sobre un shapefile de calles propio en vez de la red pública de OpenStreetMap. |
| **Recolección en vía** *(Beta)* | Estima kilogramos adicionales según el tipo de calle que atraviesa cada ruta, con mapa de verificación por capas. Opcionalmente, ese kg extra puede sumarse como recolección real y reflejarse en Costos. |

## Frecuencia por ruta

**"Peso (kg)" en Puntos es la generación DIARIA (kg/día) de ese punto**, no
lo recolectado por visita. La frecuencia real de paso (una o varias veces
por semana) se asigna **después de calcular**, a la ruta ya armada:

1. En Calcular, la sección **"Frecuencia por ruta"** deja elegir los días
   de la semana en que pasa cada ruta resultante.
2. Con eso se arma una tabla con el **peso estimado de cada día de la
   semana**, calculado como los kg/día acumulados desde la recolección
   programada anterior de esa ruta (cíclico dentro de la semana).
3. El mapa se puede filtrar por día — mostrando solo las rutas asignadas a
   ese día.

Las asignaciones de días quedan guardadas en base de datos (`RutaFrecuencia`),
a diferencia de la v8 en Streamlit donde se perdían al reiniciar el servidor.

## Estructura del proyecto

```
core/optimizador.py    # lógica de cálculo pura (VRP, modelo de tiempo,
                        # exportaciones, red propia) — sin dependencias de
                        # Django ni de UI, con pruebas propias
core/tests/             # test_modelo_tiempo.py: escenarios del modelo de
                        # tiempo (almuerzo, tope de jornada, frecuencia)
rutas/models.py         # Punto, Camion, ConfiguracionGeneral, Costos*,
                        # ResultadoCalculo, RutaFrecuencia, Red propia, etc.
rutas/views.py          # una vista por página + endpoints de guardado/cálculo
rutas/templates/rutas/  # una plantilla por página, todas heredan de _base.html
rutas/static/rutas/     # style.css (sistema de diseño) y app.js (helpers compartidos)
```

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

La base de datos por defecto es SQLite (`db.sqlite3`, no versionado).

Antes de tocar `core/optimizador.py`, correr las pruebas:

```bash
python -m pytest core/tests/test_modelo_tiempo.py -q
```

## Pendientes

- Migrar de SQLite a Postgres para producción.
- Tests automáticos de las vistas de Django (hoy solo `core/` tiene pruebas).
- Revisar diseño responsive / mobile.
- Evaluar cálculo en background (Celery) si el volumen de datos crece.
