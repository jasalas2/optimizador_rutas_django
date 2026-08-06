import io
import json
import math
import threading
import time
from datetime import datetime as dt

import geopandas as gpd
import networkx as nx
import pandas as pd
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from shapely.geometry import LineString

from core.optimizador import (
    DIAS_SEMANA,
    TIPOS_VIA_DEFAULT,
    ajustar_una_linea_con_osrm,
    bbox_de_camino,
    calcular_recorrido_cobertura_total,
    calcular_rutas_para_puntos,
    clasificar_tramos_ruta,
    construir_grafo_red,
    construir_indice_vias,
    contar_componentes_red,
    costo_diario_inversion,
    costo_diario_recurrente,
    descargar_red_osm_clasificada,
    dias_desde_ultima_recoleccion,
    enganchar_a_red,
    explotar_lineas_simples,
    exportar_geojson,
    exportar_gpx,
    exportar_kml,
    exportar_shapefile,
    filtrar_camiones_para_grupo,
    generar_links_google_maps,
    geocodificar_direccion,
    leer_capa_lineas,
    limpiar_lineas_con_osrm,
    obtener_ruta_completa_osrm_por_leg,
    peso_estimado_ruta_para_dia,
    reconstruir_viajes_desde_resumen,
    resolver_vrp,
)
from .models import (
    Camion,
    CamionDisponibilidad,
    ConfiguracionGeneral,
    CostoInversion,
    CostoRecurrente,
    CostosGenerales,
    Punto,
    RedPropiaCargaProgreso,
    RedPropiaGrafo,
    RedPropiaPunto,
    RedPropiaResultado,
    ResultadoCalculo,
    RutaFrecuencia,
    TasaViaKgKm,
    ViaResultado,
)

TIPO_VIA_COLOR = {
    "motorway": "#E74C3C", "trunk": "#E67E22", "primary": "#F1C40F",
    "secondary": "#27AE60", "tertiary": "#3498DB", "residential": "#8E44AD",
    "otro": "#7F8C8D",
}


def _errores_de_instancia(instancia, etiqueta, exclude=None):
    """Corre las validaciones del modelo (validators de campo, tipos, etc.)
    sobre una instancia todavía no guardada. Devuelve una lista de mensajes
    "etiqueta: campo — mensaje", vacía si todo está bien."""
    try:
        instancia.full_clean(exclude=exclude)
    except ValidationError as e:
        errores = []
        for campo, mensajes in e.message_dict.items():
            for m in mensajes:
                errores.append(f"{etiqueta}: {campo} — {m}")
        return errores
    return []


def _punto_to_row(p):
    return {
        "id": p.id,
        "Nombre": p.nombre,
        "Dirección": p.direccion,
        "Latitud": p.latitud,
        "Longitud": p.longitud,
        "Peso (kg)": p.peso_kg,
        "Camión": p.camion_asignado.nombre if p.camion_asignado_id else "Auto",
        "Cantón": p.canton,
        "Distrito": p.distrito,
    }


def _metricas_puntos(puntos_qs):
    peso_total = sum(p.peso_kg or 0 for p in puntos_qs)
    camiones = list(Camion.objects.all())
    cap_flota_efectiva = sum((c.capacidad_kg or 0) * (c.viajes_max or 1) for c in camiones)
    capacidad_max_flota = max((c.capacidad_kg or 0) for c in camiones) if camiones else 0

    puntos_pesados = []
    if camiones:
        for p in puntos_qs:
            if (p.peso_kg or 0) > capacidad_max_flota:
                cabe_en = [c.nombre for c in camiones if (p.peso_kg or 0) <= c.capacidad_kg]
                puntos_pesados.append({
                    "Punto": p.nombre,
                    "Peso (kg)": p.peso_kg,
                    "Cabe en algún camión": ", ".join(cabe_en) if cabe_en else "Ninguno",
                })

    return {
        "peso_total": peso_total,
        "cap_flota_efectiva": cap_flota_efectiva,
        "excede_capacidad": peso_total > cap_flota_efectiva,
        "puntos_pesados": puntos_pesados,
    }


def puntos_view(request):
    puntos = Punto.objects.select_related("camion_asignado").order_by("id")
    nombres_camiones = list(Camion.objects.order_by("nombre").values_list("nombre", flat=True))
    context = {
        "puntos_json": json.dumps([_punto_to_row(p) for p in puntos]),
        "camiones_json": json.dumps(["Auto"] + nombres_camiones),
        "metricas_json": json.dumps(_metricas_puntos(puntos)),
    }
    return render(request, "rutas/puntos.html", context)


def _guardar_filas(filas):
    """Reemplaza todos los puntos por las filas recibidas (mismo comportamiento
    que guardar_puntos() en Streamlit: borra todo y vuelve a insertar,
    descartando filas sin Nombre). Valida todo ANTES de tocar la base — si
    alguna fila tiene datos fuera de rango, no se guarda nada y se devuelven
    los errores para que el usuario los corrija."""
    camiones_por_nombre = {c.nombre: c for c in Camion.objects.all()}
    nuevos = []
    errores = []
    for fila in filas:
        nombre = (fila.get("Nombre") or "").strip()
        if not nombre:
            continue
        camion_nombre = fila.get("Camión") or "Auto"
        p = Punto(
            nombre=nombre,
            direccion=fila.get("Dirección") or "",
            latitud=fila.get("Latitud"),
            longitud=fila.get("Longitud"),
            peso_kg=fila.get("Peso (kg)") or 0,
            camion_asignado=camiones_por_nombre.get(camion_nombre),
            canton=fila.get("Cantón") or "",
            distrito=fila.get("Distrito") or "",
        )
        errores += _errores_de_instancia(p, nombre, exclude=["camion_asignado"])
        nuevos.append(p)
    if errores:
        return errores
    Punto.objects.all().delete()
    Punto.objects.bulk_create(nuevos)
    return []


@require_POST
def api_guardar_puntos(request):
    filas = json.loads(request.body)["filas"]
    errores = _guardar_filas(filas)
    if errores:
        return JsonResponse({"errores": errores}, status=400)
    puntos = Punto.objects.select_related("camion_asignado").order_by("id")
    return JsonResponse({
        "filas": [_punto_to_row(p) for p in puntos],
        "metricas": _metricas_puntos(puntos),
    })


@require_POST
def api_geocodificar_puntos(request):
    filas = json.loads(request.body)["filas"]
    errores_guardado = _guardar_filas(filas)
    if errores_guardado:
        return JsonResponse({"errores": errores_guardado}, status=400)

    pendientes = Punto.objects.filter(latitud__isnull=True) | Punto.objects.filter(longitud__isnull=True)
    pendientes = pendientes.exclude(direccion="").distinct()

    errores = []
    for p in pendientes:
        lat, lon, err = geocodificar_direccion(p.direccion)
        if lat is not None:
            p.latitud, p.longitud = lat, lon
            p.save(update_fields=["latitud", "longitud"])
        else:
            errores.append(f"{p.direccion}: {err}")
        time.sleep(1)

    puntos = Punto.objects.select_related("camion_asignado").order_by("id")
    return JsonResponse({
        "filas": [_punto_to_row(p) for p in puntos],
        "metricas": _metricas_puntos(puntos),
        "errores": errores,
    })


def _camion_to_row(c):
    return {
        "id": c.id,
        "Nombre": c.nombre,
        "Capacidad (kg)": c.capacidad_kg,
        "Personas": c.personas,
        "Viajes máx.": c.viajes_max,
        "Plantel Lat": c.plantel_lat,
        "Plantel Lon": c.plantel_lon,
        "Cantón asignado": c.canton_asignado,
        "Distrito asignado": c.distrito_asignado,
    }


def _metricas_camiones(camiones_qs):
    validos = [c for c in camiones_qs if c.nombre and c.capacidad_kg]
    if not validos:
        return None
    cap_total = sum(c.capacidad_kg for c in validos)
    personas_total = sum(c.personas or 1 for c in validos)
    cap_total_viajes = sum(c.capacidad_kg * (c.viajes_max or 1) for c in validos)
    return {
        "cap_total": cap_total,
        "personas_total": personas_total,
        "cap_total_viajes": cap_total_viajes,
    }


def camiones_view(request):
    camiones = Camion.objects.order_by("id")

    nombres_camiones = [c.nombre for c in camiones if c.nombre]
    disponibilidad = {
        cd.nombre_camion: cd.dias
        for cd in CamionDisponibilidad.objects.filter(nombre_camion__in=nombres_camiones)
    }
    camiones_info = []
    for idx, nombre in enumerate(nombres_camiones):
        dias_guardados = {d.strip() for d in disponibilidad.get(nombre, "").split(",") if d.strip()}
        camiones_info.append({
            "idx": idx,
            "nombre": nombre,
            "dias": [{"nombre": dia, "activo": dia in dias_guardados} for dia in DIAS_SEMANA],
        })

    context = {
        "camiones_json": json.dumps([_camion_to_row(c) for c in camiones]),
        "metricas_json": json.dumps(_metricas_camiones(camiones)),
        "camiones_info": camiones_info,
        "dias_semana": DIAS_SEMANA,
    }
    return render(request, "rutas/camiones.html", context)


@require_POST
def api_toggle_disponibilidad_dia(request):
    """Prende/apaga UN camión para UN día puntual -- versión rápida de
    api_guardar_disponibilidad_camiones (que guarda la semana completa de
    todos los camiones de una), usada desde las burbujas clicables de
    Calcular. Mismo modelo (CamionDisponibilidad), misma semántica: `dias`
    vacío = disponible todos los días."""
    data = json.loads(request.body)
    nombre_camion = (data.get("nombre_camion") or "").strip()
    dia = (data.get("dia") or "").strip()
    if not nombre_camion or dia not in DIAS_SEMANA:
        return JsonResponse({"error": "Parámetros inválidos."}, status=400)
    if not Camion.objects.filter(nombre=nombre_camion).exists():
        return JsonResponse({"error": "Ese camión ya no existe."}, status=404)

    cd, _ = CamionDisponibilidad.objects.get_or_create(nombre_camion=nombre_camion)
    dias_actuales = {d.strip() for d in cd.dias.split(",") if d.strip()}
    if not dias_actuales:
        dias_actuales = set(DIAS_SEMANA)  # "sin restricción" == disponible todos los días

    if dia in dias_actuales:
        dias_actuales.discard(dia)
    else:
        dias_actuales.add(dia)

    cd.dias = "" if dias_actuales == set(DIAS_SEMANA) else ",".join(d for d in DIAS_SEMANA if d in dias_actuales)
    cd.save()
    disponible = dia in dias_actuales
    return JsonResponse({"disponible": disponible})


