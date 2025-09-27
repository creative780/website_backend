# Back_End/admin_backend_final/chat.py
from __future__ import annotations
import os, json, uuid, math
from collections import Counter
from typing import Dict, List, Tuple, Optional

from django.http import JsonResponse, HttpRequest
from django.utils import timezone
from django.core.cache import cache
from django.db.models import Q
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from rest_framework import status, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.renderers import JSONRenderer

from .permissions import FrontendOnlyPermission

try:
    import groq  # pip install groq
except Exception:
    groq = None

# Your models
from .models import (
    Category, ProductInventory, ProductVariant, ShippingInfo, SubCategory, Product,
    ProductSubCategoryMap, CategorySubCategoryMap, VariantCombination
)

# ---------- LLM (Groq) & LangChain ----------
# pip install langchain langchain-groq
from langchain_groq import ChatGroq
from langchain.schema import SystemMessage, HumanMessage
from langchain.agents import Tool
from langchain.agents import initialize_agent, AgentType
from dotenv import load_dotenv
load_dotenv()  # <-- add this early

# Read from env first. You can override in .env:
# GROQ_MODEL=llama-3.3-70b-versatile
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
PRIMARY_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

# A ranked fallback list. You can add/remove models here as Groq updates offerings.
MODEL_CANDIDATES = [
    PRIMARY_MODEL,                       # env override wins
    "llama-3.3-70b-versatile",          # Groq’s current recommendation
    "llama-3.1-8b-instant",             # cheaper/faster fallback
    "qwen/qwen3-32b",                   # strong general model
    "deepseek-r1-distill-llama-70b",    # reasoning-oriented
]

def _llm_available() -> bool:
    return bool(GROQ_API_KEY and GROQ_API_KEY.strip())

def _new_chatgroq(model: str) -> ChatGroq:
    return ChatGroq(
        groq_api_key=GROQ_API_KEY,
        model_name=model,
        temperature=0.2,
    )

def _call_llm(messages: list) -> str:
    """
    Try models in MODEL_CANDIDATES one by one. If a model is decommissioned or errors,
    fall through to the next. If no key or all fail, raise RuntimeError.
    """
    if not _llm_available():
        raise RuntimeError("GROQ_API_KEY not configured")

    last_err = None
    for m in MODEL_CANDIDATES:
        try:
            llm = _new_chatgroq(m)
            return llm.invoke(messages).content
        except Exception as e:
            # If Groq returns 400 model_decommissioned, try next model
            if groq and isinstance(e, groq.BadRequestError):
                last_err = e
                continue
            last_err = e
            continue
    # If we got here, every model failed
    raise RuntimeError(f"All Groq models failed. Last error: {last_err}")

# ================== Cache helpers ==================
CACHE_NS = "cc_chat_langchain_v1"
DEFAULT_TTL = 60 * 60 * 24 * 30
_INPROC: Dict[str, dict] = {}

def _cget(k, default=None):
    try:
        v = cache.get(k)
        if v is not None: return v
    except Exception:
        pass
    return _INPROC.get(k, default)

def _cset(k, v, ttl=DEFAULT_TTL):
    try:
        cache.set(k, v, ttl)
    except Exception:
        _INPROC[k] = v

def _k(cid: str) -> str:
    return f"{CACHE_NS}:{cid}"

def _new_id() -> str:
    return str(uuid.uuid4())

def _now_iso() -> str:
    return timezone.now().isoformat()

# ================== Tiny text utils (no regex) ==================
_PUNC = dict.fromkeys(map(ord, '.,;:!?"“”’\'`()[]{}<>|@#$%^&*_+=~\\/'), None)
def _normalize(s: str) -> str:
    return (s or "").strip()

def _lower_clean(s: str) -> str:
    return _normalize(s).lower().translate(_PUNC)

def _tokens(s: str) -> List[str]:
    return [t for t in _lower_clean(s).split() if t]

def _char_ngrams(s: str, n: int = 3) -> Counter:
    s2 = _lower_clean(s)
    grams: List[str] = []
    L = len(s2)
    if L == 0: return Counter()
    if L < n: return Counter([s2])
    for i in range(L - n + 1):
        grams.append(s2[i:i+n])
    return Counter(grams)

def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b: return 0.0
    dot = 0.0
    for k, va in a.items():
        vb = b.get(k)
        if vb: dot += va * vb
    na = math.sqrt(sum(v*v for v in a.values()))
    nb = math.sqrt(sum(v*v for v in b.values()))
    if na == 0 or nb == 0: return 0.0
    return dot / (na * nb)

