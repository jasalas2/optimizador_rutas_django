"""
Prueba standalone de calcular_recorrido_cobertura_total() (Problema del
Cartero Chino / Route Inspection Problem) -- el motor nuevo de "Red propia"
para recorrer una red completa (no elegir el mejor orden entre puntos, como
hace resolver_vrp). No toca ni depende del modelo de tiempo/VRP existente.

Se corre como script, igual que test_modelo_tiempo.py.
"""
import networkx as nx

from core import optimizador as core


def test_todos_los_escenarios():
    # Escenario 1: cuadrado con un callejón sin salida -- el callejón se
    # recorre ida y vuelta (única forma de cubrirlo en un circuito cerrado).
    g1 = nx.Graph()
    g1.add_edge(0, 1, weight=10)
    g1.add_edge(1, 2, weight=10)
    g1.add_edge(2, 3, weight=10)
    g1.add_edge(3, 0, weight=10)
    g1.add_edge(1, 4, weight=5)
    r1 = core.calcular_recorrido_cobertura_total(g1, 0)
    assert r1 is not None
    assert r1["ruta_nodos"][0] == 0 and r1["ruta_nodos"][-1] == 0, "el circuito debe cerrar en el nodo de inicio"
    assert r1["distancia_original_m"] == 45
    assert r1["distancia_repetida_m"] == 5, "el callejón (5m) se repite una sola vez, no dos"
    assert r1["distancia_total_m"] == 50
    print("✔ Callejón sin salida: se recorre ida y vuelta, circuito cierra en el inicio")

    # Escenario 2: triángulo, todos los nodos ya de grado par -- no debería
    # duplicar ninguna arista (el circuito euleriano existe de entrada).
    g2 = nx.Graph()
    g2.add_edge(0, 1, weight=3)
    g2.add_edge(1, 2, weight=4)
    g2.add_edge(2, 0, weight=5)
    r2 = core.calcular_recorrido_cobertura_total(g2, 0)
    assert r2["distancia_repetida_m"] == 0
    assert r2["distancia_total_m"] == 12
    print("✔ Grafo ya euleriano: no se repite ninguna arista")

    # Escenario 3: dos componentes desconectados -- solo se resuelve el que
    # contiene nodo_inicio, el otro se reporta como excluido (no falla).
    g3 = nx.Graph()
    g3.add_edge(0, 1, weight=1)
    g3.add_edge(1, 2, weight=1)
    g3.add_edge(10, 11, weight=1)
    g3.add_edge(11, 12, weight=1)
    g3.add_edge(12, 10, weight=1)
    r3 = core.calcular_recorrido_cobertura_total(g3, 0)
    assert r3["componente_size"] == 3
    assert r3["nodos_excluidos"] == 3
    print("✔ Red fragmentada: resuelve solo el componente del nodo de inicio, avisa del resto")

    # Escenario 4: nodo aislado sin aristas -- caso trivial, no debe fallar.
    g4 = nx.Graph()
    g4.add_node(99)
    r4 = core.calcular_recorrido_cobertura_total(g4, 99)
    assert r4["ruta_nodos"] == [99]
    assert r4["distancia_total_m"] == 0
    print("✔ Nodo aislado sin aristas: caso trivial sin error")

    # Escenario 5: nodo de inicio que no existe en el grafo -- debe devolver
    # None en vez de lanzar una excepción.
    r5 = core.calcular_recorrido_cobertura_total(g1, 999)
    assert r5 is None
    print("✔ Nodo de inicio inexistente: devuelve None")

    # Escenario 6: cadena larga (60 nodos, 59 aristas, ambos extremos de
    # grado impar) -- fuerza el camino de emparejamiento con muchos nodos
    # impares relativo al tamaño del grafo, y ejercita el reintento con más
    # vecinos candidatos si el vecindario inicial no alcanza.
    g6 = nx.path_graph(60)
    for a, b in g6.edges():
        g6[a][b]["weight"] = 1.0
    r6 = core.calcular_recorrido_cobertura_total(g6, 0)
    assert r6["ruta_nodos"][0] == 0 and r6["ruta_nodos"][-1] == 0
    assert r6["distancia_total_m"] == 118.0, "cadena de 59 tramos: ida (59) + vuelta completa (59)"
    print("✔ Cadena larga: emparejamiento con reintento de vecindario funciona")

    print("\n=== TODOS LOS ESCENARIOS PASARON ===")


if __name__ == "__main__":
    test_todos_los_escenarios()
