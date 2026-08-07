"""
Prueba standalone de encadenar_lineas_en_ruta() -- el motor de "Ruta real"
(subís el .shp que YA es la ruta real de un camión, no una red genérica de
calles) que ordena tramos sueltos en un solo recorrido continuo. No toca ni
depende del modelo de tiempo/VRP ni de Red propia (cobertura total).

Se corre como script, igual que los otros tests de core/.
"""
import geopandas as gpd
from shapely.geometry import LineString

from core import optimizador as core


def test_todos_los_escenarios():
    l1 = LineString([(-84.10, 9.90), (-84.09, 9.91)])
    l2 = LineString([(-84.09, 9.91), (-84.08, 9.92)])
    l3 = LineString([(-84.08, 9.92), (-84.07, 9.93)])

    # Escenario 1: tramos ya en orden -- deben quedar pegados uno tras otro
    # sin ningún salto.
    camino, saltos = core.encadenar_lineas_en_ruta([l1, l2, l3])
    assert saltos == []
    assert camino[0] == (-84.10, 9.90) and camino[-1] == (-84.07, 9.93)
    print("✔ Tramos ya ordenados: se encadenan sin saltos")

    # Escenario 2: el caso difícil -- el PRIMER tramo de la lista es en
    # realidad el del MEDIO de la ruta real. Un algoritmo que solo extiende
    # por un extremo del camino acumulado falla acá (bug real, encontrado y
    # corregido durante el desarrollo) -- tiene que poder extender por
    # cualquiera de los dos extremos.
    camino2, saltos2 = core.encadenar_lineas_en_ruta([l2, l1, l3])
    assert saltos2 == [], f"no debería haber saltos, dio: {saltos2}"
    assert camino2[0] == (-84.10, 9.90) and camino2[-1] == (-84.07, 9.93), (
        "la ruta debe reconstruirse en el orden geográfico correcto sin importar "
        "el orden ni con qué tramo arranca la lista de entrada"
    )
    print("✔ Empezar por un tramo del medio: igual reconstruye el orden geográfico correcto")

    # Escenario 3: tramos desordenados Y con orientación invertida (un
    # extremo->otro al revés de como lo camina el recorrido real).
    l1_invertida = LineString(list(l1.coords)[::-1])
    camino3, saltos3 = core.encadenar_lineas_en_ruta([l3, l1_invertida, l2])
    assert saltos3 == []
    assert camino3[0] == (-84.10, 9.90) and camino3[-1] == (-84.07, 9.93)
    print("✔ Desordenado + con un tramo invertido: se reordena y reorienta bien")

    # Escenario 4: un tramo genuinamente lejos de los demás (otro grupo de
    # calles, sin conexión real) -- debe reportarse como salto, no fallar
    # ni descartarse en silencio.
    l_lejana = LineString([(-70.0, 20.0), (-70.01, 20.01)])
    camino4, saltos4 = core.encadenar_lineas_en_ruta([l1, l2, l_lejana])
    assert len(saltos4) == 1, "debería detectar exactamente 1 salto"
    assert saltos4[0]["distancia_m"] > 100_000
    print("✔ Tramo lejos de los demás: se detecta y reporta como salto")

    # Escenario 5: lista vacía -- caso trivial, no debe fallar.
    camino5, saltos5 = core.encadenar_lineas_en_ruta([])
    assert camino5 == [] and saltos5 == []
    print("✔ Lista vacía: caso trivial sin error")

    # Escenario 6: una sola línea -- se devuelve tal cual, sin nada que encadenar.
    camino6, saltos6 = core.encadenar_lineas_en_ruta([l1])
    assert camino6 == list(l1.coords)
    assert saltos6 == []
    print("✔ Una sola línea: se devuelve tal cual")

    # Escenario 7: encadenar_en_subrutas -- si dos grupos de tramos están
    # genuinamente lejos entre sí (más que umbral_split_m), NO deben forzarse
    # en un solo recorrido -- deben quedar como rutas separadas. Este es el
    # caso real: un shapefile municipal que en realidad trae varias rutas
    # (zonas/días/camiones) distintas mezcladas como tramos sueltos.
    l_lejana = LineString([(-84.50, 9.50), (-84.49, 9.51)])
    l_lejana2 = LineString([(-84.49, 9.51), (-84.48, 9.52)])
    rutas = core.encadenar_en_subrutas([l1, l2, l_lejana, l_lejana2], umbral_split_m=800)
    assert len(rutas) == 2, f"deberían quedar 2 rutas separadas, dio {len(rutas)}"
    largos = sorted(len(r["camino"]) for r in rutas)
    assert largos == [4, 4], f"cada ruta debe tener sus 2 tramos encadenados, dio {largos}"
    print("✔ encadenar_en_subrutas: grupos lejanos quedan como rutas separadas, no se fuerzan juntos")

    # Escenario 8: encadenar_en_subrutas con todo cerca -- debe dar 1 sola ruta,
    # igual que encadenar_lineas_en_ruta.
    rutas_juntas = core.encadenar_en_subrutas([l1, l2, l3], umbral_split_m=800)
    assert len(rutas_juntas) == 1
    assert rutas_juntas[0]["saltos"] == []
    print("✔ encadenar_en_subrutas: tramos todos cerca quedan en 1 sola ruta")

    # Escenario 9: encadenar_rutas_reales con columna de atributo -- si el
    # usuario eligió una columna del shapefile (ej. "dia") que ya indica a
    # qué ruta pertenece cada tramo, debe agruparse ESTRICTAMENTE por esa
    # columna, sin importar la cercanía geográfica (más confiable que
    # adivinar por distancia).
    pares = [(l1, "lunes"), (l2, "lunes"), (l3, "martes")]
    rutas_attr = core.encadenar_rutas_reales(pares)
    assert len(rutas_attr) == 2
    etiquetas = sorted(r["etiqueta"] for r in rutas_attr)
    assert etiquetas == ["lunes", "martes"]
    ruta_lunes = next(r for r in rutas_attr if r["etiqueta"] == "lunes")
    assert ruta_lunes["camino"][0] == (-84.10, 9.90) and ruta_lunes["camino"][-1] == (-84.08, 9.92)
    print("✔ encadenar_rutas_reales: agrupa estrictamente por columna de atributo cuando está presente")

    # Escenario 10: encadenar_rutas_reales SIN columna de atributo (todo
    # None) -- debe caer a agrupar por cercanía (encadenar_en_subrutas), con
    # etiqueta=None.
    pares_sin_attr = [(l1, None), (l2, None), (l_lejana, None), (l_lejana2, None)]
    rutas_sin_attr = core.encadenar_rutas_reales(pares_sin_attr, umbral_split_m=800)
    assert len(rutas_sin_attr) == 2
    assert all(r["etiqueta"] is None for r in rutas_sin_attr)
    print("✔ encadenar_rutas_reales: sin columna de atributo, cae a agrupar por cercanía")

    # distancia_total_camino_m -- suma simple de distancias consecutivas.
    dist = core.distancia_total_camino_m([(0, 0), (0, 0.001), (0, 0.002)])
    assert dist > 0
    print(f"✔ distancia_total_camino_m calcula una distancia positiva ({dist:.1f} m)")

    # Escenario 11: explotar_lineas_simples fuerza cada geometría a 2D --
    # un shapefile real exportado de un GIS a menudo trae una tercera
    # coordenada (elevación), y dejarla pasar rompe el resto del pipeline
    # (bug real, encontrado con un shapefile municipal real -- ver
    # core/optimizador.py explotar_lineas_simples).
    l_3d = LineString([(-84.11, 9.99, 120), (-84.10, 10.00, 125)])
    gdf_3d = gpd.GeoDataFrame(geometry=[l_3d])
    simples_2d = core.explotar_lineas_simples(gdf_3d)
    assert len(simples_2d) == 1
    assert simples_2d[0].has_z is False, "la geometría 3D debe quedar forzada a 2D"
    assert list(simples_2d[0].coords) == [(-84.11, 9.99), (-84.1, 10.0)]
    print("✔ explotar_lineas_simples: geometría 3D (con elevación) se fuerza a 2D")

    # Escenario 12: explotar_lineas_simples_con_atributo -- cada línea
    # simple debe heredar el valor de la columna elegida de SU fila
    # original, y una MultiLineString debe repartir el mismo valor a cada
    # una de sus partes.
    from shapely.geometry import MultiLineString
    multi_l = MultiLineString([list(l1.coords), list(l2.coords)])
    gdf_attr = gpd.GeoDataFrame({"dia": ["lunes", "martes"]}, geometry=[multi_l, l3])
    pares_attr = core.explotar_lineas_simples_con_atributo(gdf_attr, "dia")
    assert len(pares_attr) == 3, "la MultiLineString debe separarse en sus 2 partes + la línea simple"
    valores = sorted(v for _, v in pares_attr)
    assert valores == ["lunes", "lunes", "martes"], "las 2 partes de la MultiLineString heredan el valor de su fila"
    print("✔ explotar_lineas_simples_con_atributo: cada parte hereda el valor de atributo de su fila original")

    pares_sin_col = core.explotar_lineas_simples_con_atributo(gdf_attr, None)
    assert all(v is None for _, v in pares_sin_col), "sin columna, el valor debe ser None para todas"
    print("✔ explotar_lineas_simples_con_atributo: sin columna, el valor es None para todas las líneas")

    # Escenario 13: limpiar_ruta_con_osrm -- fallback POR TRAMO (no todo o
    # nada). Se simula OSRM (sin red real) para que la tanda 0 "matchee" y
    # la tanda 1 falle -- el resultado debe traer la tanda 0 ajustada y la
    # tanda 1 con su geometría cruda, sin descartar el ajuste completo por
    # un solo tramo problemático (bug real corregido durante el desarrollo
    # -- ver core/optimizador.py limpiar_ruta_con_osrm).
    _osrm_original = core._osrm_match_tanda

    def _osrm_falso(tanda):
        # la "tanda" que arranca en (0, 0) matchea: se devuelve corrida 1
        # unidad en Y para poder distinguirla del original en el assert.
        if tanda[0] == (0.0, 0.0):
            return [(x, y + 1) for x, y in tanda]
        return None  # cualquier otra tanda falla el match

    core._osrm_match_tanda = _osrm_falso
    try:
        # zigzag (no colineal) para que Douglas-Peucker (tolerancia=0) no
        # colapse los puntos intermedios de una línea perfectamente recta.
        camino_largo = [(0.0, 0.0), (1.0, 0.5), (2.0, 0.0), (3.0, 0.5)]
        ajustado, n_ajustados, n_total = core.limpiar_ruta_con_osrm(
            camino_largo, tolerancia_simplificado=0, tamano_tramo=2,
        )
        assert n_total == 2, f"con tamano_tramo=2 y 4 puntos debería haber 2 tandas, dio {n_total}"
        assert n_ajustados == 1, "solo 1 de las 2 tandas debería haber matcheado"
        assert ajustado[0] == (0.0, 1.0), "la tanda que matcheó debe quedar con la geometría AJUSTADA"
        # el punto de unión entre tandas se descarta (se asume que repite el
        # último de la tanda anterior) -- lo que importa es que el ÚLTIMO
        # punto de la tanda que falló (que no se comparte con la anterior)
        # quede en su posición CRUDA, sin el offset que aplicó el fake match.
        assert ajustado[-1] == (3.0, 0.5), "la tanda que falló debe conservar su geometría CRUDA, no descartarse"
        print("✔ limpiar_ruta_con_osrm: fallback es por tramo -- un tramo sin match no tira abajo el resto")
    finally:
        core._osrm_match_tanda = _osrm_original

    # Escenario 14: exportar_gpx_camino / exportar_kml_camino -- deben
    # producir XML válido con el nombre y las coordenadas del camino.
    camino_exportar = [(-84.10, 9.90), (-84.09, 9.91)]
    gpx = core.exportar_gpx_camino(camino_exportar, "Mi ruta")
    assert isinstance(gpx, bytes)
    assert b"Mi ruta" in gpx and b'lat="9.9"' in gpx and b'lon="-84.1"' in gpx
    print("✔ exportar_gpx_camino: genera un GPX con el nombre y las coordenadas del camino")

    kml = core.exportar_kml_camino(camino_exportar, "Mi ruta")
    assert isinstance(kml, bytes)
    assert b"Mi ruta" in kml and b"-84.1,9.9,0" in kml
    print("✔ exportar_kml_camino: genera un KML con el nombre y las coordenadas del camino")

    print("\n=== TODOS LOS ESCENARIOS PASARON ===")


if __name__ == "__main__":
    test_todos_los_escenarios()
