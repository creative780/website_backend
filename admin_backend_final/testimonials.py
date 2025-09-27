# views/testimonial_views.py
import json
import logging
from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.db.models import Avg, Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Testimonial,
    Image,
    Product,
    SubCategory,
    ProductTestimonial,
)
from .permissions import FrontendOnlyPermission
from .utilities import (
    save_image,
    _parse_payload,
    _now,
)


logger = logging.getLogger(__name__)
# --------------------------
# Helpers
# --------------------------

def _as_bool(val, default=False):
    if val is None or val == "":
        return default
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default

def _clamp_rating(n):
    try:
        n = int(round(float(n)))
    except Exception:
        return 5
    return max(1, min(5, n))

def _parse_body(request):
    """Return (data, files) for JSON or form/multipart."""
    if request.content_type and "application/json" in (request.content_type or ""):
        try:
            if isinstance(request.data, dict):
                return request.data, {}
            body = request.body.decode("utf-8") if request.body else "{}"
            return json.loads(body or "{}"), {}
        except Exception:
            return {}, {}
    return request.POST, request.FILES

def _normalize_id(val):
    v = (str(val or "")).strip()
    return v or None
# --------------------------
# Helpers (updated signature only)
# --------------------------

def _serialize_testimonial(t: Testimonial, request=None):
    """
    Return a dict for the testimonial. If an Image file exists, prefer its URL.
    Otherwise fall back to image_url. Build absolute URL when request is available.
    """
    avatar = ""
    try:
        if t.image and getattr(t.image, "image_file", None):
            avatar = t.image.image_file.url or ""
    except Exception:
        avatar = ""

    if not avatar:
        avatar = t.image_url or ""

    # If we have a relative URL and a request, make it absolute
    if request and isinstance(avatar, str) and avatar.startswith("/"):
        try:
            avatar = request.build_absolute_uri(avatar)
        except Exception:
            pass

    return {
        "id": t.testimonial_id,
        "testimonial_id": t.testimonial_id,
        "name": t.name,
        "role": t.role or "",
        "content": t.content or "",
        "image": avatar,                       # resolved, absolute when possible
        "rating": int(t.rating or 5),
        "status": t.status.title() if t.status else "Draft",
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        # raw fields if needed
        "image_id": getattr(t.image, "image_id", None) if t.image_id else None,
        "image_url": t.image_url or "",
        "order": t.order,
    }


# --------------------------
# 1) SHOW (list)
# GET /api/show-testimonials[?all=1]
# --------------------------
class ShowTestimonialsAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        include_all = _as_bool(request.query_params.get("all"), default=False)

        qs = Testimonial.objects.all().order_by("order", "-updated_at", "-created_at")
        if not include_all:
            qs = qs.filter(status="published")

        # pass request so image URLs become absolute
        data = [_serialize_testimonial(t, request) for t in qs]
        return Response(data, status=status.HTTP_200_OK)
    
