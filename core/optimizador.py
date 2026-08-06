"""
Motor de calculo puro del Optimizador de Rutas de Recoleccion.

Extraido tal cual desde app.py (Streamlit, v8) durante la migracion a
Django v9 -- sin reescribir logica, solo removiendo referencias a
st.* y session_state. Cualquier cambio a este modulo debe seguir
pasando core/tests/test_modelo_tiempo.py.
"""
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd
import requests
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
DIAS_SEMANA_LOWER = [d.lower() for d in DIAS_SEMANA]

COLORES_KML = ["ff3c4ce7", "ffb98029", "ff60ae27", "ffad448e", "ff12c9f3", "ff85a016", "ff0054d3", "ff8d8c7f"]

TIPOS_VIA_DEFAULT = ["motorway", "trunk", "primary", "secondary", "tertiary",
                     "residential", "otro"]
TIPOS_VIA_RAPIDA = {"motorway", "trunk"}

FRECUENCIAS_DIAS = {"Día": 1, "Semana": 7, "Mes": 30, "Año": 365}


def costo_diario_inversion(df_ui):
    """Suma de monto / (vida_util_años × 365) — prorrateo diario de compras grandes."""
    total = 0.0
    for _, fila in df_ui.iterrows():
        monto = fila.get("Monto total (CRC)")
        vida = fila.get("Vida útil (años)")
        if pd.notna(monto) and pd.notna(vida) and vida > 0:
            total += float(monto) / (float(vida) * 365)
    return total


def costo_diario_recurrente(df_ui):
    """Suma de monto / días-de-la-frecuencia — prorrateo diario de gastos periódicos."""
    total = 0.0
    for _, fila in df_ui.iterrows():
        monto = fila.get("Monto (CRC)")
        frecuencia = fila.get("Frecuencia")
        if pd.notna(monto) and frecuencia in FRECUENCIAS_DIAS:
            total += float(monto) / FRECUENCIAS_DIAS[frecuencia]
    return total


def dias_desde_ultima_recoleccion(dia_actual, dias_activos):
    """
    Cuántos días de generación se acumulan en la recolección de 'dia_actual',
    dado el horario de una ruta YA CALCULADA (dias_activos, ej. ['Lunes',
    'Jueves']). Es cíclico dentro de la semana: cuenta hacia atrás hasta el
    día programado anterior más cercano, dando la vuelta a la semana si
    hace falta. Con un solo día en el horario, siempre da 7 (una vez por
    semana).

    Ejemplo Lunes/Jueves: Jueves acumula 3 días (Mar,Mié,Jue), Lunes acumula
    4 días (Vie,Sáb,Dom,Lun) — la suma de la semana siempre da 7.
    """
    idx_actual = DIAS_SEMANA_LOWER.index(dia_actual.strip().lower())
    indices = sorted(DIAS_SEMANA_LOWER.index(d.strip().lower()) for d in dias_activos)
    anteriores = [i for i in indices if i < idx_actual]
    if anteriores:
        idx_anterior = max(anteriores)
        gap = idx_actual - idx_anterior
    else:
        # Ningún día programado antes en la semana -> el anterior fue el
        # último día activo de la semana pasada (el de índice más alto).
        idx_anterior = max(indices)
        gap = 7 - idx_anterior + idx_actual
    return gap


def peso_estimado_ruta_para_dia(peso_diario_ruta, dias_ruta_str, dia_seleccionado):
    """
    Peso estimado de la visita de un día específico para una ruta YA
    CALCULADA (ej. "Heredia — Camión 2"), a partir de su peso diario total
    (la suma de "Peso (kg)" de los puntos que le tocaron al optimizar) y
    los días que esa ruta tiene asignados DESPUÉS de calcular (ej.
    "Martes,Viernes") — la frecuencia es de la ruta, no de cada punto
    suelto: no cambia cómo el optimizador agrupó los puntos, solo estima
    cuánto pesa cada visita real.

    - dia_seleccionado=None ("Todos los días") o dias_ruta_str vacío (la
      ruta todavía no tiene días asignados): no acumula, devuelve el peso
      diario tal cual (mismo número que siempre se mostró en Resultados).
    """
    if peso_diario_ruta is None or (isinstance(peso_diario_ruta, float) and math.isnan(peso_diario_ruta)):
        peso_diario_ruta = 0
    texto = str(dias_ruta_str or "").strip()
    if dia_seleccionado is None or texto == "":
        return peso_diario_ruta
    dias_de_la_ruta = [d.strip() for d in texto.split(",") if d.strip()]
    gap = dias_desde_ultima_recoleccion(dia_seleccionado, dias_de_la_ruta)
    return peso_diario_ruta * gap


def haversine(c1, c2):
    R = 6_371_000
    lat1, lon1 = math.radians(c1[0]), math.radians(c1[1])
    lat2, lon2 = math.radians(c2[0]), math.radians(c2[1])
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return int(R * 2 * math.asin(math.sqrt(a)))


def geocodificar_direccion(direccion):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": direccion, "format": "json", "limit": 1}
    headers = {"User-Agent": "optimizador-rutas-app/1.0"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"]), None
        return None, None, "no se encontraron resultados"
    except requests.exceptions.Timeout:
        return None, None, "timeout consultando Nominatim"
    except requests.exceptions.ConnectionError:
        return None, None, "sin conexión a Nominatim"
    except Exception as e:
        return None, None, f"error inesperado ({e})"


def obtener_matriz_osrm(locations):
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in locations)
    url = f"http://router.project-osrm.org/table/v1/driving/{coords_str}?annotations=distance"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok":
            return [[int(d) for d in row] for row in data["distances"]], True, None
        error_msg = f"OSRM respondió con código '{data.get('code')}'"
    except requests.exceptions.Timeout:
        error_msg = "OSRM no respondió a tiempo (timeout)."
    except requests.exceptions.ConnectionError:
        error_msg = "No se pudo conectar con OSRM (revisá tu conexión a internet)."
    except Exception as e:
        error_msg = f"Error inesperado consultando OSRM: {e}"
    n = len(locations)
    matriz = [[0 if i == j else haversine(locations[i], locations[j]) for j in range(n)] for i in range(n)]
    return matriz, False, error_msg


def obtener_ruta_completa_osrm_por_leg(stops):
    """
    Igual que obtener_ruta_completa_osrm, pero ADEMÁS devuelve la geometría
    de CADA tramo (parada a parada) por separado — usa steps=true de OSRM
    para poder reconstruir el trazado exacto de cada tramo individual, no
    solo el del viaje completo. Se usa solo cuando está activa la
    "velocidad variable por tipo de vía" (más lento, así que no se llama
    por defecto).

    Devuelve (camino, dist_legs_m, camino_por_tramo, error):
    - camino: lista (lat, lon) del viaje completo (igual que la función normal)
    - dist_legs_m: distancia de cada tramo parada-a-parada (igual)
    - camino_por_tramo: lista de listas (lat, lon), una por cada tramo
    """
    if len(stops) < 2:
        return list(stops), [], [], None
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in stops)
    url = (f"http://router.project-osrm.org/route/v1/driving/{coords_str}"
           f"?overview=full&geometries=geojson&steps=true")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok":
            ruta = data["routes"][0]
            camino = [(lat, lon) for lon, lat in ruta["geometry"]["coordinates"]]
            dist_legs = [leg["distance"] for leg in ruta["legs"]]
            camino_por_tramo = []
            for leg in ruta["legs"]:
                puntos_leg = []
                for step in leg["steps"]:
                    coords = step["geometry"]["coordinates"]
                    pts = [(lat, lon) for lon, lat in coords]
                    puntos_leg.extend(pts if not puntos_leg else pts[1:])
                camino_por_tramo.append(puntos_leg)
            return camino, dist_legs, camino_por_tramo, None
        err = f"OSRM código '{data.get('code')}'"
    except requests.exceptions.Timeout:
        err = "timeout"
    except requests.exceptions.ConnectionError:
        err = "sin conexión"
    except Exception as e:
        err = f"error inesperado ({e})"
    camino = list(stops)
    dist_legs = [haversine(stops[i], stops[i + 1]) for i in range(len(stops) - 1)]
    camino_por_tramo = [[stops[i], stops[i + 1]] for i in range(len(stops) - 1)]
    return camino, dist_legs, camino_por_tramo, err


def tiempo_leg_velocidad_variable(leg_geom, arbol, tipos_via, velocidad_normal,
                                  velocidad_rapida, tipos_rapidos):
    """
    Horas que demora un tramo, ponderando la velocidad según el tipo de vía
    que atraviesa: los kilómetros clasificados como `tipos_rapidos` (ej.
    motorway/trunk) usan `velocidad_rapida`; el resto usa `velocidad_normal`.
    """
    tramos = clasificar_tramos_ruta(leg_geom, arbol, tipos_via)
    dist_rapida_km = sum(t["dist_m"] for t in tramos if t["tipo"] in tipos_rapidos) / 1000
    dist_normal_km = sum(t["dist_m"] for t in tramos if t["tipo"] not in tipos_rapidos) / 1000
    return dist_rapida_km / velocidad_rapida + dist_normal_km / velocidad_normal


