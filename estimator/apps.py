"""壁紙積算アプリケーションの設定を定義する。"""

from django.apps import AppConfig


class EstimatorConfig(AppConfig):
    """壁紙積算アプリケーションのDjango設定。"""
    name = "estimator"
    verbose_name = "壁紙積算"