@require_POST
def api_guardar_disponibilidad_camiones(request):
    idx = 0
    while f"camion__{idx}" in request.POST:
        camion_nombre = request.POST[f"camion__{idx}"]
        dias_marcados = request.POST.getlist(f"dias__{idx}")
        CamionDisponibilidad.objects.update_or_create(
            nombre_camion=camion_nombre, defaults={"dias": ",".join(dias_marcados)}
        )
        idx += 1
    messages.success(request, "Disponibilidad guardada.")
    return redirect("rutas:camiones")


def _guardar_filas_camiones(filas):
    """Reemplaza todos los camiones por las filas recibidas — mismo
    comportamiento que guardar_camiones() en Streamlit (borra todo, vuelve
    a insertar, descartando filas sin Nombre). Los puntos que tenían un
    camión asignado que ya no existe vuelven a "Auto" (SET_NULL). Valida
    todo antes de tocar la base — ver _guardar_filas (Puntos)."""
    nuevos = []
    errores = []
    for fila in filas:
        nombre = (fila.get("Nombre") or "").strip()
        if not nombre or fila.get("Capacidad (kg)") in (None, ""):
            continue
        c = Camion(
            nombre=nombre,
            capacidad_kg=fila.get("Capacidad (kg)") or 1000,
            personas=fila.get("Personas") or 1,
            viajes_max=fila.get("Viajes máx.") or 1,
            plantel_lat=fila.get("Plantel Lat"),
            plantel_lon=fila.get("Plantel Lon"),
            canton_asignado=fila.get("Cantón asignado") or "",
            distrito_asignado=fila.get("Distrito asignado") or "",
        )
        errores += _errores_de_instancia(c, nombre)
        nuevos.append(c)
    if errores:
        return errores
    Camion.objects.all().delete()
    Camion.objects.bulk_create(nuevos)
    return []


@require_POST
def api_guardar_camiones(request):
    filas = json.loads(request.body)["filas"]
    errores = _guardar_filas_camiones(filas)
    if errores:
        return JsonResponse({"errores": errores}, status=400)
    camiones = Camion.objects.order_by("id")
    return JsonResponse({
        "filas": [_camion_to_row(c) for c in camiones],
        "metricas": _metricas_camiones(camiones),
    })


def _parsear_hora(valor, default, etiqueta, warnings):
    try:
        return dt.strptime((valor or "").strip(), "%H:%M").time()
    except ValueError:
        warnings.append(f"{etiqueta}: formato inválido, usando {default.strftime('%H:%M')}.")
        return default


def _parsear_num(valor, default, tipo=float):
    try:
        return tipo(valor)
    except (TypeError, ValueError):
        return default


def configuracion_view(request):
    config = ConfiguracionGeneral.cargar()

    if request.method == "POST":
        warnings = []
        config.hora_inicio = _parsear_hora(
            request.POST.get("hora_inicio"), ConfiguracionGeneral._meta.get_field("hora_inicio").default,
            "Hora de inicio", warnings)
        config.velocidad_kmh = _parsear_num(request.POST.get("velocidad_kmh"), config.velocidad_kmh, int)
        config.tiempo_parada = _parsear_num(request.POST.get("tiempo_parada"), config.tiempo_parada, int)
        config.tiempo_descarga = _parsear_num(request.POST.get("tiempo_descarga"), config.tiempo_descarga, int)

        config.usar_almuerzo = request.POST.get("usar_almuerzo") == "on"
        if config.usar_almuerzo:
            config.hora_almuerzo_inicio = _parsear_hora(
                request.POST.get("hora_almuerzo_inicio"), ConfiguracionGeneral._meta.get_field("hora_almuerzo_inicio").default,
                "Almuerzo — inicio", warnings)
            config.hora_almuerzo_fin = _parsear_hora(
                request.POST.get("hora_almuerzo_fin"), ConfiguracionGeneral._meta.get_field("hora_almuerzo_fin").default,
                "Almuerzo — fin", warnings)
            if config.hora_almuerzo_fin <= config.hora_almuerzo_inicio:
                messages.error(request, "El fin del almuerzo debe ser después del inicio.")
                return render(request, "rutas/configuracion.html", {"config": config})

        config.tiempo_lavado = _parsear_num(request.POST.get("tiempo_lavado"), config.tiempo_lavado, int)

        config.tope_horas_jornada = _parsear_num(request.POST.get("tope_horas_jornada"), config.tope_horas_jornada)

        config.velocidad_variable_via = request.POST.get("velocidad_variable_via") == "on"
        if config.velocidad_variable_via:
            config.velocidad_rapida_kmh = _parsear_num(request.POST.get("velocidad_rapida_kmh"), config.velocidad_rapida_kmh, int)

        config.balancear = request.POST.get("balancear") == "on"

        config.peso_minimo_viaje_extra_kg = _parsear_num(
            request.POST.get("peso_minimo_viaje_extra_kg"), config.peso_minimo_viaje_extra_kg, int)

        config.depot2_lat = _parsear_num(request.POST.get("depot2_lat"), config.depot2_lat)
        config.depot2_lon = _parsear_num(request.POST.get("depot2_lon"), config.depot2_lon)

        config.porcentaje_relleno = _parsear_num(request.POST.get("porcentaje_relleno"), config.porcentaje_relleno)
        relleno_lat = request.POST.get("relleno_lat", "").strip()
        relleno_lon = request.POST.get("relleno_lon", "").strip()
        config.relleno_lat = _parsear_num(relleno_lat, None) if relleno_lat else None
        config.relleno_lon = _parsear_num(relleno_lon, None) if relleno_lon else None

        errores = _errores_de_instancia(config, "Configuración")
        if errores:
            for e in errores:
                messages.error(request, e)
            return render(request, "rutas/configuracion.html", {"config": config})

        config.save()

        for w in warnings:
            messages.warning(request, w)
        messages.success(request, "Guardada")
        return redirect("rutas:configuracion")

    return render(request, "rutas/configuracion.html", {"config": config})


MODOS_CALCULO = [
    "Todos los puntos juntos",
    "Una ruta por Distrito",
    "Una ruta por Cantón",
    "Mixto (elegir nivel por cantón)",
]


def _puntos_dataframe():
    cols = ["Nombre", "Latitud", "Longitud", "Peso (kg)", "Camión", "Cantón", "Distrito"]
    filas = [{
        "Nombre": p.nombre, "Latitud": p.latitud, "Longitud": p.longitud,
        "Peso (kg)": p.peso_kg,
        "Camión": p.camion_asignado.nombre if p.camion_asignado_id else "Auto",
        "Cantón": p.canton, "Distrito": p.distrito,
    } for p in Punto.objects.select_related("camion_asignado").all()]
    return pd.DataFrame(filas, columns=cols)


def _camiones_disponibles_ese_dia(dia):
    """Nombres de camión EXCLUIDOS de un día dado — los que tienen
    disponibilidad configurada (CamionDisponibilidad) y ese día no está
    entre los suyos. Un camión sin fila de disponibilidad (o con "dias"
    vacío) está disponible todos los días — comportamiento por defecto,
    para no afectar a nadie que no use este filtro."""
    if not dia:
        return set()
    excluidos = set()
    for cd in CamionDisponibilidad.objects.all():
        dias_cam = {d.strip() for d in cd.dias.split(",") if d.strip()}
        if dias_cam and dia not in dias_cam:
            excluidos.add(cd.nombre_camion)
    return excluidos


def _camiones_dataframe(dia=""):
    cols = ["Nombre", "Capacidad (kg)", "Personas", "Viajes máx.", "Plantel Lat",
            "Plantel Lon", "Cantón asignado", "Distrito asignado"]
    excluidos = _camiones_disponibles_ese_dia(dia)
    filas = [{
        "Nombre": c.nombre, "Capacidad (kg)": c.capacidad_kg, "Personas": c.personas,
        "Viajes máx.": c.viajes_max, "Plantel Lat": c.plantel_lat, "Plantel Lon": c.plantel_lon,
        "Cantón asignado": c.canton_asignado, "Distrito asignado": c.distrito_asignado,
    } for c in Camion.objects.all() if c.nombre not in excluidos]
    return pd.DataFrame(filas, columns=cols)


def _tabla_semana(resultado, rutas_frecuencia):
    """Filas con claves simples (ascii, sin espacios) a propósito, para poder
    iterarlas con notación de punto en el template Django."""
    rutas_con_dias = [nom for nom, dias in rutas_frecuencia.items() if dias.strip()]
    filas = []
    for nom in rutas_con_dias:
        c_ruta = next((c for c in resultado["camiones"] if c["nombre"] == nom), None)
        if c_ruta is None:
            continue
        dias_ruta_str = rutas_frecuencia[nom]
        dias_de_la_ruta = {d.strip() for d in dias_ruta_str.split(",") if d.strip()}
        celdas = []
        for dia in DIAS_SEMANA:
            if dia in dias_de_la_ruta:
                peso_dia = peso_estimado_ruta_para_dia(c_ruta["peso_total"], dias_ruta_str, dia)
                valor = f"{peso_dia:,.0f} kg"
            else:
                valor = "-"
            celdas.append({"dia": dia[:3], "valor": valor})
        filas.append({"ruta": nom, "celdas": celdas})
    return filas


def _dia_actual_desde_request(request):
    dia_actual = request.GET.get("dia", "")
    if dia_actual not in DIAS_SEMANA:
        dia_actual = ""
    return dia_actual


