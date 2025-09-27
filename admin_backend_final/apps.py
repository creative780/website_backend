
from django.apps import AppConfig

class AdminBackendFinalConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'admin_backend_final'

    def ready(self):
        import admin_backend_final.signals