def obtener_ruta_completa_osrm(stops):
    """
    UNA sola llamada OSRM para todo el recorrido de un camión (multi-waypoint).
    Devuelve (camino, dist_legs_m, error):
    - camino: lista (lat, lon) con la geometría completa por carretera
    - dist_legs_m: distancia en metros de cada tramo parada-a-parada
    """
    if len(stops) < 2:
        return list(stops), [], None
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in stops)
    url = (f"http://router.project-osrm.org/route/v1/driving/{coords_str}"
           f"?overview=full&geometries=geojson")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok":
            ruta = data["routes"][0]
            camino = [(lat, lon) for lon, lat in ruta["geometry"]["coordinates"]]
            dist_legs = [leg["distance"] for leg in ruta["legs"]]
            return camino, dist_legs, None
        err = f"OSRM código '{data.get('code')}'"
    except requests.exceptions.Timeout:
        err = "timeout"
    except requests.exceptions.ConnectionError:
        err = "sin conexión"
    except Exception as e:
        err = f"error inesperado ({e})"
    # Fallback: línea recta entre paradas
    camino = list(stops)
    dist_legs = [haversine(stops[i], stops[i + 1]) for i in range(len(stops) - 1)]
    return camino, dist_legs, err


def resolver_vrp(distancias, demandas, capacidades, start_nodes, end_node,
                 asignaciones=None, balancear=False, viajes_max=1,
                 peso_minimo_viaje_extra_kg=0):
    """
    VRP multi-vehículo con restricción de capacidad POR CAMIÓN, SALIDA
    PROPIA POR CAMIÓN, y soporte de VIAJES MÚLTIPLES: un camión puede
    llenarse, ir a descargar al depot de llegada (siempre el mismo, un
    único vertedero/relleno para toda la flota), y volver a salir a
    recolectar más, hasta su propio máximo de viajes.

    Se modela internamente con "pseudo-vehículos": cada camión real se
    representa como N vehículos de OR-Tools encadenados (N = su propio
    viajes_max) — el primero sale del punto de salida DE ESE CAMIÓN
    (start_nodes[i]), los siguientes "salen" directamente del depot de
    llegada (porque ahí es donde el camión real queda parqueado tras
    descargar). Todos terminan en el depot de llegada. Al final se
    agrupan de vuelta por camión real.

    - distancias: matriz NxN en metros
    - demandas: peso en kg de cada nodo
    - capacidades: lista con la capacidad en kg de cada camión real
    - start_nodes: lista con el nodo de salida de CADA camión (uno por
      camión, pueden repetirse si dos camiones salen del mismo lugar)
    - end_node: nodo único del depot de llegada/descarga (compartido por
      todos los camiones y todos los viajes)
    - asignaciones: dict {nodo: índice_camión_real} para fijar manualmente
      (el punto puede caer en cualquiera de los viajes de ESE camión)
    - balancear: penaliza que un camión recorra mucho más que otro
    - viajes_max: máximo de viajes por camión. Puede ser:
        · un entero → se aplica igual a todos los camiones
        · una lista del mismo largo que `capacidades` → un valor por camión
    - peso_minimo_viaje_extra_kg: penaliza (blando, no prohíbe) que el 2do,
      3er, etc. viaje de un camión cargue MENOS que esto -- para evitar
      viajes casi vacíos a descargar (gasto de tiempo/combustible por poco).
      El primer viaje de cada camión NUNCA se penaliza (siempre hace falta).
      0 = desactivado (comportamiento igual que antes). Es un empujón, no
      una regla dura: si de verdad no hay forma de evitar un viaje chico
      (ej. un punto lejano que no cabe en ningún otro viaje), igual se
      genera -- verificado que el solver NUNCA penaliza un viaje que
      termina SIN USARSE, solo uno que se usa con poca carga.

    Devuelve una lista por camión real, y cada elemento es a su vez una
    lista de "viajes" (sub-rutas) EFECTIVAMENTE USADOS, en orden:
        [
          [[s0, 3, 1, end], [end, 5, 2, end]],   # Camión 0: usó 2 viajes
          [[s1, 4, end]],                          # Camión 1: usó 1 viaje
          ...
        ]
    Un camión sin ningún punto asignado devuelve [] (lista vacía de viajes).
    """
    n_camiones = len(capacidades)
    assert len(start_nodes) == n_camiones, "start_nodes debe tener un valor por camión"
    if isinstance(viajes_max, (list, tuple)):
        vm_list = [max(1, int(v)) for v in viajes_max]
        assert len(vm_list) == n_camiones, "viajes_max debe tener un valor por camión"
    else:
        vm_list = [max(1, int(viajes_max))] * n_camiones

    real_end = end_node
    n_pseudo = sum(vm_list)

    # Offsets: en qué índice de pseudo-vehículo empieza cada camión real
    offsets = [0]
    for vm in vm_list:
        offsets.append(offsets[-1] + vm)

    starts, ends = [], []
    for i in range(n_camiones):
        for trip in range(vm_list[i]):
            starts.append(start_nodes[i] if trip == 0 else real_end)
            ends.append(real_end)

    manager = pywrapcp.RoutingIndexManager(len(distancias), n_pseudo, starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    def cb_dist(from_idx, to_idx):
        return distancias[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    t = routing.RegisterTransitCallback(cb_dist)
    routing.SetArcCostEvaluatorOfAllVehicles(t)

    # ── Restricción de capacidad (por viaje, no por camión completo) ──
    def cb_demanda(from_idx):
        return int(demandas[manager.IndexToNode(from_idx)])

    d = routing.RegisterUnaryTransitCallback(cb_demanda)
    pseudo_capacidades = [
        int(capacidades[i]) for i in range(n_camiones) for _ in range(vm_list[i])
    ]
    routing.AddDimensionWithVehicleCapacity(d, 0, pseudo_capacidades, True, "Capacidad")

    # ── Penalizar viajes EXTRA (2do, 3er...) con poca carga (opcional) ──
    # Probado a mano: SetCumulVarSoftLowerBound NO penaliza un pseudo-vehículo
    # que termina sin usarse (ruta vacía) -- solo uno que se usa de verdad con
    # carga por debajo del mínimo. Coeficiente fijo (no expuesto al usuario,
    # mismo criterio que el 100 de "balancear"): cada kg de más bajo el
    # mínimo "cuesta" como si fueran 50 metros extra de recorrido.
    if peso_minimo_viaje_extra_kg > 0:
        cap_dim = routing.GetDimensionOrDie("Capacidad")
        COEFICIENTE_PENALIZACION_VIAJE_CHICO = 50
        for i in range(n_camiones):
            for trip in range(1, vm_list[i]):  # nunca el primer viaje (trip 0)
                pseudo_idx = offsets[i] + trip
                cap_dim.SetCumulVarSoftLowerBound(
                    routing.End(pseudo_idx), int(peso_minimo_viaje_extra_kg),
                    COEFICIENTE_PENALIZACION_VIAJE_CHICO)

    # ── Asignación manual: el punto puede ir en CUALQUIER viaje de ese camión ──
    if asignaciones:
        for nodo, camion_idx in asignaciones.items():
            index = manager.NodeToIndex(nodo)
            permitidos = list(range(offsets[camion_idx], offsets[camion_idx + 1]))
            routing.VehicleVar(index).SetValues(permitidos)

    # ── Balanceo opcional ──
    if balancear:
        routing.AddDimension(t, 0, 3_000_000, True, "Distancia")
        dist_dim = routing.GetDimensionOrDie("Distancia")
        dist_dim.SetGlobalSpanCostCoefficient(100)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    n_nodos = len(distancias)
    params.time_limit.seconds = max(10, min(90, n_nodos * max(vm_list) * 2))
    # Guided Local Search en sí es determinístico (mismos datos -> misma
    # secuencia de movimientos), pero SOLO time_limit como criterio de
    # parada no lo es: cuántas iteraciones alcanza a hacer en N segundos de
    # reloj real depende de qué tan cargada esté la máquina en ese momento
    # -- por eso el mismo cálculo, corrido dos veces, podía dar un resultado
    # distinto. solution_limit para en una cantidad FIJA de soluciones
    # mejoradas encontradas, sin depender del reloj -- mismos datos, mismo
    # resultado siempre. time_limit queda como techo de seguridad nada más.
    params.solution_limit = 100

    sol = routing.SolveWithParameters(params)
    if not sol:
        return None

    # ── Extraer la ruta cruda de cada pseudo-vehículo ──
    rutas_pseudo = []
    for v in range(n_pseudo):
        idx = routing.Start(v)
        ruta = []
        while not routing.IsEnd(idx):
            ruta.append(manager.IndexToNode(idx))
            idx = sol.Value(routing.NextVar(idx))
        ruta.append(manager.IndexToNode(idx))
        rutas_pseudo.append(ruta)

    # ── Agrupar de vuelta por camión real, quedándonos solo con los
    #    viajes que efectivamente recogieron algo (más de 2 nodos) ──
    resultado = []
    for i in range(n_camiones):
        viajes_camion = rutas_pseudo[offsets[i]: offsets[i + 1]]
        usados = [v for v in viajes_camion if len(v) > 2]

        # Si se usó algún viaje posterior al primero, pero el primero
        # (el único que realmente sale del depot de salida) quedó vacío,
        # igual hay que representar ese trayecto inicial obligatorio
        # (el camión tiene que llegar físicamente hasta el depot de
        # llegada antes de poder volver a salir).
        if usados and len(viajes_camion[0]) <= 2 and viajes_camion[0] not in usados:
            resultado.append([viajes_camion[0]] + usados)
        else:
            resultado.append(usados)
    return resultado


def generar_links_google_maps(locations_in_order):
    """Divide la ruta en segmentos de máx. 10 puntos (límite de Google Maps sin API)."""
    CHUNK = 9
    links = []
    puntos = locations_in_order
    i = 0
    seg_num = 1
    while i < len(puntos) - 1:
        chunk = puntos[i: i + CHUNK + 2]
        if len(chunk) < 2:
            break
        origin, destination = chunk[0], chunk[-1]
        waypoints = chunk[1:-1]
        url = ("https://www.google.com/maps/dir/?api=1"
               f"&origin={origin[0]},{origin[1]}"
               f"&destination={destination[0]},{destination[1]}")
        if waypoints:
            url += "&waypoints=" + "|".join(f"{lat},{lon}" for lat, lon in waypoints)
        url += "&travelmode=driving"
        es_ultimo = (i + CHUNK + 1 >= len(puntos) - 1)
        links.append((f"Segmento {seg_num}" + (" (final)" if es_ultimo else ""), url))
        i += CHUNK + 1
        seg_num += 1
    return links


def verificar_almuerzo(hora_actual, almuerzo_tomado,
                       hora_almuerzo_inicio, hora_almuerzo_fin):
    """
    Revisa si corresponde insertar el almuerzo en este punto del reloj y,
    si corresponde, avanza hora_actual hasta el fin del almuerzo. Devuelve
    (hora_actual_nueva, almuerzo_tomado, fila_almuerzo_o_None).

    - hora_almuerzo_inicio / hora_almuerzo_fin definen la VENTANA en la que
      puede arrancar el almuerzo y su DURACIÓN (fin - inicio) — no una hora
      de reloj a la que hay que volver. Se inserta UNA sola vez por
      camión/día, la primera vez que el reloj llega o pasa la hora de
      inicio de la ventana estando la ruta en curso, y siempre dura la
      duración completa configurada desde ese momento (llegue el camión
      justo al inicio de la ventana, a la mitad, o ya tarde y pasado el
      cierre — nunca se acorta ni se salta el descanso).
    - Si hora_almuerzo_inicio es None, el almuerzo está desactivado y
      nunca se inserta (reproduce el comportamiento de antes).
    - Se llama tanto después de cada parada/descarga como después del
      tramo final de regreso al plantel, para que un mediodía que cae
      DURANTE ese último tramo (y no justo en una parada) tampoco se
      salte el almuerzo.
    """
    fila_almuerzo = None
    if (not almuerzo_tomado) and (hora_almuerzo_inicio is not None) \
            and hora_actual.time() >= hora_almuerzo_inicio:
        duracion_almuerzo = (
            datetime.combine(datetime.today(), hora_almuerzo_fin)
            - datetime.combine(datetime.today(), hora_almuerzo_inicio)
        )
        inicio_almuerzo = hora_actual
        fin_almuerzo = hora_actual + duracion_almuerzo
        hora_actual = fin_almuerzo
        almuerzo_tomado = True
        fila_almuerzo = {"inicio": inicio_almuerzo, "fin": fin_almuerzo}

    return hora_actual, almuerzo_tomado, fila_almuerzo


def avanzar_reloj_tras_parada(hora_actual, es_descarga, tiempo_parada_min,
                              tiempo_descarga_min, almuerzo_tomado,
                              hora_almuerzo_inicio, hora_almuerzo_fin):
    """
    Avanza el reloj después de atender una parada (recolección normal o
    descarga en el depot). Devuelve (hora_actual_nueva, almuerzo_tomado,
    fila_almuerzo_o_None).

    - Descarga en el depot usa tiempo_descarga_min (ej. 30 min), más largo
      que una parada normal (tiempo_parada_min), en vez de reutilizar el
      mismo valor para ambas cosas.
    - La revisión del almuerzo la hace verificar_almuerzo().
    """
    hora_actual = hora_actual + timedelta(
        minutes=(tiempo_descarga_min if es_descarga else tiempo_parada_min)
    )
    return verificar_almuerzo(hora_actual, almuerzo_tomado,
                              hora_almuerzo_inicio, hora_almuerzo_fin)


def filtrar_camiones_para_grupo(cams, campo_grupo, valor, canton_de_distrito):
    """
    Filtra la flota para un grupo (cantón o distrito) específico, según las
    columnas "Cantón asignado" / "Distrito asignado" de cada camión:
    - Ambas vacías -> comodín, siempre disponible en cualquier grupo.
    - Agrupando por Distrito: disponible si su "Distrito asignado" == valor,
      o si tiene "Cantón asignado" == cantón de ese distrito (y el distrito
      propio quedó vacío — una asignación de distrito más específica no se
      pisa por la de cantón).
    - Agrupando por Cantón: disponible si su "Cantón asignado" == valor, o
      si tiene un "Distrito asignado" que pertenece a ese cantón.

    canton_de_distrito: dict {distrito: cantón}, derivado de los puntos.
    """
    canton_col = cams["Cantón asignado"].fillna("").astype(str).str.strip()
    distrito_col = cams["Distrito asignado"].fillna("").astype(str).str.strip()

    disponible = (canton_col == "") & (distrito_col == "")  # comodín

    if campo_grupo == "Distrito":
        disponible = disponible | (distrito_col == valor)
        canton_de_este_distrito = canton_de_distrito.get(valor, "")
        if canton_de_este_distrito:
            disponible = disponible | ((distrito_col == "") & (canton_col == canton_de_este_distrito))
    else:  # "Cantón"
        disponible = disponible | (canton_col == valor)
        for distrito_val, canton_val in canton_de_distrito.items():
            if canton_val == valor:
                disponible = disponible | (distrito_col == distrito_val)

    return cams[disponible]


def calcular_rutas_para_puntos(puntos, cams, depot2_lat, depot2_lon,
                               hora_inicio, velocidad_kmh, tiempo_parada, balancear,
                               velocidad_variable_via=False, velocidad_rapida_kmh=None,
                               tiempo_descarga=None, hora_almuerzo_inicio=None,
                               hora_almuerzo_fin=None, tope_horas_jornada=8.0,
                               peso_minimo_viaje_extra_kg=0):
    """
    Corre el cálculo completo de rutas para un subconjunto de puntos y la
    flota de camiones dada. Devuelve (resultado, None) si todo salió bien,
    o (None, mensaje_error) si no se pudo calcular — así el que llama decide
    si frena todo (modo clásico) o solo salta ese grupo (modo por lotes).

    velocidad_variable_via=False (default) reproduce EXACTAMENTE el cálculo
    de siempre. Si es True, los tramos que atraviesan autopista/vía troncal
    (OpenStreetMap) usan velocidad_rapida_kmh en vez de velocidad_kmh — más
    realista, pero requiere descargar la red vial clasificada (internet) y
    hace el cálculo más lento. Si la descarga falla, se cae de vuelta al
    cálculo normal con velocidad_kmh constante, sin romper nada.

    Modelo de tiempo (opcional, todo con default que reproduce el cálculo
    de siempre si no se pasa nada):
    - tiempo_descarga: minutos que tarda la descarga en el depot de llegada
      (más largo que tiempo_parada). Si es None, se usa tiempo_parada para
      la descarga también (comportamiento anterior).
    - hora_almuerzo_inicio / hora_almuerzo_fin: ventana de almuerzo de hora
      fija de reloj, se inserta una sola vez por camión/día. Si
      hora_almuerzo_inicio es None, el almuerzo está desactivado.
    - tope_horas_jornada: NO bloquea el cálculo (restricción blanda) — cada
      camión que termine su jornada por encima de este tope queda marcado
      con "excede_jornada"=True en su resultado, para avisar sin arriesgar
      que todo el cálculo falle por un solo camión problemático.
    """
    if tiempo_descarga is None:
        tiempo_descarga = tiempo_parada
    if len(puntos) < 1:
        return None, "No hay puntos con coordenadas en este grupo."
    if len(cams) < 1:
        return None, "Necesitás al menos 1 camión (pestaña Camiones)."

    CAPACIDADES = cams["Capacidad (kg)"].tolist()
    NOMBRES_CAM = cams["Nombre"].tolist()
    PERSONAS_CAM = cams["Personas"].fillna(1).astype(int).tolist()
    VIAJES_MAX_CAM = cams["Viajes máx."].fillna(1).astype(int).tolist()
    PLANTEL_LAT_CAM = cams["Plantel Lat"].tolist()
    PLANTEL_LON_CAM = cams["Plantel Lon"].tolist()

    if any(pd.isna(PLANTEL_LAT_CAM[i]) or pd.isna(PLANTEL_LON_CAM[i])
           for i in range(len(NOMBRES_CAM))):
        return None, ("Todos los camiones necesitan su Plantel (Lat/Lon) completo "
                      "en la pestaña Camiones — es de donde salen y a donde vuelven.")

    n_camiones_flota = len(NOMBRES_CAM)
    # Nodos: [plantel de cada camión, sale y vuelve ahí] + [puntos] + [depot de llegada, único]
    LOCATIONS = (
        [(PLANTEL_LAT_CAM[i], PLANTEL_LON_CAM[i]) for i in range(n_camiones_flota)]
        + list(zip(puntos["Latitud"], puntos["Longitud"]))
        + [(depot2_lat, depot2_lon)]
    )
    NOMBRES = (
        [f"PLANTEL — {NOMBRES_CAM[i]}" for i in range(n_camiones_flota)]
        + puntos["Nombre"].tolist()
        + ["Planta San Antonio"]
    )
    PESOS = [0] * n_camiones_flota + puntos["Peso (kg)"].fillna(0).tolist() + [0]

    start_nodes = list(range(n_camiones_flota))   # cada camión sale de su propio plantel
    end_node = len(LOCATIONS) - 1                 # el vertedero, compartido
    real_end_coords = LOCATIONS[end_node]

    # El camión vuelve, al final del día, a su propio plantel (mismo punto de salida)
    PLANTEL_CAM = [
        (PLANTEL_LAT_CAM[i], PLANTEL_LON_CAM[i]) for i in range(n_camiones_flota)
    ]

    capacidad_efectiva_flota = sum(
        CAPACIDADES[i] * VIAJES_MAX_CAM[i] for i in range(len(CAPACIDADES))
    )
    if sum(PESOS) > capacidad_efectiva_flota:
        return None, (f"El peso total ({sum(PESOS):,.0f} kg) excede la capacidad "
                      f"efectiva de la flota considerando viajes "
                      f"({capacidad_efectiva_flota:,.0f} kg).")

    # Asignaciones manuales: nodo → índice de camión
    asignaciones = {}
    camion_col = puntos["Camión"].fillna("Auto").tolist()
    for i, cam_nombre in enumerate(camion_col):
        if cam_nombre != "Auto" and cam_nombre in NOMBRES_CAM:
            # +n_camiones_flota porque los primeros nodos son las salidas
            asignaciones[i + n_camiones_flota] = NOMBRES_CAM.index(cam_nombre)

    distancias, uso_osrm, error_matriz = obtener_matriz_osrm(LOCATIONS)
    rutas = resolver_vrp(distancias, PESOS, CAPACIDADES, start_nodes, end_node,
                         asignaciones=asignaciones or None, balancear=balancear,
                         viajes_max=VIAJES_MAX_CAM,
                         peso_minimo_viaje_extra_kg=peso_minimo_viaje_extra_kg)

    # ── Velocidad variable por tipo de vía (opcional) ──
    # Se descarga (o se reusa del caché, ver descargar_red_osm_clasificada_cacheada)
    # UNA sola vez para todo este grupo de puntos, antes de procesar los
    # viajes. Si falla (sin internet, Overpass caído, etc.), se apaga sola y
    # el cálculo sigue igual que si estuviera desactivada.
    arbol_via, tipos_via_clasif = None, None
    errores_osrm_previos = []
    if velocidad_variable_via:
        try:
            bbox_grupo = bbox_de_camino(LOCATIONS)
            gdf_vias_calc = descargar_red_osm_clasificada_cacheada(bbox_grupo)
            arbol_via, tipos_via_clasif = construir_indice_vias(gdf_vias_calc)
        except Exception as e:
            errores_osrm_previos.append(
                f"Velocidad variable por vía desactivada para este cálculo "
                f"(no se pudo clasificar la red vial: {e}). Se usó la "
                f"velocidad promedio constante."
            )

    if rutas is None:
        return None, ("No se encontró solución. Posibles causas: asignaciones "
                      "manuales imposibles de cumplir con las capacidades, o "
                      "capacidad insuficiente incluso considerando los viajes "
                      "máximos configurados.")

    camiones_res = []
    errores_osrm = list(errores_osrm_previos)
    for v, viajes_nodos in enumerate(rutas):
        if not viajes_nodos:
            continue  # camión sin ningún viaje usado

        hora_actual = datetime.combine(datetime.today(), hora_inicio)
        hora_inicio_dt = hora_actual  # para medir la duración total de la jornada
        almuerzo_tomado = False
        peso_dia = 0
        orden_counter = 0
        resumen = []
        camino_total = []
        tramos = []  # un tramo dibujable por viaje, con su propio color/selección
        dist_recoleccion_m = 0.0

        for trip_idx, ruta_nodos in enumerate(viajes_nodos):
            stops = [LOCATIONS[n] for n in ruta_nodos]
            camino_por_leg = None
            if velocidad_variable_via and arbol_via is not None:
                camino_tramo, dist_legs, camino_por_leg, err = obtener_ruta_completa_osrm_por_leg(stops)
            else:
                camino_tramo, dist_legs, err = obtener_ruta_completa_osrm(stops)
            if err:
                errores_osrm.append(f"{NOMBRES_CAM[v]} (viaje {trip_idx + 1}): {err}")
            dist_recoleccion_m += sum(dist_legs)
            tramos.append({
                "trip_idx": trip_idx,
                "etiqueta": f"{NOMBRES_CAM[v]} — Viaje {trip_idx + 1}",
                "camino": camino_tramo,
                "dist_m": sum(dist_legs),
            })

            # Evitar duplicar el punto de unión entre el final de un viaje
            # y el inicio del siguiente (ambos son el mismo punto físico).
            camino_total.extend(camino_tramo if trip_idx == 0 else camino_tramo[1:])

            for i, node in enumerate(ruta_nodos):
                lat, lon = LOCATIONS[node]
                if i == 0:
                    # El nodo inicial de un viaje POSTERIOR al primero es el
                    # mismo punto físico y el mismo instante que la fila de
                    # "Descarga (fin viaje anterior)" ya agregada — no se
                    # duplica una fila nueva para eso, solo se registra el
                    # inicio real (primer viaje, sale del depot de salida).
                    if trip_idx != 0:
                        continue
                    resumen.append({
                        "orden": orden_counter, "lat": lat, "lon": lon, "tipo": "inicio",
                        "trip_idx": trip_idx,
                        "Parada": "Inicio (Depot Salida)", "Nombre": NOMBRES[node],
                        "Hora llegada": hora_actual.strftime("%H:%M"),
                        "Peso recogido (kg)": 0, "Peso acumulado (kg)": peso_dia,
                        "Distancia tramo (km)": "-",
                    })
                    orden_counter += 1
                else:
                    dist_m = dist_legs[i - 1]
                    if camino_por_leg is not None:
                        horas_tramo = tiempo_leg_velocidad_variable(
                            camino_por_leg[i - 1], arbol_via, tipos_via_clasif,
                            velocidad_kmh, velocidad_rapida_kmh, TIPOS_VIA_RAPIDA)
                    else:
                        horas_tramo = (dist_m / 1000) / velocidad_kmh
                    hora_actual += timedelta(hours=horas_tramo)
                    es_fin_viaje = (i == len(ruta_nodos) - 1)
                    peso_p = PESOS[node] if not es_fin_viaje else 0
                    peso_dia += peso_p
                    tipo = "descarga" if es_fin_viaje else "parada"
                    label = (f"Descarga (fin viaje {trip_idx + 1})" if es_fin_viaje
                            else f"Parada {orden_counter}")
                    resumen.append({
                        "orden": orden_counter, "lat": lat, "lon": lon, "tipo": tipo,
                        "trip_idx": trip_idx,
                        "Parada": label, "Nombre": NOMBRES[node],
                        "Hora llegada": hora_actual.strftime("%H:%M"),
                        "Peso recogido (kg)": peso_p, "Peso acumulado (kg)": peso_dia,
                        "Distancia tramo (km)": f"{dist_m / 1000:.2f}",
                    })
                    orden_counter += 1
                    if es_fin_viaje:
                        # Fila informativa: cuándo termina de descargar (usa
                        # tiempo_descarga, ej. 30 min) y sale vacío rumbo al
                        # siguiente viaje o de regreso al plantel.
                        hora_sale_vacio = hora_actual + timedelta(minutes=tiempo_descarga)
                        resumen.append({
                            "orden": orden_counter, "lat": lat, "lon": lon, "tipo": "sale_vacio",
                            "trip_idx": trip_idx,
                            "Parada": f"Sale vacío ({tiempo_descarga:.0f} min descarga)",
                            "Nombre": NOMBRES[node],
                            "Hora llegada": hora_sale_vacio.strftime("%H:%M"),
                            "Peso recogido (kg)": 0, "Peso acumulado (kg)": peso_dia,
                            "Distancia tramo (km)": "-",
                        })
                        orden_counter += 1
                    hora_actual, almuerzo_tomado, fila_almuerzo = avanzar_reloj_tras_parada(
                        hora_actual, es_fin_viaje, tiempo_parada, tiempo_descarga,
                        almuerzo_tomado, hora_almuerzo_inicio, hora_almuerzo_fin,
                    )
                    if fila_almuerzo is not None:
                        resumen.append({
                            "orden": orden_counter, "lat": lat, "lon": lon, "tipo": "almuerzo",
                            "trip_idx": trip_idx,
                            "Parada": "Almuerzo", "Nombre": "—",
                            "Hora llegada": fila_almuerzo["inicio"].strftime("%H:%M"),
                            "Peso recogido (kg)": 0, "Peso acumulado (kg)": peso_dia,
                            "Distancia tramo (km)": "-",
                        })
                        orden_counter += 1

        # ── Tramo final: del depot de llegada al plantel de ESE camión ──
        plantel_coords = PLANTEL_CAM[v]
        camino_plantel, dist_legs_plantel, err_plantel = obtener_ruta_completa_osrm(
            [real_end_coords, plantel_coords]
        )
        if err_plantel:
            errores_osrm.append(f"{NOMBRES_CAM[v]} (a plantel): {err_plantel}")
        dist_plantel_m = sum(dist_legs_plantel) if dist_legs_plantel else 0.0
        camino_total.extend(camino_plantel[1:] if camino_plantel else [])
        tramos.append({
            "trip_idx": len(viajes_nodos),
            "etiqueta": f"{NOMBRES_CAM[v]} — A plantel",
            "camino": camino_plantel,
            "dist_m": dist_plantel_m,
        })
        if dist_plantel_m > 0:
            if arbol_via is not None and camino_plantel:
                horas_plantel = tiempo_leg_velocidad_variable(
                    camino_plantel, arbol_via, tipos_via_clasif,
                    velocidad_kmh, velocidad_rapida_kmh, TIPOS_VIA_RAPIDA)
            else:
                horas_plantel = (dist_plantel_m / 1000) / velocidad_kmh
            hora_actual += timedelta(hours=horas_plantel)

        # Si el mediodía cae durante este último tramo (y no en una parada
        # anterior), el almuerzo no se salta: se registra acá.
        hora_actual, almuerzo_tomado, fila_almuerzo = verificar_almuerzo(
            hora_actual, almuerzo_tomado, hora_almuerzo_inicio, hora_almuerzo_fin
        )
        if fila_almuerzo is not None:
            resumen.append({
                "orden": orden_counter, "lat": plantel_coords[0], "lon": plantel_coords[1],
                "tipo": "almuerzo", "trip_idx": len(viajes_nodos),
                "Parada": "Almuerzo", "Nombre": "—",
                "Hora llegada": fila_almuerzo["inicio"].strftime("%H:%M"),
                "Peso recogido (kg)": 0, "Peso acumulado (kg)": peso_dia,
                "Distancia tramo (km)": "-",
            })
            orden_counter += 1

        resumen.append({
            "orden": orden_counter, "lat": plantel_coords[0], "lon": plantel_coords[1],
            "tipo": "fin_jornada", "trip_idx": len(viajes_nodos),
            "Parada": "Fin de jornada (Plantel)", "Nombre": "PLANTEL",
            "Hora llegada": hora_actual.strftime("%H:%M"),
            "Peso recogido (kg)": 0, "Peso acumulado (kg)": peso_dia,
            "Distancia tramo (km)": f"{dist_plantel_m / 1000:.2f}",
        })

        horas_jornada = (hora_actual - hora_inicio_dt).total_seconds() / 3600.0
        camiones_res.append({
            "nombre": NOMBRES_CAM[v],
            "capacidad": CAPACIDADES[v],
            "personas": PERSONAS_CAM[v],
            "viajes_max": VIAJES_MAX_CAM[v],
            "n_viajes_usados": len(viajes_nodos),
            "vehiculo_idx": v,
            "camino": camino_total,
            "tramos": tramos,
            "dist_recoleccion_m": dist_recoleccion_m,
            "dist_plantel_m": dist_plantel_m,
            "dist_total_m": dist_recoleccion_m + dist_plantel_m,
            "resumen": resumen,
            "peso_total": peso_dia,
            "hora_fin": hora_actual.strftime("%H:%M"),
            "horas_jornada": round(horas_jornada, 2),
            # Restricción BLANDA: nunca bloquea el cálculo, solo marca para
            # avisar en Resultados — ver docstring de la función.
            "excede_jornada": horas_jornada > tope_horas_jornada,
        })

    if not camiones_res:
        return None, "Ningún camión terminó con una ruta asignada en este grupo."

    resultado = {
        "camiones": camiones_res,
        "uso_osrm": uso_osrm,
        "error_matriz": error_matriz,
        "errores_osrm": errores_osrm,
        "hora_inicio": hora_inicio.strftime("%H:%M"),
    }
    return resultado, None


def exportar_geojson(res):
    import json
    features = []
    for c in res["camiones"]:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[lon, lat] for lat, lon in c["camino"]]},
            "properties": {"camion": c["nombre"], "tipo": "ruta",
                           "distancia_km": round(c["dist_total_m"] / 1000, 2)},
        })
        for fila in c["resumen"]:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point",
                             "coordinates": [fila["lon"], fila["lat"]]},
                "properties": {
                    "camion": c["nombre"], "orden": fila["orden"],
                    "nombre": fila["Nombre"], "hora_llegada": fila["Hora llegada"],
                    "peso_kg": fila["Peso recogido (kg)"],
                    "tipo": fila["tipo"],
                },
            })
    return json.dumps({"type": "FeatureCollection", "features": features},
                      ensure_ascii=False, indent=2).encode("utf-8")