# --------------------------
# 2) SAVE (create)
# POST /api/save-testimonials
# Accepts JSON or multipart/form with fields:
#  - id|testimonial_id? (if provided and exists, will update-in-place)
#  - name (required), role?, content?, rating?, status? (draft|published)
#  - image_id? (existing Image PK), or image / avatar / image_file (base64 or file)
#  - image_url? (external fallback)
# --------------------------
class SaveTestimonialsAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    @transaction.atomic
    def post(self, request):
        data, files = _parse_body(request)

        # core fields
        tid = _normalize_id(data.get("id") or data.get("testimonial_id"))
        name = (data.get("name") or "").strip()
        role = (data.get("role") or "").strip()
        content = (data.get("content") or "").strip()
        rating = _clamp_rating(data.get("rating") or 5)
        status_in = (data.get("status") or "").strip().lower()
        status_norm = "published" if status_in == "published" else "draft"

        if not name:
            return Response({"error": "name is required"}, status=status.HTTP_400_BAD_REQUEST)

        created = False
        if tid:
            obj, created = Testimonial.objects.get_or_create(
                testimonial_id=tid,
                defaults={
                    "name": name,
                    "role": role,
                    "content": content,
                    "rating": rating,
                    "status": status_norm,
                },
            )
            if not created:
                obj.name = name or obj.name
                obj.role = role if role != "" else obj.role
                obj.content = content if content != "" else obj.content
                obj.rating = rating
                obj.status = status_norm or obj.status
        else:
            obj = Testimonial(
                testimonial_id=f"t-{uuid4().hex[:12]}",
                name=name,
                role=role,
                content=content,
                rating=rating,
                status=status_norm,
            )
            created = True

        # Image resolution order: image_id > image/avatar/image_file (upload/base64) > image_url
        image_id = _normalize_id(data.get("image_id"))
        image_payload = (
            files.get("image") or files.get("avatar") or files.get("image_file")
            or data.get("image") or data.get("avatar")
        )
        image_url_fallback = (data.get("image_url") or "").strip()

        # NEW: if client sent a normal http(s) URL inside "image", treat it as image_url
        if isinstance(image_payload, str) and image_payload.strip().lower().startswith(("http://", "https://")):
            image_url_fallback = image_payload.strip()
            image_payload = None

        if image_id:
            try:
                img = Image.objects.get(image_id=image_id)
                obj.image = img
            except Image.DoesNotExist:
                pass
        elif image_payload:
            saved_img = save_image(
                file_or_base64=image_payload,
                alt_text=f"{name} avatar",
                tags="testimonial,avatar",
                linked_table="testimonial",
                linked_page="TestimonialManagement",
                linked_id=obj.testimonial_id,
            )
            if saved_img:
                obj.image = saved_img

        if image_url_fallback:
            obj.image_url = image_url_fallback

        # Optional audit/order fields
        cb = (data.get("created_by") or "").strip()
        cbt = (data.get("created_by_type") or "").strip().lower()
        if cb:
            obj.created_by = cb
        if cbt in ("admin", "user"):
            obj.created_by_type = cbt

        try:
            if "order" in data and str(data.get("order")).strip() != "":
                obj.order = max(0, int(data.get("order")))
        except Exception:
            pass

        obj.updated_at = timezone.now()
        if created:
            obj.created_at = timezone.now()
        obj.save()

        # return with absolute image URL when possible
        return Response(
            _serialize_testimonial(obj, request),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

# --------------------------
# 3) EDIT (update / delete)
# PUT/POST /api/edit-testimonials   (body includes id|testimonial_id)
# DELETE   /api/edit-testimonials?id=<id>
# --------------------------
class EditTestimonialsAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def put(self, request):
        return self._save_or_update(request)

    def post(self, request):
        return self._save_or_update(request)

    @transaction.atomic
    def delete(self, request):
        tid = _normalize_id(request.query_params.get("id") or request.query_params.get("testimonial_id"))
        if not tid:
            return Response({"error": "id is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            obj = Testimonial.objects.get(testimonial_id=tid)
        except Testimonial.DoesNotExist:
            return Response({"error": "Testimonial not found"}, status=status.HTTP_404_NOT_FOUND)

        obj.delete()
        return Response({"success": True, "deleted": tid}, status=status.HTTP_200_OK)

    @transaction.atomic
    def _save_or_update(self, request):
        data, files = _parse_body(request)
        tid = _normalize_id(data.get("id") or data.get("testimonial_id"))
        if not tid:
            return Response({"error": "id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            obj = Testimonial.objects.get(testimonial_id=tid)
        except Testimonial.DoesNotExist:
            return Response({"error": "Testimonial not found"}, status=status.HTTP_404_NOT_FOUND)

        # Patchable fields
        if "name" in data:
            v = (data.get("name") or "").strip()
            if v:
                obj.name = v

        if "role" in data:
            obj.role = (data.get("role") or "").strip()

        if "content" in data or "message" in data:
            obj.content = (data.get("content") or data.get("message") or "").strip() or obj.content

        if "rating" in data:
            obj.rating = _clamp_rating(data.get("rating"))

        if "status" in data:
            st = (data.get("status") or "").strip().lower()
            if st in ("draft", "published"):
                obj.status = st

        if "order" in data:
            try:
                obj.order = max(0, int(data.get("order")))
            except Exception:
                pass

        # Image update
        image_id = _normalize_id(data.get("image_id"))
        image_payload = (
            files.get("image") or files.get("avatar") or files.get("image_file")
            or data.get("image") or data.get("avatar")
        )
        image_url_fallback = (data.get("image_url") or "").strip()

        # NEW: accept plain URL inside "image" as image_url
        if isinstance(image_payload, str) and image_payload.strip().lower().startswith(("http://", "https://")):
            image_url_fallback = image_payload.strip()
            image_payload = None

        if image_id:
            try:
                img = Image.objects.get(image_id=image_id)
                obj.image = img
            except Image.DoesNotExist:
                pass

        elif image_payload:
            saved_img = save_image(
                file_or_base64=image_payload,
                alt_text=f"{obj.name} avatar",
                tags="testimonial,avatar",
                linked_table="testimonial",
                linked_page="TestimonialManagement",
                linked_id=obj.testimonial_id,
            )
            if saved_img:
                obj.image = saved_img

        if "image_url" in data or image_url_fallback:
            obj.image_url = image_url_fallback

        obj.updated_at = timezone.now()
        obj.save()

        # include absolute image URL
        return Response({"success": True, **_serialize_testimonial(obj, request)}, status=status.HTTP_200_OK)
    
    
def _coerce_half_star(value, default=0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    v = max(0.0, min(5.0, v))
    # round to nearest 0.5
    return round(v * 2) / 2.0

def _one_of_product_or_subcategory(payload):
    """Return (product, subcategory) ensuring exactly one is provided; both None means error upstream."""
    product_id = payload.get("product_id")
    subcategory_id = payload.get("subcategory_id")
    if bool(product_id) == bool(subcategory_id):
        # Either both set or both empty -> invalid for creation; for listing we handle elsewhere
        return None, None

    if product_id:
        product = get_object_or_404(Product, product_id=product_id)
        return product, None

    subcategory = get_object_or_404(SubCategory, subcategory_id=subcategory_id)
    return None, subcategory

def _serialize_product_testimonial(t: ProductTestimonial):
    return {
        "id": str(t.testimonial_id),
        "name": t.name,
        "content": t.content or "",
        "rating": float(t.rating or 0.0),
        "rating_count": int(t.rating_count or 0),
        "status": t.status,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
        # For transparency/debug, FE currently doesn’t need these:
        "product_id": getattr(t.product, "product_id", None),
        "subcategory_id": getattr(t.subcategory, "subcategory_id", None),
    }

def _recompute_product_aggregate(product: Product):
    """
    Recompute Product.rating and Product.rating_count from APPROVED testimonials only.
    - rating = half-star rounded average of non-null testimonial.rating
    - rating_count = number of approved testimonials
    """
    if not product:
        return
    approved = ProductTestimonial.objects.filter(product=product, status="approved")
    agg = approved.aggregate(avg=Avg("rating"), cnt=Count("testimonial_id"))
    avg = float(agg.get("avg") or 0.0)
    cnt = int(agg.get("cnt") or 0)
    product.rating = _coerce_half_star(avg, 0.0)
    product.rating_count = max(0, cnt)
    product.save(update_fields=["rating", "rating_count"])


# -----------------------
# API Views
# -----------------------
class ShowProductCommentAPIView(APIView):
    """
    POST /api/show-product-comment
    Body:
      {
        "product_id": "P-123" | null,
        "subcategory_id": "S-123" | null,
        "include_pending": false,         # default False (only approved)
        "include_hidden": false,          # default False
        "limit": 50,                      # optional
        "offset": 0                       # optional
      }
    Returns: list[comment]
    """
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = _parse_payload(request)
        except Exception:
            return Response({"error": "Invalid JSON body"}, status=status.HTTP_400_BAD_REQUEST)

        product_id = data.get("product_id")
        subcategory_id = data.get("subcategory_id")
        include_pending = bool(data.get("include_pending", False))
        include_hidden = bool(data.get("include_hidden", False))
        limit = max(1, min(int(data.get("limit") or 50), 200))
        offset = max(0, int(data.get("offset") or 0))

        qs = ProductTestimonial.objects.all().order_by("-created_at")

        # Scope to either product or subcategory when provided; if neither provided, return empty (FE always passes one)
        if product_id and subcategory_id:
            return Response({"error": "Provide either product_id OR subcategory_id, not both."},
                            status=status.HTTP_400_BAD_REQUEST)
        if product_id:
            qs = qs.filter(product__product_id=product_id)
        elif subcategory_id:
            qs = qs.filter(subcategory__subcategory_id=subcategory_id)
        else:
            # No scope → nothing to show
            return Response([], status=status.HTTP_200_OK)

        # Visibility rules: default to approved only
        visible = Q(status="approved")
        if include_pending:
            visible |= Q(status="pending")
        if include_hidden:
            visible |= Q(status="hidden")
        qs = qs.filter(visible)

        rows = list(qs[offset: offset + limit])
        out = [_serialize_product_testimonial(t) for t in rows]
        return Response(out, status=status.HTTP_200_OK)

class EditProductCommentAPIView(APIView):
    """
    POST /api/edit-product-comment
    Create or update.

    Create (no comment_id):
      {
        "name": "...", "email": "...",
        "content": "...",
        "rating": 0..5, "rating_count": 1,
        "status": "pending" | "approved" | "rejected" | "hidden",
        "product_id": "...",    # exactly one of these required
        "subcategory_id": "..."
      }

    Update (with comment_id):
      {
        "comment_id": "...",
        # any of: name, email, content, rating, rating_count, status
      }
    """
    permission_classes = [FrontendOnlyPermission]

    @transaction.atomic
    def post(self, request):
        try:
            data = _parse_payload(request)
        except Exception:
            return Response({"error": "Invalid JSON body"}, status=status.HTTP_400_BAD_REQUEST)

        comment_id = data.get("comment_id")

        # ---------------- Create ----------------
        if not comment_id:
            # minimal validation
            name = (data.get("name") or "").strip()
            email = (data.get("email") or "").strip()
            content = (data.get("content") or "").strip()
            if not (name and email and content):
                return Response({"error": "name, email and content are required."},
                                status=status.HTTP_400_BAD_REQUEST)

            product, subcategory = _one_of_product_or_subcategory(data)
            if not (product or subcategory):
                return Response({"error": "Provide exactly one of product_id or subcategory_id."},
                                status=status.HTTP_400_BAD_REQUEST)

            rating = _coerce_half_star(data.get("rating", 0.0), 0.0)
            rating_count = int(data.get("rating_count") or 1)
            status_val = data.get("status") or "pending"
            if status_val not in {"pending", "approved", "rejected", "hidden"}:
                status_val = "pending"

            t = ProductTestimonial.objects.create(
                product=product,
                subcategory=subcategory,
                name=name[:120],
                email=email[:254],
                content=content,
                rating=rating,
                rating_count=max(1, rating_count),
                status=status_val,
            )

            # If linked to product, recompute aggregates when status is approved
            if product and status_val == "approved":
                _recompute_product_aggregate(product)

            return Response({"success": True, "comment": _serialize_product_testimonial(t)},
                            status=status.HTTP_200_OK)

        # ---------------- Update ----------------
        t = get_object_or_404(ProductTestimonial, pk=comment_id)

        # track product before/after for aggregate updates
        linked_product = t.product

        fields_to_update = []
        if "name" in data and (data.get("name") or "").strip():
            t.name = data["name"].strip()[:120]
            fields_to_update.append("name")
        if "email" in data and (data.get("email") or "").strip():
            t.email = data["email"].strip()[:254]
            fields_to_update.append("email")
        if "content" in data and (data.get("content") or "").strip():
            t.content = data["content"]
            fields_to_update.append("content")
        if "rating" in data:
            t.rating = _coerce_half_star(data.get("rating"), t.rating)
            fields_to_update.append("rating")
        if "rating_count" in data:
            try:
                t.rating_count = max(1, int(data["rating_count"]))
            except (TypeError, ValueError):
                pass
            else:
                fields_to_update.append("rating_count")
        if "status" in data:
            new_status = str(data.get("status") or "pending")
            if new_status in {"pending", "approved", "rejected", "hidden"}:
                t.status = new_status
                fields_to_update.append("status")

        # Optional move between product/subcategory (still exactly one)
        want_product_id = data.get("product_id")
        want_subcategory_id = data.get("subcategory_id")
        if want_product_id or want_subcategory_id:
            if bool(want_product_id) == bool(want_subcategory_id):
                return Response({"error": "Provide exactly one of product_id or subcategory_id when re-linking."},
                                status=status.HTTP_400_BAD_REQUEST)
            if want_product_id:
                t.product = get_object_or_404(Product, product_id=want_product_id)
                t.subcategory = None
            else:
                t.subcategory = get_object_or_404(SubCategory, subcategory_id=want_subcategory_id)
                t.product = None
            fields_to_update.extend(["product", "subcategory"])

        if fields_to_update:
            t.save()

        # Recompute product aggregates if:
        #  - it is linked to a product, AND
        #  - status or rating changed in a way that affects approved set.
        # Conservative rule: recompute whenever linked product exists and any update happened.
        if t.product:
            _recompute_product_aggregate(t.product)
        elif linked_product:
            # Moved off product, recompute old product as well
            _recompute_product_aggregate(linked_product)

        return Response({"success": True, "comment": _serialize_product_testimonial(t)},
                        status=status.HTTP_200_OK)

class DeleteProductCommentAPIView(APIView):
    """
    POST /api/delete-product-comment
    Body: { "comment_id": "..." }
    """
    permission_classes = [FrontendOnlyPermission]

    @transaction.atomic
    def post(self, request):
        try:
            data = _parse_payload(request)
        except Exception:
            return Response({"error": "Invalid JSON body"}, status=status.HTTP_400_BAD_REQUEST)

        cid = data.get("comment_id")
        if not cid:
            return Response({"error": "comment_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        t = get_object_or_404(ProductTestimonial, pk=cid)
        linked_product = t.product
        t.delete()

        if linked_product:
            _recompute_product_aggregate(linked_product)

        return Response({"success": True}, status=status.HTTP_200_OK)