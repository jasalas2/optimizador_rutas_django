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
| **Calcular** | Selector de día ("¿Para qué día calculás?"), camiones disponibles ese día, y modo de cálculo (todos los puntos juntos / por Distrito / por Cantón / mixto). Cada día guarda su propio resultado — se puede armar Lunes con una flota y Martes con otra sin que se pisen. |
| **Resultados** | Mapa con selector de rutas, "Frecuencia por ruta", detalle por camión, y alerta si algún camión excede el tope de jornada. Tiene su propio selector de día para repasar cualquier cálculo ya guardado sin recalcular. |
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

Cuando el cálculo es para un **día específico** (no "Todos") y una ruta ya
tiene frecuencia multi-día guardada, el sistema no se limita a mostrar el
peso acumulado — **vuelve a resolver ese camión** con el peso real de ese
día y tantos viajes como hagan falta para cubrirlo (el VRP no puede partir
el peso de un mismo punto entre dos viajes, así que un punto que por sí
solo supera la capacidad se parte en "copias" en la misma ubicación, para
simular la vuelta real del camión).

## Camiones disponibles por día

Cada camión puede tener días de la semana específicos en los que trabaja
(pestaña Camiones → "Días disponibles"). Sin ningún día marcado, el camión
está disponible todos los días (comportamiento por defecto). Al calcular
para un día puntual, la flota se filtra automáticamente por esa
disponibilidad, y el resultado de ese día queda guardado aparte —
`ResultadoCalculo` tiene una fila por día de la semana, más una para
"Todos" (el cálculo único de toda la semana, comportamiento original).

## Estructura del proyecto

```
core/optimizador.py    # lógica de cálculo pura (VRP, modelo de tiempo,
                        # exportaciones, red propia) — sin dependencias de
                        # Django ni de UI, con pruebas propias
core/tests/             # test_modelo_tiempo.py: escenarios del modelo de
                        # tiempo (almuerzo, tope de jornada, frecuencia)
rutas/models.py         # Punto, Camion, CamionDisponibilidad, ConfiguracionGeneral,
                        # Costos*, ResultadoCalculo, RutaFrecuencia, Red propia, etc.
rutas/views.py          # una vista por página + endpoints de guardado/cálculo
rutas/templates/rutas/  # una plantilla por página (Calcular y Resultados separadas),
                        # todas heredan de _base.html; _dias_tabs.html es el selector
                        # de día compartido entre Calcular y Resultados
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

Antes de tocar `core/optimizador.py`, correr las pruebas (ver sección Tests).

## Actualización — 2026-07-28

**Cálculo por día de la semana, con flota propia y recálculo real de acumulados**
- Nueva página **Resultados**, separada de **Calcular** — antes todo vivía en
  una sola pantalla (modo de cálculo, mapa, detalle por camión, frecuencia
  por ruta) y quedaba saturada.
- Camiones ahora pueden tener días de la semana específicos en los que
  trabajan (`CamionDisponibilidad`). Calcular filtra la flota según el día
  elegido y guarda un resultado por día (`ResultadoCalculo.dia`) — Lunes y
  Martes pueden tener flotas y resultados distintos sin pisarse.
- Cuando una ruta con frecuencia multi-día (ej. pasa cada 4 días) se calcula
  para un día específico, el sistema ya no se queda con el peso diario
  base: **vuelve a resolver ese camión con el peso real acumulado**,
  abriendo tantos viajes como hagan falta — incluso partiendo el peso de un
  mismo punto en "copias" cuando por sí solo supera la capacidad de un
  viaje (el VRP no soporta entregas parciales de un punto entre dos viajes).
- Nueva fila **"Sale vacío"** en el detalle por camión, con la hora real en
  que termina la descarga (usa el tiempo de descarga configurado) y sale
  rumbo al siguiente viaje o de vuelta al plantel.
- El depot de llegada ahora se muestra como **"Planta San Antonio"** en vez
  de "DEPOT LLEGADA".

**Correcciones**
- El almuerzo ahora respeta siempre la **duración completa** configurada
  (antes, si el camión llegaba tarde dentro de la ventana, el descanso
  podía durar solo unos minutos en vez de la hora completa).
- El almuerzo también se revisa en el tramo final de regreso al plantel —
  antes, si el mediodía caía justo ahí, se saltaba por completo.
- Arreglada la columna **"Viajes máx."** en Camiones, que se guardaba bien
  en la base de datos pero se mostraba vacía en la tabla (Tabulator
  interpretaba el punto final del nombre de columna como un separador de
  campo anidado).
- El filtro de frecuencia por día (dentro de "Mapa de rutas") ya no se
  puede aplicar dos veces sobre un resultado que ya es específico de un
  día — antes eso inflaba el peso mostrado (ej. 200% de carga en vez del
  87% real).

**Diseño**
- Tipografía Inter, bordes más redondeados, modo oscuro más profundo
  (inspirado en guildstats.eu) manteniendo el modo claro como default.
- Tablas con encabezados en negrita, divisores de columna más visibles, y
  tipografía más grande en general.

**Tests**
- 3 escenarios nuevos en `core/tests/test_modelo_tiempo.py`: duración
  completa del almuerzo, almuerzo no salteado en el tramo final, y la fila
  "Sale vacío".
- Suite nueva `rutas/tests/test_validaciones.py` (Django, base de datos de
  test en memoria): límites de los modelos y guardado "todo o nada" de
  Puntos/Camiones.

## Tests

```bash
python -m pytest core/tests/test_modelo_tiempo.py -q   # lógica pura (core/)
python manage.py test rutas                             # validaciones y vistas (Django)
```

`rutas/tests/test_validaciones.py` cubre los límites de los modelos (Punto,
Camion, ConfiguracionGeneral) y el comportamiento "todo o nada" de guardar
Puntos/Camiones (una fila inválida rechaza el lote completo sin tocar lo que
ya había). Corre sobre la base de datos de test que crea Django (en
memoria) — nunca toca `db.sqlite3`.

## Pendientes

- Migrar de SQLite a Postgres para producción.
- Ampliar los tests de Django a más vistas (Calcular/Resultados, Costos,
  Red propia) — hoy solo cubren las validaciones de guardado.
- Revisar diseño responsive / mobile.
- Evaluar cálculo en background (Celery) si el volumen de datos crece.