def _construir_dias_tabs():
    dias_calculados = set(
        ResultadoCalculo.objects.exclude(resultado_json__isnull=True).values_list("dia", flat=True)
    )
    return [{"dia": "", "etiqueta": "Todos", "calculado": "" in dias_calculados}] + [
        {"dia": d, "etiqueta": d[:3], "calculado": d in dias_calculados} for d in DIAS_SEMANA
    ]


def calcular_view(request):
    dia_actual = _dia_actual_desde_request(request)

    cantones_disponibles = sorted({
        p.canton.strip() for p in Punto.objects.exclude(canton="") if p.canton.strip()
    })

    resultado_obj = ResultadoCalculo.cargar(dia_actual)
    dias_tabs = _construir_dias_tabs()

    nombres_camiones_todos = list(Camion.objects.order_by("nombre").values_list("nombre", flat=True))
    excluidos_hoy = _camiones_disponibles_ese_dia(dia_actual)
    camiones_disponibilidad = [
        {"nombre": n, "disponible": n not in excluidos_hoy} for n in nombres_camiones_todos
    ]

    context = {
        "modos_calculo": MODOS_CALCULO,
        "modo_calculo_actual": resultado_obj.modo_calculo or "Todos los puntos juntos",
        "cantones_disponibles": cantones_disponibles,
        "dia_actual": dia_actual,
        "dias_tabs": dias_tabs,
        "camiones_disponibilidad": camiones_disponibilidad,
        "camiones_activos_count": sum(1 for c in camiones_disponibilidad if c["disponible"]),
        "camiones_total_count": len(camiones_disponibilidad),
        "tiene_resultado_este_dia": bool(resultado_obj.resultado_json),
    }
    return render(request, "rutas/calcular.html", context)


def resultados_view(request):
    dia_actual = _dia_actual_desde_request(request)

    resultado_obj = ResultadoCalculo.cargar(dia_actual)
    resultado = resultado_obj.resultado_json
    dias_tabs = _construir_dias_tabs()

    rutas_frecuencia = {}
    tabla_semana = []
    if resultado:
        nombres_rutas = [c["nombre"] for c in resultado["camiones"]]
        frecuencias = {
            rf.nombre_ruta: rf.dias
            for rf in RutaFrecuencia.objects.filter(nombre_ruta__in=nombres_rutas)
        }
        rutas_frecuencia = {nom: frecuencias.get(nom, "") for nom in nombres_rutas}
        # Esta tabla multiplica el peso según cuántos días pasaron desde la
        # última recolección de esa ruta — tiene sentido en "Todos" (un solo
        # cálculo reusado toda la semana), pero NO cuando ya estás parado en
        # el resultado propio de un día específico (ese ya es el peso real
        # de ESE día, multiplicarlo de nuevo lo infla al doble/triple).
        if not dia_actual:
            tabla_semana = _tabla_semana(resultado, rutas_frecuencia)

    metricas = None
    camiones_excedidos = []
    rutas_info = []
    if resultado:
        config = ConfiguracionGeneral.cargar()
        peso_total = sum(c["peso_total"] for c in resultado["camiones"])
        peso_relleno = peso_total * config.porcentaje_relleno / 100
        metricas = {
            "camiones_usados": len(resultado["camiones"]),
            "dist_total_km": sum(c["dist_total_m"] for c in resultado["camiones"]) / 1000,
            "peso_total": peso_total,
            "hora_fin_max": max(c["hora_fin"] for c in resultado["camiones"]),
            "porcentaje_relleno": config.porcentaje_relleno,
            "peso_relleno": peso_relleno,
            "peso_neto": peso_total - peso_relleno,
        }
        camiones_excedidos = [c for c in resultado["camiones"] if c.get("excede_jornada")]

        for idx, c in enumerate(resultado["camiones"]):
            dias_guardados = {d.strip() for d in rutas_frecuencia.get(c["nombre"], "").split(",") if d.strip()}
            rutas_info.append({
                "idx": idx,
                "nombre": c["nombre"],
                "dias": [{"nombre": dia, "activo": dia in dias_guardados} for dia in DIAS_SEMANA],
            })

    context = {
        "resultado": resultado,
        "resultado_json": json.dumps(resultado) if resultado else "null",
        "rutas_info": rutas_info,
        "rutas_info_json": json.dumps(rutas_info),
        "dias_semana": DIAS_SEMANA,
        "dias_semana_json": json.dumps(DIAS_SEMANA),
        "tabla_semana": tabla_semana,
        "metricas": metricas,
        "camiones_excedidos": camiones_excedidos,
        "dia_actual": dia_actual,
        "dias_tabs": dias_tabs,
    }
    return render(request, "rutas/resultados.html", context)


def _reporte_por_ruta(resultado, config):
    """Arma, por cada camión/ruta ya calculada, los indicadores operativos
    del reporte (identificación, distancias por tipo de tramo, y tiempos).

    Distancias — se clasifica cada tramo del resumen según su tipo:
    - "parada": la PRIMERA parada de cada viaje es aproximación (viene
      vacío desde el plantel o desde el depot, tras la descarga anterior);
      las paradas siguientes del mismo viaje son recolección.
    - "descarga": tramo hasta la planta (cargado).
    - "fin_jornada": tramo final de regreso al plantel (vacío).

    Tiempos — no se re-derivan de los timestamps del resumen; se calculan
    a partir de la distancia y de los mismos parámetros de Configuración
    que ya usa el cálculo (velocidad, tiempo por parada, tiempo de
    descarga, tiempo de lavado), para quedar consistentes con esos valores.
    """
    filas = []
    for c in resultado["camiones"]:
        camion_real = c.get("camion_real") or c["nombre"]
        resumen = c["resumen"]

        disponibilidad = CamionDisponibilidad.objects.filter(nombre_camion=camion_real).first()
        dias_activos = [d.strip() for d in (disponibilidad.dias if disponibilidad else "").split(",") if d.strip()]
        dias_por_semana = len(dias_activos) if dias_activos else 7
        dias_operativos_anio = dias_por_semana * 52

        n_paradas = sum(1 for f in resumen if f["tipo"] == "parada")

        aproximacion_km = 0.0
        recoleccion_km = 0.0
        a_planta_km = 0.0
        retorno_km = 0.0
        primera_parada_vista = set()
        for f in resumen:
            dist_txt = f["Distancia tramo (km)"]
            if dist_txt in ("-", None):
                continue
            dist = float(dist_txt)
            if f["tipo"] == "parada":
                if f["trip_idx"] not in primera_parada_vista:
                    aproximacion_km += dist
                    primera_parada_vista.add(f["trip_idx"])
                else:
                    recoleccion_km += dist
            elif f["tipo"] == "descarga":
                a_planta_km += dist
            elif f["tipo"] == "fin_jornada":
                retorno_km += dist

        velocidad = config.velocidad_kmh or 1
        distancia_total_km = aproximacion_km + recoleccion_km + a_planta_km + retorno_km

        filas.append({
            "nombre": c["nombre"],
            "tipo_camion": camion_real,
            "dias_operativos_anio": dias_operativos_anio,
            "viajes_diarios": c["n_viajes_usados"],
            "toneladas_dia": c["peso_total"] / 1000,
            "aproximacion_km": aproximacion_km,
            "recoleccion_km": recoleccion_km,
            "a_planta_km": a_planta_km,
            "retorno_km": retorno_km,
            "horas_conduccion": distancia_total_km / velocidad,
            "horas_recoleccion": n_paradas * config.tiempo_parada / 60,
            "horas_descarga": c["n_viajes_usados"] * config.tiempo_descarga / 60,
            "horas_lavado": config.tiempo_lavado / 60,
        })
    return filas


def reporte_view(request):
    dia_actual = _dia_actual_desde_request(request)
    resultado_obj = ResultadoCalculo.cargar(dia_actual)
    resultado = resultado_obj.resultado_json
    dias_tabs = _construir_dias_tabs()
    config = ConfiguracionGeneral.cargar()

    context = {
        "resultado": resultado,
        "dia_actual": dia_actual,
        "dias_tabs": dias_tabs,
        "filas_reporte": _reporte_por_ruta(resultado, config) if resultado else [],
    }
    return render(request, "rutas/reporte.html", context)


def _resumen_calculo(resultado):
    """Métricas agregadas de un resultado de calcular_rutas_para_puntos --
    usado tanto por el simulador (líneas base y simulada) como se podría
    reusar en cualquier otro lado que solo necesite el resumen, no el detalle
    completo por camión."""
    peso_total = sum(c["peso_total"] for c in resultado["camiones"])
    return {
        "camiones_usados": len(resultado["camiones"]),
        "dist_total_km": sum(c["dist_total_m"] for c in resultado["camiones"]) / 1000,
        "peso_total": peso_total,
        "hora_fin_max": max(c["hora_fin"] for c in resultado["camiones"]),
        "camiones_excedidos": sum(1 for c in resultado["camiones"] if c.get("excede_jornada")),
    }


def _especificaciones_promedio_flota():
    """Capacidad/personas/viajes/plantel promedio de la flota REAL actual --
    usado tanto para armar un "camión simulado" en el Simulador como para
    crearlo de verdad si el usuario aplica esa simulación."""
    base = _camiones_dataframe().dropna(subset=["Nombre", "Capacidad (kg)"])
    if len(base) == 0:
        return None
    return {
        "capacidad_kg": base["Capacidad (kg)"].mean(),
        "personas": max(1, round(base["Personas"].mean())),
        "viajes_max": max(1, round(base["Viajes máx."].mean())),
        "plantel_lat": base["Plantel Lat"].dropna().mean(),
        "plantel_lon": base["Plantel Lon"].dropna().mean(),
    }


