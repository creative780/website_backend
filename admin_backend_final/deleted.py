from contextlib import contextmanager
from typing import List, Dict, Any
from django.apps import apps
from django.db import models, transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes

from .permissions import FrontendOnlyPermission
from .models import RecentlyDeletedItem

# ---------- helpers ----------

def _model(name: str):
    """Resolve model by class __name__."""
    for m in apps.get_models():
        if m.__name__ == name:
            return m
    return None

def _recreate_instance(model, payload: dict):
    """
    Upsert row using JSON snapshot (FKs through attname, e.g. *_id).
    """
    data = {}
    pk_name = model._meta.pk.name

    for field in model._meta.concrete_fields:
        if isinstance(field, models.ForeignKey):
            key = field.attname  # product_id, subcategory_id, etc.
            if key in payload:
                data[key] = payload[key]
        else:
            key = field.name
            if key in payload:
                data[key] = payload[key]

    obj, _ = model.objects.update_or_create(**{pk_name: payload.get(pk_name)}, defaults=data)
    return obj


def _guess_display_name(record_data, table_name, record_id):
    """Human-friendly label for FE."""
    if not isinstance(record_data, dict):
        record_data = {}
    for key in ("name", "title", "slug", "code", "sku", "label"):
        val = record_data.get(key)
        if val:
            return str(val)
    return f"{table_name} #{record_id}"


# ---------- dependency rules (single source of truth) ----------
DEPENDENCY_RULES = {
    # PRODUCT TREE
    "Product": {
        "standalone": True, "parents": [],
        "corestore": [
            ("ProductSEO", "product_id", "product_id"),
            ("ProductCards", "product_id", "product_id"),
            ("ProductInventory", "product_id", "product_id"),
            ("ProductVariant", "product_id", "product_id"),
            ("ProductImage", "product_id", "product_id"),
            ("Attribute", "product_id", "product_id"),
            ("ProductSubCategoryMap", "product_id", "product_id"),
            ("ProductTestimonial", "product_id", "product_id"),
            ("ShippingInfo", "product_id", "product_id"),
        ],
    },
    "ProductVariant": {
        "standalone": False,
        "parents": [lambda rd: ("Product", rd.get("product_id"), "product_id")],
        "corestore": [("VariantCombination", "variant_id", "variant_id")],
    },
    "VariantCombination": {
        "standalone": False,
        "parents": [lambda rd: ("ProductVariant", rd.get("variant_id"), "variant_id")],
        "corestore": [],
    },
    "ProductImage": {
        "standalone": False,
        "parents": [
            lambda rd: ("Product", rd.get("product_id"), "product_id"),
            lambda rd: ("Image", rd.get("image_id"), "image_id"),  # independent; we may auto-include if present in trash
        ],
        "corestore": [],
    },
    "ProductInventory": {"standalone": False, "parents": [lambda rd: ("Product", rd.get("product_id"), "product_id")], "corestore": []},
    "ProductSEO":       {"standalone": False, "parents": [lambda rd: ("Product", rd.get("product_id"), "product_id")], "corestore": []},
    "ProductCards":     {"standalone": False, "parents": [lambda rd: ("Product", rd.get("product_id"), "product_id")], "corestore": []},
    "Attribute": {
        "standalone": False,
        "parents": [
            lambda rd: ("Product", rd.get("product_id"), "product_id"),
            (lambda rd: ("Attribute", rd.get("parent_id"), "attr_id") if rd.get("parent_id") else None),
        ],
        "corestore": [],
    },
    "ProductSubCategoryMap": {
        "standalone": False,
        "parents": [
            lambda rd: ("Product", rd.get("product_id"), "product_id"),
            lambda rd: ("SubCategory", rd.get("subcategory_id"), "subcategory_id"),
        ],
        "corestore": [],
    },
    "ProductTestimonial": {"standalone": False, "parents": [lambda rd: ("Product", rd.get("product_id"), "product_id")], "corestore": []},
    "ShippingInfo":      {"standalone": False, "parents": [lambda rd: ("Product", rd.get("product_id"), "product_id")], "corestore": []},

    # CATEGORY / SUBCATEGORY
    "Category": {
        "standalone": True, "parents": [],
        "corestore": [("CategoryImage", "category_id", "category_id"), ("CategorySubCategoryMap", "category_id", "category_id")],
    },
    "CategoryImage": {
        "standalone": False,
        "parents": [lambda rd: ("Category", rd.get("category_id"), "category_id"), lambda rd: ("Image", rd.get("image_id"), "image_id")],
        "corestore": [],
    },
    "SubCategory": {
        "standalone": True, "parents": [],
        "corestore": [
            ("SubCategoryImage", "subcategory_id", "subcategory_id"),
            ("CategorySubCategoryMap", "subcategory_id", "subcategory_id"),
            ("ProductSubCategoryMap", "subcategory_id", "subcategory_id"),
        ],
    },
    "SubCategoryImage": {
        "standalone": False,
        "parents": [lambda rd: ("SubCategory", rd.get("subcategory_id"), "subcategory_id"), lambda rd: ("Image", rd.get("image_id"), "image_id")],
        "corestore": [],
    },
    "CategorySubCategoryMap": {
        "standalone": False,
        "parents": [lambda rd: ("Category", rd.get("category_id"), "category_id"), lambda rd: ("SubCategory", rd.get("subcategory_id"), "subcategory_id")],
        "corestore": [],
    },

    # BLOG
    "BlogPost": {
        "standalone": True, "parents": [],
        "corestore": [("BlogImage", "blog_id", "blog_id"), ("BlogComment", "blog_id", "blog_id")],
    },
    "BlogImage": {
        "standalone": False,
        "parents": [lambda rd: ("BlogPost", rd.get("blog_id"), "blog_id"), lambda rd: ("Image", rd.get("image_id"), "image_id")],
        "corestore": [],
    },
    "BlogComment": {"standalone": False, "parents": [lambda rd: ("BlogPost", rd.get("blog_id"), "blog_id")], "corestore": []},

    # CAROUSELS
    "FirstCarousel":  {"standalone": True, "parents": [], "corestore": [("FirstCarouselImage", "carousel_id", "id")]},
    "FirstCarouselImage": {
        "standalone": False,
        "parents": [lambda rd: ("FirstCarousel", rd.get("carousel_id"), "id"), lambda rd: ("Image", rd.get("image_id"), "image_id"),
                    (lambda rd: ("SubCategory", rd.get("subcategory_id"), "subcategory_id") if rd.get("subcategory_id") else None)],
        "corestore": [],
    },
    "SecondCarousel": {"standalone": True, "parents": [], "corestore": [("SecondCarouselImage", "carousel_id", "id")]},
    "SecondCarouselImage": {
        "standalone": False,
        "parents": [lambda rd: ("SecondCarousel", rd.get("carousel_id"), "id"), lambda rd: ("Image", rd.get("image_id"), "image_id"),
                    (lambda rd: ("SubCategory", rd.get("subcategory_id"), "subcategory_id") if rd.get("subcategory_id") else None)],
        "corestore": [],
    },

    # IMAGES (independent)
    "Image": {"standalone": True, "parents": [], "corestore": []},

    # CARTS (independent; but notifications must be muted)
    "Cart": {"standalone": True, "parents": [], "corestore": [("CartItem", "cart_id", "cart_id")]},
    "CartItem": {"standalone": False, "parents": [lambda rd: ("Cart", rd.get("cart_id"), "cart_id")], "corestore": []},
}

