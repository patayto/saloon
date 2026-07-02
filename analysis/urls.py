from django.urls import path

from analysis import views

app_name = "analysis"

urlpatterns = [
    path("overview/", views.analysis_overview, name="overview"),
    path("graph/", views.graph_page, name="graph"),
    path("graph/data/", views.graph_data, name="graph_data"),
]
