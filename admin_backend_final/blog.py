# views/blog_views.py

import json
from uuid import uuid4
import base64, mimetypes, os
from typing import Optional
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .models import (
    BlogPost,
    BlogImage,
    Image,
    ProductImage,
    CategoryImage,
    SubCategoryImage,
    Attribute,
    BlogComment,
)
from .permissions import FrontendOnlyPermission
from .utilities import save_image

# --------------------------
# Helpers
# --------------------------

def parse_bool(val, default=False):
    """Consistent boolean parsing for 'true/false/1/0/yes/no/on/off'."""
    if val is None or val == "":
        return default
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default

def generate_blog_id(title: str = "") -> str:
    base = slugify(title).replace("-", "")[:18] if title else ""
    return (base or "blog") + "-" + uuid4().hex[:10]

def ensure_unique_slug(raw_slug: str, exclude_blog_id: str = None) -> str:
    slug = slugify((raw_slug or "").strip()) or uuid4().hex[:8]
    i = 2
    q = BlogPost.objects.all()
    if exclude_blog_id:
        q = q.exclude(blog_id=exclude_blog_id)
    while q.filter(slug=slug).exists():
        slug = f"{slug}-{i}"
        i += 1
    return slug

def _image_to_data_uri(img) -> Optional[str]:
    """Try to return a data URI from Image.image_file; fallback to url."""
    if not img:
        return None
    f = getattr(img, "image_file", None)
    try:
        if f and hasattr(f, "path") and os.path.exists(f.path):
            with open(f.path, "rb") as fh:
                raw = fh.read()
            mime, _ = mimetypes.guess_type(f.name or "")
            mime = mime or "image/jpeg"
            b64 = base64.b64encode(raw).decode("ascii")
            return f"data:{mime};base64,{b64}"
    except Exception:
        pass
    return img.url or None

def get_primary_thumbnail_url(blog: BlogPost) -> Optional[str]:
    """
    Returns a data URI (base64) for the primary image if available,
    otherwise the first image. Falls back to .url if the file cannot be read.
    """
    rel = (blog.images.select_related("image").filter(is_primary=True).first()
           or blog.images.select_related("image").first())
    if not rel or not rel.image:
        return None
    return _image_to_data_uri(rel.image)

@transaction.atomic
def set_primary_image(blog: BlogPost, img: Image) -> None:
    BlogImage.objects.filter(blog=blog, is_primary=True).update(is_primary=False)
    BlogImage.objects.update_or_create(
        blog=blog,
        image=img,
        defaults={"is_primary": True, "order": 0},
    )

def _compute_status(draft: bool, publish_date):
    now = timezone.now()
    if draft:
        return "draft"
    if publish_date and publish_date > now:
        return "scheduled"
    return "published"