# ================== Memory ==================
class ChatTurn:
    def __init__(self, role: str, text: str, time: str):
        self.role = role
        self.text = text
        self.time = time

class Persona:
    def __init__(self):
        self.name: Optional[str] = None
        self.address: Optional[str] = None
        self.wants: Optional[str] = None
        self.price_range_aed: Optional[Tuple[float, float]] = None

class State:
    def __init__(self, cid: str):
        self.conversation_id = cid
        self.turns: List[ChatTurn] = []
        self.summary: str = ""   # ≤ 5 lines
        self.persona = Persona()

def _load_state(cid: Optional[str]) -> State:
    if not cid: cid = _new_id()
    blob = _cget(_k(cid))
    if not blob:
        st = State(cid)
        _cset(_k(cid), _dump_state(st))
        return st
    return _undump_state(blob)

def _save_state(st: State):
    _cset(_k(st.conversation_id), _dump_state(st))

def _dump_state(st: State) -> dict:
    return {
        "conversation_id": st.conversation_id,
        "turns": [{"role": t.role, "text": t.text, "time": t.time} for t in st.turns],
        "summary": st.summary,
        "persona": {
            "name": st.persona.name,
            "address": st.persona.address,
            "wants": st.persona.wants,
            "price_range_aed": st.persona.price_range_aed,
        }
    }

def _undump_state(d: dict) -> State:
    st = State(d.get("conversation_id"))
    for t in d.get("turns", []):
        st.turns.append(ChatTurn(t["role"], t["text"], t["time"]))
    st.summary = d.get("summary", "")
    p = d.get("persona", {}) or {}
    st.persona.name = p.get("name")
    st.persona.address = p.get("address")
    st.persona.wants = p.get("wants")
    pr = p.get("price_range_aed")
    st.persona.price_range_aed = tuple(pr) if pr else None
    return st

def _append_turn(st: State, role: str, text: str):
    st.turns.append(ChatTurn(role, _normalize(text), _now_iso()))
    if len(st.turns) > 200:
        older = st.turns[:-20]
        st.turns = st.turns[-20:]
        # compress older → keep last 5 line snippets (no regex)
        lines: List[str] = []
        for t in older[-50:]:
            s = _normalize(t.text)
            # naive first-sentence cut
            cut = len(s)
            for ch in ".!?":
                i = s.find(ch)
                if i != -1: cut = min(cut, i+1)
            s1 = s[:min(cut, 120)]
            if s1:
                lines.append(("U: " if t.role == "user" else "B: ") + s1)
        st.summary = "\n".join((st.summary+"\n"+("\n".join(lines))).strip().splitlines()[-5:])

# ================== Taxonomy index (DB-driven) ==================
class LexItem:
    def __init__(self, kind: str, key: str, text: str):
        self.kind = kind
        self.key = key
        self.text = text
        self.vec = _char_ngrams(text)

def _load_lexicon() -> List[LexItem]:
    ck = f"{CACHE_NS}:lex"
    cached = _cget(ck)
    if cached:
        out: List[LexItem] = []
        for row in cached:
            li = LexItem(row["kind"], row["key"], row["text"])
            li.vec = Counter(row["vec"])
            out.append(li)
        return out

    items: List[LexItem] = []
    for c in Category.objects.all().values("category_id", "name"):
        items.append(LexItem("category", c["category_id"], c.get("name") or ""))
    for s in SubCategory.objects.all().values("subcategory_id", "name"):
        items.append(LexItem("subcategory", s["subcategory_id"], s.get("name") or ""))
    for p in Product.objects.all().values("product_id", "title"):
        items.append(LexItem("product", p["product_id"], p.get("title") or ""))

    serial = [{"kind": i.kind, "key": i.key, "text": i.text, "vec": dict(i.vec)} for i in items]
    _cset(ck, serial, ttl=300)  # cache for 5 minutes (was 60s)
    return items

def _nearest_terms(query: str, k: int = 5) -> List[LexItem]:
    qv = _char_ngrams(query)
    if not qv: return []
    best: List[Tuple[float, LexItem]] = []
    for it in _load_lexicon():
        sc = _cosine(qv, it.vec)
        if sc > 0:
            best.append((sc, it))
    best.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in best[:k]]

# ================== Tools (deterministic) ==================
def tool_clock(_: str = "") -> str:
    dt = timezone.localtime()
    tod = "morning" if 5 <= dt.hour < 12 else ("afternoon" if 12 <= dt.hour < 18 else "evening")
    return f"Good {tod}! The current date & time is {dt.strftime('%Y-%m-%d %H:%M')}."

