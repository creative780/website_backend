# app/attributes_api.py
# DRF backend for AttributeSubCategory – aligned with your frontend contract.

import json
import uuid
from typing import List, Tuple

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from typing import Optional
from .models import AttributeSubCategory  # <- model added earlier
from .permissions import FrontendOnlyPermission


# ------------------------------
# Helpers
# ------------------------------
def _ensure_unique_slug(base_slug: Optional[str]) -> str:
    """
    Ensure slug uniqueness across AttributeSubCategory by suffixing -2, -3, ...
    """
    slug = base_slug or "attr"
    if not AttributeSubCategory.objects.filter(slug=slug).exists():
        return slug
    i = 2
    while True:
        candidate = f"{slug}-{i}"
        if not AttributeSubCategory.objects.filter(slug=candidate).exists():
            return candidate
        i += 1

def _normalize_values(values):
    if values is None:
        return ([], "")
    if not isinstance(values, list):
        return ([], "values must be a list")

    normalized = []
    default_count = 0

    for v in values:
        if not isinstance(v, dict):
            return ([], "each value must be an object")

        vid = str(v.get("id") or uuid.uuid4())
        name = (v.get("name") or "").strip()
        if not name:
            return ([], "each value requires a non-empty 'name'")

        pd = v.get("price_delta", None)
        if pd is not None:
            try:
                pd = float(pd)
            except Exception:
                return ([], "price_delta must be numeric")

        is_default = bool(v.get("is_default", False))
        if is_default:
            default_count += 1

        image_url = (v.get("image_url") or "").strip()
        image_id  = (v.get("image_id") or "").strip()   # ✅ new
        desc = (v.get("description") or "").strip()

        item = {
            "id": vid,
            "name": name,
            "is_default": is_default,
        }
        if pd is not None:
            item["price_delta"] = pd
        if image_url:
            item["image_url"] = image_url
        if image_id:
            item["image_id"] = image_id                 # ✅ persist
        if desc:
            item["description"] = desc

        normalized.append(item)

    if default_count > 1:
        return ([], "only one option can be marked as default")

    return (normalized, "")

def _normalize_sub_ids(sub_ids) -> Tuple[List[str], str]:
    if sub_ids is None:
        return ([], "")
    if not isinstance(sub_ids, list):
        return ([], "subcategory_ids must be a list")
    out = [str(x).strip() for x in sub_ids if str(x).strip()]
    return (out, "")

def _normalize_payload(obj: dict, *, is_create: bool) -> Tuple[dict, str]:
    """
    Map incoming JSON to model fields, validate, and return normalized payload.
    NEW: pass through 'description' for the attribute itself.
    """
    if not isinstance(obj, dict):
        return ({}, "invalid payload")

    name = (obj.get("name") or "").strip()
    if not name:
        return ({}, "name is required")

    type_ = (obj.get("type") or "custom").strip().lower()
    if type_ not in {"size", "color", "material", "custom"}:
        return ({}, "type must be one of: size, color, material, custom")

    status_val = (obj.get("status") or "visible").strip().lower()
    if status_val not in {"visible", "hidden"}:
        return ({}, "status must be 'visible' or 'hidden'")

    values, v_err = _normalize_values(obj.get("values"))
    if v_err:
        return ({}, v_err)

    sub_ids, s_err = _normalize_sub_ids(obj.get("subcategory_ids"))
    if s_err:
        return ({}, s_err)

    raw_slug = (obj.get("slug") or slugify(name) or "attr").lower()
    slug = raw_slug

    # only ensure uniqueness on create or when user changed slug on edit
    if is_create or (obj.get("slug") and AttributeSubCategory.objects.exclude(slug=raw_slug).filter(slug=raw_slug).exists()):
        slug = _ensure_unique_slug(raw_slug)

    normalized = {
        # Use client id if present, else generate UUID (string)
        "attribute_id": str(obj.get("id") or uuid.uuid4()),
        "name": name,
        "slug": slug,
        "type": type_,
        "status": status_val,
        "description": (obj.get("description") or "").strip(),  # NEW
        "values": values,
        "subcategory_ids": sub_ids,  # empty list => global
    }

    return (normalized, "")

def _serialize_attribute(m: AttributeSubCategory) -> dict:
    clean_values = []
    for val in (m.values or []):
        if isinstance(val, dict):
            # strip only image_data, leave image_id and image_url
            clean = {k: v for k, v in val.items() if k != "image_data"}
            clean_values.append(clean)
        else:
            clean_values.append(val)

    return {
        "id": str(m.attribute_id),
        "name": m.name,
        "slug": m.slug,
        "type": m.type,
        "status": m.status,
        "description": getattr(m, "description", "") or "",
        "values": clean_values,
        "created_at": m.created_at.isoformat(),
        "subcategory_ids": m.subcategory_ids or [],
    }