# --------------------------
# 1) SAVE (create or update)
# --------------------------
class SaveBlogAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    @transaction.atomic
    def post(self, request):
        # Accept JSON or multipart
        if request.content_type and 'application/json' in (request.content_type or ''):
            try:
                data = request.data if isinstance(request.data, dict) \
                    else json.loads(request.body.decode('utf-8') or '{}')
            except Exception:
                data = {}
            files = {}
        else:
            data = request.POST
            files = request.FILES

        # ---- map inputs (camelCase supported) ----
        blog_id = (data.get('id') or data.get('blog_id') or '').strip() or None
        title = (data.get('title') or '').strip()
        slug_in = (data.get('slug') or '').strip()
        content_html = data.get('content') or data.get('content_html') or ''
        author = (data.get('author') or '').strip()
        meta_title = (data.get('metaTitle') or data.get('meta_title') or '').strip()
        meta_description = data.get('metaDescription') or data.get('meta_description') or ''
        og_title = (data.get('ogTitle') or data.get('og_title') or '').strip()
        og_image_url = (data.get('ogImage') or data.get('og_image') or data.get('og_image_url') or '').strip()
        tags_csv = (data.get('tags') or data.get('tags_csv') or '').strip()
        schema_enabled = parse_bool(data.get('schemaEnabled'), default=False)
        draft = parse_bool(data.get('draft'), default=False)

        # --- robust publishDate parsing (handles Z/offset + naive) ---
        raw_pd = data.get('publishDate') or data.get('publish_date') or None
        publish_date = None
        if raw_pd:
            try:
                dt = timezone.datetime.fromisoformat(str(raw_pd).replace('Z', '+00:00'))
                if timezone.is_naive(dt):
                    dt = timezone.make_aware(dt, timezone.get_current_timezone())
                publish_date = dt
            except Exception:
                publish_date = None

        featured_image_data = files.get('featuredImage') or data.get('featuredImage') or None

        if not title:
            return Response({'error': 'Title is required'}, status=status.HTTP_400_BAD_REQUEST)

        # ---- upsert ----
        created = False
        if blog_id:
            try:
                blog = BlogPost.objects.get(blog_id=blog_id)
            except BlogPost.DoesNotExist:
                blog = BlogPost(blog_id=blog_id)
                created = True
        else:
            blog = BlogPost(blog_id=generate_blog_id(title))
            created = True

        # slug
        if slug_in:
            blog.slug = ensure_unique_slug(slug_in, exclude_blog_id=None if created else blog.blog_id)
        elif created:
            blog.slug = ensure_unique_slug(title, exclude_blog_id=None if created else blog.blog_id)

        # assign
        blog.title = title
        blog.content_html = content_html or ""
        blog.author = author
        blog.meta_title = meta_title
        blog.meta_description = meta_description or ""
        blog.og_title = og_title
        blog.og_image_url = og_image_url
        blog.tags = tags_csv
        blog.schema_enabled = schema_enabled
        blog.publish_date = publish_date
        blog.draft = draft
        blog.status = _compute_status(blog.draft, blog.publish_date)
        blog.updated_at = timezone.now()
        if created:
            blog.created_at = timezone.now()
        blog.save()

        # image: save and force primary
        if featured_image_data:
            img = save_image(
                file_or_base64=featured_image_data,
                alt_text=f"{blog.title} featured image",
                tags="blog,featured",
                linked_table="blog",
                linked_page="BlogManagementPage",
                linked_id=blog.blog_id
            )
            if img:
                set_primary_image(blog, img)

        if not blog.images.filter(is_primary=True).exists():
            first_rel = blog.images.select_related("image").first()
            if first_rel and first_rel.image:
                set_primary_image(blog, first_rel.image)

        thumb = get_primary_thumbnail_url(blog)

        return Response({
            'id': blog.blog_id,
            'blog_id': blog.blog_id,
            'title': blog.title,
            'slug': blog.slug,
            'content': blog.content_html,
            'tags': blog.tags,
            'author': blog.author,
            'metaTitle': blog.meta_title,
            'metaDescription': blog.meta_description,
            'ogTitle': blog.og_title,
            'ogImage': blog.og_image_url,
            'schemaEnabled': blog.schema_enabled,
            'publishDate': blog.publish_date.isoformat() if blog.publish_date else None,
            'draft': blog.draft,
            'status': blog.status,
            'created_at': blog.created_at.isoformat() if blog.created_at else None,
            'updated_at': blog.updated_at.isoformat() if blog.updated_at else None,
            'thumbnail': thumb or None
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

class ShowAllBlogsAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    
    def get(self, request):
        now = timezone.now()
        include_all = str(request.query_params.get('all', '')).lower() in ('1','true','yes')

        qs = (BlogPost.objects.all() if include_all else
              BlogPost.objects.filter(draft=False).filter(
                  Q(publish_date__isnull=True) | Q(publish_date__lte=now)
              )).order_by('-created_at')

        result = []
        for b in qs:
            effective_status = b.compute_status()
            if effective_status != (b.status or ""):
                BlogPost.objects.filter(pk=b.pk).update(status=effective_status)

            thumb = get_primary_thumbnail_url(b)
            status_label = effective_status.title()
            created_str = b.created_at.date().isoformat() if b.created_at else ""
            updated_str = b.updated_at.date().isoformat() if b.updated_at else ""

            result.append({
                'id': b.blog_id,
                'title': b.title,
                'slug': b.slug,
                'thumbnail': thumb,
                'author': b.author or "",
                'category': 'General',
                'status': status_label,
                'created': created_str,
                'updated': updated_str,
                'content': b.content_html or "",

                # 👇 NEW fields so the fallback isn’t lossy
                'tags': b.tags or "",
                'metaTitle': b.meta_title or "",
                'metaDescription': b.meta_description or "",
                'ogTitle': b.og_title or "",
                'ogImage': b.og_image_url or "",
            })
        return Response(result, status=status.HTTP_200_OK)

# --------------------------
# 3) EDIT BY ID (POST/PUT)
# --------------------------
class EditBlogAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def put(self, request, blog_id):
        return self.post(request, blog_id)

    @transaction.atomic
    def post(self, request, blog_id):
        if request.content_type and 'application/json' in (request.content_type or ''):
            try:
                data = request.data if isinstance(request.data, dict) \
                    else json.loads(request.body.decode('utf-8') or '{}')
            except Exception:
                data = {}
            files = {}
        else:
            data = request.POST
            files = request.FILES

        try:
            blog = BlogPost.objects.get(blog_id=blog_id)
        except BlogPost.DoesNotExist:
            return Response({'error': 'Blog not found'}, status=status.HTTP_404_NOT_FOUND)

        if 'title' in data:
            v = (data.get('title') or '').strip()
            if v:
                blog.title = v

        if 'slug' in data:
            raw = (data.get('slug') or '').strip()
            if raw:
                blog.slug = ensure_unique_slug(raw, exclude_blog_id=blog.blog_id)

        if 'content' in data or 'content_html' in data:
            blog.content_html = data.get('content') or data.get('content_html') or blog.content_html

        if 'author' in data:
            blog.author = (data.get('author') or '').strip()

        if any(k in data for k in ('metaTitle','meta_title')):
            blog.meta_title = (data.get('metaTitle') or data.get('meta_title') or '').strip()

        if any(k in data for k in ('metaDescription','meta_description')):
            blog.meta_description = data.get('metaDescription') or data.get('meta_description') or blog.meta_description

        if any(k in data for k in ('ogTitle','og_title')):
            blog.og_title = (data.get('ogTitle') or data.get('og_title') or '').strip()

        if any(k in data for k in ('ogImage','og_image','og_image_url')):
            blog.og_image_url = (data.get('ogImage') or data.get('og_image') or data.get('og_image_url') or '').strip()

        if 'tags' in data or 'tags_csv' in data:
            blog.tags = (data.get('tags') or data.get('tags_csv') or '').strip()

        if 'schemaEnabled' in data:
            blog.schema_enabled = parse_bool(data.get('schemaEnabled'), default=blog.schema_enabled)

        if 'draft' in data:
            blog.draft = False  # (note: this forces non-draft if present)

        if 'publishDate' in data or 'publish_date' in data:
            pd = data.get('publishDate') or data.get('publish_date')
            if pd is None or pd == "":
                blog.publish_date = None
            else:
                try:
                    dt = timezone.datetime.fromisoformat(str(pd).replace('Z', '+00:00'))
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt, timezone.get_current_timezone())
                    blog.publish_date = dt
                except Exception:
                    pass

        blog.status = _compute_status(blog.draft, blog.publish_date)
        blog.updated_at = timezone.now()
        blog.save()

        featured_image_data = files.get('featuredImage') or data.get('featuredImage') or None
        if featured_image_data:
            img = save_image(
                file_or_base64=featured_image_data,
                alt_text=f"{blog.title} featured image",
                tags="blog,featured",
                linked_table="blog",
                linked_page="BlogManagementPage",
                linked_id=blog.blog_id
            )
            if img:
                set_primary_image(blog, img)

        if not blog.images.filter(is_primary=True).exists():
            first_rel = blog.images.select_related("image").first()
            if first_rel and first_rel.image:
                set_primary_image(blog, first_rel.image)

        thumb = get_primary_thumbnail_url(blog)
        return Response({
            'success': True,
            'id': blog.blog_id,
            'thumbnail': thumb
        }, status=status.HTTP_200_OK)

# --------------------------
# 4) DELETE (bulk; POST/DELETE)
# --------------------------

class DeleteBlogsAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def delete(self, request):
        data = self._parse_json_body(request)
        return self._delete_impl(data)

    def post(self, request):
        data = self._parse_json_body(request)
        return self._delete_impl(data)

    # ----- helpers -----

    def _parse_json_body(self, request):
        try:
            if request.content_type and 'application/json' in (request.content_type or ''):
                if isinstance(request.data, dict):
                    return request.data
            body = request.body.decode('utf-8') if request.body else '{}'
            return json.loads(body or '{}')
        except Exception:
            return {}

    def _image_is_orphan(self, img):
        return not (
            BlogImage.objects.filter(image=img).exists() or
            CategoryImage.objects.filter(image=img).exists() or
            SubCategoryImage.objects.filter(image=img).exists() or
            ProductImage.objects.filter(image=img).exists() or
            Attribute.objects.filter(image=img).exists()
        )

    @transaction.atomic
    def _delete_impl(self, data):
        blog_ids = data.get('ids') or data.get('id') or []
        if isinstance(blog_ids, (str, int)):
            blog_ids = [blog_ids]

        blog_ids = [str(x).strip() for x in blog_ids if str(x).strip()]
        if not blog_ids:
            return Response({'error': 'No blog IDs provided'}, status=status.HTTP_400_BAD_REQUEST)

        deleted, not_found = [], []
        candidate_images, files_removed, images_removed = [], 0, 0

        for bid in blog_ids:
            try:
                blog = BlogPost.objects.get(blog_id=bid)
            except BlogPost.DoesNotExist:
                not_found.append(bid)
                continue

            rels = list(BlogImage.objects.filter(blog=blog).select_related('image'))
            imgs = [r.image for r in rels if r.image_id]
            candidate_images.extend(imgs)

            BlogImage.objects.filter(blog=blog).delete()
            blog.delete()
            deleted.append(bid)

        seen = set()
        for img in candidate_images:
            if not img or img.image_id in seen:
                continue
            seen.add(img.image_id)

            if self._image_is_orphan(img):
                try:
                    if getattr(img, 'image_file', None):
                        try:
                            img.image_file.delete(save=False)
                            files_removed += 1
                        except Exception:
                            pass
                    img.delete()
                    images_removed += 1
                except Exception:
                    pass

        return Response({
            'success': True,
            'deleted': deleted,
            'not_found': not_found,
            'images_removed': images_removed,
            'files_removed': files_removed
        }, status=status.HTTP_200_OK)

class ShowAllCommentsAPIView(APIView):
    """
    GET /api/show-all-comments
      Optional filters:
        - ?blog_id=<id>
        - ?blog_slug=<slug>
    Returns list of:
      { id, name, date, message, website, blog_id, blog_slug }
    """
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        blog_id = (request.query_params.get("blog_id") or "").strip()
        blog_slug = (request.query_params.get("blog_slug") or "").strip()

        qs = BlogComment.objects.select_related("blog").all().order_by("-created_at")

        # Filter if caller provided linkage
        if blog_id:
            qs = qs.filter(blog__blog_id=blog_id)
        elif blog_slug:
            qs = qs.filter(blog__slug=blog_slug)

        result = []
        for c in qs:
            b = getattr(c, "blog", None)
            result.append({
                "id": str(c.comment_id),
                "name": c.name,
                "date": c.created_at.isoformat() if c.created_at else "",
                "message": c.comment,
                "website": c.website or "",
                "blog_id": getattr(b, "blog_id", None),
                "blog_slug": getattr(b, "slug", None),
            })
        return Response(result, status=status.HTTP_200_OK)


class SaveCommentsAPIView(APIView):
    """
    POST /api/save-comments
    Accepts (JSON or form):
      { name, email, website?, message, blog_id?, blog_slug? }

    Returns saved comment as:
      { id, name, date, message, website, blog_id, blog_slug }
    """
    permission_classes = [FrontendOnlyPermission]

    @transaction.atomic
    def post(self, request):
        # Accept JSON or multipart/form
        if request.content_type and "application/json" in (request.content_type or ""):
            try:
                data = request.data if isinstance(request.data, dict) \
                    else json.loads(request.body.decode("utf-8") or "{}")
            except Exception:
                data = {}
        else:
            data = request.POST

        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip()
        website = (data.get("website") or "").strip()
        # Frontend may send message or comment
        message = (data.get("message") or data.get("comment") or "").strip()

        blog_id = (data.get("blog_id") or "").strip()
        blog_slug = (data.get("blog_slug") or "").strip()

        if not name or not email or not message:
            return Response(
                {"error": "name, email and message are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve blog linkage (optional)
        blog_obj = None
        if blog_id:
            try:
                blog_obj = BlogPost.objects.get(blog_id=blog_id)
            except BlogPost.DoesNotExist:
                return Response({"error": "Blog not found"}, status=status.HTTP_404_NOT_FOUND)
        elif blog_slug:
            try:
                blog_obj = BlogPost.objects.get(slug=blog_slug)
            except BlogPost.DoesNotExist:
                return Response({"error": "Blog not found"}, status=status.HTTP_404_NOT_FOUND)

        # Create comment
        c = BlogComment.objects.create(
            blog=blog_obj if blog_obj else None,  # FK allows null? If not, this is guarded above.
            name=name,
            email=email,
            website=website,
            comment=message,
        )

        # Build response
        b = getattr(c, "blog", None)
        resp = {
            "id": str(c.comment_id),
            "name": c.name,
            "date": c.created_at.isoformat() if c.created_at else "",
            "message": c.comment,
            "website": c.website or "",
            "blog_id": getattr(b, "blog_id", None),
            "blog_slug": getattr(b, "slug", None),
        }
        return Response(resp, status=status.HTTP_201_CREATED)

class ShowSpecificBlogAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        blog_id = (request.query_params.get("blog_id") or "").strip()
        slug = (request.query_params.get("slug") or "").strip()
        include_all = str(request.query_params.get("all", "")).lower() in ("1", "true", "yes")

        if not blog_id and not slug:
            return Response({"error": "Provide blog_id or slug"}, status=status.HTTP_400_BAD_REQUEST)

        qs = BlogPost.objects.all()
        if blog_id:
            qs = qs.filter(blog_id=blog_id)
        else:
            qs = qs.filter(slug=slug)

        if not include_all:
            now = timezone.now()
            qs = qs.filter(draft=False).filter(Q(publish_date__isnull=True) | Q(publish_date__lte=now))

        blog = qs.first()
        if not blog:
            return Response({"error": "Blog not found"}, status=status.HTTP_404_NOT_FOUND)

        images_payload = []
        rels = list(blog.images.select_related("image").order_by("order", "-is_primary", "pk"))
        for rel in rels:
            img = rel.image
            if not img:
                continue
            data_uri = _image_to_data_uri(img)
            images_payload.append({
                "id": getattr(img, "image_id", None),
                "url": data_uri,
                "alt": getattr(img, "alt_text", "") or "",
                "is_primary": bool(rel.is_primary),
                "order": rel.order if rel.order is not None else 0,
            })

        thumb = get_primary_thumbnail_url(blog)

        effective_status = blog.compute_status()
        if effective_status != (blog.status or ""):
            BlogPost.objects.filter(pk=blog.pk).update(status=effective_status)

        resp = {
            "id": blog.blog_id,
            "blog_id": blog.blog_id,
            "title": blog.title,
            "slug": blog.slug,
            "content": blog.content_html or "",
            "tags": blog.tags or "",
            "author": blog.author or "",
            "metaTitle": blog.meta_title or "",
            "metaDescription": blog.meta_description or "",
            "ogTitle": blog.og_title or "",
            "ogImage": blog.og_image_url or "",
            "schemaEnabled": bool(blog.schema_enabled),
            "publishDate": blog.publish_date.isoformat() if blog.publish_date else None,
            "draft": bool(blog.draft),
            "status": effective_status,
            "created_at": blog.created_at.isoformat() if blog.created_at else None,
            "updated_at": blog.updated_at.isoformat() if blog.updated_at else None,
            "thumbnail": thumb or None,
            "images": images_payload,
        }
        return Response(resp, status=status.HTTP_200_OK)
