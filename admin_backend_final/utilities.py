# Standard Library
from decimal import InvalidOperation
import os
import re
import json
import uuid
import base64
from io import BytesIO
import logging
# Third-party
from PIL import Image as PILImage
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# Django
from django.utils import timezone
from django.core.files.base import ContentFile
from django.utils.text import slugify

# Django REST Framework
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

# Local Imports
from .models import *  # Consider specifying models instead of wildcard import
from .permissions import FrontendOnlyPermission
from django.core.files.base import ContentFile
from django.db import DatabaseError, IntegrityError

def format_datetime(dt):
    return dt.strftime('%d-%B-%Y-%I:%M%p')

def format_slug(name):
    return slugify(name)

def generate_category_id(name):
    base = ''.join(word[0].upper() for word in name.split())
    similar = Category.objects.filter(category_id__startswith=base).count()
    return f"{base}-{similar + 1}"

def generate_subcategory_id(name, category_ids):
    # Extract base name
    base = name.split()[0].upper()

    # Extract category prefix (e.g., from "CAT-01" -> "CAT")
    category_code = category_ids[0].split('-')[0] if category_ids else "GEN"

    # Build the ID prefix to search
    id_prefix = f"{category_code}-{base}-"

    # Find the highest existing number for this prefix
    existing_ids = SubCategory.objects.filter(subcategory_id__startswith=id_prefix).values_list('subcategory_id', flat=True)

    max_number = 0
    for sid in existing_ids:
        try:
            number_part = int(sid.split('-')[-1])
            if number_part > max_number:
                max_number = number_part
        except (ValueError, IndexError):
            continue

    next_number = max_number + 1

    return f"{category_code}-{base}-{next_number:03d}"

def generate_product_id(name, subcat_id):
    fallback_subcat_prefix = subcat_id[0].upper() if subcat_id else 'X'

    if '-' in subcat_id:
        parts = subcat_id.split('-', 1)
        after_hyphen = parts[1]
        segment = after_hyphen.split('-')[0].upper()
        subcat_prefix = segment[:2] if len(segment) >= 1 else fallback_subcat_prefix
    else:
        subcat_prefix = fallback_subcat_prefix

    # Get the first letters of the first two words only
    words = re.findall(r'\b\w+', name)
    first_letters = ''.join(word[0].upper() for word in words[:2])

    base = f"{subcat_prefix}-{first_letters}"

    existing_ids = Product.objects.filter(product_id__startswith=base).values_list('product_id', flat=True)

    numbers = []
    for pid in existing_ids:
        match = re.search(r'(\d{3})$', pid)
        if match:
            numbers.append(int(match.group(1)))

    next_num = 1
    if numbers:
        next_num = max(numbers) + 1

    if next_num > 999:
        raise ValueError("Exceeded maximum 3-digit unique number for this base ID")

    num_str = f"{next_num:03d}"

    return f"{base}-{num_str}"


def generate_inventory_id(product_id):
    return f"INV-{product_id}"

def generate_unique_slug(base_slug, instance=None):
    slug = base_slug
    counter = 1
    while ProductSEO.objects.filter(slug=slug).exclude(product=instance).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug

def generate_unique_seo_id(base_id):
    count = 1
    new_id = base_id
    while ProductSEO.objects.filter(seo_id=new_id).exists():
        new_id = f"{base_id}-{count}"
        count += 1
    return new_id

def generate_custom_order_id(user_name, email):
    prefix = 'O'
    uname = user_name[:2].upper() if user_name else "GU"
    email_part = email[:2].upper() if email else "EM"
    base_id = f"{prefix}{uname}-{email_part}-"

    existing_ids = Orders.objects.filter(order_id__startswith=f"{base_id}").values_list('order_id', flat=True)

    existing_suffixes = [
        int(order_id.split("-")[-1]) for order_id in existing_ids
        if order_id.split("-")[-1].isdigit()
    ]
    next_number = max(existing_suffixes, default=0) + 1
    formatted_number = f"{next_number:03}"

    return f"{base_id}{formatted_number}"

