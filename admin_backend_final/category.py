# Standard Library
import json
import traceback


# Django
from django.utils import timezone
from django.db import transaction

# Django REST Framework
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .utilities import generate_category_id, generate_subcategory_id, save_image
# Local Imports
from .models import *  # Consider specifying models instead of wildcard import
from .permissions import FrontendOnlyPermission

class SaveCategoryAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        # Keep existing behavior: support both JSON and multipart
        if request.content_type and 'application/json' in request.content_type:
            try:
                data = request.data if isinstance(request.data, dict) else json.loads(request.body.decode('utf-8') or '{}')
            except Exception:
                data = {}
            files = {}
        else:
            data = request.POST
            files = request.FILES

        name = (data.get('name') or '').strip()
        if not name:
            return Response({'error': 'Name is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Keep original "replace existing with same name"
        if Category.objects.filter(name=name).exists():
            Category.objects.get(name=name).delete()

        category_id = generate_category_id(name)
        now = timezone.now()
        order = Category.objects.count() + 1

        # NEW: optional caption/description
        caption = (data.get('caption') or '').strip() or None
        description = (data.get('description') or '').strip() or None

        category = Category.objects.create(
            category_id=category_id,
            name=name,
            status='visible',
            caption=caption,
            description=description,
            created_by='SuperAdmin',
            created_at=now,
            updated_at=now,
            order=order
        )

        # Normalize alt text & tags
        alt_text = (
            (data.get('alt_text') or data.get('alText') or f"{name} category image")
        ).strip()
        tags = (data.get('tags') or data.get('imageTags') or '')

        # Image can be a file OR a base64 data URL string
        image_data = files.get('image') or data.get('image')

        if image_data:
            img = save_image(
                file_or_base64=image_data,
                alt_text=alt_text,
                tags=tags,
                linked_table="category",
                linked_page="CategorySubCategoryPage",
                linked_id=category_id
            )
            if img:
                CategoryImage.objects.create(category=category, image=img)

        return Response({
            'success': True,
            'category_id': category_id,
            'caption': caption,
            'description': description
        }, status=status.HTTP_201_CREATED)


class ShowCategoryAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        categories = Category.objects.all().order_by('order')
        result = []

        for cat in categories:
            # Subcategories mapped to this category
            subcat_maps = CategorySubCategoryMap.objects.filter(category=cat).select_related('subcategory')
            subcats = [m.subcategory for m in subcat_maps]
            subcat_names = [s.name for s in subcats]

            # Product count across those subcategories
            product_count = ProductSubCategoryMap.objects.filter(subcategory__in=subcats).count()

            # First image (if any) + its alt text
            rel = cat.images.select_related('image').first()
            img = rel.image if rel else None
            img_url = img.url if img else None
            alt_text = img.alt_text if img else ""

            result.append({
                "id": cat.category_id,
                "name": cat.name,
                "image": img_url,
                "imageAlt": alt_text,
                "subcategories": {
                    "names": subcat_names or None,
                    "count": len(subcat_names) or 0
                },
                "products": product_count or 0,
                "status": cat.status,
                "order": cat.order,
                "caption": cat.caption,
                "description": cat.description,
            })

        return Response(result, status=status.HTTP_200_OK)


class EditCategoryAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    @transaction.atomic
    def post(self, request):
        data = request.POST
        category_id = data.get('category_id')
        try:
            category = Category.objects.select_related().get(category_id=category_id)
        except Category.DoesNotExist:
            return Response({'error': 'Category not found'}, status=status.HTTP_404_NOT_FOUND)

        # -------- Basic fields --------
        new_name = (data.get('name') or '').strip()
        if new_name:
            category.name = new_name

        if 'caption' in data:
            category.caption = (data.get('caption') or '').strip() or None
        if 'description' in data:
            category.description = (data.get('description') or '').strip() or None

        category.updated_at = timezone.now()
        category.save(update_fields=['name','caption','description','updated_at'])

        # -------- Image handling --------
        alt_text = (
            data.get('alt_text') or
            data.get('imageAlt') or
            data.get('altText') or
            ''
        ).strip()

        image_data = request.FILES.get('image') or request.POST.get('image')

        if image_data:
            # HARD REPLACE: remove old bindings & delete orphaned images/files
            old_rels = CategoryImage.objects.filter(category=category).select_related('image')
            old_images = [rel.image for rel in old_rels if rel.image_id]
            # delete relations first
            CategoryImage.objects.filter(category=category).delete()

            # delete image files/records if no other relation uses them
            for img in old_images:
                if img and not CategoryImage.objects.filter(image=img).exists():
                    if getattr(img, 'image_file', None):
                        img.image_file.delete(save=False)  # delete file from storage
                    img.delete()

            # Save new image and bind
            image = save_image(
                image_data,
                alt_text or "Alt-text",
                data.get("tags", ""),
                "category",
                "CategorySubCategoryPage",
                category_id
            )
            if image:
                CategoryImage.objects.create(category=category, image=image)
        else:
            # No new image => just alt_text update on existing FIRST image
            if alt_text:
                rel = category.images.select_related('image').first()
                if rel and rel.image:
                    rel.image.alt_text = alt_text
                    rel.image.save(update_fields=['alt_text'])

        return Response({'success': True, 'message': 'Category updated'}, status=status.HTTP_200_OK)
    
class DeleteCategoryAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = json.loads(request.body)
        except Exception:
            data = {}
        category_ids = data.get('ids', [])
        confirm = data.get('confirm', False)

        if not category_ids:
            return Response({'error': 'No category IDs provided'}, status=status.HTTP_400_BAD_REQUEST)

        for category_id in category_ids:
            try:
                category = Category.objects.get(category_id=category_id)
                related_mappings = CategorySubCategoryMap.objects.filter(category=category)

                if not confirm and related_mappings.exists():
                    return Response({
                        'confirm': True,
                        'message': 'Deleting this category will delete its subcategories and related products. Continue?'
                    }, status=status.HTTP_200_OK)

                for mapping in related_mappings:
                    subcat = mapping.subcategory
                    other_links = CategorySubCategoryMap.objects.filter(subcategory=subcat).exclude(category=category)
                    if not other_links.exists():
                        subcat.delete()
                    mapping.delete()

                category.delete()
            except Category.DoesNotExist:
                continue

        return Response({'success': True, 'message': 'Selected categories deleted'}, status=status.HTTP_200_OK)


class UpdateCategoryOrderAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = json.loads(request.body)
            ordered = data.get("ordered_categories", [])
            for item in ordered:
                Category.objects.filter(category_id=item["id"]).update(order=item["order"])
            return Response({'success': True}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SaveSubCategoryAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        data = request.POST
        name = (data.get('name') or '').strip()
        category_ids = data.getlist('category_ids')

        if not name or not category_ids:
            return Response({'error': 'Name and category_ids are required'}, status=status.HTTP_400_BAD_REQUEST)

        categories = Category.objects.filter(category_id__in=category_ids)
        if not categories.exists():
            return Response({'error': 'One or more category IDs not found'}, status=status.HTTP_400_BAD_REQUEST)

        # Duplicate subcategory name in same category
        existing_matches = CategorySubCategoryMap.objects.filter(
            category__in=categories,
            subcategory__name__iexact=name
        ).exists()
        if existing_matches:
            return Response({'error': f"Subcategory '{name}' already exists in one or more selected categories."},
                            status=status.HTTP_400_BAD_REQUEST)

        subcategory_id = generate_subcategory_id(name, category_ids)
        now = timezone.now()

        caption = (data.get('caption') or '').strip() or None
        description = (data.get('description') or '').strip() or None

        subcategory = SubCategory.objects.create(
            subcategory_id=subcategory_id,
            name=name,
            status='visible',
            created_by='SuperAdmin',
            created_at=now,
            updated_at=now,
            caption=caption,
            description=description,
            order=SubCategory.objects.count() + 1
        )

        for category in categories:
            CategorySubCategoryMap.objects.create(category=category, subcategory=subcategory)

        # Normalize alt text
        alt_text = (
            data.get('alt_text') or
            data.get('imageAlt') or
            data.get('altText') or
            ''
        ).strip()

        image_data = request.FILES.get('image') or request.POST.get('image')
        if image_data:
            image = save_image(
                image_data,
                alt_text or "Alt-text",
                data.get("tags", ""),
                "subcategory",
                "CategorySubCategoryPage",
                subcategory_id
            )
            if image:
                SubCategoryImage.objects.create(subcategory=subcategory, image=image)

        return Response({
            'success': True,
            'subcategory_id': subcategory_id,
            'caption': caption,
            'description': description
        }, status=status.HTTP_201_CREATED)

class ShowSubCategoryAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        subcategories = SubCategory.objects.all().order_by("order")
        result = []
        for sub in subcategories:
            maps = CategorySubCategoryMap.objects.filter(subcategory=sub).select_related('category')
            category_names = [m.category.name for m in maps]
            category_ids = [m.category.category_id for m in maps]  # NEW
            product_count = ProductSubCategoryMap.objects.filter(subcategory=sub).count()

            img_rel = sub.images.select_related('image').first()
            img = img_rel.image if img_rel else None

            result.append({
                "id": sub.subcategory_id,
                "name": sub.name,
                "image": img.url if img else None,
                "imageAlt": img.alt_text if img else "",
                "categories": category_names or None,
                "category_ids": category_ids,  # NEW
                "products": product_count or 0,
                "status": sub.status,
                "caption": sub.caption,
                "description": sub.description,
                "order": sub.order,
            })
        return Response(result, status=status.HTTP_200_OK)


class EditSubCategoryAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    @transaction.atomic
    def post(self, request):
        data = request.POST
        subcategory_id = data.get('subcategory_id')
        try:
            subcategory = SubCategory.objects.select_related().get(subcategory_id=subcategory_id)
        except SubCategory.DoesNotExist:
            return Response({'error': 'SubCategory not found'}, status=status.HTTP_404_NOT_FOUND)

        # -------- Basic fields --------
        new_name = (data.get('name') or '').strip()
        if new_name:
            subcategory.name = new_name

        if 'caption' in data:
            subcategory.caption = (data.get('caption') or '').strip() or None
        if 'description' in data:
            subcategory.description = (data.get('description') or '').strip() or None

        # -------- Category mappings (THIS WAS MISSING) --------
        # Frontend sends multiple category_ids fields
        incoming_category_ids = data.getlist('category_ids')
        if incoming_category_ids:
            # Normalize & validate targets
            new_cat_ids = set([cid.strip() for cid in incoming_category_ids if cid and cid.strip()])
            if not new_cat_ids:
                return Response({'error': 'At least one category is required'}, status=status.HTTP_400_BAD_REQUEST)

            categories_qs = Category.objects.filter(category_id__in=new_cat_ids)
            if categories_qs.count() != len(new_cat_ids):
                return Response({'error': 'One or more category IDs not found'}, status=status.HTTP_400_BAD_REQUEST)

            # Prevent duplicate name in same category (excluding self)
            effective_name = new_name or subcategory.name
            dup_exists = CategorySubCategoryMap.objects.filter(
                category__in=categories_qs,
                subcategory__name__iexact=effective_name
            ).exclude(subcategory=subcategory).exists()
            if dup_exists:
                return Response(
                    {'error': f"Subcategory '{effective_name}' already exists in one or more selected categories."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Reconcile mappings
            existing_maps = CategorySubCategoryMap.objects.filter(subcategory=subcategory)
            existing_ids = set(existing_maps.values_list('category__category_id', flat=True))

            to_add = new_cat_ids - existing_ids
            to_remove = existing_ids - new_cat_ids

            if to_remove:
                CategorySubCategoryMap.objects.filter(
                    subcategory=subcategory,
                    category__category_id__in=to_remove
                ).delete()

            if to_add:
                cats_to_add = {c.category_id: c for c in categories_qs if c.category_id in to_add}
                CategorySubCategoryMap.objects.bulk_create([
                    CategorySubCategoryMap(category=cats_to_add[cid], subcategory=subcategory)
                    for cid in to_add
                ])

        # Persist basic fields
        subcategory.updated_at = timezone.now()
        subcategory.save(update_fields=['name','caption','description','updated_at'])

        # -------- Image handling (unchanged) --------
        alt_text = (
            data.get('alt_text') or
            data.get('imageAlt') or
            data.get('altText') or
            ''
        ).strip()

        image_data = request.FILES.get('image') or request.POST.get('image')

        if image_data:
            old_rels = SubCategoryImage.objects.filter(subcategory=subcategory).select_related('image')
            old_images = [rel.image for rel in old_rels if rel.image_id]
            SubCategoryImage.objects.filter(subcategory=subcategory).delete()

            for img in old_images:
                if img and not SubCategoryImage.objects.filter(image=img).exists():
                    if getattr(img, 'image_file', None):
                        img.image_file.delete(save=False)
                    img.delete()

            image = save_image(
                image_data,
                alt_text or "Alt-text",
                data.get("tags", ""),
                "subcategory",
                "CategorySubCategoryPage",
                subcategory_id
            )
            if image:
                SubCategoryImage.objects.create(subcategory=subcategory, image=image)
        else:
            if alt_text:
                rel = subcategory.images.select_related('image').first()
                if rel and rel.image:
                    rel.image.alt_text = alt_text
                    rel.image.save(update_fields=['alt_text'])

        return Response({'success': True, 'message': 'SubCategory updated'}, status=status.HTTP_200_OK)


class DeleteSubCategoryAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = json.loads(request.body)
        except Exception:
            data = {}
        subcategory_ids = data.get('ids', [])
        confirm = data.get('confirm', False)

        if not subcategory_ids:
            return Response({'error': 'No subcategory IDs provided'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            for sub_id in set(subcategory_ids):  # de-duplicate IDs
                print(f"Processing subcategory_id: {sub_id}")

                try:
                    subcat = SubCategory.objects.get(subcategory_id=sub_id)
                except SubCategory.DoesNotExist:
                    print(f"Subcategory {sub_id} not found.")
                    continue

                related_products = ProductSubCategoryMap.objects.filter(subcategory=subcat)

                if not confirm and related_products.exists():
                    return Response({
                        'confirm': True,
                        'message': f'Deleting subcategory "{sub_id}" will delete all its related products. Continue?'
                    }, status=status.HTTP_200_OK)

                for mapping in related_products:
                    try:
                        other_links = ProductSubCategoryMap.objects.filter(product=mapping.product).exclude(subcategory=subcat)
                        if not other_links.exists():
                            mapping.product.delete()
                        mapping.delete()
                    except Exception as e:
                        print(f"Error while deleting mapping or product: {e}")

                # Delete image relationships
                try:
                    SubCategoryImage.objects.filter(subcategory=subcat).delete()
                except Exception as e:
                    print(f"Error deleting SubCategoryImage for {sub_id}: {e}")

                # Remove category-subcategory mapping
                CategorySubCategoryMap.objects.filter(subcategory=subcat).delete()

                # Finally, delete subcategory
                subcat.delete()

            return Response({'success': True, 'message': 'Selected subcategories deleted successfully'}, status=status.HTTP_200_OK)

        except Exception as e:
            traceback.print_exc()
            return Response({'error': f'Unexpected error: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UpdateSubCategoryOrderAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = json.loads(request.body)
            ordered = data.get("ordered_subcategories", [])
            for item in ordered:
                SubCategory.objects.filter(subcategory_id=item["id"]).update(order=item["order"])
            return Response({'success': True}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({'success': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UpdateHiddenStatusAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = json.loads(request.body)
            item_type = data.get('type')
            ids = data.get('ids', [])
            new_status = data.get('status', 'visible')

            if not ids or not isinstance(ids, list):
                return Response({'error': 'No valid IDs provided'}, status=status.HTTP_400_BAD_REQUEST)

            if item_type == 'categories':
                Category.objects.filter(category_id__in=ids).update(status=new_status)
            elif item_type == 'subcategories':
                SubCategory.objects.filter(subcategory_id__in=ids).update(status=new_status)
            else:
                return Response({'error': 'Invalid type'}, status=status.HTTP_400_BAD_REQUEST)

            return Response({'success': True, 'message': f"{item_type.title()} status updated to {new_status}"}, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        return Response({'error': 'Invalid request method'}, status=status.HTTP_405_METHOD_NOT_ALLOWED)