def _exists_live(model_name: str, pk):
    """Check if a live row exists by pk for the given model name."""
    M = _model(model_name)
    if not M or pk in (None, "", 0):
        return False
    try:
        return M.objects.filter(pk=pk).exists()
    except Exception:
        return False

def _parents_for(item) -> List[Dict[str, Any]]:
    """Return list of required parents that are missing (neither live nor in trash)."""
    rules = DEPENDENCY_RULES.get(item.table_name, {"parents": [], "standalone": True})
    rd = item.record_data or {}
    blocked = []
    for fn in rules.get("parents", []):
        if not fn:
            continue
        try:
            tup = fn(rd)
        except Exception:
            tup = None
        if not tup:
            continue
        parent_model, parent_pk, _fkname = tup
        in_live = _exists_live(parent_model, parent_pk)
        in_trash = RecentlyDeletedItem.objects.filter(table_name=parent_model, record_id=str(parent_pk)).exists()
        if not (in_live or in_trash):
            blocked.append({"model": parent_model, "id": str(parent_pk or ""), "reason": "missing-parent"})
    return blocked

def _will_restore_with(item) -> list:
    """Children that will be co-restored when restoring this item."""
    rules = DEPENDENCY_RULES.get(item.table_name, {})
    out = []
    for (child_model, child_fk_key, parent_pk_field) in rules.get("corestore", []):
        out.append({"model": child_model, "by": child_fk_key, "parent_field": parent_pk_field})
    return out