def _ejecutar_calculo_simulado(camiones_excluidos, camiones_extra, cambio_peso_pct):
    """Corre calcular_rutas_para_puntos con la flota/puntos actuales pero
    MODIFICADOS en memoria (nunca se guarda nada) -- para el Simulador
    "¿qué pasa si...?". Reusa la misma lógica que Calcular ("Todos los
    puntos juntos"), sin tocar core/optimizador.py. Devuelve el resultado
    COMPLETO (no solo el resumen) para poder animarlo en el mapa."""
    config = ConfiguracionGeneral.cargar()

    tabla_puntos = _puntos_dataframe().dropna(subset=["Latitud", "Longitud"]).copy()
    if len(tabla_puntos) < 1:
        return None, "Necesitás al menos 1 punto con coordenadas."
    tabla_puntos["Peso (kg)"] = tabla_puntos["Peso (kg)"] * (1 + cambio_peso_pct / 100)

    tabla_camiones = _camiones_dataframe().dropna(subset=["Nombre", "Capacidad (kg)"]).copy()
    if camiones_excluidos:
        tabla_camiones = tabla_camiones[~tabla_camiones["Nombre"].isin(camiones_excluidos)]

    if camiones_extra > 0:
        specs = _especificaciones_promedio_flota()
        if specs is not None:
            filas_extra = [{
                "Nombre": f"Camión simulado {i + 1}", "Capacidad (kg)": specs["capacidad_kg"],
                "Personas": specs["personas"], "Viajes máx.": specs["viajes_max"],
                "Plantel Lat": specs["plantel_lat"], "Plantel Lon": specs["plantel_lon"],
                "Cantón asignado": "", "Distrito asignado": "",
            } for i in range(camiones_extra)]
            tabla_camiones = pd.concat([tabla_camiones, pd.DataFrame(filas_extra)], ignore_index=True)

    tabla_camiones = tabla_camiones.dropna(subset=["Plantel Lat", "Plantel Lon"])
    if len(tabla_camiones) < 1:
        return None, "Necesitás al menos 1 camión activo (con plantel) en la simulación."

    kwargs_comunes = dict(
        depot2_lat=config.depot2_lat, depot2_lon=config.depot2_lon,
        hora_inicio=config.hora_inicio, velocidad_kmh=config.velocidad_kmh,
        tiempo_parada=config.tiempo_parada, balancear=config.balancear,
        velocidad_variable_via=config.velocidad_variable_via,
        velocidad_rapida_kmh=config.velocidad_rapida_kmh,
        tiempo_descarga=config.tiempo_descarga,
        hora_almuerzo_inicio=config.hora_almuerzo_inicio if config.usar_almuerzo else None,
        hora_almuerzo_fin=config.hora_almuerzo_fin if config.usar_almuerzo else None,
        tope_horas_jornada=config.tope_horas_jornada,
        peso_minimo_viaje_extra_kg=config.peso_minimo_viaje_extra_kg,
    )
    resultado, error = calcular_rutas_para_puntos(tabla_puntos, tabla_camiones, **kwargs_comunes)
    if error:
        return None, error
    return resultado, None


def simulador_view(request):
    camiones = list(Camion.objects.all())
    tiene_datos = Punto.objects.exclude(latitud__isnull=True).exclude(longitud__isnull=True).exists() and camiones
    resultado_actual, error_actual = (_ejecutar_calculo_simulado([], 0, 0) if tiene_datos else (None, None))
    config = ConfiguracionGeneral.cargar()
    context = {
        "camiones": camiones,
        "tiene_datos": tiene_datos,
        "resumen_actual": _resumen_calculo(resultado_actual) if resultado_actual else None,
        "resultado_actual_json": json.dumps(resultado_actual) if resultado_actual else "null",
        "error_actual": error_actual,
        "hora_inicio": config.hora_inicio.strftime("%H:%M"),
    }
    return render(request, "rutas/simulador.html", context)


@require_POST
def api_simulador_calcular(request):
    data = json.loads(request.body)
    camiones_excluidos = data.get("camiones_excluidos", [])
    try:
        camiones_extra = max(0, min(10, int(data.get("camiones_extra", 0))))
        cambio_peso_pct = max(-90, min(300, float(data.get("cambio_peso_pct", 0))))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Parámetros inválidos."}, status=400)

    resultado, error = _ejecutar_calculo_simulado(camiones_excluidos, camiones_extra, cambio_peso_pct)
    if error:
        return JsonResponse({"error": error}, status=400)
    return JsonResponse({"resumen": _resumen_calculo(resultado), "resultado": resultado})


@require_POST
def api_simulador_aplicar(request):
    """Crea de verdad, como Camion reales, los "camiones simulados" que el
    usuario probó en el Simulador -- NUNCA toca los camiones que se
    excluyeron en la simulación (eso es hipotético, no se borra nada real)
    ni el % de cambio de peso (es una proyección, no un dato a guardar)."""
    data = json.loads(request.body)
    try:
        camiones_extra = max(1, min(10, int(data.get("camiones_extra", 0))))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Parámetros inválidos."}, status=400)

    specs = _especificaciones_promedio_flota()
    if specs is None:
        return JsonResponse({"error": "Necesitás al menos 1 camión real para calcular el promedio."}, status=400)

    existentes = set(Camion.objects.values_list("nombre", flat=True))
    creados = []
    n = 1
    while len(creados) < camiones_extra and n <= 1000:
        nombre = f"Camión simulado {n}"
        n += 1
        if nombre in existentes:
            continue
        Camion.objects.create(
            nombre=nombre, capacidad_kg=specs["capacidad_kg"], personas=specs["personas"],
            viajes_max=specs["viajes_max"], plantel_lat=specs["plantel_lat"], plantel_lon=specs["plantel_lon"],
        )
        creados.append(nombre)
        existentes.add(nombre)
    return JsonResponse({"creados": creados})


def _redirect_calcular(dia):
    url = reverse("rutas:calcular")
    return redirect(f"{url}?dia={dia}" if dia else url)


def _redirect_resultados(dia):
    url = reverse("rutas:resultados")
    return redirect(f"{url}?dia={dia}" if dia else url)


def _aplicar_frecuencia_acumulada(camiones_res, dia, kwargs_comunes, nombre_frecuencia=None):
    """Si `dia` es un día específico y una ruta YA CALCULADA tiene una
    frecuencia guardada (RutaFrecuencia) que acumula más de un día para esa
    fecha, vuelve a resolver SOLO ese camión con el peso real acumulado —
    y con tantos viajes como hagan falta para cubrirlo, no el "Viajes máx."
    configurado (que es un promedio semanal, no el tope de un día pesado).
    Modifica camiones_res en el lugar.

    No aplica en modo "Todos" (dia=""), porque ahí la frecuencia solo sirve
    para la tabla informativa "Peso estimado por día de la semana" — cambiar
    ese resultado único reusado toda la semana sería un cambio de alcance
    mayor, no lo que se pidió acá.

    nombre_frecuencia: función opcional nombre_crudo_del_camión -> nombre
    usado para buscar en RutaFrecuencia (por defecto, el mismo nombre). En
    modo por zona la frecuencia se guarda con el nombre YA combinado (ej.
    "Barrantes — Camión Barrantes"), pero el Camion en la base de datos se
    busca siempre por su nombre crudo ("Camión Barrantes").
    """
    if not dia:
        return
    if nombre_frecuencia is None:
        nombre_frecuencia = lambda nombre: nombre  # noqa: E731

    nombres_busqueda = [nombre_frecuencia(c["nombre"]) for c in camiones_res]
    frecuencias = {
        rf.nombre_ruta: rf.dias
        for rf in RutaFrecuencia.objects.filter(nombre_ruta__in=nombres_busqueda)
    }
    for idx, c in enumerate(camiones_res):
        dias_ruta_str = frecuencias.get(nombre_frecuencia(c["nombre"]), "")
        dias_de_la_ruta = [d.strip() for d in dias_ruta_str.split(",") if d.strip()]
        if len(dias_de_la_ruta) < 2:
            continue  # sin frecuencia multi-día guardada, nada que acumular

        gap = dias_desde_ultima_recoleccion(dia, dias_de_la_ruta)
        if gap <= 1:
            continue

        camion = Camion.objects.filter(nombre=c["nombre"]).first()
        if camion is None or camion.plantel_lat is None or camion.plantel_lon is None:
            continue

        paradas = [f for f in c["resumen"] if f["tipo"] == "parada"]
        if not paradas:
            continue

        # El VRP recoge cada parada ENTERA en un solo viaje — no reparte el
        # peso de UN mismo punto entre dos viajes. Si al acumular el peso de
        # varios días un punto por sí solo supera la capacidad del camión
        # (ej. 10 000 kg con un camión de 5 000 kg), el cálculo sería
        # imposible tal cual. Para simular la vuelta real del camión, ese
        # punto se parte en "copias" en la misma ubicación, cada una hasta
        # la capacidad del camión — el VRP las reparte en viajes distintos,
        # con el costo real de ida/vuelta entre cada una.
        filas_puntos = []
        for f in paradas:
            restante = f["Peso recogido (kg)"] * gap
            while restante > 0:
                porcion = min(restante, camion.capacidad_kg)
                filas_puntos.append({
                    "Nombre": f["Nombre"], "Latitud": f["lat"], "Longitud": f["lon"],
                    "Peso (kg)": porcion, "Camión": "Auto",
                })
                restante -= porcion
        puntos_grupo = pd.DataFrame(filas_puntos)

        viajes_necesarios = max(1, math.ceil(puntos_grupo["Peso (kg)"].sum() / camion.capacidad_kg))
        cams_uno = pd.DataFrame([{
            "Nombre": camion.nombre, "Capacidad (kg)": camion.capacidad_kg,
            "Personas": camion.personas, "Viajes máx.": viajes_necesarios,
            "Plantel Lat": camion.plantel_lat, "Plantel Lon": camion.plantel_lon,
        }])

        resultado_uno, error = calcular_rutas_para_puntos(puntos_grupo, cams_uno, **kwargs_comunes)
        if error or not resultado_uno["camiones"]:
            continue
        nuevo = resultado_uno["camiones"][0]
        nuevo["nombre"] = c["nombre"]
        nuevo["gap_acumulado"] = gap
        camiones_res[idx] = nuevo


