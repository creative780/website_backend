import uuid
from django.db import models 
from django.contrib.auth.models import AbstractUser 
from django.conf import settings # for AUTH_USER_MODEL-safe FKs 
from decimal import Decimal 
from django.utils import timezone 
from django.utils.text import slugify 
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.exceptions import ValidationError


class User(AbstractUser):
    user_id = models.CharField(primary_key=True, max_length=100)
    email = models.EmailField(unique=True, db_index=True)
    password_hash = models.CharField(max_length=255, blank=True, null=True)
    is_verified = models.BooleanField(default=False)  # email verified flag from Firebase
    username = models.CharField(max_length=150, unique=True, blank=False)
    emirates_id = models.CharField(max_length=50, unique=True, blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, default="")
    address = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.username or self.email or self.user_id
    
class Admin(models.Model):
    admin_id = models.CharField(primary_key=True, max_length=100)
    admin_name = models.CharField(max_length=100, db_index=True)
    password_hash = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.admin_name

class AdminRole(models.Model):
    role_id = models.CharField(primary_key=True, max_length=100)
    role_name = models.CharField(max_length=100, db_index=True)
    description = models.TextField()
    access_pages = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.role_name

class AdminRoleMap(models.Model):
    admin = models.ForeignKey(Admin, on_delete=models.CASCADE)
    role = models.ForeignKey(AdminRole, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["admin"]),
            models.Index(fields=["role"]),
        ]

class Image(models.Model):
    image_id = models.CharField(primary_key=True, max_length=100)
    image_file = models.ImageField(upload_to='uploads/', null=True, blank=True)
    alt_text = models.CharField(max_length=255, blank=True, default="")
    width = models.IntegerField()
    height = models.IntegerField()
    tags = models.JSONField(default=list)
    image_type = models.CharField(max_length=50, blank=True, default="")
    linked_table = models.CharField(max_length=100, blank=True, default="")
    linked_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    linked_page = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    
    @property
    def url(self):
        try:
            return self.image_file.url
        except ValueError:
            return None

    def __str__(self):
        return self.image_id

class Category(models.Model):
    category_id = models.CharField(primary_key=True, max_length=100)
    name = models.CharField(max_length=100, db_index=True)
    status = models.CharField(max_length=20, choices=[("hidden", "Hidden"), ("visible", "Visible")], db_index=True)
    caption = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

