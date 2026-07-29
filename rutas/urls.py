from django.urls import path

from . import views

app_name = "rutas"

urlpatterns = [
    path("puntos/", views.puntos_view, name="puntos"),
    path("puntos/guardar/", views.api_guardar_puntos, name="api_guardar_puntos"),
    path("puntos/geocodificar/", views.api_geocodificar_puntos, name="api_geocodificar_puntos"),
    path("camiones/", views.camiones_view, name="camiones"),
    path("camiones/guardar/", views.api_guardar_camiones, name="api_guardar_camiones"),
    path("camiones/disponibilidad/", views.api_guardar_disponibilidad_camiones, name="api_guardar_disponibilidad_camiones"),
    path("configuracion/", views.configuracion_view, name="configuracion"),
    path("calcular/", views.calcular_view, name="calcular"),
    path("resultados/", views.resultados_view, name="resultados"),
    path("calcular/ejecutar/", views.api_ejecutar_calculo, name="api_ejecutar_calculo"),
    path("calcular/frecuencia/", views.api_guardar_frecuencia, name="api_guardar_frecuencia"),
    path("costos/", views.costos_view, name="costos"),
    path("costos/toneladas/", views.api_guardar_toneladas, name="api_guardar_toneladas"),
    path("costos/mano-obra/", views.api_guardar_mano_obra, name="api_guardar_mano_obra"),
    path("costos/combustible/", views.api_guardar_combustible, name="api_guardar_combustible"),
    path("costos/inversion/", views.api_guardar_inversion, name="api_guardar_inversion"),
    path("costos/recurrente/<str:tipo>/", views.api_guardar_recurrente, name="api_guardar_recurrente"),
    path("exportar/", views.exportar_view, name="exportar"),
    path("exportar/csv/", views.exportar_csv, name="exportar_csv"),
    path("exportar/geojson/", views.exportar_geojson_view, name="exportar_geojson"),
    path("exportar/shapefile/", views.exportar_shapefile_view, name="exportar_shapefile"),
    path("exportar/gpx/", views.exportar_gpx_view, name="exportar_gpx"),
    path("exportar/kml/", views.exportar_kml_view, name="exportar_kml"),
    path("red-propia/", views.red_propia_view, name="red_propia"),
    path("red-propia/cargar/", views.api_red_propia_cargar, name="api_red_propia_cargar"),
    path("red-propia/puntos/guardar/", views.api_red_propia_guardar_puntos, name="api_red_propia_guardar_puntos"),
    path("red-propia/calcular/", views.api_red_propia_calcular, name="api_red_propia_calcular"),
    path("recoleccion-via/", views.recoleccion_via_view, name="recoleccion_via"),
    path("recoleccion-via/calcular/", views.api_recoleccion_via_calcular, name="api_recoleccion_via_calcular"),
]