def _safe_eval_arith(s: str) -> Optional[str]:
    s2 = _lower_clean(s)
    if any(ch.isalpha() for ch in s2):
        return None
    for ch in s2:
        if not (ch.isdigit() or ch in " +-*/()."):
            return None
    try:
        val = eval(s2, {"__builtins__": {}}, {})
        return str(val)
    except Exception:
        return None

def _parse_linear_x(s: str) -> Optional[str]:
    expr = _lower_clean(s).replace(" ", "")
    if "x" not in expr or "=" not in expr:
        return None
    parts = expr.split("=")
    if len(parts) != 2: return None
    left, right = parts[0], parts[1]
    xi = left.find("x")
    if xi == -1: return None
    coef_str = left[:xi] or "1"
    try:
        a = float(coef_str)
        c = float(right)
    except Exception:
        return None
    rest = left[xi+1:]  # e.g., +7 / -5 / ''
    if not rest:
        if a == 0: return "No unique solution (a=0)."
        return f"x = {c / a}"
    sign = rest[0]
    try:
        b = float(rest[1:]) if len(rest) > 1 else 0.0
    except Exception:
        return None
    if sign == "+": rhs = c - b
    elif sign == "-": rhs = c + b
    else: return None
    if a == 0: return "No unique solution (a=0)."
    return f"x = {rhs / a}"

def tool_calculator(expr: str) -> str:
    linear = _parse_linear_x(expr)
    if linear: return linear
    val = _safe_eval_arith(expr)
    if val is not None: return val
    return "I can help with basic arithmetics just to calculate your budget."

def _extract_budget(s: str) -> Tuple[Optional[float], Optional[float]]:
    s2 = _lower_clean(s)
    nums: List[float] = []
    cur = ""
    for ch in s2:
        if ch.isdigit() or ch == ".":
            cur += ch
        else:
            if cur:
                try: nums.append(float(cur))
                except Exception: pass
                cur = ""
    if cur:
        try: nums.append(float(cur))
        except Exception: pass
    if not nums: return None, None
    has_between = ("between" in s2) or ("from" in s2) or ("to" in s2) or ("-" in s2)
    has_under = ("under" in s2) or ("below" in s2) or ("upto" in s2) or ("up to" in s2) or ("max" in s2) or ("<=" in s2) or ("<" in s2)
    if has_between and len(nums) >= 2:
        a, b = nums[0], nums[1]
        return (a if a <= b else b), (b if b >= a else a)
    if has_under:
        return None, max(nums)
    return None, max(nums)

def _build_product_qs(query_text: str, pmin: Optional[float], pmax: Optional[float]):
    near = _nearest_terms(query_text, k=5)
    qs = Product.objects.all()

    sub_ids = [t.key for t in near if t.kind == "subcategory"]
    if sub_ids:
        prod_ids = ProductSubCategoryMap.objects.filter(
            subcategory_id__in=sub_ids
        ).values_list("product_id", flat=True)
        qs = qs.filter(product_id__in=list(prod_ids))
    else:
        cat_ids = [t.key for t in near if t.kind == "category"]
        if cat_ids:
            # subcats under those categories
            sub_ids = SubCategory.objects.filter(
                categorysubcategorymap__category_id__in=cat_ids
            ).values_list("subcategory_id", flat=True)
            prod_ids = ProductSubCategoryMap.objects.filter(
                subcategory_id__in=list(sub_ids)
            ).values_list("product_id", flat=True)
            qs = qs.filter(product_id__in=list(prod_ids))
        else:
            prod_keys = [t.key for t in near if t.kind == "product"]
            if prod_keys:
                qs = qs.filter(product_id__in=prod_keys)

    # ★ ONLY THROUGH VISIBLE CATEGORY & SUBCATEGORY
    qs = qs.filter(
        productsubcategorymap__subcategory__status="visible",
        productsubcategorymap__subcategory__categorysubcategorymap__category__status="visible",
    ).distinct()

    if pmin is not None:
        qs = qs.filter(price__gte=pmin)
    if pmax is not None:
        qs = qs.filter(price__lte=pmax)

    if hasattr(Product, "order"):
        qs = qs.order_by("order")
    else:
        qs = qs.order_by("title")
    return qs