def _parents_in_trash(node: RecentlyDeletedItem) -> list[RecentlyDeletedItem]:
    """Return direct parents of node if they exist in trash (and not already live)."""
    rules = DEPENDENCY_RULES.get(node.table_name, {})
    rd = node.record_data or {}
    out = []
    for fn in rules.get("parents", []):
        if not fn:
            continue
        try:
            tup = fn(rd)
        except Exception:
            tup = None
        if not tup:
            continue
        parent_model, parent_pk, _fkname = tup
        if not parent_pk:
            continue
        if _exists_live(parent_model, parent_pk):
            continue
        hit = RecentlyDeletedItem.objects.filter(
            table_name=parent_model, record_id=str(parent_pk)
        ).first()
        if hit:
            out.append(hit)
    return out


def _collect_corestore_closure(seed: RecentlyDeletedItem) -> List[RecentlyDeletedItem]:
    """BFS over rules.corestore edges to collect seed + all descendants."""
    def direct_children(node):
        rules = DEPENDENCY_RULES.get(node.table_name, {})
        out = []
        for (child_model, fk_key, _parent_field) in rules.get("corestore", []):
            qs = RecentlyDeletedItem.objects.filter(
                table_name=child_model,
                record_data__contains={fk_key: str(node.record_id)},
            )
            out.extend(list(qs))
        return out

    seen = {seed.id}
    queue = [seed]
    result = [seed]
    while queue:
        cur = queue.pop(0)
        for ch in direct_children(cur):
            if ch.id not in seen:
                seen.add(ch.id)
                result.append(ch)
                queue.append(ch)
    return result

def _collect_upwards_closure(seed: RecentlyDeletedItem) -> list[RecentlyDeletedItem]:
    """
    Walk up to include required parents that are in trash (recursively).
    Ensures we restore parents before children (e.g., Attribute parent before option).
    """
    seen = {seed.id}
    queue = _parents_in_trash(seed)
    result = []
    for n in queue:
        seen.add(n.id)
        result.append(n)

    while queue:
        cur = queue.pop(0)
        for p in _parents_in_trash(cur):
            if p.id not in seen:
                seen.add(p.id)
                result.append(p)
                queue.append(p)
    return result


MUTE_NOTIFY_MODELS = {"Cart", "CartItem"}

@contextmanager
def muted_notifications_for(models_to_mute: set):
    """
    Hook point to disconnect your notifier signals for specific models.
    If your notifications are created in post_save/post_delete handlers,
    disconnect them here. If they’re created in service code, use a
    threadlocal flag your notifier checks.
    """
    # Example (pseudo):
    # from .signals import notify_on_create
    # if "Cart" in models_to_mute: post_save.disconnect(notify_on_create, sender=_model("Cart"))
    # if "CartItem" in models_to_mute: post_save.disconnect(notify_on_create, sender=_model("CartItem"))
    try:
        yield
    finally:
        # Reconnect if you disconnected above.
        pass


@contextmanager
def muted_signals():
    """
    Optional: mute noisy signals during restore (e.g., your global post_delete logger).
    Wire up your own handlers here if needed.
    """
    # Example:
    # from .signals import log_any_deletion
    # post_delete.disconnect(log_any_deletion)
    try:
        yield
    finally:
        # post_delete.connect(log_any_deletion)
        pass


# ---------- endpoints ----------

@api_view(['GET'])
@permission_classes([FrontendOnlyPermission])
def show_deleted_items(request):
    """
    Returns all non-permanently deleted items with dependency metadata
    so FE can disable/guide restores.
    """
    items = RecentlyDeletedItem.objects.exclude(status="PERMANENT").order_by('-deleted_at')

    data = []
    for item in items:
        blocked_by = _parents_for(item)
        will_with = _will_restore_with(item)
        data.append({
            "id": str(item.id),
            "table": item.table_name,
            "record_id": item.record_id,
            "record_data": item.record_data,
            "status": item.status,
            "deleted_at": item.deleted_at,
            "display_name": _guess_display_name(item.record_data, item.table_name, item.record_id),
            "standalone": bool(DEPENDENCY_RULES.get(item.table_name, {}).get("standalone", True)),
            "blocked_by": blocked_by,
            "will_restore_with": will_with,
        })
    return Response(data, status=200)


