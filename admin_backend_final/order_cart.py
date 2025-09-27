# Standard Library
import json
import hashlib
import uuid
import traceback
from decimal import Decimal, InvalidOperation


# Django
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q, Prefetch

# Django REST Framework
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .utilities import generate_custom_order_id
# Local Imports
from .models import *  # Consider specifying models instead of wildcard import
from .permissions import FrontendOnlyPermission

def _compute_attributes_delta_and_details(self, selected_attrs: dict) -> tuple[Decimal, list]:
    """
    selected_attrs looks like { "<parent_attr_id>": "<option_attr_id>", ... }
    Sum price_delta from each option attr and return human details, ordered by:
      1) parent Attribute.order
      2) option Attribute.order
      3) attribute_name (tiebreaker)
    """
    total_delta = Decimal("0.00")
    details = []

    if not isinstance(selected_attrs, dict) or not selected_attrs:
        return total_delta, details

    pairs = list(selected_attrs.items())
    ids = {pid for pid, _ in pairs} | {oid for _, oid in pairs}
    attr_qs = (Attribute.objects
               .filter(attr_id__in=list(ids))
               .select_related("parent")
               .only("attr_id", "name", "label", "price_delta", "order", "parent_id"))
    by_id = {a.attr_id: a for a in attr_qs}

    enriched = []
    for parent_id, opt_id in pairs:
        opt = by_id.get(opt_id)
        parent = by_id.get(parent_id)
        if not parent and opt and opt.parent and getattr(opt.parent, "attr_id", None) == parent_id:
            parent = opt.parent

        parent_order = getattr(parent, "order", 0) or 0
        option_order = getattr(opt, "order", 0) or 0
        price_delta = Decimal(str(getattr(opt, "price_delta", 0) or 0))
        total_delta += price_delta

        enriched.append((
            parent_order,
            option_order,
            {
                "attribute_id": parent_id,
                "option_id": opt_id,
                "attribute_name": getattr(parent, "name", parent_id),
                "option_label": getattr(opt, "label", opt_id),
                "price_delta": str(price_delta),
                "attribute_order": parent_order,
                "option_order": option_order,
            }
        ))

    enriched.sort(key=lambda t: (t[0], t[1], t[2]["attribute_name"]))
    details = [x[2] for x in enriched]
    return total_delta, details

def _attr_humanize(self, sel: dict):
    """
    Return (details_list, delta_sum_decimal) in a deterministic order:
      - parent Attribute.order, then option Attribute.order, then attribute_name.
    """
    details = []
    total_delta = Decimal("0.00")

    if not isinstance(sel, dict) or not sel:
        return details, total_delta

    pairs = list(sel.items())
    ids = {pid for pid, _ in pairs} | {oid for _, oid in pairs}
    attr_qs = (Attribute.objects
               .filter(attr_id__in=list(ids))
               .select_related("parent")
               .only("attr_id", "name", "label", "price_delta", "order", "parent_id"))
    by_id = {a.attr_id: a for a in attr_qs}

    enriched = []
    for parent_id, opt_id in pairs:
        opt = by_id.get(opt_id)
        parent = by_id.get(parent_id)
        if not parent and opt and opt.parent and getattr(opt.parent, "attr_id", None) == parent_id:
            parent = opt.parent

        parent_order = getattr(parent, "order", 0) or 0
        option_order = getattr(opt, "order", 0) or 0
        price_delta = Decimal(str(getattr(opt, "price_delta", 0) or 0))
        total_delta += price_delta

        enriched.append((
            parent_order,
            option_order,
            {
                "attribute_id": parent_id,
                "option_id": opt_id,
                "attribute_name": getattr(parent, "name", parent_id),
                "option_label": getattr(opt, "label", opt_id),
                "price_delta": str(price_delta),
                "attribute_order": parent_order,
                "option_order": option_order,
            }
        ))

    enriched.sort(key=lambda t: (t[0], t[1], t[2]["attribute_name"]))
    details = [x[2] for x in enriched]
    return details, total_delta

class SaveCartAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def _get_primary_cart(self, device_uuid: str) -> Cart:
        cart = Cart.objects.filter(device_uuid=device_uuid).order_by("-updated_at", "-created_at").first()
        if cart:
            # Optional: merge any accidental duplicates for same device_uuid
            dups = Cart.objects.filter(device_uuid=device_uuid).exclude(pk=cart.pk)
            if dups.exists():
                for dup in dups:
                    CartItem.objects.filter(cart=dup).update(cart=cart)
                    dup.delete()
            return cart
        return Cart.objects.create(cart_id=str(uuid.uuid4()), device_uuid=device_uuid)

    def _compute_attributes_delta_and_details(self, selected_attrs: dict) -> tuple[Decimal, list]:
        """
        selected_attrs looks like { "<parent_attr_id>": "<option_attr_id>", ... }
        Sum price_delta from each option attr and return human details.
        """
        total_delta = Decimal("0.00")
        details = []

        if not isinstance(selected_attrs, dict):
            return total_delta, details

        for parent_id, opt_id in selected_attrs.items():
            opt = Attribute.objects.filter(attr_id=opt_id).select_related("parent").first()
            parent = Attribute.objects.filter(attr_id=parent_id).first() if not (opt and opt.parent and opt.parent.attr_id == parent_id) else opt.parent

            price_delta = Decimal(str(opt.price_delta)) if (opt and opt.price_delta is not None) else Decimal("0.00")
            total_delta += price_delta

            details.append({
                "attribute_id": parent_id,
                "option_id": opt_id,
                "attribute_name": (parent.name if parent else parent_id),
                "option_label": (opt.label if opt else opt_id),
                "price_delta": str(price_delta)
            })

        return total_delta, details

    def post(self, request):
        try:
            # ---- Parse payload safely
            if isinstance(request.data, dict):
                data = request.data
            else:
                try:
                    data = json.loads(request.body or "{}")
                except json.JSONDecodeError:
                    return Response({"error": "Invalid JSON payload."}, status=status.HTTP_400_BAD_REQUEST)

            device_uuid = data.get("device_uuid") or request.headers.get("X-Device-UUID")
            if not device_uuid:
                return Response({"error": "Missing device UUID."}, status=status.HTTP_400_BAD_REQUEST)

            product_id = data.get("product_id")
            if not product_id:
                return Response({"error": "Missing product_id."}, status=status.HTTP_400_BAD_REQUEST)

            # quantity guardrails
            try:
                quantity = int(data.get("quantity", 1))
            except (TypeError, ValueError):
                return Response({"error": "Invalid quantity."}, status=status.HTTP_400_BAD_REQUEST)
            if quantity < 1:
                quantity = 1

            selected_size = (data.get("selected_size") or "").strip()
            selected_attributes = data.get("selected_attributes") or {}
            if not isinstance(selected_attributes, dict):
                return Response({"error": "selected_attributes must be an object."}, status=status.HTTP_400_BAD_REQUEST)

            product = get_object_or_404(Product, product_id=product_id)
            _ = get_object_or_404(ProductInventory, product=product)

            cart = self._get_primary_cart(device_uuid)

            # ---- Pricing
            attributes_delta, _human_details = self._compute_attributes_delta_and_details(selected_attributes)
            try:
                base_price = Decimal(str(product.discounted_price or product.price or 0))
            except (InvalidOperation, TypeError):
                base_price = Decimal("0.00")
            unit_price = base_price + attributes_delta

            # ---- Stable, short variant signature (<=255) via SHA-256
            # Include size and a sorted view of attributes so signature is deterministic.
            sig_payload = {
                "size": selected_size,
                "attrs": dict(sorted(selected_attributes.items(), key=lambda x: x[0])),
            }
            sig_str = json.dumps(sig_payload, separators=(",", ":"), sort_keys=True)
            sig_hash = hashlib.sha256(sig_str.encode("utf-8")).hexdigest()  # 64 chars
            variant_signature = f"v1:{sig_hash}"  # total length ~67, well under 255

            # ---- Upsert cart item by variant signature
            cart_item, created = CartItem.objects.get_or_create(
                cart=cart,
                product=product,
                variant_signature=variant_signature,
                defaults={
                    "item_id": str(uuid.uuid4()),
                    "quantity": quantity,
                    "price_per_unit": unit_price,
                    "subtotal": unit_price * quantity,
                    "selected_size": selected_size[:50] if selected_size else "",  # fit field limit
                    "selected_attributes": selected_attributes,
                    "attributes_price_delta": attributes_delta,
                }
            )

            if not created:
                cart_item.quantity += quantity
                cart_item.price_per_unit = unit_price  # keep latest pricing
                cart_item.attributes_price_delta = attributes_delta
                cart_item.selected_size = selected_size[:50] if selected_size else ""
                cart_item.selected_attributes = selected_attributes
                cart_item.subtotal = unit_price * cart_item.quantity
                cart_item.save(update_fields=[
                    "quantity", "price_per_unit", "attributes_price_delta",
                    "selected_size", "selected_attributes", "subtotal"
                ])

            return Response({"message": "Cart updated successfully."}, status=status.HTTP_200_OK)

        except Exception as e:
            print("❌ [SAVE_CART] Error:", str(e))
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
                            
class ShowCartAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def _attr_humanize(self, sel: dict):
        """
        Return (details_list, delta_sum_decimal).
        details_list: [{attribute_name, option_label, price_delta}, ...]
        """
        details = []
        total_delta = Decimal("0.00")
        if not isinstance(sel, dict):
            return details, total_delta

        for parent_id, opt_id in sel.items():
            opt = Attribute.objects.filter(attr_id=opt_id).select_related("parent").first()
            parent = Attribute.objects.filter(attr_id=parent_id).first() if not (opt and opt.parent and opt.parent.attr_id == parent_id) else opt.parent

            price_delta = Decimal(str(opt.price_delta)) if (opt and opt.price_delta is not None) else Decimal("0.00")
            total_delta += price_delta
            details.append({
                "attribute_id": parent_id,
                "option_id": opt_id,
                "attribute_name": (parent.name if parent else parent_id),
                "option_label": (opt.label if opt else opt_id),
                "price_delta": str(price_delta)
            })
        return details, total_delta

    def _respond(self, request, device_uuid):
        if not device_uuid:
            return Response({"error": "Missing device UUID."}, status=status.HTTP_400_BAD_REQUEST)

        cart = Cart.objects.filter(device_uuid=device_uuid).order_by("-updated_at", "-created_at").first()
        if not cart:
            return Response({"cart_items": []}, status=status.HTTP_200_OK)

        cart_items = CartItem.objects.filter(cart=cart).select_related("product")
        response_data = []

        for item in cart_items:
            # Image (first linked product image)
            image_rel = Image.objects.filter(linked_table='product', linked_id=item.product.product_id).first()
            image_url = request.build_absolute_uri(image_rel.url) if image_rel and getattr(image_rel, "url", None) else None
            alt_text = getattr(image_rel, "alt_text", "") if image_rel else ""

            # Human-readable selections
            selections, attrs_delta = self._attr_humanize(item.selected_attributes or {})

            base_price = Decimal(str(item.product.discounted_price or item.product.price or 0))
            unit_price = base_price + attrs_delta
            line_total = unit_price * item.quantity

            # e.g. "Product1 (Paper Type: Simple, Size: 49)"
            selection_bits = []
            if item.selected_size:
                selection_bits.append(f"Size: {item.selected_size}")
            for d in selections:
                selection_bits.append(f"{d['attribute_name']}: {d['option_label']}")
            parenthetical = f" ({', '.join(selection_bits)})" if selection_bits else ""

            # e.g. "3 x $(4 + 0 + 5) = $27"
            # parts: actual base + each delta
            price_parts = [str(base_price)] + [d["price_delta"] for d in selections if d["price_delta"] not in ("0", "0.0", "0.00")]
            if not price_parts:
                price_parts = [str(base_price)]
            breakdown_str = f"{item.quantity} x $(" + " + ".join(price_parts) + f") = ${line_total}"

            response_data.append({
                "product_id": item.product.product_id,
                "product_name": item.product.title,
                "product_image": image_url,
                "alt_text": alt_text,
                "quantity": item.quantity,
                "selected_size": item.selected_size or "",
                "selected_attributes": item.selected_attributes or {},  # raw ids mapping
                "selected_attributes_human": selections,                # names/labels + deltas
                "price_breakdown": {
                    "base_price": str(base_price),
                    "attributes_delta": str(attrs_delta),
                    "unit_price": str(unit_price),
                    "quantity": item.quantity,
                    "line_total": str(line_total),
                },
                "summary_line": f"{item.product.title}{parenthetical}: {breakdown_str}",
            })

        return Response({"cart_items": response_data}, status=status.HTTP_200_OK)

    def get(self, request):
        device_uuid = request.headers.get('X-Device-UUID')
        return self._respond(request, device_uuid)

    def post(self, request):
        device_uuid = request.headers.get('X-Device-UUID')
        if not device_uuid:
            try:
                data = request.data if isinstance(request.data, dict) else json.loads(request.body or "{}")
                device_uuid = data.get("device_uuid")
            except Exception:
                device_uuid = None
        return self._respond(request, device_uuid)