def exportar_shapefile(res):
    import io, zipfile, tempfile, os
    import geopandas as gpd
    from shapely.geometry import LineString, Point

    lineas, puntos = [], []
    for c in res["camiones"]:
        lineas.append({"camion": c["nombre"],
                       "dist_km": round(c["dist_total_m"] / 1000, 2),
                       "geometry": LineString([(lon, lat) for lat, lon in c["camino"]])})
        for fila in c["resumen"]:
            peso = fila["Peso recogido (kg)"]
            puntos.append({"camion": c["nombre"], "orden": fila["orden"],
                           "nombre": fila["Nombre"], "tipo": fila["tipo"],
                           "hora": fila["Hora llegada"],
                           "peso_kg": float(peso) if str(peso) not in ("", "-") else 0.0,
                           "geometry": Point(fila["lon"], fila["lat"])})

    gdf_lineas = gpd.GeoDataFrame(lineas, crs="EPSG:4326")
    gdf_puntos = gpd.GeoDataFrame(puntos, crs="EPSG:4326")

    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for nombre_capa, gdf in [("rutas_lineas", gdf_lineas), ("rutas_puntos", gdf_puntos)]:
                capa_dir = os.path.join(tmpdir, nombre_capa)
                os.makedirs(capa_dir)
                gdf.to_file(os.path.join(capa_dir, f"{nombre_capa}.shp"), driver="ESRI Shapefile")
                for fname in os.listdir(capa_dir):
                    zf.write(os.path.join(capa_dir, fname), fname)
    buf.seek(0)
    return buf.read()


