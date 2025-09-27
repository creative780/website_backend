from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.utils.timezone import now
from .models import Admin, AdminRole, AdminRoleMap, Notification, DashboardSnapshot, SiteSettings
import uuid
from django.contrib.auth.signals import user_logged_in
from .models import Product, Orders, BlogPost, Category, SubCategory, ProductTestimonial
from django.contrib.auth.signals import user_logged_out


def create_admin_notification(message, source_table, source_id):
    Notification.objects.create(
        notification_id=str(uuid.uuid4()),
        type="admin_model_change",
        title="Admin Change Detected",
        message=message,
        recipient_id="superadmin",  # or dynamic logic later
        recipient_type="admin",
        source_table=source_table,
        source_id=source_id,
        status="unread",
    )


# ==== SIGNALS ====
@receiver(post_save, sender=Product)
def notify_product_created_or_updated(sender, instance, created, **kwargs):
    action = "created" if created else "updated"
    message = f"Product '{instance.title}' was {action}."
    create_admin_notification(message, "Product", instance.product_id)

@receiver(post_delete, sender=Product)
def notify_product_deleted(sender, instance, **kwargs):
    message = f"Product '{instance.title}' was deleted."
    create_admin_notification(message, "Product", instance.product_id)


@receiver(post_save, sender=Admin)
def notify_admin_created_or_updated(sender, instance, created, **kwargs):
    print("SIGNAL TRIGGERED: Admin created or updated")  # <--- Add this
    action = "created" if created else "updated"
    message = f"Admin '{instance.admin_name}' was {action}."
    create_admin_notification(message, "Admin", instance.admin_id)

@receiver(post_delete, sender=Admin)
def notify_admin_deleted(sender, instance, **kwargs):
    message = f"Admin '{instance.admin_name}' was deleted."
    create_admin_notification(message, "Admin", instance.admin_id)

@receiver(post_save, sender=DashboardSnapshot)
def notify_snapshot_created(sender, instance, created, **kwargs):
    if created:
        message = f"Dashboard snapshot ({instance.snapshot_type}) was created by Admin ID {instance.created_by.admin_id}."
        create_admin_notification(message, "DashboardSnapshot", instance.dashboard_id)


@receiver(post_save, sender=SiteSettings)
def notify_site_settings_updated(sender, instance, **kwargs):
    message = "Site settings were updated."
    create_admin_notification(message, "SiteSettings", instance.setting_id)

@receiver(user_logged_in)
def notify_on_login(sender, request, user, **kwargs):
    from .models import Notification
    import uuid
    from django.utils.timezone import now

    Notification.objects.create(
        notification_id=str(uuid.uuid4()),
        type="login",
        title="Admin Logged In",
        message=f"{user.username} just logged in.",
        recipient_id="superadmin",
        recipient_type="admin",
        source_table="Admin",
        source_id=user.pk,
        status="unread",
        created_at=now()
    )

@receiver(post_save, sender=Orders)
def notify_order_created_or_updated(sender, instance, created, **kwargs):
    if created:
        message = f"New order '{instance.order_id}' was placed."
    else:
        message = f"Order '{instance.order_id}' status changed to '{instance.status}'."
    create_admin_notification(message, "Orders", instance.order_id)

@receiver(post_save, sender=BlogPost)
def notify_blog_created_or_updated(sender, instance, created, **kwargs):
    action = "published" if instance.status == "published" else "saved as draft"
    message = f"Blog '{instance.title}' was {action}."
    create_admin_notification(message, "Blog", instance.blog_id)

@receiver(post_save, sender=Category)
def notify_category_created_or_updated(sender, instance, created, **kwargs):
    action = "created" if created else "updated"
    message = f"Category '{instance.name}' was {action}."
    create_admin_notification(message, "Category", instance.category_id)

@receiver(post_save, sender=SubCategory)
def notify_subcategory_created_or_updated(sender, instance, created, **kwargs):
    action = "created" if created else "updated"
    message = f"Subcategory '{instance.name}' was {action}."
    create_admin_notification(message, "SubCategory", instance.subcategory_id)


@receiver(user_logged_in)
def notify_user_login(sender, request, user, **kwargs):
    from .models import Notification
    if hasattr(user, 'user_id'):  # if it's a normal user
        user_type = 'user'
        user_identifier = user.user_id
        username = user.username
    elif hasattr(user, 'admin_id'):
        user_type = 'admin'
        user_identifier = user.admin_id
        username = user.admin_name
    else:
        return

    Notification.objects.create(
        notification_id=str(uuid.uuid4()),
        type="login",
        title="Login Detected",
        message=f"{username} logged in.",
        recipient_id="superadmin",
        recipient_type="admin",
        source_table="User" if user_type == 'user' else "Admin",
        source_id=user_identifier,
        status="unread",
        created_at=now()
    )

@receiver(user_logged_out)
def notify_logout(sender, request, user, **kwargs):
    Notification.objects.create(
        notification_id=str(uuid.uuid4()),
        type="logout",
        title="Logout Detected",
        message=f"{user.username} logged out.",
        recipient_id="superadmin",
        recipient_type="admin",
        source_table="User",
        source_id=getattr(user, 'user_id', 'unknown'),
        status="unread",
        created_at=now()
    )

@receiver(post_save, sender=ProductTestimonial)
def notify_testimonial_created(sender, instance, created, **kwargs):
    if not created:
        return  # notify only when a comment is first created

    # We want this notification to live under the dedicated "Product Comments" tab on FE.
    # Therefore:
    # - source_table => "product_comment"
    # - source_id    => comment_id (testimonial_id)
    # - type         => "comment" (kept for semantics)
    source_table = "product_comment"
    source_id = str(instance.testimonial_id)

    # Human-readable target context
    if instance.product:
        target_label = f"product {getattr(instance.product, 'title', 'Unknown Product')}"
    elif instance.subcategory:
        target_label = f"subcategory {getattr(instance.subcategory, 'name', 'Unknown SubCategory')}"
    else:
        target_label = "item"

    message = (
        f"{instance.name} has commented on the {target_label}\n"
        f"Comment: {instance.content}"
    )

    Notification.objects.create(
        notification_id=str(uuid.uuid4()),
        type="comment",
        title="New Comment",
        message=message,
        recipient_id="superadmin",
        recipient_type="admin",
        source_table=source_table,   # <-- product_comment
        source_id=source_id,         # <-- comment_id
        status="unread",
        created_at=now(),
    )
