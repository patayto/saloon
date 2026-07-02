from django.urls import path

from analysis import views

app_name = "analysis"

urlpatterns = [
    path("overview/", views.analysis_overview, name="overview"),
]
