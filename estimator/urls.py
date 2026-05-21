from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("projects/new/", views.project_create, name="project_create"),
    path("projects/<int:pk>/", views.project_detail, name="project_detail"),
    path("projects/<int:pk>/pdf/", views.project_pdf, name="project_pdf"),
    path("projects/<int:pk>/csv/", views.project_csv, name="project_csv"),
]