@require_POST
def api_ejecutar_calculo(request):
    dia = request.POST.get("dia", "")
    if dia not in DIAS_SEMANA:
        dia = ""

    config = ConfiguracionGeneral.cargar()
    modo_calculo = request.POST.get("modo_calculo", "Todos los puntos juntos")

    tabla = _puntos_dataframe()
    tabla_camiones = _camiones_dataframe(dia)

    puntos_todos = tabla.dropna(subset=["Latitud", "Longitud"])
    cams = tabla_camiones.dropna(subset=["Nombre", "Capacidad (kg)"])

    if len(puntos_todos) < 1:
        messages.error(request, "Necesitás al menos 1 punto con coordenadas.")
        return _redirect_calcular(dia)
    if len(cams) < 1:
        etiqueta_dia = f" disponible el {dia}" if dia else ""
        messages.error(request, f"Necesitás al menos 1 camión{etiqueta_dia} (pestaña Camiones).")
        return _redirect_calcular(dia)

    kwargs_comunes = dict(
        depot2_lat=config.depot2_lat, depot2_lon=config.depot2_lon,
        hora_inicio=config.hora_inicio, velocidad_kmh=config.velocidad_kmh,
        tiempo_parada=config.tiempo_parada, balancear=config.balancear,
        velocidad_variable_via=config.velocidad_variable_via,
        velocidad_rapida_kmh=config.velocidad_rapida_kmh,
        tiempo_descarga=config.tiempo_descarga,
        hora_almuerzo_inicio=config.hora_almuerzo_inicio if config.usar_almuerzo else None,
        hora_almuerzo_fin=config.hora_almuerzo_fin if config.usar_almuerzo else None,
        tope_horas_jornada=config.tope_horas_jornada,
        peso_minimo_viaje_extra_kg=config.peso_minimo_viaje_extra_kg,
    )

    if modo_calculo == "Todos los puntos juntos":
        resultado, error = calcular_rutas_para_puntos(puntos_todos, cams, **kwargs_comunes)
        if error:
            messages.error(request, error)
            return _redirect_calcular(dia)
        _aplicar_frecuencia_acumulada(resultado["camiones"], dia, kwargs_comunes)
    else:
        canton_de_distrito = {}
        for _, fila_p in puntos_todos.dropna(subset=["Distrito"]).iterrows():
            dist_val = str(fila_p["Distrito"]).strip()
            cant_val = str(fila_p.get("Cantón", "") or "").strip()
            if dist_val and cant_val and dist_val not in canton_de_distrito:
                canton_de_distrito[dist_val] = cant_val

        plan_grupos = []
        if modo_calculo == "Mixto (elegir nivel por cantón)":
            etiqueta_modo = "zona (mixto)"
            cantones_disponibles = sorted(
                v for v in puntos_todos["Cantón"].fillna("").unique() if str(v).strip() != ""
            )
            if not cantones_disponibles:
                messages.error(request, "No hay cantones para el modo Mixto — llenalo en la pestaña Puntos.")
                return _redirect_calcular(dia)
            for canton_actual in cantones_disponibles:
                nivel = request.POST.get(f"nivel__{canton_actual}", "Cantón completo")
                if nivel == "Cantón completo":
                    plan_grupos.append((canton_actual, "Cantón", canton_actual))
                else:
                    distritos_del_canton = sorted(
                        v for v in puntos_todos.loc[
                            puntos_todos["Cantón"] == canton_actual, "Distrito"
                        ].fillna("").unique() if str(v).strip() != ""
                    )
                    for distrito_val in distritos_del_canton:
                        plan_grupos.append((distrito_val, "Distrito", distrito_val))
        else:
            campo_grupo = "Cantón" if modo_calculo == "Una ruta por Cantón" else "Distrito"
            etiqueta_modo = campo_grupo.lower()
            valores = sorted(
                v for v in puntos_todos[campo_grupo].fillna("").unique() if str(v).strip() != ""
            )
            for valor in valores:
                plan_grupos.append((valor, campo_grupo, valor))

        if not plan_grupos:
            messages.error(
                request,
                f"No hay ningún punto con datos completos para agrupar por {etiqueta_modo} — "
                "revisá Cantón/Distrito en la pestaña Puntos.",
            )
            return _redirect_calcular(dia)

        resultados_grupos = {}
        errores_grupos = []
        for grupo_key, campo_grupo_local, valor in plan_grupos:
            puntos_grupo = puntos_todos[puntos_todos[campo_grupo_local] == valor]
            cams_grupo = filtrar_camiones_para_grupo(cams, campo_grupo_local, valor, canton_de_distrito)
            if len(cams_grupo) < 1:
                errores_grupos.append(
                    f"{grupo_key}: ningún camión está asignado a este "
                    f"{campo_grupo_local.lower()} (ni disponible como comodín) — "
                    "revisá 'Cantón/Distrito asignado' en Camiones."
                )
                continue
            resultado_grupo, error = calcular_rutas_para_puntos(puntos_grupo, cams_grupo, **kwargs_comunes)
            if error:
                errores_grupos.append(f"{grupo_key}: {error}")
            else:
                _aplicar_frecuencia_acumulada(
                    resultado_grupo["camiones"], dia, kwargs_comunes,
                    nombre_frecuencia=lambda n, gk=grupo_key: f"{gk} — {n}",
                )
                resultados_grupos[grupo_key] = resultado_grupo

        if not resultados_grupos:
            messages.error(
                request,
                f"No se pudo calcular ninguna ruta por {etiqueta_modo}. " + " | ".join(errores_grupos),
            )
            return _redirect_calcular(dia)

        camiones_combinados = []
        errores_osrm_combinados = []
        for grupo_key, resultado_grupo in resultados_grupos.items():
            for c in resultado_grupo["camiones"]:
                c_zona = dict(c)
                c_zona["camion_real"] = c["nombre"]  # nombre crudo, sin el prefijo de zona
                c_zona["nombre"] = f"{grupo_key} — {c['nombre']}"
                camiones_combinados.append(c_zona)
            for err in resultado_grupo["errores_osrm"]:
                errores_osrm_combinados.append(f"[{grupo_key}] {err}")

        resultado = {
            "camiones": camiones_combinados,
            "uso_osrm": all(rg["uso_osrm"] for rg in resultados_grupos.values()),
            "error_matriz": None,
            "errores_osrm": errores_osrm_combinados,
            "hora_inicio": config.hora_inicio.strftime("%H:%M"),
        }
        if errores_grupos:
            messages.warning(
                request,
                f"{len(errores_grupos)} de {len(plan_grupos)} no se pudieron calcular: "
                + " | ".join(errores_grupos),
            )

    resultado_obj = ResultadoCalculo.cargar(dia)
    resultado_obj.resultado_json = resultado
    resultado_obj.modo_calculo = modo_calculo
    resultado_obj.calculado_en = timezone.now()
    resultado_obj.save()

    etiqueta_dia = f" para el {dia}" if dia else ""
    messages.success(request, f"Rutas calculadas{etiqueta_dia} para {len(resultado['camiones'])} camión(es).")
    return _redirect_resultados(dia)


@require_POST
def api_guardar_frecuencia(request):
    idx = 0
    while f"ruta__{idx}" in request.POST:
        ruta_nombre = request.POST[f"ruta__{idx}"]
        dias_marcados = request.POST.getlist(f"dias__{idx}")
        RutaFrecuencia.objects.update_or_create(
            nombre_ruta=ruta_nombre, defaults={"dias": ",".join(dias_marcados)}
        )
        idx += 1
    dia = request.POST.get("dia", "")
    if dia not in DIAS_SEMANA:
        dia = ""
    return _redirect_resultados(dia)


def _inversion_to_row(c):
    return {"id": c.id, "Concepto": c.concepto, "Monto total (CRC)": c.monto, "Vida útil (años)": c.vida_util_anios}


def _recurrente_to_row(c):
    return {"id": c.id, "Concepto": c.concepto, "Monto (CRC)": c.monto, "Frecuencia": c.frecuencia}


def _inversion_dataframe():
    cols = ["Monto total (CRC)", "Vida útil (años)"]
    filas = [{"Monto total (CRC)": c.monto, "Vida útil (años)": c.vida_util_anios}
             for c in CostoInversion.objects.all()]
    return pd.DataFrame(filas, columns=cols)


def _recurrente_dataframe(tipo):
    cols = ["Monto (CRC)", "Frecuencia"]
    filas = [{"Monto (CRC)": c.monto, "Frecuencia": c.frecuencia}
             for c in CostoRecurrente.objects.filter(tipo=tipo)]
    return pd.DataFrame(filas, columns=cols)