# ------------------------------
# Views
# ------------------------------
class ShowSubcatAttributesAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        """
        Optional filter: ?subcategory_id=<ID>
        Pagination: ?page=1&page_size=50

        No DB ordering except PK; Python-sorts the small page by name.
        """
        sub_id = (request.GET.get("subcategory_id") or "").strip()

        # Pagination (bounded)
        try:
            page = max(1, int(request.GET.get("page", 1)))
        except Exception:
            page = 1
        try:
            page_size = int(request.GET.get("page_size", 50))
        except Exception:
            page_size = 50
        page_size = min(max(1, page_size), 200)
        offset = (page - 1) * page_size

        base = AttributeSubCategory.objects.all().order_by()

        if sub_id:
            base = base.filter(
                Q(subcategory_ids__contains=[sub_id]) | Q(subcategory_ids=[])
            ).order_by()

        total = base.count()

        id_page = list(
            base.order_by("attribute_id")
                .values_list("attribute_id", flat=True)[offset : offset + page_size]
        )

        if not id_page:
            return Response(
                {"count": total, "page": page, "page_size": page_size, "results": []},
                status=status.HTTP_200_OK,
            )

        objs = AttributeSubCategory.objects.only(
            "attribute_id", "name", "slug", "type", "status",
            "description",               # NEW
            "values", "created_at", "subcategory_ids"
        ).in_bulk(id_page, field_name="attribute_id")

        items = [objs.get(aid) for aid in id_page if aid in objs and objs.get(aid)]
        items.sort(key=lambda o: (o.name or "").lower())

        data = [_serialize_attribute(a) for a in items]

        return Response(
            {"count": total, "page": page, "page_size": page_size, "results": data},
            status=status.HTTP_200_OK,
        )

class SaveSubcatAttributesAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    @transaction.atomic
    def post(self, request):
        try:
            payload = request.data if isinstance(request.data, dict) else json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        normalized, err = _normalize_payload(payload, is_create=True)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)

        # If slug collides (race), re-ensure
        if AttributeSubCategory.objects.filter(slug=normalized["slug"]).exists():
            normalized["slug"] = _ensure_unique_slug(normalized["slug"])

        obj = AttributeSubCategory.objects.create(
            attribute_id=normalized["attribute_id"],
            name=normalized["name"],
            slug=normalized["slug"],
            type=normalized["type"],
            status=normalized["status"],
            description=normalized["description"],  # NEW
            values=normalized["values"],
            subcategory_ids=normalized["subcategory_ids"],
        )
        return Response(_serialize_attribute(obj), status=status.HTTP_201_CREATED)

class EditSubcatAttributesAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    @transaction.atomic
    def put(self, request):
        try:
            payload = request.data if isinstance(request.data, dict) else json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        obj_id = str(payload.get("id") or "").strip()
        if not obj_id:
            return Response({"error": "id is required for edit"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            obj = AttributeSubCategory.objects.get(attribute_id=obj_id)
        except AttributeSubCategory.DoesNotExist:
            return Response({"error": "Attribute not found"}, status=status.HTTP_404_NOT_FOUND)

        normalized, err = _normalize_payload(payload, is_create=False)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)

        if normalized["slug"] != obj.slug and AttributeSubCategory.objects.exclude(attribute_id=obj.attribute_id).filter(slug=normalized["slug"]).exists():
            normalized["slug"] = _ensure_unique_slug(normalized["slug"])

        obj.name = normalized["name"]
        obj.slug = normalized["slug"]
        obj.type = normalized["type"]
        obj.status = normalized["status"]
        obj.description = normalized["description"]  # NEW
        obj.values = normalized["values"]
        obj.subcategory_ids = normalized["subcategory_ids"]
        obj.updated_at = timezone.now()
        obj.save()

        return Response(_serialize_attribute(obj), status=status.HTTP_200_OK)

class DeleteSubcatAttributesAPIView(APIView):
  permission_classes = [FrontendOnlyPermission]

  @transaction.atomic
  def post(self, request):
      try:
          data = request.data if isinstance(request.data, dict) else json.loads(request.body.decode("utf-8") or "{}")
      except Exception:
          data = {}
      ids = data.get("ids", [])
      if not isinstance(ids, list) or not ids:
          return Response({"error": "No IDs provided"}, status=status.HTTP_400_BAD_REQUEST)

      ids = [str(x).strip() for x in ids if str(x).strip()]
      deleted, _ = AttributeSubCategory.objects.filter(attribute_id__in=ids).delete()
      return Response({"success": True, "deleted": deleted}, status=status.HTTP_200_OK)