@api_view(['POST'])
@permission_classes([FrontendOnlyPermission])
def recover_item(request):
    """
    Toggle visibility of trash entries only (no data restore).
    Body: { id: UUID, status: 'UNHIDE' | 'HIDE' }
    """
    item_id = request.data.get("id")
    status_flag = str(request.data.get("status", "")).upper().strip()

    if status_flag not in {"UNHIDE", "HIDE"}:
        return Response({"error": "Invalid status. Must be 'UNHIDE' or 'HIDE'."}, status=400)

    try:
        item = RecentlyDeletedItem.objects.get(pk=item_id)
    except RecentlyDeletedItem.DoesNotExist:
        return Response({"error": "Item not found."}, status=404)

    def recursive_mark(target):
        target.status = status_flag
        target.save(update_fields=["status"])
        for child in target.children.all():
            recursive_mark(child)

    recursive_mark(item)
    return Response({"success": f"Item {item_id} marked as {status_flag}."}, status=200)

class RestoreItemsAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        ids = data.get("ids") or []
        record_ids = data.get("record_ids") or []

        seeds: List[RecentlyDeletedItem] = []

        if ids:
            if isinstance(ids, (str, int)):
                ids = [ids]
            ids = [str(x).strip() for x in ids if str(x).strip()]
            seeds.extend(list(RecentlyDeletedItem.objects.filter(id__in=ids)))
        if record_ids:
            for r in record_ids:
                try:
                    t = str(r.get("table") or "").strip()
                    rid = str(r.get("id") or "").strip()
                except Exception:
                    continue
                if not t or not rid:
                    continue
                hit = RecentlyDeletedItem.objects.filter(table_name=t, record_id=rid).first()
                if hit:
                    seeds.append(hit)

        if not seeds:
            return Response({"error": "No restore targets found."}, status=status.HTTP_400_BAD_REQUEST)

        # ---------- collect closure (downwards + upwards + image parents) ----------
        all_nodes: List[RecentlyDeletedItem] = []
        for s in seeds:
            rules = DEPENDENCY_RULES.get(s.table_name, {"standalone": True})
            if not rules.get("standalone", True):
                missing = _parents_for(s)
                if missing:
                    parents_str = ", ".join([f"{b['model']}:{b['id']}" for b in missing])
                    return Response({
                        "error": "Dependent item cannot be restored alone.",
                        "blocked_by": missing,
                        "suggestion": f"Restore its parent(s) first: {parents_str}",
                    }, status=status.HTTP_409_CONFLICT)

            nodes = _collect_corestore_closure(s)
            nodes.extend(_collect_upwards_closure(s))

            # transitive upwards for everything in nodes
            extra_up = []
            for n in nodes[:]:
                for p in _collect_upwards_closure(n):
                    if p not in nodes and p not in extra_up:
                        extra_up.append(p)
            nodes.extend(extra_up)

            # image parents in trash
            extra_imgs = []
            for n in nodes[:]:
                for fn in DEPENDENCY_RULES.get(n.table_name, {}).get("parents", []):
                    if not fn:
                        continue
                    tup = fn(n.record_data or {})
                    if tup and tup[0] == "Image" and tup[1]:
                        img = RecentlyDeletedItem.objects.filter(
                            table_name="Image", record_id=str(tup[1])
                        ).first()
                        if img and img not in nodes:
                            extra_imgs.append(img)
            nodes.extend(extra_imgs)

            all_nodes.extend(nodes)

        # de-dup
        seen = set()
        uniq = []
        for n in all_nodes:
            if n.id not in seen:
                uniq.append(n)
                seen.add(n.id)
        all_nodes = uniq

        # final guard
        blockers = []
        for n in all_nodes:
            rule = DEPENDENCY_RULES.get(n.table_name, {"standalone": True})
            if not rule.get("standalone", True):
                miss = _parents_for(n)
                if miss:
                    blockers.append({"item": f"{n.table_name}:{n.record_id}", "blocked_by": miss})
        if blockers:
            return Response(
                {"error": "Restore blocked by missing parents.", "details": blockers},
                status=status.HTTP_409_CONFLICT,
            )

        # ---------- topo sort (parents before children) ----------
        # Build edges: parent -> child when parent is in all_nodes and declared via DEPENDENCY_RULES
        by_key = {(n.table_name, str(n.record_id)): n for n in all_nodes}
        indeg = {n.id: 0 for n in all_nodes}
        graph = {n.id: [] for n in all_nodes}

        for child in all_nodes:
            rules = DEPENDENCY_RULES.get(child.table_name, {})
            rd = child.record_data or {}
            for fn in rules.get("parents", []):
                if not fn:
                    continue
                tup = None
                try:
                    tup = fn(rd)
                except Exception:
                    tup = None
                if not tup:
                    continue
                pm, pp, _ = tup
                if not pm or not pp:
                    continue
                parent_node = by_key.get((pm, str(pp)))
                if parent_node:
                    graph[parent_node.id].append(child.id)
                    indeg[child.id] += 1

        # Kahn topo
        from collections import deque
        q = deque([nid for nid, d in indeg.items() if d == 0])
        ordered_ids = []
        while q:
            cur = q.popleft()
            ordered_ids.append(cur)
            for nxt in graph[cur]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    q.append(nxt)

        if len(ordered_ids) != len(all_nodes):
            # cycle fallback: sort by simple weight (fewer parents first)
            def weight(node):
                return len(DEPENDENCY_RULES.get(node.table_name, {}).get("parents", []))
            ordered = sorted(all_nodes, key=weight)
        else:
            id_to_node = {n.id: n for n in all_nodes}
            ordered = [id_to_node[i] for i in ordered_ids]

        # ---------- two-phase restore for self-FK Attribute ----------
        pending_attr_parent_links: Dict[str, str] = {}  # attr_id -> parent_id

        def payload_for(node: RecentlyDeletedItem) -> Dict[str, Any]:
            return dict((node.record_data or {}).items())

        # Mute cart/cartitem notifications if present
        will_touch_models = {n.table_name for n in ordered}
        mute_set = will_touch_models.intersection(MUTE_NOTIFY_MODELS)

        with transaction.atomic(), muted_notifications_for(mute_set):
            # Pass 1: upsert everything, but for Attribute drop parent_id now
            for n in ordered:
                M = _model(n.table_name)
                if not M:
                    transaction.set_rollback(True)
                    return Response({"error": f"Unknown model {n.table_name}"}, status=status.HTTP_400_BAD_REQUEST)

                payload = payload_for(n)

                if n.table_name == "Attribute":
                    # stash and strip parent_id to avoid FK error
                    parent_id_val = payload.get("parent_id")
                    attr_id_val = payload.get("attr_id")
                    if parent_id_val:
                        pending_attr_parent_links[str(attr_id_val)] = str(parent_id_val)
                        payload = {k: v for k, v in payload.items() if k != "parent_id"}

                try:
                    _recreate_instance(M, payload)
                except Exception as e:
                    transaction.set_rollback(True)
                    return Response(
                        {"error": f"Failed to restore {n.table_name}#{n.record_id}: {e}"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            # Pass 2: patch Attribute.parent_id now that all Attributes exist
            if pending_attr_parent_links:
                Attr = _model("Attribute")
                for attr_id, parent_id in pending_attr_parent_links.items():
                    try:
                        # parent is guaranteed to exist from pass 1
                        Attr.objects.filter(pk=attr_id).update(parent_id=parent_id)
                    except Exception as e:
                        transaction.set_rollback(True)
                        return Response(
                            {"error": f"Failed to link Attribute parent for {attr_id} -> {parent_id}: {e}"},
                            status=status.HTTP_400_BAD_REQUEST,
                        )

            # Finally: remove trash entries (children last by reversing topo)
            ordered_rev = list(reversed(ordered))
            for n in ordered_rev:
                try:
                    n.delete()
                except Exception:
                    # non-fatal; keep going
                    pass

        return Response(
            {
                "success": True,
                "restored": [f"{n.table_name}:{n.record_id}" for n in ordered],
                "restored_count": len(ordered),
                "notifications_muted_for": list(mute_set) if mute_set else [],
            },
            status=status.HTTP_200_OK,
        )


@api_view(['POST'])
@permission_classes([FrontendOnlyPermission])
def permanently_delete_item(request):
    """
    Permanently removes a trash item and its nested children from the trash store.
    Original domain rows are already gone.
    Body: { id: UUID }
    """
    item_id = request.data.get("id")
    if not item_id:
        return Response({"error": "Missing id"}, status=400)

    try:
        item = RecentlyDeletedItem.objects.get(pk=item_id)
    except RecentlyDeletedItem.DoesNotExist:
        return Response({"error": "Item not found."}, status=404)

    def recursive_delete(target):
        for child in target.children.all():
            recursive_delete(child)
        target.delete()

    with transaction.atomic():
        recursive_delete(item)

    return Response({"success": f"Item {item_id} and its dependencies permanently deleted."}, status=200)
