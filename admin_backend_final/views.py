
# Standard Library
import json
import re
import traceback

# Django
from django.utils import timezone
from django.core.exceptions import ObjectDoesNotExist
from django.utils.text import slugify
from django.db import IntegrityError, transaction

# Django REST Framework
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.core.exceptions import ObjectDoesNotExist

from .utilities import format_image_object, generate_admin_id
# Local Imports
from .models import *  # Consider specifying models instead of wildcard import
from .serializers import NotificationSerializer
from .permissions import FrontendOnlyPermission

class ShowNavItemsAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        data = []

        categories = Category.objects.filter(status="visible").order_by("order")

        for cat in categories:
            cat_image_objs = CategoryImage.objects.filter(category=cat).select_related('image')
            cat_image_urls = [
                img for img in (format_image_object(obj, request=request) for obj in cat_image_objs) if img
            ]

            subcat_maps = (
                CategorySubCategoryMap.objects
                .filter(category=cat, subcategory__status="visible")
                .select_related('subcategory')
                .order_by('subcategory__order')
            )

            subcategories = []
            for map_entry in subcat_maps:
                sub = map_entry.subcategory

                sub_image_objs = SubCategoryImage.objects.filter(subcategory=sub).select_related('image')
                sub_image_urls = [
                    img for img in (format_image_object(obj, request=request) for obj in sub_image_objs) if img
                ]

                prod_maps = ProductSubCategoryMap.objects.filter(subcategory=sub).select_related('product')
                products = []
                for prod_map in prod_maps:
                    prod = prod_map.product

                    prod_image_objs = (
                        ProductImage.objects
                        .filter(product=prod)
                        .select_related('image')
                        .order_by('-is_primary', 'id')  # primary first, then stable order
                    )
                    prod_image_urls = [
                        img for img in (format_image_object(obj, request=request) for obj in prod_image_objs) if img
                    ]

                    products.append({
                        "id": prod.product_id,
                        "name": prod.title,
                        "images": prod_image_urls,  # [{url, alt_text}, ...] -> FIRST is thumbnail if present
                        "url": slugify(prod.title),
                    })

                subcategories.append({
                    "id": sub.subcategory_id,
                    "name": sub.name,
                    "images": sub_image_urls,  # [{url, alt_text}, ...]
                    "url": slugify(sub.name),
                    "products": products,
                })

            data.append({
                "id": cat.category_id,
                "name": cat.name,
                "images": cat_image_urls,  # [{url, alt_text}, ...]
                "url": slugify(cat.name),
                "subcategories": subcategories,
            })

        return Response(data, status=status.HTTP_200_OK)

EMIRATES_ID_RE = re.compile(r"^784-\d{4}-\d{7}-\d$")

def _clean_str(v, default=""):
    return (v or default).strip()

