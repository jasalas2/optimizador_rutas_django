"""
Prueba standalone del modelo de tiempo (descarga + almuerzo + tope 8h + dia)
contra core/optimizador.py -- el modulo extraido tal cual desde app.py
(Streamlit, v8) durante la migracion a Django v9.

Version original (exec-slicing sobre app.py) en modelo_rutas/test_modelo_tiempo.py.
Aca se importa el modulo real en vez de re-extraer texto, porque ya no hay
un app.py monolitico del que extraer.

Mockea obtener_matriz_osrm / obtener_ruta_completa_osrm para no requerir red.
"""
from datetime import time as dtime
from unittest import mock

import pandas as pd

from core import optimizador as core


def mock_obtener_matriz_osrm(locations):
    n = len(locations)
    dist = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                dist[i][j] = core.haversine(locations[i], locations[j])
    return dist, False, "sandbox sin acceso a OSRM (esperado, cae a linea recta)"


def mock_obtener_ruta_completa_osrm(stops):
    camino = list(stops)
    dist_legs = [core.haversine(stops[i], stops[i + 1]) for i in range(len(stops) - 1)]
    return camino, dist_legs, None


puntos = pd.DataFrame({
    "Nombre": ["P1", "P2", "P3"],
    "Latitud": [9.95, 9.90, 9.85],
    "Longitud": [-84.10, -84.05, -84.00],
    "Peso (kg)": [100, 100, 100],
    "Camión": ["Auto"] * 3,
})
camiones = pd.DataFrame({
    "Nombre": ["Camión 1"],
    "Capacidad (kg)": [1000.0],
    "Personas": [1],
    "Viajes máx.": [1],
    "Plantel Lat": [9.964356],
    "Plantel Lon": [-84.161528],
})


@mock.patch.object(core, "obtener_ruta_completa_osrm", side_effect=mock_obtener_ruta_completa_osrm)
@mock.patch.object(core, "obtener_matriz_osrm", side_effect=mock_obtener_matriz_osrm)
def test_todos_los_escenarios(mock_matriz, mock_ruta):
    print("Funciones cargadas OK desde core.optimizador\n")

    # ═══════════════════════════════════════════════════════════
    # Escenario 1: puntos lejos entre sí, cruzando el mediodía,
    # para verificar que se inserta el almuerzo UNA vez y que la
    # descarga usa tiempo_descarga (30 min) y no tiempo_parada (10 min).
    # ═══════════════════════════════════════════════════════════
    resultado, error = core.calcular_rutas_para_puntos(
        puntos, camiones, depot2_lat=9.964356, depot2_lon=-84.161528,
        hora_inicio=dtime(11, 30),  # sale a las 11:30 -> debería cruzar el mediodía
        velocidad_kmh=20,  # lento a propósito, para forzar cruce de las 12:00
        tiempo_parada=10, balancear=False,
        tiempo_descarga=30,
        hora_almuerzo_inicio=dtime(12, 0), hora_almuerzo_fin=dtime(13, 0),
        tope_horas_jornada=8.0,
    )
    assert error is None, f"Error inesperado: {error}"
    c = resultado["camiones"][0]
    resumen = c["resumen"]

    filas_almuerzo = [f for f in resumen if f["tipo"] == "almuerzo"]
    assert len(filas_almuerzo) == 1, f"Se esperaba 1 fila de almuerzo, hubo {len(filas_almuerzo)}"
    print(f"✔ Almuerzo insertado exactamente 1 vez, a las {filas_almuerzo[0]['Hora llegada']}")

    filas_descarga = [f for f in resumen if f["tipo"] == "descarga"]
    assert len(filas_descarga) == 1
    print(f"✔ Descarga registrada: {filas_descarga[0]['Parada']} a las {filas_descarga[0]['Hora llegada']}")

    for f in resumen:
        print(f"   {f['orden']:>2} | {f['tipo']:<9} | {f['Hora llegada']} | {f['Parada']}")
    print(f"   -> Hora fin: {c['hora_fin']} | horas_jornada={c['horas_jornada']} | excede_jornada={c['excede_jornada']}")

    # ═══════════════════════════════════════════════════════════
    # Escenario 2: SIN almuerzo activado (hora_almuerzo_inicio=None) ->
    # debe reproducir el comportamiento de siempre, sin fila de almuerzo,
    # y usando tiempo_parada de siempre.
    # ═══════════════════════════════════════════════════════════
    resultado2, error2 = core.calcular_rutas_para_puntos(
        puntos, camiones, depot2_lat=9.964356, depot2_lon=-84.161528,
        hora_inicio=dtime(11, 30), velocidad_kmh=20,
        tiempo_parada=10, balancear=False,
        tiempo_descarga=None,  # None -> debe caer a tiempo_parada, como antes
        hora_almuerzo_inicio=None, hora_almuerzo_fin=None,
        tope_horas_jornada=8.0,
    )
    assert error2 is None
    resumen2 = resultado2["camiones"][0]["resumen"]
    assert not any(f["tipo"] == "almuerzo" for f in resumen2), "No debería haber almuerzo si está desactivado"
    print("\n✔ Con almuerzo desactivado (None), no se inserta ninguna fila de almuerzo")

    # ═══════════════════════════════════════════════════════════
    # Escenario 3: tope de jornada MUY bajo (0.1 h) a propósito, para
    # forzar excede_jornada=True y confirmar que el cálculo NO se bloquea
    # (sigue devolviendo resultado, solo con la marca puesta).
    # ═══════════════════════════════════════════════════════════
    resultado3, error3 = core.calcular_rutas_para_puntos(
        puntos, camiones, depot2_lat=9.964356, depot2_lon=-84.161528,
        hora_inicio=dtime(8, 0), velocidad_kmh=20,
        tiempo_parada=10, balancear=False, tiempo_descarga=30,
        hora_almuerzo_inicio=None, hora_almuerzo_fin=None,
        tope_horas_jornada=0.1,
    )
    assert error3 is None, "El tope de jornada NUNCA debe bloquear el cálculo (restricción blanda)"
    c3 = resultado3["camiones"][0]
    assert c3["excede_jornada"] is True
    print(f"✔ Tope de 0.1h forzado a propósito: excede_jornada={c3['excede_jornada']} "
          f"(horas_jornada={c3['horas_jornada']}) y el cálculo NO se bloqueó")

    # ═══════════════════════════════════════════════════════════
    # Escenario 4: frecuencia por RUTA (asignada después de calcular),
    # no por punto — reemplaza el diseño anterior.
    # ═══════════════════════════════════════════════════════════
    assert core.peso_estimado_ruta_para_dia(3000, "Lunes,Jueves", "Jueves") == 9000
    assert core.peso_estimado_ruta_para_dia(3000, "Lunes,Jueves", "Lunes") == 12000
    assert core.peso_estimado_ruta_para_dia(1000, "", "Lunes") == 1000  # sin asignar -> tal cual
    print("\n✔ Frecuencia por ruta (post-cálculo): Jueves=9000kg, Lunes=12000kg, "
          "sin asignar=1000kg tal cual")

    print("\n=== TODOS LOS ESCENARIOS PASARON ===")


if __name__ == "__main__":
    test_todos_los_escenarios()
