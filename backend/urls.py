"""
URL configuration for backend project.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),

    # Your app API routes
    path('api/', include('admin_backend_final.urls')),

    # JWT auth endpoints (access in JSON, refresh via HttpOnly cookie)
    path('', include('admin_backend_final.auth_urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