def tool_ecommerce(query_text: str) -> str:
    """
    Returns JSON with:
      - text: short summary
      - categories: [{name, description}]
      - subcategories: [{name, description}]
      - items: products [{
            name, processing_time, printing_methods, sizes, color_variants,
            price, stock_status
      }]
    Only through visible category/subcategory paths.
    Price selection:
      effective_price = min(
         all VariantCombination.price_override for this product,
         discounted_price (if set),
         price
      )
    """
    from decimal import Decimal, InvalidOperation

    def _to_decimal(val):
        try:
            if val is None or str(val).strip() == "":
                return None
            return Decimal(str(val))
        except (InvalidOperation, ValueError, TypeError):
            return None

    pmin, pmax = _extract_budget(query_text)
    qs = _build_product_qs(query_text, pmin, pmax)[:30]

    # --- Visible categories
    visible_cats_qs = Category.objects.filter(status="visible").order_by("order")
    categories = [{"name": c.name or "", "description": getattr(c, "description", "") or ""} for c in visible_cats_qs]

    # --- Visible subcategories
    visible_subs_qs = SubCategory.objects.filter(
        status="visible",
        categorysubcategorymap__category__status="visible"
    ).distinct().order_by("order")
    subcategories = [{"name": s.name or "", "description": getattr(s, "description", "") or ""} for s in visible_subs_qs]

    # Prefetch maps
    product_ids = list(qs.values_list("product_id", flat=True))

    # product_id -> [variants]
    variants_by_product = {}
    for v in ProductVariant.objects.filter(product_id__in=product_ids):
        variants_by_product.setdefault(v.product_id, []).append(v)

    # product_id -> ShippingInfo
    shipping_by_product = {s.product_id: s for s in ShippingInfo.objects.filter(product_id__in=product_ids)}

    # product_id -> ProductInventory
    inventory_by_product = {inv.product_id: inv for inv in ProductInventory.objects.filter(product_id__in=product_ids)}

    # Gather overrides
    all_variant_ids = []
    for vlist in variants_by_product.values():
        for v in vlist:
            all_variant_ids.append(v.variant_id)

    overrides_by_product = {pid: [] for pid in product_ids}
    if all_variant_ids:
        # map variant -> product
        variant_to_product = {}
        for pid, vlist in variants_by_product.items():
            for v in vlist:
                variant_to_product[v.variant_id] = pid

        for combo in VariantCombination.objects.filter(variant_id__in=all_variant_ids):
            pid = variant_to_product.get(combo.variant_id)
            if pid:
                ov = _to_decimal(combo.price_override)
                if ov is not None:
                    overrides_by_product[pid].append(ov)

    # Build items
    items = []
    for p in qs:
        pid = p.product_id

        sizes = set()
        colors = set()
        printing_set = set()
        for v in variants_by_product.get(pid, []):
            if v.size:
                sizes.add(v.size)
            if v.color:
                colors.add(v.color)
            if isinstance(v.printing_methods, list):
                for pm in v.printing_methods:
                    if pm:
                        printing_set.add(str(pm))

        ship = shipping_by_product.get(pid)
        processing_time = ship.processing_time if ship else ""

        # price candidates
        base_price = _to_decimal(getattr(p, "price", None))
        disc_price = _to_decimal(getattr(p, "discounted_price", None))
        candidates = []
        if base_price is not None:
            candidates.append(base_price)
        if disc_price is not None:
            candidates.append(disc_price)
        for ov in overrides_by_product.get(pid, []):
            candidates.append(ov)

        effective_price = None
        if candidates:
            # choose min of valid decimals
            valid = [c for c in candidates if isinstance(c, Decimal)]
            if valid:
                effective_price = min(valid)

        inv = inventory_by_product.get(pid)
        stock_status = getattr(inv, "stock_status", "") if inv else ""

        items.append({
            "name": p.title or "",
            "processing_time": processing_time or "",
            "printing_methods": sorted(list(printing_set)),
            "sizes": sorted(list(sizes)),
            "color_variants": sorted(list(colors)),
            "price": str(effective_price) if effective_price is not None else None,
            "stock_status": stock_status or None,
        })

    text = "Here are some products you might like" if items else \
           "No matching visible products. Try different terms or a broader budget."

    return json.dumps({
        "text": text,
        "categories": categories,
        "subcategories": subcategories,
        "items": items
    }, default=str)

# Expose tools to the agent
TOOLS: List[Tool] = [
    Tool(
        name="clock",
        func=lambda q: tool_clock(q),
        description="Use to answer questions about the current date and time. Input can be empty."
    ),
    Tool(
        name="calculator",
        func=lambda q: tool_calculator(q),
        description="Use for basic arithmetic or simple linear equation like '2x+7=15'. Input is the math expression."
    ),
    Tool(
        name="ecommerce_search",
        func=lambda q: tool_ecommerce(q),
        description="Use to find products/categories/subcategories and filter by budget. Input is the user's request text."
    ),
]

