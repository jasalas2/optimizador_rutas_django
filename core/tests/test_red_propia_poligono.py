"""
Prueba standalone de las funciones nuevas de "Red propia" para el flujo de
polígono de zona/sector (subís el polígono, la app trae las calles reales
de OSM que caen adentro y calcula la cobertura total) -- agrupar por
columna de atributo, reproyección CRTM05, y descarga por zonas con caché.
No toca ni depende del modelo de tiempo/VRP ni de calcular_recorrido_cobertura_total
(ver test_red_propia_cobertura.py para eso).

Se corre como script, igual que los otros tests de core/.
"""
import time

import geopandas as gpd
from shapely.geometry import Polygon, LineString

from core import optimizador as core


def test_agrupar_poligonos_por_atributo():
    zona_a1 = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    zona_a2 = Polygon([(1, 0), (2, 0), (2, 1), (1, 1)])  # mismo "sector" que zona_a1
    zona_b = Polygon([(5, 5), (6, 5), (6, 6), (5, 6)])
    gdf = gpd.GeoDataFrame(
        {"sector": ["A", "A", "B"]},
        geometry=[zona_a1, zona_a2, zona_b],
        crs="EPSG:4326",
    )

    # Sin columna (o columna inexistente) -- todo el archivo es UNA sola zona.
    grupos_sin_col = core.agrupar_poligonos_por_atributo(gdf, None)
    assert len(grupos_sin_col) == 1
    assert grupos_sin_col[0][0] is None
    print("✔ agrupar_poligonos_por_atributo: sin columna, todo queda en una sola zona")

    grupos_col_inexistente = core.agrupar_poligonos_por_atributo(gdf, "no_existe")
    assert len(grupos_col_inexistente) == 1
    print("✔ agrupar_poligonos_por_atributo: columna inexistente cae igual a una sola zona")

    # Con columna -- se agrupa por valor, filas con el mismo valor se unen
    # en una sola geometría (zona_a1 + zona_a2 -> un solo polígono "A").
    grupos = core.agrupar_poligonos_por_atributo(gdf, "sector")
    assert len(grupos) == 2, "deberían quedar 2 zonas (A y B)"
    etiquetas = sorted(e for e, _ in grupos)
    assert etiquetas == ["A", "B"]
    zona_a_unida = next(p for e, p in grupos if e == "A")
    assert zona_a_unida.area == zona_a1.area + zona_a2.area, "las 2 filas de A deben unirse en un solo polígono"
    print("✔ agrupar_poligonos_por_atributo: agrupa por columna y une filas con el mismo valor")


def test_reproyectar_a_wgs84():
    # Coordenadas ya en rango lon/lat válido, sin CRS -- se asume que ya es
    # WGS84, no se toca (comportamiento histórico).
    linea_normal = LineString([(-84.1, 9.9), (-84.09, 9.91)])
    gdf_normal = gpd.GeoDataFrame(geometry=[linea_normal])  # sin CRS
    resultado = core._reproyectar_a_wgs84(gdf_normal)
    coords = list(resultado.geometry.iloc[0].coords)
    assert coords[0] == (-84.1, 9.9), "coordenadas ya válidas no deben tocarse"
    print("✔ _reproyectar_a_wgs84: coordenadas lon/lat válidas sin CRS se dejan tal cual")

    # Coordenadas proyectadas (CRTM05, en metros) sin CRS -- deben
    # reproyectarse a lon/lat real de Costa Rica, no tratarse como si ya
    # fueran grados (bug real, encontrado con un shapefile municipal real
    # sin .prj -- ver core/optimizador.py _reproyectar_a_wgs84).
    linea_crtm05 = LineString([(485229.48, 1111961.54), (485150.07, 1111925.42)])
    gdf_crtm05 = gpd.GeoDataFrame(geometry=[linea_crtm05])  # sin CRS
    resultado2 = core._reproyectar_a_wgs84(gdf_crtm05)
    lon, lat = list(resultado2.geometry.iloc[0].coords)[0]
    assert -180 <= lon <= 180 and -90 <= lat <= 90, "debe quedar en rango lon/lat válido"
    assert -85 < lon < -83 and 9 < lat < 11, "debe caer dentro de Costa Rica, no quedar en metros crudos"
    print(f"✔ _reproyectar_a_wgs84: coordenadas proyectadas (CRTM05) sin .prj se reproyectan bien ({lon:.4f}, {lat:.4f})")


