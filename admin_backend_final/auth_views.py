
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from django.views.decorators.csrf import ensure_csrf_cookie
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect

COOKIE_NAME = "refresh_token"
COOKIE_PATH = "/api/token/"
COOKIE_SECURE = False     # set True in HTTPS/prod
COOKIE_SAMESITE = "Lax"   # use "None" + SECURE=True if cross-site in prod
COOKIE_MAX_AGE = 7 * 24 * 60 * 60

@ensure_csrf_cookie
def csrf(request):
    """
    GET /api/csrf/ -> sets csrftoken cookie and returns it as JSON
    Use this once on app load before making POSTs that need CSRF.
    """
    return JsonResponse({"csrfToken": get_token(request)})

@method_decorator(csrf_protect, name="post")
class CookieTokenObtainPairView(TokenObtainPairView):
    def post(self, request, *args, **kwargs):
        res = super().post(request, *args, **kwargs)
        if res.status_code == status.HTTP_200_OK and "refresh" in res.data:
            refresh = res.data.pop("refresh")
            res.set_cookie(
                COOKIE_NAME, refresh,
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                secure=COOKIE_SECURE,
                samesite=COOKIE_SAMESITE,
                path=COOKIE_PATH,
            )
        return res


@method_decorator(csrf_protect, name="post")
class CookieTokenRefreshView(TokenRefreshView):
    """
    POST /api/token/refresh/ -> returns {"access": "..."} using HttpOnly cookie.
    Requires X-CSRFToken header (double submit).
    """
    def post(self, request, *args, **kwargs):
        request.data["refresh"] = request.COOKIES.get(COOKIE_NAME)
        return super().post(request, *args, **kwargs)

@method_decorator(csrf_protect, name="post")
class LogoutView(APIView):
    authentication_classes = ()
    permission_classes = ()

    def post(self, request):
        r = Response({"detail": "Logged out"})
        r.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
        return r
