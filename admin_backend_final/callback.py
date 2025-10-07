# --- CallbackRequest API ------------------------------------------------------
import json
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from datetime import datetime, timezone as dt_timezone 
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from django.utils.dateparse import parse_datetime
from .models import CallbackRequest
from .permissions import FrontendOnlyPermission
import re

def _s(v, default=""):
    return (v if v is not None else default).strip() if isinstance(v, str) else (v if v is not None else default)

_DT_RE = re.compile(
    r"""
    ^\s*
    (?P<date>\d{4}-\d{2}-\d{2})
    [ T]
    (?P<hour>\d{2}):(?P<minute>\d{2})
    (?::(?P<second>\d{2}))?
    (?:\.(?P<fsec>\d{1,9}))?
    (?P<tz>Z|[+-]\d{2}:?\d{2})?
    \s*$
    """,
    re.VERBOSE,
)

def _parse_dt(value):
    """
    Accepts browser/local ISO-ish datetimes and returns aware UTC datetime.
    Naive inputs are assumed in the current Django timezone.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None

    m = _DT_RE.match(s.replace("z", "Z"))
    if not m:
        try:
            s2 = s.replace(" ", "T")
            if s2.endswith("Z"):
                s2 = s2[:-1] + "+00:00"
            if re.search(r"[+-]\d{4}$", s2):  # +0500 → +05:00
                s2 = s2[:-2] + ":" + s2[-2:]
            try:
                dt = datetime.fromisoformat(s2)
            except ValueError:
                dt = parse_datetime(s2)
            if dt is None:
                return None
        except Exception:
            return None
    else:
        date = m.group("date")
        hh   = m.group("hour")
        mm   = m.group("minute")
        ss   = m.group("second") or "00"
        fsec = (m.group("fsec") or "")
        tz   = m.group("tz") or ""

        if fsec:
            fsec = fsec[:6].ljust(6, "0")  # max 6 digits for microseconds
            frac = f".{fsec}"
        else:
            frac = ""

        if tz == "Z":
            tz = "+00:00"
        elif tz and re.fullmatch(r"[+-]\d{4}", tz):
            tz = tz[:-2] + ":" + tz[-2:]

        norm = f"{date}T{hh}:{mm}:{ss}{frac}{tz}"
        try:
            dt = datetime.fromisoformat(norm)
        except ValueError:
            dt = parse_datetime(norm)
            if dt is None:
                return None

    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt.astimezone(dt_timezone.utc)  # <-- use stdlib UTC
    
def _serialize_callback(obj: CallbackRequest):
    """
    Shape matches your FE: detail/list both consume these keys.
    """
    return {
        "id": obj.callback_id,
        "device_uuid": obj.device_uuid,
        "username": obj.username,
        "email": obj.email or "",
        "phone_number": obj.phone_number,
        "event_type": obj.event_type,
        "event_venue": obj.event_venue or "",
        "approx_guest": obj.approx_guest if obj.approx_guest is not None else "",
        "status": obj.status,
        "event_datetime": obj.event_datetime.isoformat() if obj.event_datetime else "",
        "budget": obj.budget or "",
        "preferred_callback": obj.preferred_callback.isoformat() if obj.preferred_callback else "",
        "theme": obj.theme or "",
        "notes": obj.notes or "",
        "created_at": obj.created_at.isoformat() if obj.created_at else "",
        "updated_at": obj.updated_at.isoformat() if obj.updated_at else "",
    }

def _validate_seven_day_rule(preferred_callback, event_datetime):
    """
    Server-side guard: preferred must be >= 7 days before event_datetime (if event set).
    """
    if event_datetime and preferred_callback:
        if (event_datetime - preferred_callback) < timedelta(days=7):
            raise ValidationError("Preferred call-back must be at least 7 days before the event date/time.")

def _first_non_blank(*vals, default=""):
    for v in vals:
        if v is None:
            continue
        if isinstance(v, str):
            s = v.strip()
            if s != "":
                return s
        else:
            return v
    return default

class SaveCallbackAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else json.loads(request.body or "{}")

        device_uuid = _first_non_blank(data.get("device_uuid"))
        username    = _first_non_blank(data.get("username"), data.get("full_name"))
        email       = (data.get("email") or "").strip()
        phone_number= _first_non_blank(data.get("phone_number"), data.get("phone"))
        event_type  = _first_non_blank(data.get("event_type"))
        event_venue = (data.get("event_venue") or "").strip()
        approx_guest_raw = _first_non_blank(data.get("approx_guest"), data.get("estimated_guests"), default=None)
        budget      = (data.get("budget") or "").strip()

        # Parse datetimes early and validate explicitly
        preferred_callback_raw = data.get("preferred_callback")
        event_datetime_raw     = data.get("event_datetime")

        preferred_callback = _parse_dt(preferred_callback_raw)
        if preferred_callback_raw and preferred_callback is None:
            return Response({"error": "Invalid preferred_callback datetime."}, status=status.HTTP_400_BAD_REQUEST)

        event_datetime = _parse_dt(event_datetime_raw)
        if event_datetime_raw not in (None, "", "null") and event_datetime is None:
            return Response({"error": "Invalid event_datetime datetime."}, status=status.HTTP_400_BAD_REQUEST)

        theme       = (data.get("theme") or "").strip()
        notes       = (data.get("notes") or "").strip()

        # Requireds
        if not (device_uuid and username and phone_number and event_type and preferred_callback):
            return Response(
                {"error": "device_uuid, username, phone_number, event_type, preferred_callback are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Seven-day rule
        try:
            _validate_seven_day_rule(preferred_callback, event_datetime)
        except ValidationError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        # approx_guest coercion
        approx_guest = None
        if approx_guest_raw not in (None, "", "null"):
            try:
                approx_guest = int(approx_guest_raw)
                if approx_guest < 1:
                    approx_guest = None
            except Exception:
                approx_guest = None

        with transaction.atomic():
            cb = CallbackRequest(
                callback_id=CallbackRequest.new_id(),
                device_uuid=device_uuid,
                username=username,
                email=email,
                phone_number=phone_number,
                event_type=event_type,
                event_venue=event_venue,
                approx_guest=approx_guest,
                event_datetime=event_datetime,
                budget=budget,
                preferred_callback=preferred_callback,
                theme=theme,
                notes=notes,
                status="pending",
            )
            cb.clean()
            cb.save()

        return Response(_serialize_callback(cb), status=status.HTTP_201_CREATED)

class EditCallbackAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else json.loads(request.body or "{}")

        cid = _first_non_blank(data.get("id"), data.get("callback_id"))
        if not cid:
            return Response({"error": "id is required"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            try:
                cb = CallbackRequest.objects.select_for_update().get(callback_id=cid)
            except CallbackRequest.DoesNotExist:
                return Response({"error": "Callback not found"}, status=status.HTTP_404_NOT_FOUND)

            # --- Gather incoming fields (optional on edit) ---
            username       = _first_non_blank(data.get("username"), data.get("full_name"), default=None)
            email          = (data.get("email") or None)
            phone_number   = _first_non_blank(data.get("phone_number"), data.get("phone"), default=None)
            event_type     = _first_non_blank(data.get("event_type"), default=None)
            event_venue    = (data.get("event_venue") or None)
            approx_guest_in= _first_non_blank(data.get("approx_guest"), data.get("estimated_guests"), default=None)
            budget         = (data.get("budget") or None)
            theme          = (data.get("theme") or None)
            notes          = (data.get("notes") or None)
            status_in      = (data.get("status") or "").strip().lower()

            preferred_raw  = data.get("preferred_callback")
            event_dt_raw   = data.get("event_datetime")

            # --- Parse datetimes only if provided (empty/None means no change) ---
            new_preferred = None
            if preferred_raw not in (None, "", "null"):
                new_preferred = _parse_dt(preferred_raw)
                if new_preferred is None:
                    return Response({"error": "Invalid preferred_callback datetime."}, status=status.HTTP_400_BAD_REQUEST)

            new_event_dt = None
            if event_dt_raw not in (None, "", "null"):
                new_event_dt = _parse_dt(event_dt_raw)
                if new_event_dt is None:
                    return Response({"error": "Invalid event_datetime datetime."}, status=status.HTTP_400_BAD_REQUEST)

            # --- Coerce approx_guest if present ---
            approx_guest_val = None
            approx_guest_provided = approx_guest_in not in (None, "", "null")
            if approx_guest_provided:
                try:
                    approx_guest_val = int(approx_guest_in)
                    if approx_guest_val < 1:
                        approx_guest_val = None
                except Exception:
                    approx_guest_val = None

            # Determine prospective values for 7-day rule check
            preferred_candidate = new_preferred if new_preferred is not None else cb.preferred_callback
            event_dt_candidate  = new_event_dt if new_event_dt is not None else cb.event_datetime

            try:
                _validate_seven_day_rule(preferred_candidate, event_dt_candidate)
            except ValidationError as e:
                return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

            # --- Apply updates (only provided fields) ---
            update_fields = []

            if username is not None:
                cb.username = username.strip()
                update_fields.append("username")

            if email is not None:
                cb.email = (email or "").strip()
                update_fields.append("email")

            if phone_number is not None:
                cb.phone_number = phone_number.strip()
                update_fields.append("phone_number")

            if event_type is not None:
                cb.event_type = event_type.strip() or "Other"
                update_fields.append("event_type")

            if event_venue is not None:
                cb.event_venue = event_venue.strip()
                update_fields.append("event_venue")

            if approx_guest_provided:
                cb.approx_guest = approx_guest_val
                update_fields.append("approx_guest")

            if new_event_dt is not None:
                cb.event_datetime = new_event_dt
                update_fields.append("event_datetime")

            if budget is not None:
                cb.budget = (budget or "").strip()
                update_fields.append("budget")

            if new_preferred is not None:
                cb.preferred_callback = new_preferred
                update_fields.append("preferred_callback")

            if theme is not None:
                cb.theme = (theme or "").strip()
                update_fields.append("theme")

            if notes is not None:
                cb.notes = (notes or "").strip()
                update_fields.append("notes")

            if status_in in {"pending", "scheduled", "contacted", "completed", "cancelled"}:
                cb.status = status_in
                update_fields.append("status")

            # Validate model-level clean (includes seven-day rule duplicate check)
            cb.clean()

            # Always bump updated_at
            if "updated_at" not in update_fields:
                update_fields.append("updated_at")

            cb.save(update_fields=update_fields or ["updated_at"])

        return Response(_serialize_callback(cb), status=status.HTTP_200_OK)
 
class DeleteCallbackAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else json.loads(request.body or "{}")
        cid = _s(data.get("id")) or _s(data.get("callback_id"))
        if not cid:
            return Response({"error": "id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            cb = CallbackRequest.objects.get(callback_id=cid)
        except CallbackRequest.DoesNotExist:
            return Response({"error": "Callback not found"}, status=status.HTTP_404_NOT_FOUND)

        cb.delete()
        return Response({"message": "Callback deleted"}, status=status.HTTP_200_OK)


class ShowSpecificCallbackAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        cid = _s(request.query_params.get("id")) or _s(request.query_params.get("callback_id"))
        if not cid:
            return Response({"error": "id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            cb = CallbackRequest.objects.get(callback_id=cid)
        except CallbackRequest.DoesNotExist:
            return Response({"error": "Callback not found"}, status=status.HTTP_404_NOT_FOUND)

        return Response(_serialize_callback(cb), status=status.HTTP_200_OK)


class ShowAllCallbackAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        """
        Optional filters:
          - device_uuid: only this device's rows
          - status: pending|scheduled|contacted|completed|cancelled
        """
        qs = CallbackRequest.objects.all().order_by("-created_at")

        device_uuid = _s(request.query_params.get("device_uuid"))
        status_filter = _s(request.query_params.get("status")).lower()

        if device_uuid:
            qs = qs.filter(device_uuid=device_uuid)
        if status_filter in {"pending", "scheduled", "contacted", "completed", "cancelled"}:
            qs = qs.filter(status=status_filter)

        data = [_serialize_callback(x) for x in qs[:1000]]
        return Response(data, status=status.HTTP_200_OK)
