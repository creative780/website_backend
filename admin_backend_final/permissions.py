# admin_backend_final/permissions.py
import os
from dotenv import load_dotenv
from rest_framework.permissions import BasePermission

load_dotenv()
FRONTEND_KEY = os.environ.get("FRONTEND_KEY", "")

class FrontendOnlyPermission(BasePermission):
    def has_permission(self, request, view):
        return request.headers.get("X-Frontend-Key") == FRONTEND_KEY
