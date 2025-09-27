# ---- SITE BRANDING APIS ----
import uuid
import logging

from django.db import transaction
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .models import SiteBranding, Image
from .permissions import FrontendOnlyPermission
from .utilities import format_image_object
from .utilities import save_image  # <- you provided this

def _abs_media_url(request, rel_or_abs: str | None) -> str:
    if not rel_or_abs:
        return ""
    # already absolute?
    if rel_or_abs.startswith("http://") or rel_or_abs.startswith("https://"):
        return rel_or_abs
    # build absolute using API host
    return request.build_absolute_uri(rel_or_abs)

def _legacy_sitesettings_fallback():
    try:
        # your model name in provided models: SiteSettings (logo_url is a URLField)
        ss = SiteBranding.objects.order_by('-updated_at').first()
        return ss
    except Exception:
        return None

logger = logging.getLogger(__name__)


def _active_branding():
    branding, _ = SiteBranding.objects.get_or_create(
        singleton_lock="X",
        defaults={
            "branding_id": f"BRAND-{uuid.uuid4().hex[:8]}",
            "site_title": "",
        },
    )
    return branding

def _delete_image_if_owned(img: Image | None, kind: str, branding_id: str):
    """
    Delete an Image row only if it belongs to this branding linkage.
    Prevents accidental deletion of shared assets.
    """
    if not img:
        return
    if (img.linked_table == "site_branding" and
        img.linked_id == branding_id and
        img.linked_page == kind):
        try:
            img.delete()
        except Exception as e:
            logger.warning("Failed deleting old %s image %s: %s", kind, getattr(img, "image_id", ""), e)

class SaveFavIconAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        """
        Accepts:
          - multipart: field name 'file' OR 'favicon'
          - JSON: { "source": <data-url|http-url> }
        Replaces previous favicon (deletes old owned Image).
        """
        branding = _active_branding()
        file_obj = request.FILES.get("file") or request.FILES.get("favicon")
        source = request.data.get("source")

        if not file_obj and not source:
            return Response({"error": "No file or source provided"}, status=400)

        payload = file_obj or source

        with transaction.atomic():
            # Save new
            new_img = save_image(
                payload,
                alt_text="Site Favicon",
                tags="branding,favicon",
                linked_table="site_branding",
                linked_page="favicon",
                linked_id=branding.branding_id,
            )
            if not new_img:
                return Response({"error": "Invalid image"}, status=400)

            # Delete old if it was owned by branding
            _delete_image_if_owned(branding.favicon, "favicon", branding.branding_id)

            # Link new
            branding.favicon = new_img
            branding.save(update_fields=["favicon", "updated_at"])

        return Response({
            "success": True,
            "favicon": {
                "image_id": new_img.image_id,
                "url": new_img.url,
                "alt_text": new_img.alt_text,
                "width": new_img.width,
                "height": new_img.height,
            }
        }, status=200)

class SaveLogoAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        """
        Accepts:
          - multipart: field name 'file' OR 'logo'
          - JSON: { "source": <data-url|http-url> }
        Replaces previous logo (deletes old owned Image).
        """
        branding = _active_branding()
        file_obj = request.FILES.get("file") or request.FILES.get("logo")
        source = request.data.get("source")

        if not file_obj and not source:
            return Response({"error": "No file or source provided"}, status=400)

        payload = file_obj or source

        with transaction.atomic():
            new_img = save_image(
                payload,
                alt_text="Site Logo",
                tags="branding,logo",
                linked_table="site_branding",
                linked_page="logo",
                linked_id=branding.branding_id,
            )
            if not new_img:
                return Response({"error": "Invalid image"}, status=400)

            _delete_image_if_owned(branding.logo, "logo", branding.branding_id)

            branding.logo = new_img
            branding.save(update_fields=["logo", "updated_at"])

        return Response({
            "success": True,
            "logo": {
                "image_id": new_img.image_id,
                "url": new_img.url,
                "alt_text": new_img.alt_text,
                "width": new_img.width,
                "height": new_img.height,
            }
        }, status=200)

class SaveSiteTitleAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    parser_classes = [JSONParser, FormParser]

    def post(self, request):
        """
        Accepts:
          - JSON/form: site_title
        """
        branding = _active_branding()
        title = (request.data.get("site_title") or "").strip()
        with transaction.atomic():
            branding.site_title = title
            branding.save(update_fields=["site_title", "updated_at"])

        return Response({"success": True, "site_title": branding.site_title}, status=200)

class ShowLogoAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    def get(self, request):
        branding = _active_branding()
        url = branding.logo.url if getattr(branding.logo, "url", "") else ""
        if not url:
            # LEGACY FALLBACK
            ss = _legacy_sitesettings_fallback()
            if ss and ss.logo_url:
                url = ss.logo_url
        return Response({"logo": {"url": _abs_media_url(request, url)}}, status=200)

class ShowFavIconAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    def get(self, request):
        branding = _active_branding()
        url = branding.favicon.url if getattr(branding.favicon, "url", "") else ""
        if not url:
            # LEGACY FALLBACK (reuse logo_url if you stored favicon there? if you have a favicon_url add it)
            ss = _legacy_sitesettings_fallback()
            if ss and getattr(ss, "favicon_url", ""):
                url = ss.favicon_url
        return Response({"favicon": {"url": _abs_media_url(request, url)}}, status=200)

class ShowSiteTitleAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    def get(self, request):
        branding = _active_branding()
        title = branding.site_title or ""
        if not title:
            ss = _legacy_sitesettings_fallback()
            if ss and ss.site_title:
                title = ss.site_title
        return Response({"site_title": title}, status=200)

class DeleteFavIconAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        branding = _active_branding()
        with transaction.atomic():
            _delete_image_if_owned(branding.favicon, "favicon", branding.branding_id)
            branding.favicon = None
            branding.save(update_fields=["favicon", "updated_at"])
        return Response({"success": True, "message": "Favicon deleted"}, status=200)

class DeleteLogoAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        branding = _active_branding()
        with transaction.atomic():
            _delete_image_if_owned(branding.logo, "logo", branding.branding_id)
            branding.logo = None
            branding.save(update_fields=["logo", "updated_at"])
        return Response({"success": True, "message": "Logo deleted"}, status=200)

class DeleteSiteTitleAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        branding = _active_branding()
        with transaction.atomic():
            branding.site_title = ""
            branding.save(update_fields=["site_title", "updated_at"])
        return Response({"success": True, "message": "Site title cleared"}, status=200)