def generate_admin_id(name: str, role_name: str, attempt=1) -> str:
    first_name = name.split()[0]
    last_name = name.split()[-1]
    prefix = f"A{first_name[0]}{last_name[-2:]}".upper()
    role_prefix = role_name[:2].upper()
    base_id = f"{prefix}-{role_prefix}"

    existing_ids = Admin.objects.filter(admin_id__startswith=base_id).values_list('admin_id', flat=True)

    used_numbers = set()
    for eid in existing_ids:
        try:
            suffix = eid.replace(base_id, "").strip("-")
            used_numbers.add(int(suffix))
        except:
            continue

    i = 1
    while i in used_numbers:
        i += 1

    return f"{base_id}-{i:03d}"

logger = logging.getLogger(__name__)

# Optional: cap remote downloads to prevent abuse (5 MB here)
_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
_DEFAULT_TIMEOUT = 10  # seconds

_CONTENT_TYPE_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/x-icon": ".ico",
    "image/svg+xml": ".svg",
}

def _is_data_url(s: str) -> bool:
    return s.startswith("data:image/")

def _is_http_url(s: str) -> bool:
    try:
        p = urlparse(s)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

def _safe_fetch(url: str) -> tuple[bytes, str]:
    """
    Fetch URL with a size cap and timeout. Returns (bytes, content_type).
    Raises on HTTP or IO errors.
    """
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ImageFetcher/1.0)"})
    with urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        # Read with cap
        chunks = []
        bytes_read = 0
        while True:
            chunk = resp.read(64 * 1024)  # 64 KB
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > _MAX_DOWNLOAD_BYTES:
                raise ValueError("Remote image exceeds maximum allowed size")
            chunks.append(chunk)
        return b"".join(chunks), content_type

def _infer_ext(url: str, content_type: str, pil_format: str | None) -> str:
    # 1) From Content-Type header
    if content_type in _CONTENT_TYPE_TO_EXT:
        return _CONTENT_TYPE_TO_EXT[content_type]
    # 2) From URL path
    path = urlparse(url).path.lower()
    _, ext = os.path.splitext(path)
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".ico", ".svg"}:
        return ext
    # 3) From PIL format
    if pil_format:
        fmt = pil_format.lower()
        mapping = {
            "jpeg": ".jpg",
            "png": ".png",
            "webp": ".webp",
            "gif": ".gif",
            "bmp": ".bmp",
            "tiff": ".tiff",
            "ico": ".ico",
            "svg": ".svg",  # unlikely via PIL
        }
        return mapping.get(fmt, ".png")
    # 4) Fallback
    return ".png"

def save_image(file_or_base64, alt_text="Alt-text", tags="", linked_table="", linked_page="", linked_id=""):
    try:
        # --- CASE 1: Data URL (base64) ---
        if isinstance(file_or_base64, str) and _is_data_url(file_or_base64):
            header, encoded = file_or_base64.split(",", 1)
            file_ext = header.split("/")[1].split(";")[0]
            image_data = base64.b64decode(encoded)
            img = PILImage.open(BytesIO(image_data))
            width, height = img.size
            filename = f"{uuid.uuid4()}.{file_ext}"
            content_file = ContentFile(image_data, name=filename)
            image_type = f".{file_ext}"

        # --- CASE 2: Remote URL ---
        elif isinstance(file_or_base64, str) and _is_http_url(file_or_base64):
            url = file_or_base64
            blob, content_type = _safe_fetch(url)
            # Validate it’s an image by trying to open via PIL
            bio = BytesIO(blob)
            img = PILImage.open(bio)
            img.load()  # force decode to catch truncated files early
            width, height = img.size
            image_ext = _infer_ext(url, content_type, img.format)
            filename = f"{uuid.uuid4()}{image_ext}"
            content_file = ContentFile(blob, name=filename)
            image_type = image_ext

        # --- CASE 3: File-like / In-memory upload ---
        else:
            img = PILImage.open(file_or_base64)
            img.load()
            width, height = img.size
            filename = getattr(file_or_base64, "name", f"{uuid.uuid4()}.png")
            content_file = file_or_base64
            image_type = os.path.splitext(filename)[-1].lower() or ".png"

        parsed_tags = [tag.strip() for tag in tags.split(",")] if tags else []

        new_image = Image(
            image_id=f"IMG-{uuid.uuid4().hex[:8]}",
            alt_text=alt_text,
            width=width,
            height=height,
            tags=parsed_tags,
            image_type=image_type,
            linked_table=linked_table,
            linked_page=linked_page,
            linked_id=linked_id,
            created_at=timezone.now(),
        )
        # These can raise storage/DB errors – let them propagate to the outer except
        new_image.image_file.save(filename, content_file)
        new_image.save()
        return new_image

    except (IntegrityError, DatabaseError):
        # DB/storage-backed failures: bubble up to abort transaction
        raise
    except Exception as e:
        # Non-DB hiccups (e.g., network error, invalid image) should not kill the whole request
        logger.exception("Image save error (non-DB): %s", e)
        return None

class GenerateProductIdAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = request.data if isinstance(request.data, dict) else json.loads(request.body or "{}")
        except Exception:
            data = {}

        name = data.get('name')
        subcat_id = data.get('subcategory_id')

        if not name or not subcat_id:
            return Response({'error': 'Missing name or subcategory_id'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            product_id = generate_product_id(name, subcat_id)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({'product_id': product_id}, status=status.HTTP_200_OK)

    def get(self, request):
        return Response({'error': 'Invalid request method'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class SaveImageAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        """
        Wraps save_image() so frontend can POST either multipart (image file)
        or JSON with base64 data URL in 'image', and optional alt_text, tags, linked_*.
        Returns success + basic image metadata. (If you had a different response earlier,
        adjust below to match; defaults here are sensible.)
        """
        data = request.data if request.content_type and 'application/json' in request.content_type else request.POST
        files = request.FILES

        image_data = files.get('image') or data.get('image')
        if not image_data:
            return Response({'error': 'No image provided'}, status=status.HTTP_400_BAD_REQUEST)

        alt_text = (data.get('alt_text') or 'Alt-text').strip()
        tags = (data.get('tags') or '')
        linked_table = data.get('linked_table') or ''
        linked_page = data.get('linked_page') or ''
        linked_id = data.get('linked_id') or ''

        img = save_image(
            file_or_base64=image_data,
            alt_text=alt_text,
            tags=tags,
            linked_table=linked_table,
            linked_page=linked_page,
            linked_id=linked_id
        )
        if not img:
            return Response({'error': 'Image save failed'}, status=status.HTTP_400_BAD_REQUEST)

        url = getattr(img, 'url', None) or getattr(img.image_file, 'url', None)

        return Response({
            'success': True,
            'image_id': img.image_id,
            'url': url,
            'alt_text': img.alt_text,
            'width': img.width,
            'height': img.height,
            'image_type': img.image_type
        }, status=status.HTTP_201_CREATED)


def format_image_object(image_obj, request=None):
    """
    Accepts either:
      - a through-model instance (e.g., CategoryImage/SubCategoryImage/ProductImage) that has .image, or
      - an Image instance directly

    Returns: {"url": <abs_or_rel_url>, "alt_text": <str>} or None
    """
    img = getattr(image_obj, "image", image_obj)  # support through-model or direct Image
    if not img:
        return None

    url = getattr(img, "url", None)  # your Image.url property returns "/media/.."
    if not url:
        return None

    # build absolute URL when request is available (prod-safe; works in dev too)
    if request:
        try:
            url = request.build_absolute_uri(url)
        except Exception:
            # fallback to relative on any edge error
            pass

    return {
        "url": url,
        "alt_text": getattr(img, "alt_text", "") or "Image"
    }
  
  
def _parse_payload(request):
    """Consistent, tolerant request payload parsing."""
    if isinstance(request.data, dict):
        return request.data
    try:
        body = request.body.decode("utf-8") if request.body else "{}"
        return json.loads(body or "{}")
    except Exception:
        return {}

def _now():
    return timezone.now()

def _to_decimal(val, default="0"):
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)

def _as_list(val):
    """Coerce incoming field to list[str] safely."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val if str(x).strip()]
    if isinstance(val, str):
        return [v.strip() for v in val.split(",") if v.strip()]
    return []
