"""壁紙積算アプリのURLルーティングを定義する。"""

from django.urls import path
from django.contrib.auth.views import LogoutView

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("about/", views.about, name="about"),
    path("accounts/login/", views.StaffAwareLoginView.as_view(), name="login"),
    path("accounts/logout/", LogoutView.as_view(next_page="dashboard"), name="logout"),
    path("accounts/signup/", views.signup, name="signup"),
    path("projects/new/", views.project_create, name="project_create"),
    path("projects/<int:pk>/", views.project_detail, name="project_detail"),
    path("projects/<int:pk>/save-wallpapers/", views.project_save_wallpapers, name="project_save_wallpapers"),
    path("projects/<int:pk>/recalculate/", views.project_recalculate, name="project_recalculate"),
    path("projects/<int:pk>/pdf/", views.project_pdf, name="project_pdf"),
    path("projects/<int:pk>/csv/", views.project_csv, name="project_csv"),
]