def test_normalizar_gdf_poligonos():
    poligono = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    linea = LineString([(0, 0), (1, 1)])
    gdf_mixto = gpd.GeoDataFrame(geometry=[poligono, linea], crs="EPSG:4326")

    gdf_filtrado, error = core._normalizar_gdf_poligonos(gdf_mixto)
    assert error is None
    assert len(gdf_filtrado) == 1, "debe quedar solo la geometría de polígono, la línea se descarta"
    print("✔ _normalizar_gdf_poligonos: filtra solo geometrías de polígono")

    gdf_solo_lineas = gpd.GeoDataFrame(geometry=[linea], crs="EPSG:4326")
    _, error2 = core._normalizar_gdf_poligonos(gdf_solo_lineas)
    assert error2 is not None, "sin ningún polígono, debe devolver un mensaje de error"
    print("✔ _normalizar_gdf_poligonos: sin polígonos, devuelve error en vez de un GeoDataFrame vacío silencioso")


def test_descargar_red_osm_por_zonas_reparte_y_cachea():
    # Monkeypatch de osmnx para no depender de una conexión real a Overpass
    # -- simula 3 calles: 2 dentro de la zona A, 1 dentro de la zona B.
    import osmnx as ox

    llamadas = {"n": 0}

    def fake_graph_from_bbox(bbox, network_type="drive", simplify=True):
        llamadas["n"] += 1
        return "grafo-falso"

    calles = [
        LineString([(0.1, 0.1), (0.4, 0.1)]),
        LineString([(0.4, 0.1), (0.4, 0.4)]),
        LineString([(2.1, 2.1), (2.4, 2.1)]),
    ]
    fake_gdf = gpd.GeoDataFrame(geometry=calles, crs="EPSG:4326")

    graph_from_bbox_original = ox.graph_from_bbox
    graph_to_gdfs_original = ox.graph_to_gdfs
    ox.graph_from_bbox = fake_graph_from_bbox
    ox.graph_to_gdfs = lambda G, nodes=False, edges=True: fake_gdf
    # Bbox distinto (lejos de otros tests) para no chocar con la caché de
    # otra corrida -- el caché es un dict de módulo, vive entre tests.
    zona_a = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    zona_b = Polygon([(2, 2), (3, 2), (3, 3), (2, 3)])
    grupos = [("A", zona_a), ("B", zona_b)]

    try:
        resultado = core.descargar_red_osm_por_zonas(grupos)
        por_etiqueta = dict(resultado)
        assert len(por_etiqueta["A"]) == 2, "las 2 calles dentro de la zona A deben asignarse ahí"
        assert len(por_etiqueta["B"]) == 1, "la calle dentro de la zona B debe asignarse ahí"
        print("✔ descargar_red_osm_por_zonas: reparte cada calle a la zona con la que se superpone")

        # Repetir con el MISMO bbox -- no debe volver a llamar a osmnx (caché).
        n_antes = llamadas["n"]
        core.descargar_red_osm_por_zonas(grupos)
        core.descargar_red_osm_por_zonas(grupos)
        assert llamadas["n"] == n_antes, "una descarga del mismo bbox no debe repetir la consulta a OSM"
        print("✔ descargar_red_osm_por_zonas: reusa la descarga cacheada para el mismo bbox")
    finally:
        ox.graph_from_bbox = graph_from_bbox_original
        ox.graph_to_gdfs = graph_to_gdfs_original


