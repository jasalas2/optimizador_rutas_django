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

    # ═══════════════════════════════════════════════════════════
    # Escenario 7: multiplicador_horario -- caso base (sin franjas, o
    # franjas=None) debe dar 1.0 siempre, sin importar la hora. Fuera de
    # cualquier franja configurada, también debe dar 1.0.
    # ═══════════════════════════════════════════════════════════
    assert core.multiplicador_horario(dtime(7, 30), None) == 1.0
    assert core.multiplicador_horario(dtime(7, 30), []) == 1.0
    franjas7 = [{"inicio": "07:00", "fin": "09:00", "multiplicador": 0.6}]
    assert core.multiplicador_horario(dtime(6, 59), franjas7) == 1.0, "justo antes de la franja debe ser 1.0"
    assert core.multiplicador_horario(dtime(9, 0), franjas7) == 1.0, "el fin de franja es exclusivo"
    assert core.multiplicador_horario(dtime(8, 0), franjas7) == 0.6, "dentro de la franja debe aplicar el multiplicador"
    print("✔ multiplicador_horario: 1.0 fuera de cualquier franja, el multiplicador configurado adentro")

    # Dos franjas superpuestas para la misma hora -- debe ganar la más
    # restrictiva (multiplicador más bajo), para no subestimar el tráfico.
    franjas7b = [
        {"inicio": "07:00", "fin": "10:00", "multiplicador": 0.7},
        {"inicio": "08:00", "fin": "09:00", "multiplicador": 0.4},
    ]
    assert core.multiplicador_horario(dtime(8, 30), franjas7b) == 0.4
    print("✔ multiplicador_horario: con franjas superpuestas, gana la más restrictiva")

    # ═══════════════════════════════════════════════════════════
    # Escenario 8: franjas_trafico end-to-end -- el mismo cálculo con una
    # franja de tráfico activa en la hora de salida debe tardar MÁS en
    # llegar a la primera parada que el mismo cálculo sin franjas (todo lo
    # demás igual), y franjas_trafico=None debe reproducir EXACTAMENTE el
    # cálculo de siempre (mismo resultado que sin pasar el parámetro).
    # ═══════════════════════════════════════════════════════════
    kwargs_base = dict(
        depot2_lat=9.964356, depot2_lon=-84.161528,
        hora_inicio=dtime(7, 0), velocidad_kmh=30,
        tiempo_parada=10, balancear=False, tiempo_descarga=30,
        hora_almuerzo_inicio=None, hora_almuerzo_fin=None, tope_horas_jornada=8.0,
    )
    resultado_sin_franjas, error_sf = core.calcular_rutas_para_puntos(
        puntos, camiones, **kwargs_base, franjas_trafico=None,
    )
    resultado_con_franjas, error_cf = core.calcular_rutas_para_puntos(
        puntos, camiones, **kwargs_base,
        franjas_trafico=[{"inicio": "07:00", "fin": "09:00", "multiplicador": 0.5}],
    )
    assert error_sf is None and error_cf is None

    resumen_sin = resultado_sin_franjas["camiones"][0]["resumen"]
    resumen_con = resultado_con_franjas["camiones"][0]["resumen"]
    primera_parada_sin = next(f for f in resumen_sin if f["tipo"] == "parada")
    primera_parada_con = next(f for f in resumen_con if f["tipo"] == "parada")
    assert primera_parada_con["Hora llegada"] > primera_parada_sin["Hora llegada"], (
        "con la franja de tráfico activa en la hora de salida, debería llegar MÁS tarde a la primera parada"
    )
    print(f"✔ franjas_trafico: con tráfico (x0.5) llega a las {primera_parada_con['Hora llegada']}, "
          f"sin tráfico llega a las {primera_parada_sin['Hora llegada']} (misma ruta, mismos datos)")

    # franjas_trafico=None debe dar EXACTAMENTE el mismo resultado que no
    # pasar el parámetro (compatibilidad hacia atrás).
    resultado_default, error_def = core.calcular_rutas_para_puntos(puntos, camiones, **kwargs_base)
    assert resultado_default["camiones"][0]["resumen"] == resumen_sin, (
        "franjas_trafico=None (default) debe reproducir EXACTAMENTE el cálculo de siempre"
    )
    print("✔ franjas_trafico=None (default) reproduce exactamente el cálculo de siempre")

    # ═══════════════════════════════════════════════════════════
    # Escenario 9: recalcular_camion_manual -- debe reproducir EXACTAMENTE
    # los mismos números (distancia, hora de llegada a cada parada, hora
    # fin) que calcular_rutas_para_puntos cuando se le da el MISMO orden de
    # paradas que decidió el solver -- confirma que el recálculo manual
    # (arrastrar y soltar en la Línea de tiempo de Resultados) usa la
    # misma lógica de fondo, no una aproximación distinta.
    # ═══════════════════════════════════════════════════════════
    resultado_normal, error_n = core.calcular_rutas_para_puntos(puntos, camiones, **kwargs_base)
    assert error_n is None
    c_normal = resultado_normal["camiones"][0]
    paradas_normales = [f for f in c_normal["resumen"] if f["tipo"] == "parada"]

    def _viajes_desde_paradas(paradas):
        return [[
            {"nombre": f["Nombre"], "lat": f["lat"], "lon": f["lon"], "peso_kg": f["Peso recogido (kg)"]}
            for f in paradas
        ]]

    camion_fila = camiones.iloc[0]

    def _recalcular(viajes, capacidad_kg=None):
        return core.recalcular_camion_manual(
            nombre_camion=camion_fila["Nombre"], viajes=viajes,
            plantel_lonlat=(camion_fila["Plantel Lon"], camion_fila["Plantel Lat"]),
            depot_lonlat=(kwargs_base["depot2_lon"], kwargs_base["depot2_lat"]),
            nombre_depot="Planta San Antonio",
            capacidad_kg=capacidad_kg if capacidad_kg is not None else camion_fila["Capacidad (kg)"],
            personas=camion_fila["Personas"], viajes_max=camion_fila["Viajes máx."],
            hora_inicio=kwargs_base["hora_inicio"], velocidad_kmh=kwargs_base["velocidad_kmh"],
            tiempo_parada=kwargs_base["tiempo_parada"], tiempo_descarga=kwargs_base["tiempo_descarga"],
            hora_almuerzo_inicio=kwargs_base["hora_almuerzo_inicio"],
            hora_almuerzo_fin=kwargs_base["hora_almuerzo_fin"],
            tope_horas_jornada=kwargs_base["tope_horas_jornada"], franjas_trafico=None,
        )

    resultado_manual, errores_manual = _recalcular(_viajes_desde_paradas(paradas_normales))
    assert errores_manual == []
    assert resultado_manual["hora_fin"] == c_normal["hora_fin"], "mismo orden -> misma hora de fin"
    assert round(resultado_manual["dist_total_m"], 2) == round(c_normal["dist_total_m"], 2), (
        "mismo orden -> misma distancia total"
    )
    paradas_manual = [f for f in resultado_manual["resumen"] if f["tipo"] == "parada"]
    assert [f["Hora llegada"] for f in paradas_manual] == [f["Hora llegada"] for f in paradas_normales]
    print("✔ recalcular_camion_manual: con el MISMO orden que decidió el solver, reproduce "
          f"exactamente los mismos números (hora fin={resultado_manual['hora_fin']}, "
          f"distancia={resultado_manual['dist_total_m']:.0f}m)")

    # Reordenado a mano (invertido) -- debe seguir dando un resultado
    # válido y consistente (sin errores), con el peso por viaje bien
    # calculado, aunque el número cambie.
    resultado_invertido, errores_inv = _recalcular(_viajes_desde_paradas(list(reversed(paradas_normales))))
    assert errores_inv == []
    assert resultado_invertido["peso_total_por_viaje_kg"] == [
        sum(f["Peso recogido (kg)"] for f in paradas_normales)
    ]
    print(f"✔ recalcular_camion_manual: con un orden distinto a mano (invertido), da un resultado "
          f"válido igual (distancia={resultado_invertido['dist_total_m']:.0f}m)")

    # ═══════════════════════════════════════════════════════════
    # Escenario 10: agrupar_paradas_por_capacidad -- si al mover paradas a
    # mano un viaje queda con más peso del que el camión puede cargar de
    # una vez, deben armarse MÁS VIAJES automáticamente (no simular un
    # viaje imposible con todo el peso junto).
    # ═══════════════════════════════════════════════════════════
    paradas_100kg = [{"nombre": f"P{i}", "lat": 9.9, "lon": -84.1, "peso_kg": 100} for i in range(5)]
    grupos = core.agrupar_paradas_por_capacidad(paradas_100kg, capacidad_kg=250)
    assert [len(g) for g in grupos] == [2, 2, 1], (
        f"con capacidad 250 y paradas de 100kg, deberían quedar grupos de 2, 2 y 1, dio {[len(g) for g in grupos]}"
    )
    assert all(sum(p["peso_kg"] for p in g) <= 250 for g in grupos)
    print("✔ agrupar_paradas_por_capacidad: reparte en más viajes sin pasarse de la capacidad, en el orden dado")

    # Una parada que sola ya supera la capacidad queda en su propio viaje
    # (no hay forma de evitarlo) sin romper el agrupamiento de las demás.
    paradas_con_gigante = [
        {"nombre": "chica", "lat": 9.9, "lon": -84.1, "peso_kg": 50},
        {"nombre": "gigante", "lat": 9.9, "lon": -84.1, "peso_kg": 500},
        {"nombre": "otra_chica", "lat": 9.9, "lon": -84.1, "peso_kg": 50},
    ]
    grupos_gigante = core.agrupar_paradas_por_capacidad(paradas_con_gigante, capacidad_kg=100)
    assert len(grupos_gigante) == 3, f"la parada gigante debe quedar sola en su propio viaje, dio {len(grupos_gigante)} grupos"
    print("✔ agrupar_paradas_por_capacidad: una parada que sola supera la capacidad no rompe el resto")

    # End-to-end: recalcular_camion_manual con una capacidad chica a
    # propósito (150kg, paradas de 100kg cada una) fuerza más viajes que
    # los configurados (viajes_max=1) -- debe avisar con
    # excede_viajes_max=True, sin bloquear el recálculo.
    resultado_forzado, errores_forzado = _recalcular(
        _viajes_desde_paradas(paradas_normales), capacidad_kg=150,
    )
    assert errores_forzado == []
    assert resultado_forzado["n_viajes_usados"] == 3, (
        f"con capacidad 150 y 3 paradas de 100kg cada una, deberían salir 3 viajes, "
        f"dio {resultado_forzado['n_viajes_usados']}"
    )
    assert resultado_forzado["excede_viajes_max"] is True, (
        "3 viajes necesarios > viajes_max=1 configurado -> debe avisar, y debe ser bool de Python "
        "(no numpy.bool_, que no siempre serializa bien a JSON)"
    )
    print(f"✔ recalcular_camion_manual: capacidad chica fuerza más viajes automáticamente "
          f"({resultado_forzado['n_viajes_usados']} viajes) y avisa que supera el viajes_max configurado")

    print("\n=== TODOS LOS ESCENARIOS PASARON ===")


