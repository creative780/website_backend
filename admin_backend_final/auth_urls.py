from django.urls import path
from .auth_views import CookieTokenObtainPairView, CookieTokenRefreshView, LogoutView, csrf

urlpatterns = [
     path("api/csrf/", csrf, name="csrf"),
    path("api/token/", CookieTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/token/refresh/", CookieTokenRefreshView.as_view(), name="token_refresh"),
    path("api/logout/", LogoutView.as_view(), name="logout"),
]