# delete_cart_item -> APIView (POST)
class DeleteCartItemAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        try:
            data = json.loads(request.body)
            user_id = data.get('user_id')   # could be user id or device UUID
            product_id = data.get('product_id')

            if not user_id or not product_id:
                return Response({"error": "user_id and product_id are required."}, status=status.HTTP_400_BAD_REQUEST)

            DjangoUser = get_user_model()
            try:
                user = DjangoUser.objects.get(user_id=user_id)
                cart = Cart.objects.filter(user=user).first()
            except DjangoUser.DoesNotExist:
                cart = Cart.objects.filter(device_uuid=user_id).first()

            if not cart:
                return Response({"error": "Cart not found."}, status=status.HTTP_404_NOT_FOUND)

            cart_item = CartItem.objects.filter(cart=cart, product__product_id=product_id).first()
            if not cart_item:
                return Response({"error": "Cart item not found."}, status=status.HTTP_404_NOT_FOUND)

            cart_item.delete()
            return Response({"message": "Cart item deleted successfully."}, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        
class SaveOrderAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    @transaction.atomic
    def post(self, request):
        try:
            data = json.loads(request.body or "{}")

            # Pull device UUID from header or payload (mirrors cart usage)
            device_uuid = (
                request.headers.get("X-Device-UUID")
                or data.get("device_uuid")
                or ""
            )
            device_uuid = device_uuid.strip() or None  # normalize to None if empty

            user_name = data.get("user_name", "Guest")
            delivery_data = data.get("delivery") or {}
            email = delivery_data.get("email") or None

            order_id = generate_custom_order_id(user_name, email or "")
            order = Orders.objects.create(
                order_id=order_id,
                device_uuid=device_uuid,
                user_name=user_name,
                order_date=timezone.now(),
                status=data.get("status", "pending"),
                total_price=Decimal(str(data.get("total_price", "0"))),
                notes=data.get("notes", "")
            )

            items = data.get("items") or []
            if not isinstance(items, list) or len(items) == 0:
                return Response({"error": "No items provided"}, status=status.HTTP_400_BAD_REQUEST)

            for item in items:
                for field in ["product_id", "quantity", "unit_price", "total_price"]:
                    if field not in item:
                        return Response({"error": f"Missing {field} in item"}, status=status.HTTP_400_BAD_REQUEST)

                product = get_object_or_404(Product, product_id=item["product_id"])

                qty = int(item.get("quantity", 1))
                unit_price = Decimal(str(item.get("unit_price", "0")))
                total_price = Decimal(str(item.get("total_price", "0")))
                attrs_delta = Decimal(str(item.get("attributes_price_delta", 0)))

                # base = unit - delta (unless explicitly provided)
                if item.get("base_price") is not None:
                    base_price = Decimal(str(item["base_price"]))
                else:
                    base_price = unit_price - attrs_delta
                    if base_price < 0:
                        base_price = Decimal("0")

                selected_size = (item.get("selected_size") or "").strip()
                selected_attributes = item.get("selected_attributes") or {}
                variant_signature = item.get("variant_signature") or ""

                # Ordered humanization (inline, no extra function)
                # Build deterministic, UI-friendly list
                ordered_human = []
                if isinstance(selected_attributes, dict) and selected_attributes:
                    pairs = list(selected_attributes.items())
                    ids = {pid for pid, _ in pairs} | {oid for _, oid in pairs}
                    attr_qs = (Attribute.objects
                            .filter(attr_id__in=list(ids))
                            .select_related("parent")
                            .only("attr_id", "name", "label", "price_delta", "order", "parent_id"))
                    by_id = {a.attr_id: a for a in attr_qs}

                    enriched = []
                    for parent_id, opt_id in pairs:
                        opt = by_id.get(opt_id)
                        parent = by_id.get(parent_id)
                        if not parent and opt and opt.parent and getattr(opt.parent, "attr_id", None) == parent_id:
                            parent = opt.parent
                        parent_order = getattr(parent, "order", 0) or 0
                        option_order = getattr(opt, "order", 0) or 0
                        price_delta_h = Decimal(str(getattr(opt, "price_delta", 0) or 0))
                        enriched.append((
                            parent_order,
                            option_order,
                            {
                                "attribute_id": parent_id,
                                "option_id": opt_id,
                                "attribute_name": getattr(parent, "name", parent_id),
                                "option_label": getattr(opt, "label", opt_id),
                                "price_delta": str(price_delta_h),
                                "attribute_order": parent_order,
                                "option_order": option_order,
                            }
                        ))
                    enriched.sort(key=lambda t: (t[0], t[1], t[2]["attribute_name"]))
                    ordered_human = [x[2] for x in enriched]

                # Ensure variant_signature parity with cart if missing
                if not variant_signature:
                    sig_payload = {
                        "size": selected_size,
                        "attrs": dict(sorted((selected_attributes or {}).items(), key=lambda x: x[0])),
                    }
                    sig_str = json.dumps(sig_payload, separators=(",", ":"), sort_keys=True)
                    sig_hash = hashlib.sha256(sig_str.encode("utf-8")).hexdigest()
                    variant_signature = f"v1:{sig_hash}"

                price_breakdown = {
                    "base_price": str(base_price),
                    "attributes_delta": str(attrs_delta),
                    "unit_price": str(unit_price),
                    "line_total": str(total_price),
                    "selected_size": selected_size,
                    "selected_attributes_human": ordered_human,
                }

                OrderItem.objects.create(
                    item_id=str(uuid.uuid4()),
                    order=order,
                    product=product,
                    quantity=qty,
                    unit_price=unit_price,
                    total_price=total_price,
                    selected_size=selected_size,
                    selected_attributes=selected_attributes,
                    selected_attributes_human=ordered_human,  # ordered for FE
                    variant_signature=variant_signature,
                    attributes_price_delta=attrs_delta,
                    price_breakdown=price_breakdown,
                )

            # Normalize instructions to a list
            raw_instructions = delivery_data.get("instructions", [])
            if isinstance(raw_instructions, str):
                instructions = [raw_instructions] if raw_instructions.strip() else []
            elif isinstance(raw_instructions, list):
                instructions = raw_instructions
            else:
                instructions = []

            OrderDelivery.objects.create(
                delivery_id=str(uuid.uuid4()),
                order=order,
                name=delivery_data.get("name", user_name),
                email=email,  # may be None
                phone=delivery_data.get("phone", ""),
                street_address=delivery_data.get("street_address", ""),
                city=delivery_data.get("city", ""),
                zip_code=delivery_data.get("zip_code", ""),
                instructions=instructions,
            )

            return Response({"message": "Order saved successfully", "order_id": order_id}, status=status.HTTP_201_CREATED)

        except Product.DoesNotExist:
            return Response({"error": "One or more products not found"}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            print(traceback.format_exc())
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ShowOrderAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    def get(self, request):
        try:
            orders_data = []
            orders = Orders.objects.all().order_by('-created_at')

            for order in orders:
                order_items = (
                    OrderItem.objects
                    .filter(order=order)
                    .select_related('product')
                )

                try:
                    delivery = OrderDelivery.objects.get(order=order)
                    address = {
                        "street": delivery.street_address,
                        "city": delivery.city,
                        "zip": delivery.zip_code
                    }
                    email = delivery.email or ""
                except OrderDelivery.DoesNotExist:
                    address, email = {}, ""

                items_detail = []
                for it in order_items:
                    human = it.selected_attributes_human or []  # already ordered
                    tokens = []
                    if it.selected_size:
                        tokens.append(f"Size: {it.selected_size}")
                    for d in human:
                        tokens.append(f"{d.get('attribute_name','')}: {d.get('option_label','')}")
                    selection_str = ", ".join([t for t in tokens if t])

                    # math parts
                    try:
                        base = Decimal(it.price_breakdown.get("base_price", it.unit_price))
                    except Exception:
                        base = it.unit_price
                    deltas = []
                    for d in human:
                        try:
                            deltas.append(Decimal(d.get("price_delta", "0") or "0"))
                        except Exception:
                            deltas.append(Decimal("0"))

                    items_detail.append({
                        "product_id": it.product.product_id,
                        "product_name": it.product.title,
                        "quantity": it.quantity,
                        "unit_price": str(it.unit_price),
                        "total_price": str(it.total_price),

                        # Expose cart-parity fields to FE:
                        "selected_size": it.selected_size or "",
                        "selected_attributes": it.selected_attributes or {},        # raw ids
                        "selected_attributes_human": human,                         # ordered list
                        "selection": selection_str,                                 # legacy compact line

                        "math": {
                            "base": str(base),
                            "deltas": [str(x) for x in deltas],
                        },
                        "variant_signature": it.variant_signature or "",
                    })

                orders_data.append({
                    "orderID": order.order_id,
                    "Date": order.order_date.strftime('%Y-%m-%d %H:%M:%S'),
                    "UserName": order.user_name,
                    "item": {
                        "count": len(items_detail),
                        "names": [x["product_name"] for x in items_detail],
                        "detail": items_detail,
                    },
                    "total": float(order.total_price),
                    "status": order.status,
                    "Address": address,
                    "email": email,
                    "order_placed_on": order.created_at.strftime('%Y-%m-%d %H:%M:%S')
                })

            return Response({"orders": orders_data}, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class EditOrderAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    @transaction.atomic
    def put(self, request):
        try:
            data = json.loads(request.body or "{}")

            order_id = data.get("order_id")
            if not order_id:
                return Response({"error": "Missing order_id in request body"}, status=status.HTTP_400_BAD_REQUEST)

            order = get_object_or_404(Orders, order_id=order_id)

            # ----- Header fields -----
            order.user_name = data.get("user_name", order.user_name)
            incoming_status = data.get("status")
            if incoming_status is not None:
                order.status = incoming_status
            if data.get("total_price") is not None:
                order.total_price = Decimal(str(data["total_price"]))
            order.notes = data.get("notes", order.notes)
            order.save()

            # ----- Items (only rebuild if 'items' key is present) -----
            items_payload = data.get("items", None)
            if items_payload is not None:
                # Client provided items → treat as source of truth
                OrderItem.objects.filter(order=order).delete()

                for item in items_payload:
                    product = get_object_or_404(Product, product_id=item["product_id"])

                    qty = int(item.get("quantity", 1))
                    unit_price = Decimal(str(item.get("unit_price", "0")))
                    total_price = Decimal(str(item.get("total_price", "0")))
                    attrs_delta = Decimal(str(item.get("attributes_price_delta", 0)))

                    if item.get("base_price") is not None:
                        base_price = Decimal(str(item["base_price"]))
                    else:
                        base_price = unit_price - attrs_delta
                        if base_price < 0:
                            base_price = Decimal("0")

                    selected_size = (item.get("selected_size") or "").strip()
                    selected_attributes = item.get("selected_attributes") or {}
                    variant_signature = item.get("variant_signature") or ""

                    # Ordered humanization (inline)
                    ordered_human = []
                    if isinstance(selected_attributes, dict) and selected_attributes:
                        pairs = list(selected_attributes.items())
                        ids = {pid for pid, _ in pairs} | {oid for _, oid in pairs}
                        attr_qs = (Attribute.objects
                                .filter(attr_id__in=list(ids))
                                .select_related("parent")
                                .only("attr_id", "name", "label", "price_delta", "order", "parent_id"))
                        by_id = {a.attr_id: a for a in attr_qs}

                        enriched = []
                        for parent_id, opt_id in pairs:
                            opt = by_id.get(opt_id)
                            parent = by_id.get(parent_id)
                            if not parent and opt and opt.parent and getattr(opt.parent, "attr_id", None) == parent_id:
                                parent = opt.parent
                            parent_order = getattr(parent, "order", 0) or 0
                            option_order = getattr(opt, "order", 0) or 0
                            price_delta_h = Decimal(str(getattr(opt, "price_delta", 0) or 0))
                            enriched.append((
                                parent_order,
                                option_order,
                                {
                                    "attribute_id": parent_id,
                                    "option_id": opt_id,
                                    "attribute_name": getattr(parent, "name", parent_id),
                                    "option_label": getattr(opt, "label", opt_id),
                                    "price_delta": str(price_delta_h),
                                    "attribute_order": parent_order,
                                    "option_order": option_order,
                                }
                            ))
                        enriched.sort(key=lambda t: (t[0], t[1], t[2]["attribute_name"]))
                        ordered_human = [x[2] for x in enriched]

                    # Ensure variant_signature if missing
                    if not variant_signature:
                        sig_payload = {
                            "size": selected_size,
                            "attrs": dict(sorted((selected_attributes or {}).items(), key=lambda x: x[0])),
                        }
                        sig_str = json.dumps(sig_payload, separators=(",", ":"), sort_keys=True)
                        sig_hash = hashlib.sha256(sig_str.encode("utf-8")).hexdigest()
                        variant_signature = f"v1:{sig_hash}"

                    price_breakdown = {
                        "base_price": str(base_price),
                        "attributes_delta": str(attrs_delta),
                        "unit_price": str(unit_price),
                        "line_total": str(total_price),
                        "selected_size": selected_size,
                        "selected_attributes_human": ordered_human,
                    }

                    OrderItem.objects.create(
                        item_id=str(uuid.uuid4()),
                        order=order,
                        product=product,
                        quantity=qty,
                        unit_price=unit_price,
                        total_price=total_price,
                        selected_size=selected_size,
                        selected_attributes=selected_attributes,
                        selected_attributes_human=ordered_human,  # ordered
                        variant_signature=variant_signature,
                        attributes_price_delta=attrs_delta,
                        price_breakdown=price_breakdown,
                    )

            # ----- Delivery upsert (safe create with delivery_id) -----
            delivery_data = data.get("delivery")
            if delivery_data is not None:
                # Try to fetch existing record first
                delivery_obj = OrderDelivery.objects.filter(order=order).first()

                if delivery_obj is None:
                    # Create only if we have minimum required fields
                    required = ["name", "phone", "street_address", "city", "zip_code"]
                    if all(delivery_data.get(k) for k in required):
                        raw_instructions = delivery_data.get("instructions", [])
                        if isinstance(raw_instructions, str):
                            instructions = [raw_instructions] if raw_instructions.strip() else []
                        elif isinstance(raw_instructions, list):
                            instructions = raw_instructions
                        else:
                            instructions = []

                        delivery_obj = OrderDelivery.objects.create(
                            delivery_id=str(uuid.uuid4()),
                            order=order,
                            name=delivery_data.get("name"),
                            email=delivery_data.get("email"),
                            phone=delivery_data.get("phone"),
                            street_address=delivery_data.get("street_address"),
                            city=delivery_data.get("city"),
                            zip_code=delivery_data.get("zip_code"),
                            instructions=instructions,
                        )
                    # If not enough fields to create, silently skip creation.
                else:
                    raw_instructions = delivery_data.get("instructions", delivery_obj.instructions or [])
                    if isinstance(raw_instructions, str):
                        instructions = [raw_instructions] if raw_instructions.strip() else []
                    elif isinstance(raw_instructions, list):
                        instructions = raw_instructions
                    else:
                        instructions = delivery_obj.instructions or []

                    delivery_obj.name = delivery_data.get("name", delivery_obj.name)
                    delivery_obj.email = delivery_data.get("email", delivery_obj.email)
                    delivery_obj.phone = delivery_data.get("phone", delivery_obj.phone)
                    delivery_obj.street_address = delivery_data.get("street_address", delivery_obj.street_address)
                    delivery_obj.city = delivery_data.get("city", delivery_obj.city)
                    delivery_obj.zip_code = delivery_data.get("zip_code", delivery_obj.zip_code)
                    delivery_obj.instructions = instructions
                    delivery_obj.save()

            return Response(
                {"message": "Order updated successfully", "order_id": order_id},
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            print(traceback.format_exc())
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ShowSpecificUserOrdersAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def _split_multi(self, v):
        """
        Accept str, list, or None and return a set of trimmed lowercase tokens.
        """
        if v is None:
            return set()
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",")]
        elif isinstance(v, list):
            parts = [str(p).strip() for p in v]
        else:
            parts = [str(v).strip()]
        return {p.lower() for p in parts if p}

    def _build_filter(self, payload):
        """
        Supports filtering by:
          - Orders.device_uuid
          - Orders.user_name
          - OrderDelivery.email
          - OrderDelivery.phone
        Any provided filter will be OR'ed together.
        """
        names = self._split_multi(payload.get("user_name") or payload.get("user_names"))
        emails = self._split_multi(payload.get("email") or payload.get("emails"))
        phones = self._split_multi(payload.get("phone") or payload.get("phones"))
        device_ids = self._split_multi(payload.get("device_uuid") or payload.get("device_uuids"))

        if not (names or emails or phones or device_ids):
            return None

        q = Q()
        if device_ids:
            d_q = Q()
            for d in device_ids:
                d_q |= Q(device_uuid__iexact=d)
            q |= d_q

        if names:
            n_q = Q()
            for n in names:
                n_q |= Q(user_name__iexact=n)
            q |= n_q

        if emails:
            e_q = Q()
            for e in emails:
                e_q |= Q(orderdelivery__email__iexact=e)
            q |= e_q

        if phones:
            p_q = Q()
            for p in phones:
                p_q |= Q(orderdelivery__phone__iexact=p)
            q |= p_q

        return q

    def _serialize(self, orders):
        """
        Response structure per order:
        - order_id, date, status, total_price, product_ids, items
        """
        out = []
        for o in orders:
            items = []
            product_ids = []
            # Prefetched items available via cache; fallback to actual relation if needed
            prefetched = o._prefetched_objects_cache.get("orderitem_set") if hasattr(o, "_prefetched_objects_cache") else None
            iterable = prefetched if prefetched is not None else o.orderitem_set.select_related("product").only(
                "product__product_id", "quantity", "unit_price", "total_price"
            )

            for it in iterable:
                pid = it.product.product_id
                product_ids.append(pid)
                items.append({
                    "product_id": pid,
                    "quantity": it.quantity,
                    "unit_price": str(it.unit_price),
                    "total_price": str(it.total_price),
                })

            out.append({
                "order_id": o.order_id,
                "date": o.order_date.strftime('%Y-%m-%d %H:%M:%S'),
                "status": o.status,
                "total_price": float(o.total_price),
                "product_ids": product_ids,
                "items": items,
            })
        return {"orders": out}

    def _query(self, q: Q):
        return (
            Orders.objects
            .filter(q)
            .prefetch_related(
                Prefetch(
                    "orderitem_set",
                    queryset=OrderItem.objects.select_related("product").only(
                        "product__product_id", "quantity", "unit_price", "total_price"
                    )
                )
            )
            .order_by("-created_at")
        )

    def get(self, request):
        payload = {
            "user_name": request.GET.get("user_name"),
            "user_names": request.GET.get("user_names"),
            "email": request.GET.get("email"),
            "emails": request.GET.get("emails"),
            "phone": request.GET.get("phone"),
            "phones": request.GET.get("phones"),
            "device_uuid": request.GET.get("device_uuid"),
            "device_uuids": request.GET.get("device_uuids"),
        }
        q = self._build_filter(payload)
        if q is None:
            return Response(
                {"error": "Provide at least one filter: device_uuid(s), user_name(s), email(s), or phone(s)."},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response(self._serialize(self._query(q)), status=status.HTTP_200_OK)

    def post(self, request):
        try:
            data = request.data if isinstance(request.data, dict) else json.loads(request.body or "{}")
        except Exception:
            data = {}
        q = self._build_filter(data)
        if q is None:
            return Response(
                {"error": "Provide at least one filter in body: device_uuid(s), user_name(s), email(s), or phone(s)."},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response(self._serialize(self._query(q)), status=status.HTTP_200_OK)