def exportar_gpx(res):
    from xml.etree.ElementTree import Element, SubElement, tostring
    from xml.dom import minidom
    gpx = Element("gpx", {"version": "1.1", "creator": "Optimizador de Rutas",
                          "xmlns": "http://www.topografix.com/GPX/1/1"})
    for c in res["camiones"]:
        for fila in c["resumen"]:
            wpt = SubElement(gpx, "wpt", {"lat": str(fila["lat"]), "lon": str(fila["lon"])})
            SubElement(wpt, "name").text = f"[{c['nombre']}] {fila['Nombre']}"
            SubElement(wpt, "desc").text = (f"Orden: {fila['orden']} | Hora: {fila['Hora llegada']} | "
                                            f"Peso: {fila['Peso recogido (kg)']} kg")
        trk = SubElement(gpx, "trk")
        SubElement(trk, "name").text = f"Ruta {c['nombre']}"
        trkseg = SubElement(trk, "trkseg")
        for lat, lon in c["camino"]:
            SubElement(trkseg, "trkpt", {"lat": str(lat), "lon": str(lon)})
    raw = tostring(gpx, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")


def exportar_kml(res):
    from xml.etree.ElementTree import Element, SubElement, tostring
    from xml.dom import minidom
    kml = Element("kml", {"xmlns": "http://www.opengis.net/kml/2.2"})
    doc = SubElement(kml, "Document")
    SubElement(doc, "name").text = "Rutas de recolección"
    for i in range(len(COLORES_KML)):
        style = SubElement(doc, "Style", {"id": f"ruta{i}"})
        ls = SubElement(style, "LineStyle")
        SubElement(ls, "color").text = COLORES_KML[i]
        SubElement(ls, "width").text = "4"
    for vi, c in enumerate(res["camiones"]):
        folder = SubElement(doc, "Folder")
        SubElement(folder, "name").text = c["nombre"]
        for fila in c["resumen"]:
            pm = SubElement(folder, "Placemark")
            SubElement(pm, "name").text = fila["Nombre"]
            SubElement(pm, "description").text = (
                f"{c['nombre']} | Orden: {fila['orden']} | Hora: {fila['Hora llegada']} | "
                f"Peso: {fila['Peso recogido (kg)']} kg")
            pt = SubElement(pm, "Point")
            SubElement(pt, "coordinates").text = f"{fila['lon']},{fila['lat']},0"
        pm_ruta = SubElement(folder, "Placemark")
        SubElement(pm_ruta, "name").text = f"Recorrido {c['nombre']}"
        SubElement(pm_ruta, "styleUrl").text = f"#ruta{vi % len(COLORES_KML)}"
        ls2 = SubElement(pm_ruta, "LineString")
        SubElement(ls2, "tessellate").text = "1"
        SubElement(ls2, "coordinates").text = " ".join(
            f"{lon},{lat},0" for lat, lon in c["camino"])
    raw = tostring(kml, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")


def _haversine_m_red(a, b):
    """a, b en formato (lon, lat) — misma fórmula que haversine(), invirtiendo
    el orden de coordenadas (esta reutiliza esa, en vez de duplicar la
    fórmula). La diferencia de <1m por el redondeo a entero de haversine()
    es irrelevante para clasificación de vías."""
    return float(haversine((a[1], a[0]), (b[1], b[0])))


def construir_grafo_red(gdf_lineas, tolerancia_m=5.0):
    """
    Arma un grafo (networkx) a partir de las líneas de un GeoDataFrame.

    Dos pasadas de robustez, pensadas para shapefiles reales (que casi
    nunca vienen topológicamente perfectos):

    1. `unary_union` sobre todas las líneas: parte automáticamente cada
       línea en cada punto donde CRUZA a otra, aunque no compartan un
       vértice explícito ahí (dos calles que se cruzan en la mitad, no
       solo en sus extremos).
    2. Tolerancia de `tolerancia_m` metros entre extremos: dos puntos que
       deberían ser el mismo cruce, pero quedaron a unos centímetros/metros
       de distancia por error de digitalización, se tratan como un único
       nodo.

    Devuelve (grafo, lista_de_coordenadas_de_cada_nodo).
    Si el shapefile tiene líneas sueltas (no conectadas), el grafo queda
    con varios "componentes" separados — se reporta aparte, no es un error.
    """
    import networkx as nx
    from shapely.ops import unary_union

    geometrias = [g for g in gdf_lineas.geometry if g is not None and not g.is_empty]
    if not geometrias:
        return nx.Graph(), []

    union = unary_union(geometrias)
    if union.geom_type == "LineString":
        partes = [union]
    elif union.geom_type == "MultiLineString":
        partes = list(union.geoms)
    else:
        # GeometryCollection u otro tipo mixto: quedarnos solo con las líneas
        partes = [g for g in getattr(union, "geoms", [union]) if g.geom_type == "LineString"]

    G = nx.Graph()
    nodos = []

    def nodo_id(coord):
        for i, existente in enumerate(nodos):
            if _haversine_m_red(coord, existente) <= tolerancia_m:
                return i
        nodos.append(coord)
        return len(nodos) - 1

    for parte in partes:
        coords = list(parte.coords)
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            na, nb = nodo_id(a), nodo_id(b)
            if na == nb:
                continue
            dist = _haversine_m_red(a, b)
            if G.has_edge(na, nb):
                if dist < G[na][nb]["weight"]:
                    G[na][nb]["weight"] = dist
            else:
                G.add_edge(na, nb, weight=dist)
    return G, nodos


def _osrm_match_tanda(tanda):
    """Una sola consulta /match de OSRM para una tanda de <=10 puntos (límite
    práctico del servidor público -- con más, responde 'TooBig'). Devuelve
    la geometría ajustada de esa tanda, o None si falla."""
    coords_str = ";".join(f"{lon},{lat}" for lon, lat in tanda)
    url = (f"http://router.project-osrm.org/match/v1/driving/{coords_str}"
           f"?geometries=geojson&overview=full")
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
    except Exception:
        return None
    if data.get("code") != "Ok" or not data.get("matchings"):
        return None
    return data["matchings"][0]["geometry"]["coordinates"]


def _osrm_match_por_tramos(coords, tamano_tramo=9):
    """
    Ajusta una línea (lista de (lon, lat)) a la geometría real de calles
    usando el servicio /match de OSRM, que "engancha" una secuencia de
    puntos al camino más probable sobre la red vial real -- pensado para
    trazas GPS, pero funciona igual de bien para limpiar un shapefile
    digitalizado a mano.

    El servidor público de OSRM tiene un límite bajo de puntos por consulta
    (~10-11 -- probado a mano, no está documentado), así que se parte en
    tandas de `tamano_tramo` puntos (con solapamiento de 1 punto entre
    tandas, para no dejar huecos al unir), y las tandas se consultan EN
    PARALELO (unas pocas a la vez) para que una línea con muchos puntos no
    tarde una eternidad en resolverse en serie.

    Devuelve la lista completa de (lon, lat) ajustada, o None si CUALQUIER
    tanda falla (se prefiere no mezclar geometría ajustada con cruda dentro
    de la misma línea -- el que llama cae a la geometría original completa
    en ese caso).
    """
    if len(coords) < 2:
        return None

    tramos = []
    i = 0
    while i < len(coords) - 1:
        fin = min(i + tamano_tramo, len(coords) - 1)
        tramos.append(coords[i:fin + 1])
        if fin == len(coords) - 1:
            break
        i = fin

    with ThreadPoolExecutor(max_workers=6) as pool:
        geoms_por_tramo = list(pool.map(_osrm_match_tanda, tramos))

    if any(g is None for g in geoms_por_tramo):
        return None

    resultado = []
    for geom_tramo in geoms_por_tramo:
        if resultado:
            resultado.extend(geom_tramo[1:])  # el primer punto repite el último de la tanda anterior
        else:
            resultado.extend(geom_tramo)
    return resultado if len(resultado) >= 2 else None


def explotar_lineas_simples(gdf_lineas):
    """
    Devuelve una lista de geometrías LineString "simples" a partir de un
    GeoDataFrame de líneas -- separando cada parte de una MultiLineString en
    su propia entrada. Shapely no permite pedir `.coords` directamente sobre
    una geometría multi-parte (lanza NotImplementedError), y un shapefile
    real perfectamente puede traer filas MultiLineString (varias calles
    agrupadas en una sola fila de atributos).
    """
    simples = []
    for geom in gdf_lineas.geometry:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "LineString":
            simples.append(geom)
        elif geom.geom_type == "MultiLineString":
            simples.extend(geom.geoms)
    return simples


def ajustar_una_linea_con_osrm(geom, tolerancia_simplificado=0.00008):
    """
    Ajusta UNA línea a la geometría real de calles (vía OSRM /match) -- ver
    limpiar_lineas_con_osrm para el detalle de por qué se simplifica antes
    de mandarla. Separada como función aparte (no solo un paso interno del
    for de limpiar_lineas_con_osrm) para que quien cargue una red con
    muchas líneas pueda ir reportando progreso línea por línea, en vez de
    esperar a que las 24 (o las que sean) terminen todas juntas.

    Devuelve (coords, se_ajustó): coords es la lista de (lon, lat) a usar
    (ajustada si se pudo, original si no), se_ajustó indica cuál de las dos.
    """
    original = list(geom.coords)
    simplificada = list(geom.simplify(tolerancia_simplificado).coords)
    ajustada = _osrm_match_por_tramos(simplificada if len(simplificada) >= 2 else original)
    if ajustada:
        return ajustada, True
    return original, False


def limpiar_lineas_con_osrm(lineas_geoms, tolerancia_simplificado=0.00008):
    """
    Ajusta cada línea (ya "simple", ver explotar_lineas_simples) a la
    geometría real de calles (vía OSRM /match), UNA SOLA VEZ al cargar la
    red -- así el recorrido de cobertura total que se calcule después (que
    puede tener miles de puntos, por las aristas repetidas) hereda calles
    reales sin tener que consultar OSRM en cada cálculo.

    Antes de mandar cada línea a OSRM se SIMPLIFICA (Douglas-Peucker, vía
    shapely) con `tolerancia_simplificado` grados (~8-9 metros por
    defecto): un shapefile digitalizado a mano suele traer muchos puntos
    casi-colineales que no aportan nada a la forma real de la calle, y cada
    punto de más es una consulta de más al servidor público de OSRM (que
    solo acepta ~10 puntos por consulta).

    Si OSRM no logra ajustar una línea en particular (falla la consulta, no
    hay calle cercana, etc.), esa línea se deja con su geometría original
    (SIN simplificar) -- no se descarta la red completa por una línea
    problemática.

    Devuelve (lineas_ajustadas, n_ajustadas):
      - lineas_ajustadas: lista de listas de (lon, lat), una por línea de
        `lineas_geoms`, en el mismo orden (ajustada si se pudo, original si no).
      - n_ajustadas: cantidad de líneas que sí se pudieron ajustar.
    """
    lineas_ajustadas = []
    n_ajustadas = 0
    for geom in lineas_geoms:
        coords, se_ajusto = ajustar_una_linea_con_osrm(geom, tolerancia_simplificado)
        lineas_ajustadas.append(coords)
        if se_ajusto:
            n_ajustadas += 1
    return lineas_ajustadas, n_ajustadas


def enganchar_a_red(punto_lonlat, nodos):
    """Nodo más cercano de la red a un punto dado. Devuelve (nodo_id, distancia_m)."""
    mejor_id, mejor_dist = None, float("inf")
    for i, n in enumerate(nodos):
        d = _haversine_m_red(punto_lonlat, n)
        if d < mejor_dist:
            mejor_id, mejor_dist = i, d
    return mejor_id, mejor_dist


def matriz_distancias_red(puntos_lonlat, G, nodos):
    """
    Distancias por la red (Dijkstra) entre todos los pares de puntos.
    Si dos puntos no están en el mismo componente conectado (red
    fragmentada / líneas sueltas), cae a línea recta para ESE par y lo
    reporta en `pares_sin_red` para poder avisarle al usuario.
    """
    import networkx as nx
    n = len(puntos_lonlat)
    enganches = [enganchar_a_red(p, nodos) for p in puntos_lonlat]
    nodos_enganchados = [e[0] for e in enganches]

    matriz = [[0.0] * n for _ in range(n)]
    pares_sin_red = []

    for i in range(n):
        try:
            dist_desde_i = nx.single_source_dijkstra_path_length(
                G, nodos_enganchados[i], weight="weight")
        except nx.NodeNotFound:
            dist_desde_i = {}
        for j in range(n):
            if i == j:
                continue
            nodo_j = nodos_enganchados[j]
            if nodo_j in dist_desde_i:
                matriz[i][j] = dist_desde_i[nodo_j]
            else:
                matriz[i][j] = _haversine_m_red(puntos_lonlat[i], puntos_lonlat[j])
                if (j, i) not in pares_sin_red:
                    pares_sin_red.append((i, j))
    return matriz, nodos_enganchados, enganches, pares_sin_red


def _normalizar_highway(valor):
    """OSM a veces da una lista de tipos para la misma vía (ej. una calle
    que cambia de categoría) — nos quedamos con el primero."""
    if isinstance(valor, list):
        valor = valor[0] if valor else "otro"
    return valor if valor in TIPOS_VIA_DEFAULT else "otro"


def descargar_red_osm_clasificada(bbox, network_type="drive"):
    """
    Descarga la red vial de OSM (vía Overpass) dentro de un bbox y la
    devuelve como GeoDataFrame de líneas con columna 'highway' normalizada.
    bbox: (lon_min, lat_min, lon_max, lat_max). Requiere conexión a internet.
    """
    import osmnx as ox
    G = ox.graph_from_bbox(bbox, network_type=network_type, simplify=True)
    gdf_edges = ox.graph_to_gdfs(G, nodes=False, edges=True)
    gdf_edges = gdf_edges.reset_index()
    gdf_edges["highway"] = gdf_edges["highway"].apply(_normalizar_highway)
    return gdf_edges[["highway", "geometry"]]


# Caché en memoria (por proceso) de la red vial ya clasificada, para no
# volver a descargarla de OSM/Overpass en cada cálculo cuando está activa
# "velocidad variable por tipo de vía" -- la descarga puede tardar varios
# minutos (Overpass parte el área en muchas sub-consultas si es grande), y
# la red vial real cambia muy poco día a día. Vive en un dict simple (no
# Django cache) para que este módulo siga sin depender de Django.
_CACHE_RED_OSM = {}
_CACHE_RED_OSM_TTL_S = 24 * 3600  # 24 horas


def _bbox_redondeado(bbox, decimales=3):
    """~110m de grilla en el ecuador -- suficiente para que corridas con los
    mismos puntos (o casi) reusen la misma entrada de caché."""
    return tuple(round(v, decimales) for v in bbox)


def descargar_red_osm_clasificada_cacheada(bbox, network_type="drive"):
    """Igual que descargar_red_osm_clasificada, pero reusa el resultado si ya
    se descargó ese mismo bbox (redondeado) hace menos de _CACHE_RED_OSM_TTL_S.
    Si la descarga falla, NO se guarda nada en caché (para reintentar la
    próxima vez, en vez de quedar pegado a un fallo)."""
    clave = (_bbox_redondeado(bbox), network_type)
    ahora = time.time()
    entrada = _CACHE_RED_OSM.get(clave)
    if entrada is not None and (ahora - entrada[0]) < _CACHE_RED_OSM_TTL_S:
        return entrada[1]
    gdf = descargar_red_osm_clasificada(bbox, network_type=network_type)
    _CACHE_RED_OSM[clave] = (ahora, gdf)
    return gdf


def construir_indice_vias(gdf_vias):
    """STRtree para buscar rápido la vía clasificada más cercana a un punto."""
    from shapely.strtree import STRtree
    geoms = list(gdf_vias.geometry)
    tipos = list(gdf_vias["highway"])
    arbol = STRtree(geoms)
    return arbol, tipos


def clasificar_tramos_ruta(camino_latlon, arbol, tipos, max_sub_tramo_m=150.0):
    """
    camino_latlon: lista de (lat, lon) del trazado YA CALCULADO de una ruta.
    Devuelve una lista de tramos clasificados, cada uno:
        {"lat1", "lon1", "lat2", "lon2", "tipo", "dist_m", "edge_id"}
    "edge_id" es el índice de la vía específica que se enganchó (dentro del
    mismo árbol/red usado) — sirve para detectar si dos tramos (de un mismo
    camión en otro viaje, o de otro camión) caen en LA MISMA vía física, y
    así no sumar el km/kg dos veces por esa vía.

    Cada tramo del camino se subdivide en pedazos de a lo sumo
    `max_sub_tramo_m` metros antes de clasificar: si dependiera de un único
    punto medio para un tramo largo (ej. una recta de varios km, típica en
    autopistas con pocos puntos de geometría), un solo error de enganche
    haría fallar la clasificación de todo ese tramo de una — subdividiendo,
    el error queda acotado a un pedazo chico.

    Útil para DIBUJAR el mapa coloreado por tipo de vía (a diferencia de
    clasificar_distancia_ruta, que solo da el total agregado).
    """
    from shapely.geometry import Point
    tramos_clasificados = []
    for i in range(len(camino_latlon) - 1):
        lat1, lon1 = camino_latlon[i]
        lat2, lon2 = camino_latlon[i + 1]
        dist_total = _haversine_m_red((lon1, lat1), (lon2, lat2))
        if dist_total == 0:
            continue

        n_subtramos = max(1, math.ceil(dist_total / max_sub_tramo_m))
        for k in range(n_subtramos):
            f1 = k / n_subtramos
            f2 = (k + 1) / n_subtramos
            slat1, slon1 = lat1 + (lat2 - lat1) * f1, lon1 + (lon2 - lon1) * f1
            slat2, slon2 = lat1 + (lat2 - lat1) * f2, lon1 + (lon2 - lon1) * f2
            dist_sub = dist_total / n_subtramos
            medio = Point((slon1 + slon2) / 2, (slat1 + slat2) / 2)
            idx_cercano = arbol.nearest(medio)
            tramos_clasificados.append({
                "lat1": slat1, "lon1": slon1, "lat2": slat2, "lon2": slon2,
                "tipo": tipos[idx_cercano], "dist_m": dist_sub,
                "edge_id": idx_cercano,
            })
    return tramos_clasificados


def clasificar_distancia_ruta(camino_latlon, arbol, tipos):
    """
    camino_latlon: lista de (lat, lon) del trazado YA CALCULADO de una ruta.
    Devuelve {tipo_de_via: metros_recorridos_en_esa_categoria}.
    """
    distancia_por_tipo = {t: 0.0 for t in TIPOS_VIA_DEFAULT}
    for tramo in clasificar_tramos_ruta(camino_latlon, arbol, tipos):
        distancia_por_tipo[tramo["tipo"]] += tramo["dist_m"]
    return distancia_por_tipo


def bbox_de_camino(camino_latlon, margen_grados=0.01):
    """Bounding box (lon_min, lat_min, lon_max, lat_max) con margen, para
    descargar solo la red de OSM alrededor de la ruta (no el país entero)."""
    lats = [p[0] for p in camino_latlon]
    lons = [p[1] for p in camino_latlon]
    return (min(lons) - margen_grados, min(lats) - margen_grados,
            max(lons) + margen_grados, max(lats) + margen_grados)


def reconstruir_viajes_desde_resumen(resumen):
    """
    Devuelve una lista de "stops" (lat, lon) por viaje, reconstruida a
    partir de las filas de "resumen" YA CALCULADAS — en el mismo orden en
    que se le pidió la ruta a OSRM originalmente. El punto de salida de un
    viaje posterior al primero es el mismo que el de descarga del viaje
    anterior (esa fila no está duplicada en resumen, así que se reutiliza
    como frontera entre viajes).
    """
    viajes = {}
    ultimo_punto_frontera = None
    for fila in resumen:
        if fila["tipo"] == "inicio":
            ultimo_punto_frontera = (fila["lat"], fila["lon"])
            viajes.setdefault(fila["trip_idx"], []).append(ultimo_punto_frontera)
        elif fila["tipo"] == "parada":
            trip_idx = fila["trip_idx"]
            if trip_idx not in viajes:
                viajes[trip_idx] = [ultimo_punto_frontera]
            viajes[trip_idx].append((fila["lat"], fila["lon"]))
        elif fila["tipo"] == "descarga":
            trip_idx = fila["trip_idx"]
            if trip_idx not in viajes:
                viajes[trip_idx] = [ultimo_punto_frontera]
            viajes[trip_idx].append((fila["lat"], fila["lon"]))
            ultimo_punto_frontera = (fila["lat"], fila["lon"])
        # "fin_jornada" no es parte de ningún viaje (es el tramo al plantel, aparte)
    return [viajes[k] for k in sorted(viajes.keys())]


def contar_componentes_red(G):
    """Lista de componentes conectados del grafo (cada uno, un set de nodos)."""
    import networkx as nx
    return list(nx.connected_components(G))


def camino_geometria_red(G, nodos, nodo_a, nodo_b):
    """Coordenadas (lon, lat) del camino más corto entre dos nodos de la red."""
    import networkx as nx
    try:
        ruta = nx.shortest_path(G, nodo_a, nodo_b, weight="weight")
        return [nodos[n] for n in ruta]
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return [nodos[nodo_a], nodos[nodo_b]]


def calcular_recorrido_cobertura_total(G, nodo_inicio):
    """
    Problema del Cartero Chino (Route Inspection Problem): recorrido CERRADO
    que cubre TODAS las aristas del grafo al menos una vez, minimizando la
    distancia repetida. A diferencia de resolver_vrp/matriz_distancias_red
    (que eligen el mejor orden para visitar un subconjunto de puntos), acá
    no se elige nada -- hay que pasar por cada calle del grafo sí o sí. Es
    el algoritmo que usa "Red propia" para acelerar una ruta que YA recorre
    la red completa, no para armar una ruta nueva más corta.

    Función completamente nueva e independiente -- no modifica ni reutiliza
    resolver_vrp ni ninguna otra lógica del optimizador principal.

    Pasos clásicos del algoritmo:
      1. Encontrar los nodos de grado impar (en un grafo con todos los
         nodos de grado par, ya se puede recorrer cada arista una sola vez
         sin repetir nada -- eso casi nunca pasa en una red real: hay
         callejones sin salida y cruces de 3 vías).
      2. Calcular la distancia más corta (por la red) entre cada par de
         nodos impares.
      3. Emparejarlos de la forma más barata posible (emparejamiento de
         peso mínimo) -- cada par emparejado indica qué tramo hay que
         repetir para "parchar" esos dos nodos y dejarlos de grado par.
      4. Duplicar esos tramos (como aristas paralelas) y armar el circuito
         que recorre cada arista (incluidas las duplicadas) exactamente una
         vez -- un circuito euleriano, que por construcción siempre empieza
         y termina en el mismo nodo.

    G: networkx.Graph con pesos en "weight" (metros), tal como lo arma
    construir_grafo_red(). nodo_inicio: id de nodo (índice en la lista de
    `nodos` de construir_grafo_red) donde empieza y termina el recorrido.

    Si el grafo está fragmentado (varios componentes no conectados), solo
    se resuelve el componente que contiene a nodo_inicio -- el resto queda
    afuera y se reporta en "nodos_excluidos" para poder avisarle al usuario,
    en vez de fallar.

    Devuelve un dict, o None si nodo_inicio no existe en el grafo:
      - "componente_size": cantidad de nodos en el componente usado
      - "nodos_excluidos": cantidad de nodos del grafo fuera de ese componente
      - "ruta_nodos": lista de ids de nodo, en el orden del recorrido
        (empieza y termina en nodo_inicio)
      - "distancia_original_m": suma de las aristas ÚNICAS del componente
        (la longitud real de calles a cubrir)
      - "distancia_repetida_m": metros de más por los tramos duplicados,
        necesarios para poder cerrar el circuito
      - "distancia_total_m": distancia_original_m + distancia_repetida_m
      - "aristas_duplicadas": lista de (a, b) de las aristas que se repiten

    Rendimiento: el paso más caro es encontrar el emparejamiento de peso
    mínimo entre los nodos de grado impar -- el algoritmo exacto (blossom)
    es O(k³) en la cantidad de nodos impares, y en Python puro se vuelve
    inutilizable con redes viales reales (con ~700 nodos impares, más de un
    minuto). Para que siga siendo exacto pero rápido:
      - Las distancias por la red entre nodos impares se calculan con
        `scipy.sparse.csgraph.dijkstra` (vectorizado, en C) en vez de
        Dijkstra de networkx nodo por nodo -- foto completa en <1s en vez
        de decenas de segundos.
      - El emparejamiento no se corre sobre el grafo COMPLETO de pares
        impares (k² aristas) sino sobre uno con solo los N vecinos más
        cercanos de cada nodo -- el emparejamiento óptimo casi siempre usa
        pares cercanos, así que el resultado sigue siendo el óptimo exacto
        (verificado contra el cálculo completo con datos reales), pero el
        blossom corre sobre un grafo mucho más chico. Si con ese vecindario
        no alcanza para un emparejamiento perfecto (puede pasar si queda
        muy disperso), se reintenta duplicando el vecindario hasta lograrlo.
    """
    import networkx as nx
    import numpy as np
    from scipy.sparse.csgraph import dijkstra as _dijkstra_scipy

    if nodo_inicio not in G:
        return None

    componente = nx.node_connected_component(G, nodo_inicio)
    H = G.subgraph(componente).copy()
    distancia_original_m = sum(d["weight"] for _, _, d in H.edges(data=True))

    if H.number_of_edges() == 0:
        return {
            "componente_size": len(componente),
            "nodos_excluidos": G.number_of_nodes() - len(componente),
            "ruta_nodos": [nodo_inicio],
            "distancia_original_m": 0.0,
            "distancia_repetida_m": 0.0,
            "distancia_total_m": 0.0,
            "aristas_duplicadas": [],
        }

    grados_impares = [n for n in H.nodes if H.degree(n) % 2 == 1]
    aristas_duplicadas = []
    distancia_repetida_m = 0.0
    M = nx.MultiGraph(H)

    if grados_impares:
        nodelist = list(H.nodes())
        idx_de_nodo = {n: i for i, n in enumerate(nodelist)}
        csr = nx.to_scipy_sparse_array(H, nodelist=nodelist, weight="weight", format="csr")
        indices_impares = [idx_de_nodo[n] for n in grados_impares]

        dist_mat, predecesores = _dijkstra_scipy(
            csr, directed=False, indices=indices_impares, return_predecessors=True)
        sub = dist_mat[:, indices_impares]  # k x k, distancias entre nodos impares
        k = len(grados_impares)

        def _construir_aux(n_vecinos):
            aux = nx.Graph()
            aux.add_nodes_from(range(k))
            for i in range(k):
                vecinos = np.argsort(sub[i])
                agregados = 0
                for j in vecinos:
                    if j == i or not np.isfinite(sub[i, j]):
                        continue
                    aux.add_edge(i, j, weight=sub[i, j])
                    agregados += 1
                    if agregados >= n_vecinos:
                        break
            return aux

        n_vecinos = min(25, k - 1)
        emparejamiento_local = []
        while True:
            aux = _construir_aux(n_vecinos)
            emparejamiento_local = nx.algorithms.matching.min_weight_matching(aux)
            if len(emparejamiento_local) * 2 == k or n_vecinos >= k - 1:
                break
            n_vecinos = min(n_vecinos * 2, k - 1)

        def _camino_desde_predecesores(fila_pred, origen_idx, destino_idx):
            camino_idx = [destino_idx]
            actual = destino_idx
            while actual != origen_idx:
                actual = fila_pred[actual]
                camino_idx.append(actual)
            camino_idx.reverse()
            return [nodelist[i] for i in camino_idx]

        for i, j in emparejamiento_local:
            u, v = grados_impares[i], grados_impares[j]
            camino = _camino_desde_predecesores(predecesores[i], idx_de_nodo[u], idx_de_nodo[v])
            for a, b in zip(camino, camino[1:]):
                peso = H[a][b]["weight"]
                M.add_edge(a, b, weight=peso)
                aristas_duplicadas.append((a, b))
                distancia_repetida_m += peso

    if not nx.is_eulerian(M):
        return None

    circuito = list(nx.eulerian_circuit(M, source=nodo_inicio))
    ruta_nodos = [circuito[0][0]] + [b for _, b in circuito]

    return {
        "componente_size": len(componente),
        "nodos_excluidos": G.number_of_nodes() - len(componente),
        "ruta_nodos": ruta_nodos,
        "distancia_original_m": distancia_original_m,
        "distancia_repetida_m": distancia_repetida_m,
        "distancia_total_m": distancia_original_m + distancia_repetida_m,
        "aristas_duplicadas": aristas_duplicadas,
    }


def _normalizar_gdf_lineas(gdf):
    """Reproyecta a EPSG:4326 y filtra solo geometrías de línea."""
    if gdf.crs is not None and str(gdf.crs) != "EPSG:4326":
        gdf = gdf.to_crs("EPSG:4326")
    tipos_validos = {"LineString", "MultiLineString"}
    gdf = gdf[gdf.geometry.geom_type.isin(tipos_validos)]
    if len(gdf) == 0:
        return None, ("El archivo no contiene geometrías de línea "
                      "(¿es una capa de puntos o polígonos?).")
    return gdf, None


def leer_capa_lineas(archivos_subidos):
    """
    Lee una capa de líneas desde archivos subidos, en cualquiera de estos
    formatos:
      A) Un .zip conteniendo el shapefile (aunque los archivos estén dentro
         de una subcarpeta, como pasa al comprimir con clic derecho en Windows)
      B) Un .geojson / .json
      C) Un .gpkg (GeoPackage)
      D) Los archivos del shapefile SUELTOS sin comprimir: .shp + .shx + .dbf
         (y .prj si existe), subidos juntos en la misma carga

    Devuelve (GeoDataFrame en EPSG:4326, None) o (None, mensaje_error).
    """
    import zipfile
    import tempfile
    import os
    import geopandas as gpd

    if not archivos_subidos:
        return None, "No se subió ningún archivo."

    nombres = [a.name.lower() for a in archivos_subidos]

    # ── B) GeoJSON directo ──
    for archivo, nombre in zip(archivos_subidos, nombres):
        if nombre.endswith((".geojson", ".json")):
            try:
                gdf = gpd.read_file(archivo)
            except Exception as e:
                return None, f"No se pudo leer el GeoJSON: {e}"
            return _normalizar_gdf_lineas(gdf)

    # ── C) GeoPackage directo ──
    for archivo, nombre in zip(archivos_subidos, nombres):
        if nombre.endswith(".gpkg"):
            with tempfile.TemporaryDirectory() as tmpdir:
                ruta = os.path.join(tmpdir, "capa.gpkg")
                with open(ruta, "wb") as f:
                    f.write(archivo.getbuffer())
                try:
                    gdf = gpd.read_file(ruta)
                except Exception as e:
                    return None, f"No se pudo leer el GeoPackage: {e}"
            return _normalizar_gdf_lineas(gdf)

    # ── A) Zip con shapefile (búsqueda RECURSIVA del .shp, tolera subcarpetas) ──
    for archivo, nombre in zip(archivos_subidos, nombres):
        if nombre.endswith(".zip"):
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    with zipfile.ZipFile(archivo) as zf:
                        zf.extractall(tmpdir)
                except zipfile.BadZipFile:
                    return None, "El archivo no es un .zip válido."

                shp_path = None
                for raiz, _, archivos_dir in os.walk(tmpdir):
                    for fname in archivos_dir:
                        if fname.lower().endswith(".shp"):
                            shp_path = os.path.join(raiz, fname)
                            break
                    if shp_path:
                        break
                if shp_path is None:
                    return None, ("No se encontró ningún archivo .shp dentro del .zip "
                                  "(ni en subcarpetas). Verificá el contenido del zip.")
                try:
                    gdf = gpd.read_file(shp_path)
                except Exception as e:
                    return None, f"No se pudo leer el shapefile: {e}"
            return _normalizar_gdf_lineas(gdf)

    # ── D) Archivos del shapefile sueltos (.shp + .shx + .dbf juntos) ──
    if any(n.endswith(".shp") for n in nombres):
        requeridos = {".shp", ".shx", ".dbf"}
        extensiones = {os.path.splitext(n)[1] for n in nombres}
        faltantes = requeridos - extensiones
        if faltantes:
            return None, (f"Faltan archivos del shapefile: {', '.join(sorted(faltantes))}. "
                          "Subí juntos el .shp, .shx y .dbf (y el .prj si lo tenés).")
        with tempfile.TemporaryDirectory() as tmpdir:
            base = None
            for archivo, nombre in zip(archivos_subidos, nombres):
                ruta = os.path.join(tmpdir, os.path.basename(nombre))
                with open(ruta, "wb") as f:
                    f.write(archivo.getbuffer())
                if nombre.endswith(".shp"):
                    base = ruta
            try:
                gdf = gpd.read_file(base)
            except Exception as e:
                return None, f"No se pudo leer el shapefile: {e}"
        return _normalizar_gdf_lineas(gdf)

    return None, ("Formato no reconocido. Subí un .zip con el shapefile, un "
                  ".geojson, un .gpkg, o los archivos .shp + .shx + .dbf juntos.")