def _llm() -> ChatGroq:
    """
    Build a ChatGroq with a non-decommissioned model.
    We try MODEL_CANDIDATES in order and return the first that initializes successfully.
    """
    if not _llm_available():
        # Let downstream code fall back to deterministic tools
        # but we still raise here if something tries to instantiate the agent without a key
        raise RuntimeError("GROQ_API_KEY not configured")

    last_err = None
    for m in MODEL_CANDIDATES:
        try:
            # Just return the first that constructs cleanly
            return _new_chatgroq(m)
        except Exception as e:
            last_err = e
            continue
    # If none construct, raise — the agent layer will catch and fall back
    raise RuntimeError(f"Failed to initialize ChatGroq. Last error: {last_err}")

def _parse_json(text: str, fallback: dict) -> dict:
    try:
        return json.loads(text)
    except Exception:
        # try to find a json block by naive scan
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except Exception:
                pass
    return fallback

# ================== Chains (Groq-driven) ==================
def llm_intent_and_focus(user_text: str, st: State) -> dict:
    """
    Multilingual, LLM-driven intent classifier.
    Returns ONLY:
      - intent ∈ {greetings, datetime, math, ecommerce, irrelevant}
      - focus  (1–2 words, e.g., "greeting", "time", "budget", "category")
      - relevant: bool
      - price_min: float or null
      - price_max: float or null
    """
    s = _normalize(user_text)

    if not _llm_available():
        # Deterministic (non-LLM) fallback — conservative:
        sl = s.lower()
        # keep lightweight routes for speed, but no greeting heuristics here
        if any(w in sl for w in ["time", "date", "today", "now", ":"]) and len(sl.split()) <= 6:
            return {"intent": "datetime", "focus": "time", "relevant": True, "price_min": None, "price_max": None}
        if any(ch in sl for ch in "+-*/=") and any(c.isdigit() for c in sl):
            return {"intent": "math", "focus": "math", "relevant": True, "price_min": None, "price_max": None}
        # default to ecommerce if we can't be sure
        return {"intent": "ecommerce", "focus": "start", "relevant": True, "price_min": None, "price_max": None}

    try:
        sys = SystemMessage(content=(
            "You are an intent classifier for a shopping assistant. Users may speak ANY language.\n"
            "Decide the user's intent and a short focus (1-2 words). Extract price hints if present.\n"
            "ALLOWED intents: greetings, datetime, math, ecommerce, irrelevant.\n"
            "Output ONLY JSON with keys: intent, focus, relevant, price_min, price_max.\n"
            "Guidelines:\n"
            "- greetings: salutations/openers in any language (e.g., 'hola', 'bonjour', 'salam', 'ciao', 'नमस्ते').\n"
            "- datetime: asking for current time/date or similar.\n"
            "- math: arithmetic or simple equation solving requests.\n"
            "- ecommerce: product/category/budget/quantity/quote/delivery queries.\n"
            "- irrelevant: off-topic or unsafe.\n"
            "Price extraction:\n"
            "- price_min/price_max: detect ranges like 'between 50 and 100', caps like 'under 200', currencies ignored.\n"
            "- If no price mentioned, return null.\n"
            "Focus examples:\n"
            "- greetings→ 'greeting'\n"
            "- datetime→ 'time' or 'date'\n"
            "- math→ 'calculation' or 'equation'\n"
            "- ecommerce→ 'budget', 'category', 'quote', 'delivery', etc.\n"
            "Be concise and robust to short or emoji messages."
        ))

        hum = HumanMessage(content=f"User message: {s}")

        out = _call_llm([sys, hum])
        data = _parse_json(out, {"intent": "irrelevant", "focus": "chat", "relevant": False, "price_min": None, "price_max": None})

        # sanitize
        allowed = {"greetings", "datetime", "math", "ecommerce", "irrelevant"}
        if data.get("intent") not in allowed:
            data["intent"] = "irrelevant"
        data["focus"] = (data.get("focus") or "chat")[:20]
        data["relevant"] = bool(data.get("relevant", False))

        # normalize price fields
        def _to_float_or_none(v):
            try:
                return float(v) if v is not None else None
            except Exception:
                return None
        data["price_min"] = _to_float_or_none(data.get("price_min"))
        data["price_max"] = _to_float_or_none(data.get("price_max"))

        # if min/max both present but reversed, fix ordering
        pmn, pmx = data["price_min"], data["price_max"]
        if pmn is not None and pmx is not None and pmn > pmx:
            data["price_min"], data["price_max"] = pmx, pmn

        return data

    except Exception:
        # Safe fallback if LLM errors
        return {"intent": "ecommerce", "focus": "start", "relevant": True, "price_min": None, "price_max": None}