def costos_view(request):
    config = CostosGenerales.cargar()
    resultado = ResultadoCalculo.cargar().resultado_json

    costo_inversion_dia = costo_diario_inversion(_inversion_dataframe())
    costo_mantenimiento_dia = costo_diario_recurrente(_recurrente_dataframe(CostoRecurrente.TIPO_MANTENIMIENTO))
    costo_administrativa_dia = costo_diario_recurrente(_recurrente_dataframe(CostoRecurrente.TIPO_ADMINISTRATIVA))

    toneladas_ruta = None
    kg_extra_via_sumado = 0.0
    if resultado:
        toneladas_ruta = sum(c["peso_total"] for c in resultado["camiones"]) / 1000
        via = ViaResultado.cargar()
        if via.sumar_a_recoleccion and via.resultados_via_json:
            kg_extra_via_sumado = sum(f["Kg extra estimados"] for f in via.resultados_via_json)
            toneladas_ruta += kg_extra_via_sumado / 1000
    ton_nuevo = toneladas_ruta if toneladas_ruta is not None else (config.ton_nuevo_manual or 0)

    if not resultado:
        costo_por_tonelada = costo_operativo = km_total = litros = 0.0
        costo_combustible = costo_variable = costo_mano_obra = 0.0
        personas_total = 0
    else:
        km_total = sum(c["dist_total_m"] for c in resultado["camiones"]) / 1000
        personas_total = sum(c["personas"] for c in resultado["camiones"])
        litros = km_total / config.rendimiento if config.rendimiento > 0 else 0
        costo_combustible = litros * config.precio_litro
        costo_variable = km_total * config.costo_km_extra
        costo_mano_obra = config.horas_laboradas * config.precio_hora * personas_total
        costo_operativo = (costo_combustible + costo_variable + costo_mano_obra
                           + costo_inversion_dia + costo_mantenimiento_dia + costo_administrativa_dia)
        costo_por_tonelada = costo_operativo / ton_nuevo if ton_nuevo > 0 else 0.0

    ton_actual_neta = max(config.ton_actual - ton_nuevo, 0.0)
    costo_modelo_actual = ton_actual_neta * config.precio_ton_actual
    costo_modelo_nuevo = ton_nuevo * costo_por_tonelada
    diferencia = costo_modelo_actual - costo_modelo_nuevo
    diferencia_pct = (diferencia / costo_modelo_actual * 100) if costo_modelo_actual > 0 else None

    desglose = [
        {"concepto": "Combustible", "monto": costo_combustible, "detalle": f"{litros:.1f} L x CRC {config.precio_litro:,.2f}"},
        {"concepto": "Variables por km", "monto": costo_variable, "detalle": f"{km_total:.1f} km x CRC {config.costo_km_extra:,.2f}"},
        {"concepto": "Mano de obra", "monto": costo_mano_obra,
         "detalle": f"{config.horas_laboradas:.2f} h x CRC {config.precio_hora:,.2f} x {personas_total} persona(s)"},
        {"concepto": "Inversión (prorrateada)", "monto": costo_inversion_dia, "detalle": "Camiones, garaje, otros — por vida útil"},
        {"concepto": "Mantenimiento (prorrateado)", "monto": costo_mantenimiento_dia, "detalle": "Lavado, extintores, etc. — por frecuencia"},
        {"concepto": "Administrativa (prorrateada)", "monto": costo_administrativa_dia, "detalle": "Contabilidad, permisos, seguros — por frecuencia"},
        {"concepto": "TOTAL operativo diario", "monto": costo_operativo, "detalle": ""},
        {"concepto": "Costo por tonelada", "monto": costo_por_tonelada, "detalle": f"Sobre {ton_nuevo:.2f} ton del modelo nuevo"},
    ]

    context = {
        "config": config,
        "toneladas_ruta": toneladas_ruta,
        "ton_nuevo": ton_nuevo,
        "kg_extra_via_sumado": kg_extra_via_sumado,
        "tiene_resultado": resultado is not None,
        "inversion_json": json.dumps([_inversion_to_row(c) for c in CostoInversion.objects.all()]),
        "mantenimiento_json": json.dumps([_recurrente_to_row(c) for c in CostoRecurrente.objects.filter(tipo=CostoRecurrente.TIPO_MANTENIMIENTO)]),
        "administrativa_json": json.dumps([_recurrente_to_row(c) for c in CostoRecurrente.objects.filter(tipo=CostoRecurrente.TIPO_ADMINISTRATIVA)]),
        "costo_inversion_dia": costo_inversion_dia,
        "costo_mantenimiento_dia": costo_mantenimiento_dia,
        "costo_administrativa_dia": costo_administrativa_dia,
        "km_total": km_total,
        "litros": litros,
        "costo_combustible": costo_combustible,
        "costo_variable": costo_variable,
        "costo_mano_obra": costo_mano_obra,
        "personas_total": personas_total,
        "costo_operativo": costo_operativo,
        "costo_por_tonelada": costo_por_tonelada,
        "ton_actual_neta": ton_actual_neta,
        "costo_modelo_actual": costo_modelo_actual,
        "costo_modelo_nuevo": costo_modelo_nuevo,
        "diferencia": diferencia,
        "diferencia_pct": diferencia_pct,
        "desglose": desglose,
    }
    return render(request, "rutas/costos.html", context)


@require_POST
def api_guardar_toneladas(request):
    config = CostosGenerales.cargar()
    config.ton_actual = _parsear_num(request.POST.get("ton_actual"), config.ton_actual)
    config.precio_ton_actual = _parsear_num(request.POST.get("precio_actual"), config.precio_ton_actual)
    config.ton_nuevo_manual = _parsear_num(request.POST.get("ton_nuevo"), config.ton_nuevo_manual)
    errores = _errores_de_instancia(config, "Toneladas", exclude=[
        "horas_laboradas", "precio_hora", "rendimiento", "precio_litro", "costo_km_extra"])
    if errores:
        for e in errores:
            messages.error(request, e)
        return redirect("rutas:costos")
    config.save()
    messages.success(request, "Toneladas guardadas.")
    return redirect("rutas:costos")


@require_POST
def api_guardar_mano_obra(request):
    config = CostosGenerales.cargar()
    config.horas_laboradas = _parsear_num(request.POST.get("horas_laboradas"), config.horas_laboradas)
    config.precio_hora = _parsear_num(request.POST.get("precio_hora"), config.precio_hora)
    errores = _errores_de_instancia(config, "Mano de obra", exclude=[
        "ton_actual", "precio_ton_actual", "ton_nuevo_manual", "rendimiento", "precio_litro", "costo_km_extra"])
    if errores:
        for e in errores:
            messages.error(request, e)
        return redirect("rutas:costos")
    config.save()
    messages.success(request, "Mano de obra guardada.")
    return redirect("rutas:costos")


@require_POST
def api_guardar_combustible(request):
    config = CostosGenerales.cargar()
    config.rendimiento = _parsear_num(request.POST.get("rendimiento"), config.rendimiento)
    config.precio_litro = _parsear_num(request.POST.get("precio_litro"), config.precio_litro)
    config.costo_km_extra = _parsear_num(request.POST.get("costo_km_extra"), config.costo_km_extra)
    errores = _errores_de_instancia(config, "Combustible", exclude=[
        "ton_actual", "precio_ton_actual", "ton_nuevo_manual", "horas_laboradas", "precio_hora"])
    if errores:
        for e in errores:
            messages.error(request, e)
        return redirect("rutas:costos")
    config.save()
    messages.success(request, "Combustible guardado.")
    return redirect("rutas:costos")


@require_POST
def api_guardar_inversion(request):
    filas = json.loads(request.body)["filas"]
    nuevos = []
    errores = []
    for fila in filas:
        concepto = (fila.get("Concepto") or "").strip()
        if not concepto:
            continue
        c = CostoInversion(
            concepto=concepto,
            monto=fila.get("Monto total (CRC)") or 0,
            vida_util_anios=fila.get("Vida útil (años)") or 1,
        )
        errores += _errores_de_instancia(c, concepto)
        nuevos.append(c)
    if errores:
        return JsonResponse({"errores": errores}, status=400)
    CostoInversion.objects.all().delete()
    CostoInversion.objects.bulk_create(nuevos)
    costo_dia = costo_diario_inversion(_inversion_dataframe())
    return JsonResponse({
        "filas": [_inversion_to_row(c) for c in CostoInversion.objects.all()],
        "costo_dia": costo_dia,
    })


@require_POST
def api_guardar_recurrente(request, tipo):
    if tipo not in (CostoRecurrente.TIPO_MANTENIMIENTO, CostoRecurrente.TIPO_ADMINISTRATIVA):
        return JsonResponse({"error": "tipo inválido"}, status=400)
    filas = json.loads(request.body)["filas"]
    nuevos = []
    errores = []
    for fila in filas:
        concepto = (fila.get("Concepto") or "").strip()
        if not concepto:
            continue
        c = CostoRecurrente(
            tipo=tipo,
            concepto=concepto,
            monto=fila.get("Monto (CRC)") or 0,
            frecuencia=fila.get("Frecuencia") or "Mes",
        )
        errores += _errores_de_instancia(c, concepto)
        nuevos.append(c)
    if errores:
        return JsonResponse({"errores": errores}, status=400)
    CostoRecurrente.objects.filter(tipo=tipo).delete()
    CostoRecurrente.objects.bulk_create(nuevos)
    costo_dia = costo_diario_recurrente(_recurrente_dataframe(tipo))
    return JsonResponse({
        "filas": [_recurrente_to_row(c) for c in CostoRecurrente.objects.filter(tipo=tipo)],
        "costo_dia": costo_dia,
    })


def _resultado_o_404():
    resultado = ResultadoCalculo.cargar().resultado_json
    if not resultado:
        raise Http404("Todavía no hay rutas calculadas.")
    return resultado


def exportar_view(request):
    resultado = ResultadoCalculo.cargar().resultado_json
    rutas_google_maps = []
    rutas_waze = []
    if resultado:
        for c in resultado["camiones"]:
            stops = [(fila["lat"], fila["lon"]) for fila in c["resumen"]]
            links = generar_links_google_maps(stops)
            rutas_google_maps.append({"nombre": c["nombre"], "links": links})

            paradas_waze = [
                {
                    "orden": fila["orden"], "nombre": fila["Nombre"], "hora": fila["Hora llegada"],
                    "url": f"https://waze.com/ul?ll={fila['lat']},{fila['lon']}&navigate=yes",
                }
                for fila in c["resumen"] if fila["tipo"] == "parada"
            ]
            rutas_waze.append({"nombre": c["nombre"], "paradas": paradas_waze})

    context = {
        "tiene_resultado": resultado is not None,
        "rutas_google_maps": rutas_google_maps,
        "rutas_waze": rutas_waze,
    }
    return render(request, "rutas/exportar.html", context)