def test_descargar_red_osm_en_poligono_repara_invalido():
    # Polígono "bowtie" (auto-intersección) -- geometría inválida, común en
    # shapefiles municipales digitalizados a mano. Si se le pasa así a
    # osmnx tal cual, el recorte al polígono puede fallar en silencio (ver
    # docstring de descargar_red_osm_en_poligono) -- debe repararse con
    # buffer(0) ANTES de llamar a osmnx.
    import osmnx as ox

    poligono_invalido = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])
    assert not poligono_invalido.is_valid, "el polígono de prueba debe ser inválido a propósito"

    recibido = {}

    def fake_graph_from_polygon(poligono, network_type="drive", simplify=True):
        recibido["poligono"] = poligono
        return "grafo-falso"

    graph_from_polygon_original = ox.graph_from_polygon
    graph_to_gdfs_original = ox.graph_to_gdfs
    ox.graph_from_polygon = fake_graph_from_polygon
    ox.graph_to_gdfs = lambda G, nodes=False, edges=True: gpd.GeoDataFrame(
        geometry=[LineString([(0.1, 0.1), (0.2, 0.2)])], crs="EPSG:4326",
    )
    try:
        core.descargar_red_osm_en_poligono(poligono_invalido)
        assert recibido["poligono"].is_valid, "el polígono reparado (buffer(0)) debe pasarse a osmnx, no el inválido"
        print("✔ descargar_red_osm_en_poligono: repara un polígono inválido (buffer(0)) antes de pedirlo a osmnx")
    finally:
        ox.graph_from_polygon = graph_from_polygon_original
        ox.graph_to_gdfs = graph_to_gdfs_original


def test_descargar_grafo_osm_con_reintentos_prueba_espejos():
    # Si el primer espejo de Overpass falla (timeout, servidor caído), debe
    # probar el siguiente en vez de fallar de una -- y devolver el timeout
    # y la URL de Overpass a como estaban antes de terminar, sea que haya
    # funcionado o no.
    import osmnx as ox

    timeout_original = ox.settings.requests_timeout
    url_original = ox.settings.overpass_url
    intentos = []

    def descarga_falla_las_primeras_2_veces():
        intentos.append(ox.settings.overpass_url)
        if len(intentos) < 3:
            raise ConnectionError("simulando servidor caído")
        return "grafo-exitoso"

    try:
        resultado = core._descargar_grafo_osm_con_reintentos(descarga_falla_las_primeras_2_veces)
        assert resultado == "grafo-exitoso"
        assert len(intentos) == 3, "debe probar hasta encontrar un espejo que funcione"
        assert len(set(intentos)) == 3, "cada intento debe haber usado un espejo DISTINTO"
        print("✔ _descargar_grafo_osm_con_reintentos: si un espejo falla, prueba el siguiente hasta que uno funcione")
    finally:
        assert ox.settings.requests_timeout == timeout_original, "el timeout original debe quedar restaurado"
        assert ox.settings.overpass_url == url_original, "la URL de Overpass original debe quedar restaurada"
        print("✔ _descargar_grafo_osm_con_reintentos: restaura timeout/URL originales de osmnx al terminar")

    # Si TODOS los espejos fallan, debe levantar un error claro (no colgarse
    # ni devolver None en silencio).
    def descarga_siempre_falla():
        raise TimeoutError("todos caídos")

    try:
        core._descargar_grafo_osm_con_reintentos(descarga_siempre_falla)
        assert False, "debería haber lanzado una excepción -- ningún espejo respondió"
    except ConnectionError:
        print("✔ _descargar_grafo_osm_con_reintentos: si TODOS los espejos fallan, levanta un error claro")


def test_todos_los_escenarios():
    test_agrupar_poligonos_por_atributo()
    test_reproyectar_a_wgs84()
    test_normalizar_gdf_poligonos()
    test_descargar_red_osm_por_zonas_reparte_y_cachea()
    test_descargar_red_osm_en_poligono_repara_invalido()
    test_descargar_grafo_osm_con_reintentos_prueba_espejos()
    print("\n=== TODOS LOS ESCENARIOS PASARON ===")


if __name__ == "__main__":
    test_todos_los_escenarios()