def llm_greeting_and_openers(st: State) -> dict:
    # deterministic fallback if no key
    if not _llm_available():
        dt = timezone.localtime()
        tod = "morning" if 5 <= dt.hour < 12 else ("afternoon" if 12 <= dt.hour < 18 else "evening")
        top_cats = list(Category.objects.all().order_by("order").values_list("name", flat=True)[:3])
        g = f"Good {tod}! I'm CreativeAI."
        defaults = ["Tell me what you need and budget.", "Share your delivery address for delivery options."]
        if top_cats:
            defaults[0] = f"You can browse by categories like {', '.join(top_cats)}."
        return {"greeting": g, "openers": defaults[:2]}

    try:
        top_cats = list(Category.objects.all().order_by("order").values_list("name", flat=True)[:3])
        sys = SystemMessage(content=(
            "You are CreativeAI, a concise, helpful e-commerce assistant.\n"
            "Generate a short friendly greeting and one helpful opening messages.\n"
            "Return ONLY JSON with keys: greeting, openers (array of 1 string)."
        ))
        hum = HumanMessage(content=json.dumps({
            "time": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
            "top_categories": top_cats,
            "persona_known": {
                "name": st.persona.name, "address": bool(st.persona.address),
                "wants": bool(st.persona.wants), "budget": bool(st.persona.price_range_aed),
            }
        }))
        out = _call_llm([sys, hum])
        data = _parse_json(out, {"greeting":"Hello!","openers":[]})
        ops = data.get("openers") or []
        defaults = ["Tell me what you need and budget.", "Share your delivery address for delivery options."]
        if top_cats:
            defaults[0] = f"You can browse by categories like {', '.join(top_cats)}."
        data["openers"] = (ops + defaults)[:2]
        return data
    except Exception:
        dt = timezone.localtime()
        tod = "morning" if 5 <= dt.hour < 12 else ("afternoon" if 12 <= dt.hour < 18 else "evening")
        return {"greeting": f"Good {tod}! I'm CreativeAI.",
                "openers": ["Tell me what you need and budget.", "Share your delivery address for delivery options."]}

def llm_persona_extract(user_text: str, st: State) -> Persona:
    if not _llm_available():
        return st.persona
    try:
        sys = SystemMessage(content=(
            "Extract persona info from the user message for an e-commerce assistant.\n"
            "Return ONLY JSON with keys: name, address, wants, price_min, price_max. Values may be null. Never return Ids of anything"
        ))
        hum = HumanMessage(content=user_text)
        out = _call_llm([sys, hum])
        data = _parse_json(out, {"name":None,"address":None,"wants":None,"price_min":None,"price_max":None})
        if data.get("name"): st.persona.name = str(data["name"])[:30]
        if data.get("address"): st.persona.address = str(data["address"])[:120]
        if data.get("wants"): st.persona.wants = str(data["wants"])[:120]
        pmin = data.get("price_min"); pmax = data.get("price_max")
        try:
            if pmin is not None or pmax is not None:
                st.persona.price_range_aed = (float(pmin or 0.0), float(pmax or (pmin or 0.0)))
        except Exception:
            pass
        return st.persona
    except Exception:
        return st.persona

def llm_next_prompts(st: State) -> List[str]:
    if not _llm_available():
        base = []
        if not st.persona.address: base.append("My delivery address is Sharja, Dubai and I want to buy some Writing Instruments Product.")
        if not st.persona.wants: base.append("I am looking for Some Business Cards under 1000 AED")
        if not st.persona.price_range_aed: base.append("I want to buy a product as quick as possible under 2 days.")
        while len(base) < 2:
            base.append("Would you like a quick quote if you share quantity and logo?")
        return base[:2]

    try:
        last_users = [t.text for t in st.turns if t.role == "user"][-10:]
        sys = SystemMessage(content=(
            "Suggest the next two helpful prompts that a user can give for an e-commerce conversation.\n"
            "Consider the user's recent questions and any missing persona info (address, wants, budget).\n"
            "Return ONLY JSON with key: prompts (array of 2 strings). \n"
            "Example of Prompt 1: I want to buy writing instruments. \n"
            "Example of Prompt 2: Show me premium categories.\n"
            "Example of Prompt 3: What are the best products available?"
        ))
        hum = HumanMessage(content=json.dumps({
            "recent_user_messages": last_users,
            "persona": {
                "name": st.persona.name,
                "address": st.persona.address,
                "wants": st.persona.wants,
                "price_range_aed": st.persona.price_range_aed
            }
        }))
        out = _call_llm([sys, hum])
        data = _parse_json(out, {"prompts":[]})
        prompts = data.get("prompts") or []
        while len(prompts) < 2:
            prompts.append("Would you like a quick quote if you share quantity and logo?")
        outp = []
        for p in prompts:
            if p not in outp:
                outp.append(p)
            if len(outp) == 2:
                break
        return outp
    except Exception:
        return [
            "Would you like a quick quote if you share quantity and logo?",
            "Do you have a target budget in AED?",
        ]

