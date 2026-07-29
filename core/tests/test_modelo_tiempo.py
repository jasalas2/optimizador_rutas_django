"""
Prueba standalone del modelo de tiempo (descarga + almuerzo + tope 8h + dia)
contra core/optimizador.py -- el modulo extraido tal cual desde app.py
(Streamlit, v8) durante la migracion a Django v9.

Version original (exec-slicing sobre app.py) en modelo_rutas/test_modelo_tiempo.py.
Aca se importa el modulo real en vez de re-extraer texto, porque ya no hay
un app.py monolitico del que extraer.

Mockea obtener_matriz_osrm / obtener_ruta_completa_osrm para no requerir red.
"""
from datetime import datetime as dt
from datetime import time as dtime
from datetime import timedelta as td
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

    filas_sale_vacio = [f for f in resumen if f["tipo"] == "sale_vacio"]
    assert len(filas_sale_vacio) == 1, f"Se esperaba 1 fila 'Sale vacío', hubo {len(filas_sale_vacio)}"
    hora_descarga = dt.strptime(filas_descarga[0]["Hora llegada"], "%H:%M")
    hora_sale_vacio = dt.strptime(filas_sale_vacio[0]["Hora llegada"], "%H:%M")
    minutos_descarga = (hora_sale_vacio - hora_descarga).total_seconds() / 60
    assert minutos_descarga == 30, f"'Sale vacío' debería ser 30 min después de la descarga, fue {minutos_descarga}"
    print(f"✔ 'Sale vacío' registrado {minutos_descarga:.0f} min después de la descarga, a las {filas_sale_vacio[0]['Hora llegada']}")

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

    # ═══════════════════════════════════════════════════════════
    # Escenario 5: el almuerzo SIEMPRE dura la duración completa
    # configurada (fin - inicio de la ventana), sin importar en qué
    # momento de la ventana arranca. Antes, si el camión llegaba tarde
    # DENTRO de la ventana (ej. 12:29 con cierre a las 12:30), el
    # almuerzo se acortaba a solo 1 minuto en vez de la hora completa.
    # ═══════════════════════════════════════════════════════════
    hora_llegada_tardia = dt(2026, 1, 1, 12, 19)  # +10 min de parada -> 12:29
    _, tomado5, fila5 = core.avanzar_reloj_tras_parada(
        hora_llegada_tardia, False, 10, 30, False, dtime(11, 30), dtime(12, 30),
    )
    assert tomado5 is True
    duracion5 = fila5["fin"] - fila5["inicio"]
    assert duracion5 == td(hours=1), f"El almuerzo debería durar 1h completa, duró {duracion5}"
    print(f"\n✔ Almuerzo iniciado casi al cierre de la ventana (12:29) igual dura {duracion5}, de "
          f"{fila5['inicio'].strftime('%H:%M')} a {fila5['fin'].strftime('%H:%M')}")

    # ═══════════════════════════════════════════════════════════
    # Escenario 6: el almuerzo no se salta si el mediodía cae DURANTE el
    # tramo final de regreso al plantel (y no justo en una parada
    # anterior) — antes, ese tramo no se revisaba y el camión se quedaba
    # sin almorzar ese día.
    # ═══════════════════════════════════════════════════════════
    puntos_cercanos = pd.DataFrame({
        "Nombre": ["MuyCerca"], "Latitud": [9.9645], "Longitud": [-84.1613],
        "Peso (kg)": [500], "Camión": ["Auto"],
    })
    camiones_plantel_lejos = pd.DataFrame({
        "Nombre": ["Camión X"], "Capacidad (kg)": [5000.0], "Personas": [1],
        "Viajes máx.": [1], "Plantel Lat": [10.04], "Plantel Lon": [-84.16],
    })
    resultado6, error6 = core.calcular_rutas_para_puntos(
        puntos_cercanos, camiones_plantel_lejos, depot2_lat=9.964356, depot2_lon=-84.161528,
        hora_inicio=dtime(11, 20), velocidad_kmh=20, tiempo_parada=5,
        balancear=False, tiempo_descarga=10,
        hora_almuerzo_inicio=dtime(12, 0), hora_almuerzo_fin=dtime(13, 0),
        tope_horas_jornada=8.0,
    )
    assert error6 is None, f"Error inesperado: {error6}"
    resumen6 = resultado6["camiones"][0]["resumen"]
    tipos6 = [f["tipo"] for f in resumen6]
    idx_descarga6 = tipos6.index("descarga")
    assert resumen6[idx_descarga6]["Hora llegada"] < "12:00", (
        "este escenario requiere que la descarga sea ANTES del mediodía, "
        "para que el cruce ocurra recién en el tramo de regreso al plantel"
    )
    assert "almuerzo" in tipos6, "el almuerzo no debería saltarse en el tramo final de regreso al plantel"
    print(f"✔ Almuerzo no se salta en el tramo final de regreso al plantel "
          f"(descarga a las {resumen6[idx_descarga6]['Hora llegada']}, almuerzo sí insertado)")

    print("\n=== TODOS LOS ESCENARIOS PASARON ===")


if __name__ == "__main__":
    test_todos_los_escenarios()