class SaveUserAPIView(APIView):
    """
    Idempotent create/upsert WITHOUT password.
    Frontend should call this on first load/sign-in to ensure row exists.
    """
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = json.loads(request.body or "{}")

            user_id = _clean_str(data.get("user_id"))
            email = _clean_str(data.get("email")).lower()
            name = _clean_str(data.get("name"))
            username = _clean_str(data.get("username") or email or user_id)

            is_verified = bool(data.get("is_verified", False))
            emirates_id = _clean_str(data.get("emirates_id") or None, default=None)
            phone_number = _clean_str(data.get("phone_number"))
            address = _clean_str(data.get("address"))

            if not user_id or not email:
                return Response({"error": "Missing user_id or email"}, status=status.HTTP_400_BAD_REQUEST)

            # Validate optional Emirates ID format (if provided)
            if emirates_id and not EMIRATES_ID_RE.match(emirates_id):
                return Response({"error": "Invalid Emirates ID format. Use 784-YYYY-NNNNNNN-C"}, status=400)

            with transaction.atomic():
                user, created = User.objects.get_or_create(
                    user_id=user_id,
                    defaults={
                        "username": username,
                        "email": email,
                        "first_name": name,
                        "is_verified": is_verified,
                        "emirates_id": emirates_id,
                        "phone_number": phone_number,
                        "address": address,
                        # NOTE: password_hash intentionally ignored (Firebase only)
                    },
                )

                if not created:
                    # Minimal safe upsert: do not overwrite with blanks unless explicitly provided
                    user.username = username or user.username
                    user.email = email or user.email
                    if data.get("name") is not None:
                        user.first_name = name
                    if "is_verified" in data:
                        user.is_verified = is_verified
                    if "emirates_id" in data:
                        user.emirates_id = emirates_id
                    if "phone_number" in data:
                        user.phone_number = phone_number
                    if "address" in data:
                        user.address = address
                    user.updated_at = timezone.now()
                    user.save()

            return Response({"message": "User saved successfully", "created": created}, status=status.HTTP_201_CREATED)

        except IntegrityError as e:
            return Response({"error": "Integrity error", "detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ShowUserAPIView(APIView):
    """
    GET a flat list of users with the new fields.
    """
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        users = User.objects.all().values(
            "user_id",
            "username",
            "email",
            "first_name",
            "is_verified",
            "emirates_id",
            "phone_number",
            "address",
            "created_at",
            "updated_at",
        )
        return Response({"users": list(users)}, status=status.HTTP_200_OK)


class EditUserAPIView(APIView):
    """
    Partial update WITHOUT password.
    Security is enforced by FrontendOnlyPermission (header gate) and the client’s Firebase session.
    """
    permission_classes = [FrontendOnlyPermission]

    def patch(self, request):
        try:
            data = json.loads(request.body or "{}")
            user_id = _clean_str(data.get("user_id"))

            if not user_id:
                return Response({"error": "user_id is required"}, status=status.HTTP_400_BAD_REQUEST)

            try:
                user = User.objects.get(user_id=user_id)
            except User.DoesNotExist:
                return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)

            updates = {}

            # Supported fields (no password here)
            if "email" in data and _clean_str(data["email"]):
                user.email = _clean_str(data["email"]).lower()
                updates["email"] = True

            # Either 'username' or fallback legacy 'UserName'
            new_username = data.get("username") or data.get("UserName")
            if new_username:
                user.username = _clean_str(new_username)
                updates["username"] = True

            if "name" in data:
                user.first_name = _clean_str(data.get("name"))
                updates["first_name"] = True

            if "is_verified" in data:
                user.is_verified = bool(data.get("is_verified"))
                updates["is_verified"] = True

            if "emirates_id" in data:
                eid = data.get("emirates_id")
                if eid:
                    eid = _clean_str(eid)
                    if not EMIRATES_ID_RE.match(eid):
                        return Response({"error": "Invalid Emirates ID format. Use 784-YYYY-NNNNNNN-C"}, status=400)
                    user.emirates_id = eid
                else:
                    user.emirates_id = None
                updates["emirates_id"] = True

            if "phone_number" in data:
                user.phone_number = _clean_str(data.get("phone_number"))
                updates["phone_number"] = True

            if "address" in data:
                user.address = _clean_str(data.get("address"))
                updates["address"] = True

            user.updated_at = timezone.now()

            try:
                user.save()
            except IntegrityError as e:
                return Response({"error": "Integrity error", "detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

            return Response(
                {"message": "User updated successfully", "updated_fields": list(updates.keys())},
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
class ShowAdminAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        try:
            result = []
            all_admins = Admin.objects.all()

            for admin in all_admins:
                role_map = AdminRoleMap.objects.filter(admin=admin).first()
                if role_map:
                    role = role_map.role
                    result.append({
                        "admin_id": admin.admin_id,
                        "admin_name": admin.admin_name,
                        "password_hash": admin.password_hash,
                        "role_id": role.role_id,
                        "role_name": role.role_name,
                        "access_pages": role.access_pages,
                        "created_at": admin.created_at,
                    })

            return Response({"success": True, "admins": result}, status=status.HTTP_200_OK)
        except Exception as e:
            traceback.print_exc()
            return Response({"success": False, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class SaveAdminAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = request.data
            admin_name = data.get("admin_name")
            password = data.get("password")
            role_name = data.get("role_name")
            access_pages = data.get("access_pages", [])

            if not admin_name or not password or not role_name:
                return Response({"success": False, "error": "Missing required fields"}, status=status.HTTP_400_BAD_REQUEST)

            # store plaintext compat as before
            password_hash = password

            # create or get role
            role, created = AdminRole.objects.get_or_create(
                role_name=role_name,
                defaults={
                    "role_id": f"R-{role_name}",
                    "description": f"{role_name} role",
                    "access_pages": access_pages
                }
            )
            if not created and not role.access_pages:
                role.access_pages = access_pages
                role.save()

            # retry ID collision up to 5 times
            for attempt in range(1, 6):
                try:
                    admin_id = generate_admin_id(admin_name, role_name, attempt)
                    admin = Admin.objects.create(
                        admin_id=admin_id,
                        admin_name=admin_name,
                        password_hash=password_hash
                    )
                    break
                except IntegrityError:
                    if attempt == 5:
                        raise
                    continue

            AdminRoleMap.objects.create(admin=admin, role=role)

            return Response({"success": True, "admin_id": admin_id}, status=status.HTTP_200_OK)

        except Exception as e:
            traceback.print_exc()
            return Response({
                "success": False,
                "error": str(e),
                "hint": "Check for admin_id collisions or access_pages misconfig"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# ---- EDIT ADMIN ----
class EditAdminAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        """
        Expected JSON:
        {
          "admin_id": "A-123...",
          "admin_name": "newname",
          "role_name": "Admin",
          "access_pages": ["Products Section", ...],
          "password": "optional-new-password"   # optional
        }
        """
        try:
            data = request.data
            admin_id = data.get("admin_id")
            admin_name = (data.get("admin_name") or "").strip()
            role_name = (data.get("role_name") or "").strip()
            access_pages = data.get("access_pages", None)  # list or None
            new_password = (data.get("password") or "").strip()

            # Validate
            if not admin_id:
                return Response({"success": False, "error": "admin_id is required"},
                                status=status.HTTP_400_BAD_REQUEST)
            if not admin_name or not role_name:
                return Response({"success": False, "error": "admin_name and role_name are required"},
                                status=status.HTTP_400_BAD_REQUEST)

            # Fetch target admin
            try:
                admin = Admin.objects.get(admin_id=admin_id)
            except Admin.DoesNotExist:
                return Response({"success": False, "error": "Admin not found"},
                                status=status.HTTP_404_NOT_FOUND)

            # Enforce unique username (case-insensitive) excluding current admin
            exists_conflict = Admin.objects.exclude(admin_id=admin_id) \
                .filter(admin_name__iexact=admin_name).exists()
            if exists_conflict:
                return Response({"success": False, "error": "Username already exists"},
                                status=status.HTTP_409_CONFLICT)

            # Update username
            admin.admin_name = admin_name

            # Optional password update (maintaining plaintext compat per current system)
            if new_password:
                admin.password_hash = new_password

            admin.save()

            # Create or update role
            role, created = AdminRole.objects.get_or_create(
                role_name=role_name,
                defaults={
                    "role_id": f"R-{role_name}",
                    "description": f"{role_name} role",
                    "access_pages": access_pages or []
                }
            )

            # If role already exists and client sent access_pages, update it
            # (Note: role.access_pages is shared across all admins with this role)
            if not created and access_pages is not None:
                role.access_pages = access_pages
                role.save()

            # Update admin ↔ role mapping
            AdminRoleMap.objects.update_or_create(
                admin=admin,
                defaults={"role": role}
            )

            # Response payload aligned with ShowAdminAPIView shape (where possible)
            return Response({
                "success": True,
                "admin": {
                    "admin_id": admin.admin_id,
                    "admin_name": admin.admin_name,
                    "password_hash": admin.password_hash,
                    "role_id": role.role_id,
                    "role_name": role.role_name,
                    "access_pages": role.access_pages,
                    "created_at": admin.created_at,
                }
            }, status=status.HTTP_200_OK)

        except Exception as e:
            traceback.print_exc()
            return Response({"success": False, "error": str(e)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DeleteAdminAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            admin_id = request.data.get("admin_id")

            if not admin_id:
                return Response({"success": False, "error": "admin_id is required"}, status=status.HTTP_400_BAD_REQUEST)

            admin = Admin.objects.get(admin_id=admin_id)

            # Delete role mapping first
            AdminRoleMap.objects.filter(admin=admin).delete()
            # Then delete admin
            admin.delete()

            return Response({"success": True, "message": f"Admin '{admin_id}' deleted successfully"}, status=status.HTTP_200_OK)

        except Admin.DoesNotExist:
            return Response({"success": False, "error": "Admin not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            traceback.print_exc()
            return Response({"success": False, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class AdminLoginAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            admin_name = request.data.get('username')
            password = request.data.get('password')

            if not admin_name or not password:
                return Response({"success": False, "error": "Username and password are required"}, status=status.HTTP_400_BAD_REQUEST)

            admin = Admin.objects.get(admin_name=admin_name)

            # plaintext comparison to keep old behavior
            if admin.password_hash == password:
                role_map = AdminRoleMap.objects.get(admin=admin)
                role = role_map.role
                return Response({
                    "success": True,
                    "admin_id": admin.admin_id,
                    "access_pages": role.access_pages
                }, status=status.HTTP_200_OK)
            else:
                return Response({"success": False, "error": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        except Admin.DoesNotExist:
            return Response({"success": False, "error": "Admin not found"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            traceback.print_exc()
            return Response({"success": False, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
class ShowAllImagesAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def get(self, request):
        images = Image.objects.all().order_by('-created_at')
        data = []
        for image in images:
            data.append({
                'image_id': image.image_id,
                'url': image.url,
                'alt_text': image.alt_text,
                'width': image.width,
                'height': image.height,
                'linked_page': image.linked_page,
                'linked_id': image.linked_id,
                'linked_table': image.linked_table,
                'image_type': image.image_type,
                'tags': image.tags,
                'created_at': image.created_at,
            })
        # keep same shape (list)
        return Response(data, status=status.HTTP_200_OK)

class EditImageAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def put(self, request):
        try:
            data = json.loads(request.body)
            image_id = data.get('image_id')
            if not image_id:
                return Response({'error': 'Image ID is required'}, status=status.HTTP_400_BAD_REQUEST)

            image = Image.objects.get(image_id=image_id)

            image.alt_text = data.get('alt_text', image.alt_text)
            image.width = data.get('width', image.width)
            image.height = data.get('height', image.height)
            image.linked_page = data.get('linked_page', image.linked_page)
            image.linked_id = data.get('linked_id', image.linked_id)
            image.linked_table = data.get('linked_table', image.linked_table)
            image.image_type = data.get('image_type', image.image_type)
            image.tags = data.get('tags', image.tags)

            image.save()

            return Response({'message': 'Image updated successfully.'}, status=status.HTTP_200_OK)
        except Image.DoesNotExist:
            return Response({'error': 'Image not found'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class DeleteImageAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = json.loads(request.body)
            image_id = data.get('image_id')
            if not image_id:
                return Response({'error': 'Image ID is required'}, status=status.HTTP_400_BAD_REQUEST)

            image = Image.objects.get(image_id=image_id)
            image.delete()

            return Response({'message': 'Image deleted successfully'}, status=status.HTTP_200_OK)
        except Image.DoesNotExist:
            return Response({'error': 'Image not found'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([FrontendOnlyPermission])
def get_notifications(request):
    notifications = Notification.objects.order_by('-created_at')[:1000]
    serializer = NotificationSerializer(notifications, many=True)
    return Response(serializer.data, status=status.HTTP_200_OK)

@api_view(['POST'])
@permission_classes([FrontendOnlyPermission])
def update_notification_status(request):
    notification_id = request.data.get("notification_id")
    new_status = request.data.get("status")
    if not notification_id or new_status not in ["read", "unread"]:
        return Response({"error": "Invalid data"}, status=status.HTTP_400_BAD_REQUEST)
    try:
        notification = Notification.objects.get(notification_id=notification_id)
    except Notification.DoesNotExist:
        return Response({"error": "Notification not found"}, status=status.HTTP_404_NOT_FOUND)
    notification.status = new_status
    notification.save()
    return Response({"message": "Status updated"}, status=status.HTTP_200_OK)

@api_view(['POST'])
@permission_classes([FrontendOnlyPermission])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def update_image(request, image_id):
    try:
        image = Image.objects.get(image_id=image_id)
    except ObjectDoesNotExist:
        return Response({'error': 'Image not found'}, status=status.HTTP_404_NOT_FOUND)

    uploaded = request.FILES.get('image_file')
    if uploaded:
        image.image_file = uploaded

    alt_text = request.data.get('alt_text', None)
    if alt_text is not None:
        image.alt_text = alt_text

    tags_raw = request.data.get('tags', None)
    if tags_raw is not None:
        if isinstance(tags_raw, list):
            image.tags = tags_raw
        elif isinstance(tags_raw, str):
            try:
                image.tags = json.loads(tags_raw)
            except json.JSONDecodeError:
                cleaned = [t.strip() for t in tags_raw.split(',') if t.strip()]
                image.tags = cleaned

    image.save()

    return Response({
        'message': 'Image updated successfully',
        'url': image.url,
        'image_id': image.image_id,
        'alt_text': image.alt_text,
        'tags': image.tags,
    }, status=status.HTTP_200_OK)