class CategoryImage(models.Model):
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="images")
    image = models.ForeignKey(Image, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

class SubCategory(models.Model):
    subcategory_id = models.CharField(primary_key=True, max_length=100)
    name = models.CharField(max_length=100, db_index=True)
    status = models.CharField(max_length=20, choices=[("hidden", "Hidden"), ("visible", "Visible")], db_index=True)
    created_by = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    caption = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name

class SubCategoryImage(models.Model):
    subcategory = models.ForeignKey(SubCategory, on_delete=models.CASCADE, related_name="images")
    image = models.ForeignKey(Image, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

class CategorySubCategoryMap(models.Model):
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    subcategory = models.ForeignKey(SubCategory, on_delete=models.CASCADE)

    class Meta:
        indexes = [
            models.Index(fields=["category"]),
            models.Index(fields=["subcategory"]),
        ]
        unique_together = ("category", "subcategory")

class AttributeSubCategory(models.Model):
    """
    Attribute/option system for Products & SubCategories.

    Each record represents a single attribute definition (e.g. "Size", "Color"),
    optionally scoped to one or more subcategories.
    """

    attribute_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    # Core info
    name = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255, unique=True, db_index=True)
    type = models.CharField(
        max_length=50,
        choices=[
            ("size", "Size"),
            ("color", "Color"),
            ("material", "Material"),
            ("custom", "Custom"),
        ],
        default="custom",
        db_index=True,
    )
    status = models.CharField(
        max_length=20,
        choices=[("visible", "Visible"), ("hidden", "Hidden")],
        default="visible",
        db_index=True,
    )

    # NEW: attribute-level description
    description = models.TextField(blank=True, default="")

    # Option list: each item is {id, name, price_delta?, is_default?, image_data?, description?}
    values = models.JSONField(default=list, blank=True)

    # Scope: empty list means global attribute
    subcategory_ids = models.JSONField(default=list, blank=True)

    # Audit
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    def clean(self):
        # Validate structure
        if not isinstance(self.values, list):
            raise ValueError("values must be a list of option objects")

        defaults = [v for v in self.values if v.get("is_default")]
        if len(defaults) > 1:
            raise ValueError("Only one option can be marked as default.")

        # Optional: light schema checks
        for v in self.values:
            if not isinstance(v, dict):
                raise ValueError("Each value must be an object.")
            if "name" in v and not isinstance(v["name"], str):
                raise ValueError("Option 'name' must be a string if provided.")
            if "description" in v and not isinstance(v["description"], str):
                raise ValueError("Option 'description' must be a string if provided.")

    @property
    def is_global(self):
        """True if this attribute is available to all subcategories."""
        return len(self.subcategory_ids or []) == 0

# === PRODUCT SYSTEM ===
class Product(models.Model):
    product_id = models.CharField(primary_key=True, max_length=100)
    title = models.CharField(max_length=511, db_index=True)
    description = models.TextField()
    long_description = models.TextField(blank=True, default="")
    brand = models.CharField(max_length=255, blank=True, default="")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    discounted_price = models.DecimalField(max_digits=10, decimal_places=2)
    tax_rate = models.FloatField()
    price_calculator = models.TextField()
    video_url = models.URLField(blank=True, null=True)
    status = models.CharField(max_length=50, db_index=True)
    created_by = models.CharField(max_length=100)
    created_by_type = models.CharField(
        max_length=10, choices=[('admin', 'Admin'), ('user', 'User')]
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    order = models.PositiveIntegerField(default=0)

    # New rating system
    rating = models.FloatField(
        default=0.0,
        validators=[MinValueValidator(0.0), MaxValueValidator(5.0)],
        help_text="Allowed values: 0, 0.5, 1, ... , 5"
    )
    rating_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "title"]

    def __str__(self):
        return self.title

    def set_rating(self, new_rating):
        allowed_values = [x * 0.5 for x in range(11)] 
        if new_rating not in allowed_values:
            raise ValueError
        self.rating = new_rating
        self.save()

class ProductInventory(models.Model):
    inventory_id = models.CharField(primary_key=True, max_length=100)
    product = models.OneToOneField(Product, on_delete=models.CASCADE)
    stock_quantity = models.IntegerField()
    low_stock_alert = models.IntegerField()
    stock_status = models.CharField(max_length=50, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class ProductVariant(models.Model):
    variant_id = models.CharField(primary_key=True, max_length=100)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    size = models.CharField(max_length=50, blank=True, default="")
    color = models.CharField(max_length=50, blank=True, default="")
    material_type = models.CharField(max_length=50, blank=True, default="")
    fabric_finish = models.CharField(max_length=50, blank=True, default="")
    printing_methods = models.JSONField(default=list)
    add_on_options = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

class VariantCombination(models.Model):
    combo_id = models.CharField(primary_key=True, max_length=100)
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE)
    description = models.TextField()
    price_override = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

class ShippingInfo(models.Model):
    shipping_id = models.CharField(primary_key=True, max_length=100)
    product = models.OneToOneField(Product, on_delete=models.CASCADE)
    shipping_class = models.CharField(max_length=100)
    processing_time = models.CharField(max_length=100)
    entered_by_id = models.CharField(max_length=100)
    entered_by_type = models.CharField(max_length=10)
    created_at = models.DateTimeField(auto_now_add=True)

class ProductSEO(models.Model):
    seo_id = models.CharField(primary_key=True, max_length=100)
    product = models.OneToOneField(Product, on_delete=models.CASCADE)
    image_alt_text = models.CharField(max_length=255, blank=True, default="")
    meta_title = models.CharField(max_length=255, blank=True, default="")
    meta_description = models.TextField(blank=True, default="")
    meta_keywords = models.JSONField(null=False, blank=True, default=list)
    open_graph_title = models.CharField(max_length=255, blank=True, default="")
    open_graph_desc = models.TextField(blank=True, default="")
    open_graph_image_url = models.URLField(blank=True, default="")
    canonical_url = models.URLField(blank=True, default="")
    json_ld = models.TextField(blank=True, default="")
    custom_tags = models.JSONField(null=False, blank=True, default=list)
    grouped_filters = models.JSONField(null=False, blank=True, default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class ProductSubCategoryMap(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    subcategory = models.ForeignKey(SubCategory, on_delete=models.CASCADE)

    class Meta:
        indexes = [
            models.Index(fields=["product"]),
            models.Index(fields=["subcategory"]),
        ]
        unique_together = ("product", "subcategory")

class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="images")
    image = models.ForeignKey(Image, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    caption = models.TextField(blank=True, default="")
    is_primary = models.BooleanField(default=False)
    
class ProductTestimonial(models.Model):
    """
    Minimal, API-aligned testimonial/comment entity for Products or SubCategories.

    Fields mirror the frontend:
      - name, email
      - content (comment body)
      - rating (0..5, 0.5 steps allowed)
      - rating_count (usually 1 per comment; kept for parity)
      - status: approved | pending | rejected | hidden
      - FK to either Product OR SubCategory (exactly one required)
      - created_at / updated_at
    """
    testimonial_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Link to either a product or a subcategory (one must be non-null)
    product = models.ForeignKey(
        "Product",
        to_field="product_id",
        db_column="product_id",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="testimonials",
    )
    subcategory = models.ForeignKey(
        "SubCategory",
        to_field="subcategory_id",
        db_column="subcategory_id",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="testimonials",
    )

    # Author + content
    name = models.CharField(max_length=120, db_index=True)
    email = models.EmailField(max_length=254)
    content = models.TextField()  # sanitized on the frontend; still validate server-side later

    # Ratings (per-comment)
    rating = models.FloatField(
        default=0.0,
        validators=[MinValueValidator(0.0), MaxValueValidator(5.0)],
        help_text="Allowed values: 0, 0.5, 1, ... , 5",
        db_index=True,
    )
    rating_count = models.PositiveIntegerField(default=1)

    # Moderation / visibility
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("hidden", "Hidden"),
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
        db_index=True,
    )

    # Audit
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["product"]),
            models.Index(fields=["subcategory"]),
            models.Index(fields=["status"]),
            models.Index(fields=["rating"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        target = self.product_id_display or self.subcategory_id_display or "unlinked"
        return f"Testimonial {self.testimonial_id} by {self.name} → {target}"

    def clean(self):
        """
        Enforce exactly one target: either product or subcategory, not both, not neither.
        """
        if bool(self.product) == bool(self.subcategory):
            raise ValidationError("Exactly one of product or subcategory must be set.")

        # Enforce half-star steps (0, 0.5, 1, ... 5) as per frontend constraints.
        allowed = {x * 0.5 for x in range(11)}
        if float(self.rating) not in allowed:
            raise ValidationError({"rating": "Rating must be in 0.5 steps between 0 and 5."})

    @property
    def product_id_display(self) -> str:
        try:
            return getattr(self.product, "product_id", "") or ""
        except Exception:
            return ""

    @property
    def subcategory_id_display(self) -> str:
        try:
            return getattr(self.subcategory, "subcategory_id", "") or ""
        except Exception:
            return ""
    
class Attribute(models.Model):
    attr_id = models.CharField(primary_key=True, max_length=100)

    product = models.ForeignKey(
        "Product",
        on_delete=models.CASCADE,
        related_name="attributes",
        db_index=True,
    )

    # self-referencing for options
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="options",
        db_index=True,
    )

    # attribute-level fields (when parent is NULL)
    name = models.CharField(max_length=255, blank=True, default="")

    # option-level fields (when parent is NOT NULL)
    label = models.CharField(max_length=255, blank=True, default="")
    image = models.ForeignKey(
        "Image",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attribute_images",
    )
    price_delta = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    is_default = models.BooleanField(default=False)

    # NEW: shared description for both attribute and option nodes
    description = models.TextField(blank=True, default="")

    # housekeeping
    order = models.PositiveIntegerField(default=0, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["product", "parent", "order"]),
        ]
        ordering = ["order", "name", "label"]

    def is_attribute(self):
        return self.parent_id is None

    def is_option(self):
        return self.parent_id is not None

    def __str__(self):
        if self.is_attribute():
            return f"[Attribute] {self.name} :: {self.product.title}"
        return f"[Option] {self.label} -> {self.parent.name}"
    
class Orders(models.Model):
    order_id = models.CharField(primary_key=True, max_length=100)
    device_uuid = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    user_name = models.CharField(max_length=255, blank=True)
    order_date = models.DateTimeField()
    status = models.CharField(max_length=50, choices=[
        ("pending", "Pending"),
        ("shipped", "Shipped"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ], db_index=True)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class OrderItem(models.Model):
    item_id = models.CharField(primary_key=True, max_length=100)
    order = models.ForeignKey(Orders, on_delete=models.CASCADE)
    product = models.ForeignKey('Product', on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    selected_size = models.CharField(max_length=50, blank=True, null=True)
    selected_attributes = models.JSONField(default=dict, blank=True)
    selected_attributes_human = models.JSONField(default=list, blank=True)
    variant_signature = models.CharField(max_length=255, blank=True, null=True)
    attributes_price_delta = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price_breakdown = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

class OrderDelivery(models.Model):
    delivery_id = models.CharField(primary_key=True, max_length=100)
    order = models.OneToOneField(Orders, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, null=True)  # ← allow missing/NA
    phone = models.CharField(max_length=20)
    street_address = models.TextField()
    city = models.CharField(max_length=100, db_index=True)
    zip_code = models.CharField(max_length=20, db_index=True)
    instructions = models.JSONField(default=list, blank=True)  # ← always list
    created_at = models.DateTimeField(auto_now_add=True)
    
class Cart(models.Model):
    cart_id = models.CharField(primary_key=True, max_length=100)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
    device_uuid = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class CartItem(models.Model):
    item_id = models.CharField(primary_key=True, max_length=100)
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
    price_per_unit = models.DecimalField(max_digits=10, decimal_places=2)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    selected_size = models.CharField(max_length=50, blank=True, null=True)
    selected_attributes = models.JSONField(default=dict, blank=True)
    variant_signature = models.CharField(max_length=255, blank=True, default="", db_index=True)
    attributes_price_delta = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    
# === BLOG SYSTEM (aligned with your patterns) ===
class BlogPost(models.Model):
    blog_id = models.CharField(primary_key=True, max_length=100)

    title = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255, unique=True)
    content_html = models.TextField(blank=True, default="")         # Quill HTML
    author = models.CharField(max_length=120, blank=True, default="")

    # SEO / social
    meta_title = models.CharField(max_length=255, blank=True, default="")
    meta_description = models.TextField(blank=True, default="")
    og_title = models.CharField(max_length=255, blank=True, default="")
    og_image_url = models.URLField(blank=True, default="")
    tags = models.CharField(max_length=255, blank=True, default="")  # CSV like "tag1, tag2"
    schema_enabled = models.BooleanField(default=False)

    # publishing
    publish_date = models.DateTimeField(null=True, blank=True)
    draft = models.BooleanField(default=True)
    status = models.CharField(
        max_length=20,
        choices=[("draft", "Draft"), ("scheduled", "Scheduled"), ("published", "Published")],
        default="draft",
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def compute_status(self):
        # authoritative status, stored lower-case to match your enums elsewhere
        if self.draft:
            return "draft"
        if self.publish_date and self.publish_date > timezone.now():
            return "scheduled"
        return "published"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.title)[:255]
        self.status = self.compute_status()
        super().save(*args, **kwargs)

class BlogImage(models.Model):
    """
    Mirrors your ProductImage pattern.
    Multiple images per blog; one may be primary; ordered; optional caption.
    """
    blog = models.ForeignKey(BlogPost, on_delete=models.CASCADE, related_name="images")
    image = models.ForeignKey("Image", on_delete=models.CASCADE)
    caption = models.TextField(blank=True, default="")
    is_primary = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [
            models.Index(fields=["blog"]),
            models.Index(fields=["image"]),
        ]
        unique_together = ("blog", "image")  # same image not linked twice

class BlogComment(models.Model):
    """
    Minimal, API-aligned comment entity:
    - comment_id: UUID primary key (serializes to string)
    - blog (FK)    -> BlogPost.blog_id
    - name, email, website, comment
    - created_at, updated_at
    """
    comment_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Store FK against BlogPost.blog_id (which is the PK and a CharField)
    blog = models.ForeignKey(
        BlogPost,
        on_delete=models.CASCADE,
        related_name="comments",
        db_column="blog_id",         # column named exactly "blog_id"
        to_field="blog_id",          # link to BlogPost.blog_id
    )

    # Author fields
    name = models.CharField(max_length=120)
    email = models.EmailField(max_length=254)
    website = models.URLField(blank=True, default="")

    # Content
    comment = models.TextField()

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["blog"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"Comment {self.comment_id} on {self.blog_id_display}"

    @property
    def blog_id_display(self) -> str:
        # Convenience for admin/readability
        return getattr(self.blog, "blog_id", "")
    
class Notification(models.Model):
    notification_id = models.CharField(primary_key=True, max_length=100)
    type = models.CharField(max_length=100, db_index=True)
    title = models.CharField(max_length=255)
    message = models.TextField()
    recipient_id = models.CharField(max_length=100, db_index=True)
    recipient_type = models.CharField(max_length=10, choices=[("user", "User"), ("admin", "Admin")], db_index=True)
    source_table = models.CharField(max_length=100)
    source_id = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=[("unread", "Unread"), ("read", "Read")], db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

class CallbackRequest(models.Model):
    callback_id = models.CharField(primary_key=True, max_length=100)
    sender_id = models.CharField(max_length=100)
    contact_info = models.CharField(max_length=255)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

class HeroBanner(models.Model):
    hero_id = models.CharField(primary_key=True, max_length=100)
    alt_text = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

class HeroBannerImage(models.Model):
    banner = models.ForeignKey(HeroBanner, on_delete=models.CASCADE, related_name="images")
    image = models.ForeignKey('Image', on_delete=models.CASCADE)
    device_type = models.CharField(max_length=20, choices=[('desktop', 'Desktop'), ('mobile', 'Mobile')], db_index=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

class DeletedItemsCache(models.Model):
    cache_id = models.CharField(primary_key=True, max_length=100)
    table_name = models.CharField(max_length=100, db_index=True)
    record_data = models.JSONField()
    deleted_at = models.DateTimeField()
    deleted_reason = models.TextField()
    restored = models.BooleanField(default=False)
    restored_at = models.DateTimeField(null=True, blank=True)

class SiteSettings(models.Model):
    setting_id = models.CharField(primary_key=True, max_length=100)
    site_title = models.CharField(max_length=100)
    logo_url = models.URLField()
    language = models.CharField(max_length=20)
    currency = models.CharField(max_length=10)
    timezone = models.CharField(max_length=50)
    tax_rate = models.FloatField()
    payment_modes = models.JSONField()
    shipping_zones = models.JSONField()
    social_links = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

class DashboardSnapshot(models.Model):
    dashboard_id = models.CharField(primary_key=True, max_length=100)
    snapshot_type = models.CharField(
        max_length=50,
        choices=[("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly"), ("yearly", "Yearly")]
    )
    snapshot_date = models.DateField()

    new_users = models.IntegerField()
    orders_placed = models.IntegerField()
    orders_cancelled = models.IntegerField()
    orders_delivered = models.IntegerField()
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2)
    active_users = models.IntegerField()

    order_growth_rate = models.FloatField()
    user_growth_rate = models.FloatField()
    active_user_growth_rate = models.FloatField()

    top_visited_pages = models.JSONField(default=list)
    top_companies = models.JSONField(default=list)
    countries_ordered_from = models.JSONField(default=list)

    data_source = models.CharField(max_length=100)  # e.g. 'live_data', 'backup', etc.
    created_by = models.ForeignKey(Admin, on_delete=models.CASCADE)

    created_at = models.DateTimeField(auto_now_add=True)

class FirstCarousel(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField()

    def __str__(self):
        return self.title
    
class FirstCarouselImage(models.Model):
    carousel = models.ForeignKey(
        FirstCarousel, 
        on_delete=models.CASCADE, 
        related_name="images"
    )
    image = models.ForeignKey('Image', on_delete=models.CASCADE)
    title = models.CharField(max_length=255, default="", blank=True)

    # Now linked to SubCategory instead of Category
    subcategory = models.ForeignKey(
        'SubCategory',
        to_field='subcategory_id',        # use the Char PK
        db_column='subcategory_id',       # column literally named subcategory_id
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='first_carousel_images',
    )

    caption = models.CharField(max_length=255, default="", blank=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

class SecondCarousel(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField()

    def __str__(self):
        return self.title

class SecondCarouselImage(models.Model):
    carousel = models.ForeignKey(
        SecondCarousel, 
        on_delete=models.CASCADE, 
        related_name="images"
    )
    image = models.ForeignKey('Image', on_delete=models.CASCADE)
    title = models.CharField(max_length=255, default="", blank=True)

    # Now linked to SubCategory instead of Category
    subcategory = models.ForeignKey(
        'SubCategory',
        to_field='subcategory_id',
        db_column='subcategory_id',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='second_carousel_images',
    )

    caption = models.CharField(max_length=255, default="", blank=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

class Testimonial(models.Model):
    """
    Single-source-of-truth for customer testimonials.
    - Char PK to align with your ID strategy
    - Optional FK to Image (preferred), with fallback image_url for external avatars
    - Integer rating (1–5), validated
    - Publish workflow via status field
    - Creator bookkeeping mirrors Product.created_by / created_by_type
    """
    testimonial_id = models.CharField(primary_key=True, max_length=100)

    # Core content
    name = models.CharField(max_length=255, db_index=True)
    role = models.CharField(max_length=255, blank=True, default="")
    content = models.TextField()

    # Avatar (prefer internal Image; allow external URL as fallback)
    image = models.ForeignKey(
        Image,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="testimonial_avatars",
    )
    image_url = models.URLField(blank=True, default="")  # used when no Image FK

    # Rating: 1..5 (whole-star scale to match frontend UI)
    rating = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        db_index=True,
        help_text="Whole-star rating from 1 to 5.",
    )

    # Publish state
    status = models.CharField(
        max_length=20,
        choices=[("draft", "Draft"), ("published", "Published")],
        default="draft",
        db_index=True,
    )

    # Audit / ordering
    created_by = models.CharField(max_length=100, blank=True, default="")
    created_by_type = models.CharField(
        max_length=10,
        choices=[("admin", "Admin"), ("user", "User")],
        blank=True,
        default="admin",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    order = models.PositiveIntegerField(default=0, db_index=True)

    class Meta:
        ordering = ["order", "-updated_at", "-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["rating"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.role})"

    @property
    def avatar_url(self) -> str:
        """
        Serializer-friendly: prefer managed Image file, fall back to image_url, else empty.
        Frontend already handles default avatar when this is empty.
        """
        try:
            if self.image and self.image.image_file:
                return self.image.image_file.url
        except ValueError:
            pass
        return self.image_url or ""

class SiteBranding(models.Model):
    """
    Hard singleton for brand basics.
    MySQL-safe: enforced via a unique, constant lock field.
    """
    branding_id = models.CharField(primary_key=True, max_length=100)

    site_title = models.CharField(max_length=255, blank=True, default="", db_index=True)

    logo = models.ForeignKey(
        "Image",
        to_field="image_id",
        db_column="logo_image_id",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="as_logo_for_branding",
    )
    favicon = models.ForeignKey(
        "Image",
        to_field="image_id",
        db_column="favicon_image_id",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="as_favicon_for_branding",
    )

    # Hard singleton lock: only one row can exist.
    singleton_lock = models.CharField(
        max_length=1, default="X", unique=True, editable=False, db_index=True
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site Branding"
        verbose_name_plural = "Site Branding"

    def __str__(self):
        return self.site_title or "Site Branding"

    @property
    def logo_url(self):
        try:
            return self.logo.url if self.logo else ""
        except Exception:
            return ""

    @property
    def favicon_url(self):
        try:
            return self.favicon.url if self.favicon else ""
        except Exception:
            return ""