# ================== Agent (LLM + tools) ==================
def _agent() -> any:
    # Keep tools but reduce chain depth for speed
    return initialize_agent(
        tools=TOOLS,
        llm=_llm(),
        agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
        verbose=False,
        handle_parsing_errors=True,
        max_iterations=2  # reduced from 4 for latency
    )

def run_agent_with_tools(user_text: str) -> Tuple[str, List[dict]]:
    """
    Let the agent decide which tool(s) to use.
    We then try to pull items from ecommerce tool JSON if present.
    """
    agent = _agent()
    result = agent.run(user_text)  # final string from LLM
    # try to extract embedded JSON from ecommerce tool (if the agent inlined it)
    items: List[dict] = []
    parsed = _parse_json(result, {})
    if isinstance(parsed, dict) and "items" in parsed and "text" in parsed:
        try:
            items = parsed.get("items") or []
            result = parsed.get("text") or result
        except Exception:
            pass
    return str(result), items

# ================== Public endpoints ==================
@method_decorator(csrf_exempt, name='dispatch')
class UserResponseAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    authentication_classes = []  # mirrors csrf_exempt behavior
    renderer_classes = [JSONRenderer]

    def post(self, request):
        try:
            body = json.loads(request.body.decode("utf-8"))
        except Exception:
            return Response({"detail": "Invalid JSON"}, status=status.HTTP_400_BAD_REQUEST)

        msg = _normalize(body.get("message", ""))
        cid = body.get("conversation_id") or None
        st = _load_state(cid)

        # LLM intent
        intent_info = llm_intent_and_focus(msg, st) if msg else {
            "intent": "ecommerce", "focus": "start", "relevant": True, "price_min": None, "price_max": None
        }

        # Bot greeting + openers
        greet = llm_greeting_and_openers(st)
        greeting = greet.get("greeting", "Hello!")
        openers = greet.get("openers", [])[:3]

        if msg and not intent_info.get("relevant", False):
            return Response({
                "conversation_id": st.conversation_id,
                "focus": intent_info.get("focus", "chat"),
                "intent": "irrelevant",
                "relevant": False,
                "greeting": greeting,
                "bot_openers": openers,
                "bot_gate": "I can't answer such type of question"
            }, status=status.HTTP_200_OK)

        # relevant → keep memory
        if msg:
            _append_turn(st, "user", msg)
            llm_persona_extract(msg, st)
            _save_state(st)

        mem_flags = []
        if st.persona.name: mem_flags.append("name")
        if st.persona.address: mem_flags.append("address")
        if st.persona.wants: mem_flags.append("wants")
        if st.persona.price_range_aed: mem_flags.append("budget")

        return Response({
            "conversation_id": st.conversation_id,
            "focus": intent_info.get("focus", "start"),
            "intent": intent_info.get("intent", "ecommerce"),
            "relevant": True,
            "greeting": greeting,
            "bot_openers": openers,
            "memory_hint": ", ".join(mem_flags) if mem_flags else None
        }, status=status.HTTP_200_OK)

    # keep exact 405 body shape used before
    def get(self, request):
        return Response({"detail": "Method not allowed"}, status=status.HTTP_405_METHOD_NOT_ALLOWED)