def exportar_csv(request):
    resultado = _resultado_o_404()
    filas = []
    for c in resultado["camiones"]:
        for fila in c["resumen"]:
            f2 = {"Camión": c["nombre"], **{k: v for k, v in fila.items() if k not in ("lat", "lon", "orden")}}
            filas.append(f2)
    csv_bytes = pd.DataFrame(filas).to_csv(index=False).encode("utf-8")
    resp = HttpResponse(csv_bytes, content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="rutas_optimas.csv"'
    return resp


def exportar_geojson_view(request):
    resultado = _resultado_o_404()
    resp = HttpResponse(exportar_geojson(resultado), content_type="application/geo+json")
    resp["Content-Disposition"] = 'attachment; filename="rutas_optimas.geojson"'
    return resp


def exportar_shapefile_view(request):
    resultado = _resultado_o_404()
    resp = HttpResponse(exportar_shapefile(resultado), content_type="application/zip")
    resp["Content-Disposition"] = 'attachment; filename="rutas_optimas_shp.zip"'
    return resp


def exportar_gpx_view(request):
    resultado = _resultado_o_404()
    resp = HttpResponse(exportar_gpx(resultado), content_type="application/gpx+xml")
    resp["Content-Disposition"] = 'attachment; filename="rutas_optimas.gpx"'
    return resp


def exportar_kml_view(request):
    resultado = _resultado_o_404()
    resp = HttpResponse(exportar_kml(resultado), content_type="application/vnd.google-earth.kml+xml")
    resp["Content-Disposition"] = 'attachment; filename="rutas_optimas.kml"'
    return resp


# ══════════════ Red propia (Beta) ══════════════
# Sección 100% independiente: no lee ni escribe Puntos/Camiones/Resultados.

class _ArchivoDjango(io.BytesIO):
    """Envoltorio para que un UploadedFile de Django tenga la misma interfaz
    que un UploadedFile de Streamlit (.name + .getbuffer()), que es lo que
    espera leer_capa_lineas() en core/optimizador.py — sin tocar esa función."""
    def __init__(self, uploaded_file):
        super().__init__(uploaded_file.read())
        self.name = uploaded_file.name


def _grafo_desde_bd():
    """Reconstruye el networkx.Graph guardado en RedPropiaGrafo (serializado
    como nodos + aristas porque un Graph no es JSON-serializable)."""
    info = RedPropiaGrafo.cargar()
    if info.nodos_json is None:
        return None, None, info
    G = nx.Graph()
    G.add_nodes_from(range(len(info.nodos_json)))
    for a, b, peso in info.aristas_json:
        G.add_edge(a, b, weight=peso)
    nodos = [tuple(n) for n in info.nodos_json]
    return G, nodos, info


def _red_propia_punto_to_row(p):
    return {"id": p.id, "Nombre": p.nombre, "Latitud": p.latitud, "Longitud": p.longitud}


def red_propia_view(request):
    G, nodos, info = _grafo_desde_bd()
    puntos = RedPropiaPunto.objects.all()
    resultado = RedPropiaResultado.cargar().resultado_json
    progreso = RedPropiaCargaProgreso.cargar()

    context = {
        "tiene_grafo": G is not None,
        "info": info,
        "progreso": progreso,
        "puntos_json": json.dumps([_red_propia_punto_to_row(p) for p in puntos]),
        "lineas_originales_json": json.dumps(info.lineas_originales_json) if info and info.lineas_originales_json else "null",
        "resultado": resultado,
        "resultado_json": json.dumps(resultado) if resultado else "null",
    }
    return render(request, "rutas/red_propia.html", context)


def _cargar_red_en_segundo_plano(lineas_simples, tolerancia_m):
    """
    Corre en un hilo aparte (lanzado desde api_red_propia_cargar) -- el
    ajuste a OSRM de una red real (decenas de líneas, varias consultas cada
    una) puede tardar minutos, y no tiene sentido dejar al navegador
    esperando una sola petición HTTP sin ninguna señal de que sigue viva.
    En vez de eso, esta función va actualizando RedPropiaCargaProgreso línea
    por línea, y el navegador consulta ese estado (api_red_propia_progreso)
    para dibujar una barra de progreso real.
    """
    from django.db import connection

    progreso = RedPropiaCargaProgreso.cargar()
    try:
        lineas_originales = [list(g.coords) for g in lineas_simples]
        lineas_ajustadas = []
        n_ajustadas = 0
        for i, geom in enumerate(lineas_simples):
            coords, se_ajusto = ajustar_una_linea_con_osrm(geom)
            lineas_ajustadas.append(coords)
            if se_ajusto:
                n_ajustadas += 1
            progreso.lineas_hechas = i + 1
            progreso.mensaje = f"Ajustando líneas a calles reales (OSM)... {i + 1}/{len(lineas_simples)}"
            progreso.save()

        geoms_ajustadas = [LineString(c) if len(c) >= 2 else None for c in lineas_ajustadas]
        gdf_ajustado = gpd.GeoDataFrame(geometry=geoms_ajustadas)

        G, nodos = construir_grafo_red(gdf_ajustado, tolerancia_m=tolerancia_m)
        componentes = contar_componentes_red(G)

        info = RedPropiaGrafo.cargar()
        info.nodos_json = [list(n) for n in nodos]
        info.aristas_json = [[a, b, d["weight"]] for a, b, d in G.edges(data=True)]
        info.n_lineas = len(lineas_simples)
        info.n_componentes = len(componentes)
        info.tamano_componentes_json = sorted((len(c) for c in componentes), reverse=True)
        info.lineas_originales_json = [[list(p) for p in linea] for linea in lineas_originales]
        info.n_lineas_ajustadas = n_ajustadas
        info.save()

        resultado_obj = RedPropiaResultado.cargar()
        resultado_obj.resultado_json = None
        resultado_obj.save()

        if n_ajustadas < len(lineas_simples):
            progreso.mensaje = (
                f"Red cargada. {n_ajustadas} de {len(lineas_simples)} líneas se ajustaron a "
                "calles reales (OSM); el resto quedó con la geometría original del archivo "
                "(OSRM no pudo emparejarlas)."
            )
        else:
            progreso.mensaje = "Red cargada y ajustada a calles reales (OSM)."
        progreso.error = ""
    except Exception as e:
        progreso.error = f"Error cargando la red: {e}"
    finally:
        progreso.en_progreso = False
        progreso.save()
        connection.close()


@require_POST
def api_red_propia_cargar(request):
    archivos = request.FILES.getlist("archivos")
    tolerancia_m = float(request.POST.get("tolerancia_m") or 5.0)
    if not archivos:
        messages.error(request, "No se subió ningún archivo.")
        return redirect("rutas:red_propia")

    archivos_adaptados = [_ArchivoDjango(a) for a in archivos]
    gdf_lineas, error_lectura = leer_capa_lineas(archivos_adaptados)
    if error_lectura:
        messages.error(request, error_lectura)
        return redirect("rutas:red_propia")

    # Separa cada MultiLineString del shapefile en líneas simples -- shapely
    # no deja pedir .coords sobre una geometría multi-parte directamente.
    lineas_simples = explotar_lineas_simples(gdf_lineas)
    if not lineas_simples:
        messages.error(request, "El archivo no tiene líneas válidas.")
        return redirect("rutas:red_propia")

    # El ajuste a OSRM se hace en un hilo de fondo (ver
    # _cargar_red_en_segundo_plano) -- acá solo se deja todo listo (archivo
    # ya leído en memoria, nada que dependa de la petición HTTP en curso) y
    # se redirige de inmediato; la pantalla muestra una barra de progreso
    # que consulta el estado sola.
    progreso = RedPropiaCargaProgreso.cargar()
    progreso.en_progreso = True
    progreso.lineas_total = len(lineas_simples)
    progreso.lineas_hechas = 0
    progreso.mensaje = "Ajustando líneas a calles reales (OSM)..."
    progreso.error = ""
    progreso.save()

    hilo = threading.Thread(
        target=_cargar_red_en_segundo_plano, args=(lineas_simples, tolerancia_m), daemon=True,
    )
    hilo.start()

    return redirect("rutas:red_propia")


def api_red_propia_progreso(request):
    p = RedPropiaCargaProgreso.cargar()
    return JsonResponse({
        "en_progreso": p.en_progreso,
        "lineas_total": p.lineas_total,
        "lineas_hechas": p.lineas_hechas,
        "mensaje": p.mensaje,
        "error": p.error,
    })


@require_POST
def api_red_propia_guardar_puntos(request):
    filas = json.loads(request.body)["filas"]
    nuevos = []
    errores = []
    for idx, fila in enumerate(filas):
        nombre = (fila.get("Nombre") or "").strip()
        if not nombre:
            continue
        p = RedPropiaPunto(
            orden=idx, nombre=nombre,
            latitud=fila.get("Latitud"), longitud=fila.get("Longitud"),
        )
        errores += _errores_de_instancia(p, nombre)
        nuevos.append(p)
    if errores:
        return JsonResponse({"errores": errores}, status=400)
    RedPropiaPunto.objects.all().delete()
    RedPropiaPunto.objects.bulk_create(nuevos)
    return JsonResponse({"filas": [_red_propia_punto_to_row(p) for p in RedPropiaPunto.objects.all()]})


@require_POST
def api_red_propia_calcular(request):
    """Calcula un recorrido que cubre TODA la red cargada (Problema del
    Cartero Chino / Route Inspection), no la mejor ruta entre puntos
    elegidos -- ver core.optimizador.calcular_recorrido_cobertura_total.
    El primer punto guardado en RedPropiaPunto se usa como inicio/fin del
    circuito (recorrido siempre cerrado)."""
    G, nodos, info = _grafo_desde_bd()
    if G is None:
        messages.error(request, "Cargá una red primero.")
        return redirect("rutas:red_propia")

    punto_inicio = RedPropiaPunto.objects.exclude(latitud__isnull=True).exclude(longitud__isnull=True).first()
    if punto_inicio is None:
        messages.error(request, "Necesitás guardar un punto de inicio con coordenadas.")
        return redirect("rutas:red_propia")

    nodo_inicio, dist_enganche_m = enganchar_a_red((punto_inicio.longitud, punto_inicio.latitud), nodos)
    resultado_cobertura = calcular_recorrido_cobertura_total(G, nodo_inicio)
    if resultado_cobertura is None:
        messages.error(request, "No se pudo calcular un recorrido con esta red.")
        return redirect("rutas:red_propia")

    camino = [list(nodos[n]) for n in resultado_cobertura["ruta_nodos"]]

    resultado = {
        "nombre_inicio": punto_inicio.nombre,
        "punto_inicio_lonlat": [punto_inicio.longitud, punto_inicio.latitud],
        "distancia_enganche_km": dist_enganche_m / 1000,
        "camino": camino,
        "distancia_original_km": resultado_cobertura["distancia_original_m"] / 1000,
        "distancia_repetida_km": resultado_cobertura["distancia_repetida_m"] / 1000,
        "distancia_total_km": resultado_cobertura["distancia_total_m"] / 1000,
        "componente_size": resultado_cobertura["componente_size"],
        "nodos_excluidos": resultado_cobertura["nodos_excluidos"],
    }
    resultado_obj = RedPropiaResultado.cargar()
    resultado_obj.resultado_json = resultado
    resultado_obj.save()

    messages.success(request, "Recorrido de cobertura total calculado.")
    return redirect("rutas:red_propia")


# ══════════════ Recolección en vía (Beta) ══════════════
# Análisis de SOLO LECTURA sobre las rutas ya calculadas en Calcular/Resultados.
# No modifica ResultadoCalculo, ni pesos, ni capacidades, ni costos del
# sistema principal — solo estima un total aparte de "kg extra" según el tipo
# de vía OSM que atraviesa cada ruta.

TASAS_VIA_DEFAULT = {
    "motorway": 5.0, "trunk": 4.0, "primary": 3.0, "secondary": 2.0,
    "tertiary": 1.0, "residential": 0.5, "otro": 0.2,
}


def _json_safe(value):
    """clasificar_tramos_ruta() devuelve edge_id como numpy.int64 (via
    shapely STRtree.nearest) -- no es serializable a JSON de forma nativa.
    Convierte recursivamente cualquier escalar tipo numpy a su equivalente
    Python antes de guardar en un JSONField."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        return value.item()
    return value


def _tasas_via_seed():
    if not TasaViaKgKm.objects.exists():
        TasaViaKgKm.objects.bulk_create([
            TasaViaKgKm(tipo=t, kg_extra_por_km=v) for t, v in TASAS_VIA_DEFAULT.items()
        ])
    orden = {t: i for i, t in enumerate(TIPOS_VIA_DEFAULT)}
    return sorted(TasaViaKgKm.objects.all(), key=lambda t: orden.get(t.tipo, 99))


def recoleccion_via_view(request):
    resultado = ResultadoCalculo.cargar().resultado_json
    tasas = _tasas_via_seed()
    via = ViaResultado.cargar()

    context = {
        "tiene_resultado": resultado is not None,
        "tiene_via_resultado": bool(via.resultados_via_json),
        "tasas": tasas,
        "sumar_a_recoleccion": via.sumar_a_recoleccion,
        "resultados_via_json": json.dumps(via.resultados_via_json) if via.resultados_via_json else "null",
        "tramos_mapa_json": json.dumps(via.tramos_mapa_json) if via.tramos_mapa_json else "null",
        "detalle_progresivo_json": json.dumps(via.detalle_progresivo_json) if via.detalle_progresivo_json else "null",
        "tipo_via_color_json": json.dumps(TIPO_VIA_COLOR),
        "tipos_via_default_json": json.dumps(TIPOS_VIA_DEFAULT),
    }
    if via.resultados_via_json:
        total_kg_extra = sum(f["Kg extra estimados"] for f in via.resultados_via_json)
        total_km_real = sum(f["Km ruta real (Resultados)"] for f in via.resultados_via_json)
        total_km_dedup = sum(f["Km clasificados (dedup)"] for f in via.resultados_via_json)
        total_km_sin_dedup = sum(f["Km clasificados (sin dedup)"] for f in via.resultados_via_json)
        context.update({
            "total_kg_extra": total_kg_extra,
            "total_km_real": total_km_real,
            "total_km_dedup": total_km_dedup,
            "total_km_sin_dedup": total_km_sin_dedup,
            "muy_por_debajo": total_km_real > 0 and total_km_dedup < total_km_real * 0.5,
        })
    return render(request, "rutas/recoleccion_via.html", context)


@require_POST
def api_recoleccion_via_calcular(request):
    resultado = ResultadoCalculo.cargar().resultado_json
    if not resultado:
        messages.error(request, "Calculá las rutas primero en la pestaña Calcular. "
                                 "Esta sección analiza esas rutas, no genera las suyas propias.")
        return redirect("rutas:recoleccion_via")

    tasas_rows = _tasas_via_seed()
    tasas = {}
    errores_tasas = []
    for t in tasas_rows:
        valor = request.POST.get(f"tasa__{t.tipo}")
        t.kg_extra_por_km = _parsear_num(valor, t.kg_extra_por_km)
        errores_tasas += _errores_de_instancia(t, t.tipo)
        tasas[t.tipo] = t.kg_extra_por_km
    if errores_tasas:
        for e in errores_tasas:
            messages.error(request, e)
        return redirect("rutas:recoleccion_via")
    TasaViaKgKm.objects.bulk_update(tasas_rows, ["kg_extra_por_km"])

    sumar_a_recoleccion = request.POST.get("sumar_a_recoleccion") == "on"

    caminos_todos = [p for c in resultado["camiones"] for p in c["camino"]]
    try:
        bbox = bbox_de_camino(caminos_todos)
        gdf_vias = descargar_red_osm_clasificada(bbox)
        arbol, tipos_via = construir_indice_vias(gdf_vias)
    except Exception as e:
        messages.error(request, f"No se pudo descargar la red vial de OpenStreetMap "
                                 f"(revisá la conexión a internet): {e}")
        return redirect("rutas:recoleccion_via")

    resultados_via = []
    tramos_via_mapa = []
    edges_ya_contadas = set()

    for c in resultado["camiones"]:
        if not c["camino"]:
            continue
        tramos_clasificados = clasificar_tramos_ruta(c["camino"], arbol, tipos_via)

        dist_por_via = {}
        tipo_por_via = {}
        for tramo in tramos_clasificados:
            eid = tramo["edge_id"]
            dist_por_via[eid] = dist_por_via.get(eid, 0.0) + tramo["dist_m"]
            tipo_por_via[eid] = tramo["tipo"]

        distancias = {t: 0.0 for t in TIPOS_VIA_DEFAULT}
        dist_sin_dedup_m = sum(dist_por_via.values())
        edges_contadas_este_camion = set()
        for eid, dist_via in dist_por_via.items():
            if eid in edges_ya_contadas:
                continue
            edges_ya_contadas.add(eid)
            edges_contadas_este_camion.add(eid)
            distancias[tipo_por_via[eid]] += dist_via

        for tramo in tramos_clasificados:
            contado = tramo["edge_id"] in edges_contadas_este_camion
            tramos_via_mapa.append({**tramo, "camion": c["nombre"], "contado": contado})

        km_total_dedup = sum(distancias.values()) / 1000
        kg_extra_camion = sum((dist_m / 1000) * tasas.get(tipo, 0.0) for tipo, dist_m in distancias.items())
        fila = {
            "Camion": c["nombre"],
            "Km ruta real (Resultados)": round(c["dist_total_m"] / 1000, 2),
            "Km clasificados (dedup)": round(km_total_dedup, 2),
            "Km clasificados (sin dedup)": round(dist_sin_dedup_m / 1000, 2),
            "Kg extra estimados": round(kg_extra_camion, 2),
        }
        for tipo in TIPOS_VIA_DEFAULT:
            fila[f"km en {tipo}"] = round(distancias.get(tipo, 0.0) / 1000, 2)
        resultados_via.append(fila)

    detalle_progresivo = None
    if sumar_a_recoleccion:
        detalle_progresivo = {}
        edges_prog_contadas = set()
        for c in resultado["camiones"]:
            if not c["camino"]:
                continue
            viajes_stops = reconstruir_viajes_desde_resumen(c["resumen"])
            filas_no_inicio = [f for f in c["resumen"] if f["tipo"] in ("parada", "descarga")]
            fila_inicio = next(f for f in c["resumen"] if f["tipo"] == "inicio")

            filas_detalle = [{
                "Orden": fila_inicio["orden"], "Nombre": fila_inicio["Nombre"],
                "Peso puntos acum. (kg)": "0.00", "Kg extra via (tramo)": "0.00",
                "Peso TOTAL acumulado (kg)": "0.00",
            }]
            idx_fila, kg_via_acum = 0, 0.0
            for stops in viajes_stops:
                try:
                    _, _, camino_por_leg_prog, _ = obtener_ruta_completa_osrm_por_leg(stops)
                except Exception:
                    camino_por_leg_prog = [[] for _ in range(len(stops) - 1)]
                for leg_geom in camino_por_leg_prog:
                    fila_actual = filas_no_inicio[idx_fila]
                    idx_fila += 1
                    kg_leg = 0.0
                    if arbol is not None and leg_geom:
                        for tramo in clasificar_tramos_ruta(leg_geom, arbol, tipos_via):
                            eid = tramo["edge_id"]
                            if eid in edges_prog_contadas:
                                continue
                            edges_prog_contadas.add(eid)
                            kg_leg += (tramo["dist_m"] / 1000) * tasas.get(tramo["tipo"], 0.0)
                    kg_via_acum += kg_leg
                    peso_puntos = fila_actual["Peso acumulado (kg)"]
                    filas_detalle.append({
                        "Orden": fila_actual["orden"], "Nombre": fila_actual["Nombre"],
                        "Peso puntos acum. (kg)": f"{peso_puntos:,.2f}",
                        "Kg extra via (tramo)": f"{kg_leg:,.2f}",
                        "Peso TOTAL acumulado (kg)": f"{peso_puntos + kg_via_acum:,.2f}",
                    })
            detalle_progresivo[c["nombre"]] = filas_detalle

    via = ViaResultado.cargar()
    via.resultados_via_json = _json_safe(resultados_via)
    via.tramos_mapa_json = _json_safe(tramos_via_mapa)
    via.sumar_a_recoleccion = sumar_a_recoleccion
    via.detalle_progresivo_json = _json_safe(detalle_progresivo)
    via.save()

    messages.success(request, "Kg extra por tipo de vía calculado.")
    return redirect("rutas:recoleccion_via")
