from django.urls import path

from spotify import views

urlpatterns = [
    path("login/", views.spotify_login, name="spotify_login"),
    path("callback/", views.spotify_callback, name="spotify_callback"),
]