@mock.patch.object(core.time, "sleep")  # no esperar de verdad durante el test
@mock.patch.object(core.requests, "get")
def test_get_osrm_con_reintentos(mock_get, mock_sleep):
    import requests

    # Escenario A: falla transitoria (timeout) en los primeros 2 intentos,
    # éxito en el 3ro -- debe reintentar y devolver el resultado bueno, sin
    # caer al fallback de línea recta por un bache momentáneo.
    resp_ok = mock.Mock()
    resp_ok.json.return_value = {"code": "Ok", "distances": [[0]]}
    resp_ok.raise_for_status.return_value = None
    mock_get.side_effect = [requests.exceptions.Timeout(), requests.exceptions.Timeout(), resp_ok]

    data, err = core._get_osrm_con_reintentos("http://fake-url")
    assert err is None
    assert data == {"code": "Ok", "distances": [[0]]}
    assert mock_get.call_count == 3, "debe haber reintentado 2 veces antes de tener éxito"
    print("✔ _get_osrm_con_reintentos: 2 timeouts seguidos + éxito en el 3er intento -> devuelve el resultado bueno")

    # Escenario B: TODOS los intentos fallan por timeout -- se agotan los
    # 3 intentos y recién ahí se reporta como error transitorio.
    mock_get.reset_mock()
    mock_get.side_effect = [requests.exceptions.Timeout()] * 3
    data2, err2 = core._get_osrm_con_reintentos("http://fake-url", intentos=3)
    assert data2 is None
    assert err2 == "timeout"
    assert mock_get.call_count == 3
    print("✔ _get_osrm_con_reintentos: si TODOS los intentos fallan (timeout), recién ahí se rinde")

    # Escenario C: OSRM responde pero con un código de error REAL (no
    # transitorio, ej. "NoRoute") -- no debe reintentar, reintentarlo no
    # cambiaría nada.
    mock_get.reset_mock()
    resp_error_real = mock.Mock()
    resp_error_real.json.return_value = {"code": "NoRoute"}
    resp_error_real.raise_for_status.return_value = None
    mock_get.side_effect = [resp_error_real]
    data3, err3 = core._get_osrm_con_reintentos("http://fake-url")
    assert data3 == {"code": "NoRoute"}
    assert err3 is None
    assert mock_get.call_count == 1, "un código de error real de OSRM no debe reintentarse"
    print("✔ _get_osrm_con_reintentos: un código de error real de OSRM (no transitorio) no se reintenta")


if __name__ == "__main__":
    test_todos_los_escenarios()
    test_get_osrm_con_reintentos()
