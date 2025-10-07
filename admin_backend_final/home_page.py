
# Standard Library
import json
import uuid
import traceback
from urllib.parse import urlparse, urlunparse
# Django REST Framework
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .utilities import save_image
# Local Imports
from .models import *  # Consider specifying models instead of wildcard import
from .permissions import FrontendOnlyPermission

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.db import transaction
import json, traceback

class FirstCarouselAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        try:
            carousel = FirstCarousel.objects.last()
            if not carousel:
                return Response({
                    'title': 'Default First Carousel Title',
                    'description': 'Default First Carousel Description',
                    'images': []
                }, status=status.HTTP_200_OK)

            images = (
                carousel.images
                .order_by("order")
                .select_related("image", "subcategory")
                .all()
            )

            image_data = []
            for img in images:
                subcategory_obj = None
                if img.subcategory:
                    subcategory_obj = {
                        'id': img.subcategory.pk,                          # CharField PK (subcategory_id)
                        'name': getattr(img.subcategory, 'name', ''),
                        'slug': getattr(img.subcategory, 'slug', ''),      # present if you add slug later
                    }

                image_data.append({
                    'src': img.image.image_file.url if img.image and img.image.image_file else '',
                    'title': img.title,
                    'subcategory': subcategory_obj,
                })

            return Response({
                'title': carousel.title,
                'description': carousel.description,
                'images': image_data,
            }, status=status.HTTP_200_OK)

        except Exception as e:
            print("❌ GET Error:", traceback.format_exc())
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @transaction.atomic
    def post(self, request):
        try:
            data = json.loads(request.body or "{}")
            title = data.get('title', '')
            description = data.get('description', '')
            raw_images = data.get('images', [])

            # Single-instance reset (unchanged)
            FirstCarousel.objects.all().delete()

            carousel = FirstCarousel.objects.create(
                title=title,
                description=description
            )

            for i, img_data in enumerate(raw_images):
                if not isinstance(img_data, dict):
                    continue

                img_src = img_data.get('src')
                img_title = img_data.get('title') or f'Product {i + 1}'

                # Prefer subcategory_id; accept legacy category_id if client hasn't updated yet
                subcategory_key = img_data.get('subcategory_id') or img_data.get('category_id')
                subcategory = None
                if subcategory_key:
                    subcategory = SubCategory.objects.filter(pk=subcategory_key).first()

                # Reuse existing /uploads/ optimization
                if isinstance(img_src, str) and img_src.startswith('/uploads/'):
                    existing_image = Image.objects.filter(
                        image_file=img_src.replace('/uploads/', 'uploads/')
                    ).first()
                    if existing_image:
                        FirstCarouselImage.objects.create(
                            carousel=carousel,
                            image=existing_image,
                            title=img_title,
                            subcategory=subcategory,
                            order=i
                        )
                    continue

                saved_image = save_image(
                    file_or_base64=img_src,
                    alt_text="Carousel Image",
                    tags="carousel",
                    linked_table="FirstCarousel",
                    linked_id=str(carousel.id),
                    linked_page="first-carousel"
                )
                if saved_image:
                    FirstCarouselImage.objects.create(
                        carousel=carousel,
                        image=saved_image,
                        title=img_title,
                        subcategory=subcategory,
                        order=i
                    )

            return Response({'message': '✅ First Carousel data saved successfully'}, status=status.HTTP_200_OK)

        except Exception as e:
            print("❌ POST Error:", traceback.format_exc())
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SecondCarouselAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        try:
            carousel = SecondCarousel.objects.last()
            if not carousel:
                return Response({
                    'title': 'Default Second Carousel Title',
                    'description': 'Default Second Carousel Description',
                    'images': []
                }, status=status.HTTP_200_OK)

            images = (
                carousel.images
                .order_by("order")
                .select_related("image", "subcategory")
                .all()
            )

            image_data = []
            for img in images:
                subcategory_obj = None
                if img.subcategory:
                    subcategory_obj = {
                        'id': img.subcategory.pk,
                        'name': getattr(img.subcategory, 'name', ''),
                        'slug': getattr(img.subcategory, 'slug', ''),
                    }

                image_data.append({
                    'src': img.image.image_file.url if img.image and img.image.image_file else '',
                    'title': img.title,
                    'subcategory': subcategory_obj,
                })

            return Response({
                'title': carousel.title,
                'description': carousel.description,
                'images': image_data,
            }, status=status.HTTP_200_OK)

        except Exception as e:
            print("❌ GET Error:", traceback.format_exc())
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @transaction.atomic
    def post(self, request):
        try:
            data = json.loads(request.body or "{}")
            title = data.get('title', '')
            description = data.get('description', '')
            raw_images = data.get('images', [])

            # Single-instance reset (unchanged)
            SecondCarousel.objects.all().delete()

            carousel = SecondCarousel.objects.create(
                title=title,
                description=description
            )

            for i, img_data in enumerate(raw_images):
                if not isinstance(img_data, dict):
                    continue

                img_src = img_data.get('src')
                img_title = img_data.get('title') or f'Product {i + 1}'

                # Prefer subcategory_id; accept legacy category_id
                subcategory_key = img_data.get('subcategory_id') or img_data.get('category_id')
                subcategory = None
                if subcategory_key:
                    subcategory = SubCategory.objects.filter(pk=subcategory_key).first()

                # Reuse existing /uploads/ optimization
                if isinstance(img_src, str) and img_src.startswith('/uploads/'):
                    existing_image = Image.objects.filter(
                        image_file=img_src.replace('/uploads/', 'uploads/')
                    ).first()
                    if existing_image:
                        SecondCarouselImage.objects.create(
                            carousel=carousel,
                            image=existing_image,
                            title=img_title,
                            subcategory=subcategory,
                            order=i
                        )
                    continue

                saved_image = save_image(
                    file_or_base64=img_src,
                    alt_text="Carousel Image",
                    tags="carousel",
                    linked_table="SecondCarousel",
                    linked_id=str(carousel.id),
                    linked_page="second-carousel"
                )
                if saved_image:
                    SecondCarouselImage.objects.create(
                        carousel=carousel,
                        image=saved_image,
                        title=img_title,
                        subcategory=subcategory,
                        order=i
                    )

            return Response({'message': '✅ Second Carousel data saved successfully'}, status=status.HTTP_200_OK)

        except Exception as e:
            print("❌ POST Error:", traceback.format_exc())
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
def absolutize_media_url(request, path_or_url: str) -> str:
    """
    Normalize media URLs:
    - If host is localhost/127.0.0.1 -> force http (dev server has no TLS)
    - Otherwise keep https (or whatever request.scheme is in prod)
    - Works with both relative and absolute inputs
    """
    host = request.get_host()
    is_local = host.startswith("127.0.0.1") or host.startswith("localhost") or host.startswith("[::1]")

    # Already absolute?
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        p = urlparse(path_or_url)
        if p.hostname in ("127.0.0.1", "localhost", "::1"):
            p = p._replace(scheme="http")
            return urlunparse(p)
        return path_or_url

    # Relative path → make it absolute
    scheme = "http" if is_local else request.scheme
    path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
    return f"{scheme}://{host}{path}"

class HeroBannerAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    def get(self, request):
        try:
            hero = HeroBanner.objects.last()
            if not hero:
                return Response({
                    "images": [
                        {
                            "url": absolutize_media_url(request, "/media/uploads/desktop_default.jpg"),
                            "device_type": "desktop",
                        },
                        {
                            "url": absolutize_media_url(request, "/media/uploads/mobile_default.jpg"),
                            "device_type": "mobile",
                        },
                    ]
                }, status=status.HTTP_200_OK)

            images = hero.images.order_by("order").all()
            image_urls = []

            for hi in images:
                raw_url = getattr(hi.image.image_file, "url", "")
                if raw_url and not raw_url.startswith("/"):
                    if raw_url.startswith("uploads/"):
                        raw_url = f"/media/{raw_url}"
                    elif raw_url.startswith("media/"):
                        raw_url = f"/{raw_url}"

                image_urls.append({
                    "url": absolutize_media_url(request, raw_url),
                    "device_type": hi.device_type,
                })

            return Response({"images": image_urls}, status=status.HTTP_200_OK)

        except Exception as e:
            print("❌ HeroBanner GET Error:", traceback.format_exc())
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        try:
            data = json.loads(request.body)
            raw_images = data.get('images', [])

            if not raw_images or len(raw_images) < 2:
                return Response({'error': 'At least two images required (1 desktop & 1 mobile)'}, status=status.HTTP_400_BAD_REQUEST)

            desktop_imgs = []
            mobile_imgs = []

            # detect if device_type provided
            has_device_labels = any(isinstance(img, dict) and 'device_type' in img for img in raw_images)

            if has_device_labels:
                for img in raw_images:
                    if isinstance(img, dict):
                        device_type = img.get('device_type', '').lower()
                        url = img.get('url', '')
                        if device_type == 'desktop':
                            desktop_imgs.append(url)
                        elif device_type == 'mobile':
                            mobile_imgs.append(url)
            else:
                midpoint = len(raw_images) // 2
                desktop_imgs = [img['url'] if isinstance(img, dict) else img for img in raw_images[:midpoint]]
                mobile_imgs = [img['url'] if isinstance(img, dict) else img for img in raw_images[midpoint:]]

            if not desktop_imgs or not mobile_imgs:
                return Response({'error': 'Must include at least one desktop and one mobile image'}, status=status.HTTP_400_BAD_REQUEST)

            # clear previous
            HeroBanner.objects.all().delete()

            banner = HeroBanner.objects.create(
                hero_id=f"HERO-{uuid.uuid4().hex[:8]}",
                alt_text="Homepage Hero Banner"
            )

            def process_images(image_list, device_type, order_start):
                order = order_start
                for img_url in image_list:
                    if isinstance(img_url, str) and img_url.startswith('/uploads/'):
                        existing = Image.objects.filter(image_file=img_url.replace('/uploads/', 'uploads/')).first()
                        if existing:
                            HeroBannerImage.objects.create(
                                banner=banner,
                                image=existing,
                                device_type=device_type,
                                order=order
                            )
                            order += 1
                            continue

                    saved_image = save_image(
                        file_or_base64=img_url,
                        alt_text=f"Hero {device_type.title()} Image",
                        tags=f"hero,{device_type}",
                        linked_table="HeroBanner",
                        linked_id=str(banner.hero_id),
                        linked_page="hero-banner"
                    )
                    if saved_image:
                        HeroBannerImage.objects.create(
                            banner=banner,
                            image=saved_image,
                            device_type=device_type,
                            order=order
                        )
                        order += 1
                return order

            order = 0
            order = process_images(desktop_imgs, 'desktop', order)
            order = process_images(mobile_imgs, 'mobile', order)

            return Response({'message': '✅ Hero Banner images saved successfully'}, status=status.HTTP_200_OK)

        except Exception as e:
            print("❌ HeroBanner POST Error:", traceback.format_exc())
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        