@method_decorator(csrf_exempt, name='dispatch')
class BotResponseAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    authentication_classes = []  # mirrors csrf_exempt behavior
    renderer_classes = [JSONRenderer]

    def post(self, request):
        try:
            body = json.loads(request.body.decode("utf-8"))
        except Exception:
            return Response({"detail": "Invalid JSON"}, status=status.HTTP_400_BAD_REQUEST)

        msg = _normalize(body.get("message", ""))
        cid = body.get("conversation_id") or None
        st = _load_state(cid)

        if not msg:
            return Response({"detail": "Message required"}, status=status.HTTP_400_BAD_REQUEST)

        # quick intent
        intent_info = llm_intent_and_focus(msg, st)
        if not intent_info.get("relevant", False):
            return Response({
                "conversation_id": st.conversation_id,
                "intent": "irrelevant",
                "bot_text": "I can't answer such type of question",
                "batch_count": 0,
                "items": []
            }, status=status.HTTP_200_OK)

        # store user + persona
        _append_turn(st, "user", msg)
        llm_persona_extract(msg, st)

        kind = intent_info.get("intent")

        def _clean_list(xs):
            return [str(x).strip() for x in (xs or []) if str(x).strip()]

        def _price_ok(p):
            if p is None:
                return False
            s = str(p).strip()
            if s in ("", "None"):
                return False
            try:
                return float(s) > 0.0
            except Exception:
                return True

        def _format_blurbs(items_list):
            lines = []
            for itm in items_list[:5]:
                name = (itm.get("name") or "").strip()
                if not name:
                    continue

                fields = []
                price = itm.get("price")
                if _price_ok(price):
                    fields.append(f'Price: "{price}"')

                sizes = ", ".join(_clean_list(itm.get("sizes")))
                if sizes:
                    fields.append(f'Sizes Available: "{sizes}"')

                stock_status = (itm.get("stock_status") or "").strip()
                if stock_status:
                    fields.append(f'Stock: "{stock_status}"')

                if fields:
                    lines.append(f'Product: {name} ({", ".join(fields)})')
                else:
                    lines.append(f'Product: {name}')
            return "\n".join(lines) if lines else "No product matches your request. Do you want to try some other products?"

        # Fast routes
        try:
            if kind == "greetings":
                g = llm_greeting_and_openers(st)
                openers = (g.get("openers") or [])[:2]
                bot_text = g.get("greeting", "Hello!")
                if openers:
                    bot_text += "\n\n" + "\n".join(f"- {o}" for o in openers)
                items = []
                extra_categories = []
                extra_subcategories = []
            elif kind == "datetime":
                bot_text = tool_clock()
                items = []
                extra_categories = []
                extra_subcategories = []
            elif kind == "math":
                bot_text = tool_calculator(msg)
                items = []
                extra_categories = []
                extra_subcategories = []
            else:
                resp = json.loads(tool_ecommerce(msg))
                items = resp.get("items", [])
                extra_categories = resp.get("categories", [])
                extra_subcategories = resp.get("subcategories", [])
                bot_text = _format_blurbs(items)
            if not bot_text or bot_text.strip() == "":
                raise ValueError("empty deterministic output")
        except Exception:
            # Agent fallback
            try:
                bot_text, items = run_agent_with_tools(msg)
                extra_categories = []
                extra_subcategories = []
                if kind == "ecommerce":
                    bot_text = _format_blurbs(items)
                if not bot_text or bot_text.strip() == "":
                    raise ValueError("empty agent output")
            except Exception:
                bot_text = "Sorry—I'm having trouble right now. Please try rephrasing your request."
                items = []
                extra_categories = []
                extra_subcategories = []

        _append_turn(st, "bot", bot_text)
        _save_state(st)

        return Response({
            "conversation_id": st.conversation_id,
            "intent": kind or "ecommerce",
            "bot_text": bot_text,
            "batch_count": math.ceil(len(items)/30) if items else 0,
            "items": items,
            "categories": extra_categories,
            "subcategories": extra_subcategories
        }, status=status.HTTP_200_OK)

    def get(self, request):
        return Response({"detail": "Method not allowed"}, status=status.HTTP_405_METHOD_NOT_ALLOWED)


@method_decorator(csrf_exempt, name='dispatch')
class BotPromptsAPIView(APIView):
    permission_classes = [FrontendOnlyPermission]
    authentication_classes = []  # mirrors csrf_exempt behavior
    renderer_classes = [JSONRenderer]

    def get(self, request):
        cid = request.GET.get("conversation_id") or None
        st = _load_state(cid)
        prompts = llm_next_prompts(st)
        return Response({"prompts": prompts}, status=status.HTTP_200_OK)

    def post(self, request):
        cid = None
        try:
            body = json.loads(request.body.decode("utf-8"))
            cid = body.get("conversation_id") or None
        except Exception:
            pass
        st = _load_state(cid)
        prompts = llm_next_prompts(st)
        return Response({"prompts": prompts}, status=status.HTTP_200_OK)
