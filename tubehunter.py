#!/usr/bin/env python3
"""
TubeHunter — scan thetubestore.com's "by leading number" catalog (CAD prices via
the site's own /api/items JSON endpoint), classify each tube against a local
reference (data/catalog.json), score it for the Voxy amp's six tube slots
(V1 pentode pre, V2 triode boost, V2 SE power, full combo drop-in, PI cathodyne,
V2 PP mate — the last two are optional) per data/voxy.json, and serve an
iTunes-style table at http://localhost:8765/ .

Run:  python3 tubehunter.py

The UI has a "Refresh from web" button. Refreshes are rate-limited to
MAX_REFRESHES_PER_DAY per 24 h window (tracked in data/snapshot.json).

Voxy has a per-rail buck-boost heater supply (any voltage up to 37 V, up to 3
distinct rails per amp), so heater compatibility is soft: any tube whose heater
voltage is ≤37 V passes; the ranker gives a small bonus for sharing an existing
rail (6.3 V for EF94, 13 V for PCL86) rather than adding a third.
"""

import http.server
import json
import os
import re
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------- configuration ----------

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
SNAPSHOT_PATH = DATA / "snapshot.json"
CATALOG_PATH = DATA / "catalog.json"
VOXY_PATH = DATA / "voxy.json"

BASE_URL = "https://www.thetubestore.com"
ROOT_PATH = "/other-tubes/by-leading-number"

PORT = 8765
USER_AGENT = "TubeHunter/1.0 (personal utility for Voxy amp build; contact tascai@icloud.com)"
REQUEST_DELAY_S = 1.5
REQUEST_TIMEOUT_S = 20
MAX_REFRESHES_PER_DAY = 3
# safety cap on total product-listing pages fetched per refresh
MAX_PAGES_PER_REFRESH = 400

# ---------- utilities ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def canon(name: str) -> str:
    """Uppercase, strip everything but A-Z0-9. Used for tube-name matching."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())

def parse_price(text: str):
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", text.replace(",", ""))
    return float(m.group(1)) if m else None

# ---------- catalog + classifier ----------

class Catalog:
    def __init__(self, path: Path):
        self.path = path
        self.raw = json.loads(path.read_text())
        # Resolve alias chains: an entry with `alias_of: "X"` inherits X's fields
        # (recursively), with anything on the alias overriding. Lets us encode
        # e.g. 12AU6 as `{"alias_of": "6AU6", "hv": 12.6, "ha": 0.15, "notes": "…"}`
        # instead of duplicating the whole spec sheet.
        self.by_canon = {}
        for key, rec in self.raw.items():
            if key.startswith("_"):
                continue
            resolved = self._resolve(key, rec, set())
            self.by_canon[canon(key)] = (key, resolved)

    def _resolve(self, key, rec, visited):
        if not isinstance(rec, dict) or "alias_of" not in rec:
            return rec
        if key in visited:
            return {k: v for k, v in rec.items() if k != "alias_of"}
        visited.add(key)
        parent_key = rec["alias_of"]
        parent = self.raw.get(parent_key)
        if not parent:
            return {k: v for k, v in rec.items() if k != "alias_of"}
        merged = dict(self._resolve(parent_key, parent, visited))
        for k, v in rec.items():
            if k == "alias_of":
                continue
            merged[k] = v
        return merged

    def classify(self, display_name: str):
        """Return (matched_key, record) or (None, None) if unknown.

        Match rules, in order:
        1. exact canonicalized token match  (EF86 == EF86)
        2. token starts with a catalog key and the tail is a short alpha suffix
           (6AU6WC → 6AU6, 12AX7A → 12AX7, 6L6GC → 6L6, EL84M → EL84)
        Longest catalog key wins so 12AX7 beats 12A on a tie.
        """
        tokens = [canon(t) for t in re.split(r"[\s/,]+", display_name)]
        tokens = [t for t in tokens if t]
        # 1. exact
        for t in tokens:
            if t in self.by_canon:
                return self.by_canon[t]
        # 2. prefix + short alpha suffix
        keys_by_len = sorted(self.by_canon.keys(), key=len, reverse=True)
        for t in tokens:
            for k in keys_by_len:
                if len(k) < 3:
                    continue
                if t.startswith(k):
                    suffix = t[len(k):]
                    if 0 < len(suffix) <= 3 and suffix.isalpha():
                        return self.by_canon[k]
        return None, None

# ---------- Voxy ranker ----------

class VoxyRanker:
    def __init__(self, path: Path):
        self.raw = json.loads(path.read_text())
        self.slots = self.raw["slots"]
        heater = self.raw["amp"]["heater_supply"]
        self.heater_v_max = heater["v_max"]
        self.existing_rails = set(heater["existing_rails_v"])

    def score_all(self, rec):
        """Return {slot_id: {"score": 0..5, "reasons": [str, ...]}} for a catalog record."""
        out = {}
        for slot_id, spec in self.slots.items():
            method = getattr(self, f"_score_{slot_id}", None)
            if method:
                out[slot_id] = method(rec, spec)
            else:
                out[slot_id] = {"score": 0, "reasons": ["no scorer"]}
        return out

    @staticmethod
    def _cap(score):
        return max(0.0, min(5.0, score))

    def _heater_check(self, rec):
        """Return (bonus, reason, hard_fail). hard_fail=True means slot score should be 0."""
        hv = rec.get("hv")
        if hv is None:
            return 0.0, "heater voltage unknown", False
        if hv > self.heater_v_max:
            return 0.0, f"{hv} V heater exceeds buck-boost max ({self.heater_v_max} V)", True
        if hv in self.existing_rails:
            return 0.5, f"{hv} V heater — shares existing rail", False
        return 0.15, f"{hv} V heater — needs a dedicated rail (≤{self.heater_v_max} V, within amp's 3-rail budget)", False

    def _vibe_bonus(self, rec):
        v = rec.get("vibe", 0)
        if v >= 3:
            return 0.7, "vibe: holy grail"
        if v == 2:
            return 0.4, "vibe: classic"
        if v == 1:
            return 0.2, "vibe: known guitar tube"
        return 0.0, None

    def _score_V1(self, rec, spec):
        reasons = []
        s = 0.0
        if rec.get("cat") != "pentode_pre":
            return {"score": 0, "reasons": ["not a small-signal pentode"]}
        s += 2.0
        reasons.append("small-signal pentode ✓")
        sock = rec.get("socket")
        if sock == spec["socket"]:
            s += 1.0
            reasons.append("B7G socket — drop-in ✓")
        elif sock == "noval":
            s += 0.4
            reasons.append("noval socket — needs rewire (EF86 path)")
        else:
            reasons.append(f"{sock} socket — mechanically won't fit")
            return {"score": self._cap(s * 0.4), "reasons": reasons}
        h_bonus, h_reason, h_fail = self._heater_check(rec)
        if h_fail:
            return {"score": 0, "reasons": reasons + [h_reason]}
        s += h_bonus
        reasons.append(h_reason)
        cutoff = rec.get("cutoff")
        if cutoff == "sharp":
            s += 0.7
            reasons.append("sharp-cutoff ✓")
        elif cutoff == "remote":
            s -= 1.0
            reasons.append("remote-cutoff — distorts audio")
        vb, vr = self._vibe_bonus(rec)
        s += vb
        if vr: reasons.append(vr)
        return {"score": round(self._cap(s), 1), "reasons": reasons}

    def _score_V2t(self, rec, spec):
        reasons = []
        s = 0.0
        if rec.get("cat") not in ("triode_pre", "combo"):
            return {"score": 0, "reasons": ["not a triode preamp"]}
        if rec.get("cat") == "combo":
            s += 1.2
            reasons.append("combo tube — triode section usable, but pick 'combo' or 'V2p' instead")
        else:
            s += 2.0
            reasons.append("triode preamp ✓")
        sock = rec.get("socket")
        if sock == spec["socket"]:
            s += 1.0
            reasons.append("noval socket — drop-in ✓")
        else:
            reasons.append(f"{sock} socket — needs adapter/rewire")
            s -= 0.5
        h_bonus, h_reason, h_fail = self._heater_check(rec)
        if h_fail:
            return {"score": 0, "reasons": reasons + [h_reason]}
        s += h_bonus
        reasons.append(h_reason)
        mu = rec.get("mu")
        lo, hi = spec["mu_range"]
        if mu and lo <= mu <= hi:
            s += 1.0
            reasons.append(f"µ={mu} in the boost sweet spot")
        elif mu:
            s += 0.3
            reasons.append(f"µ={mu} — usable but off-target")
        vb, vr = self._vibe_bonus(rec)
        s += vb
        if vr: reasons.append(vr)
        return {"score": round(self._cap(s), 1), "reasons": reasons}

    def _score_V2p(self, rec, spec):
        reasons = []
        s = 0.0
        cat = rec.get("cat")
        if cat not in ("power", "combo"):
            return {"score": 0, "reasons": ["not an output tube"]}
        if cat == "combo":
            s += 1.5
            reasons.append("combo tube — pentode section usable, but combo slot is a better match")
        else:
            s += 2.0
            reasons.append("power pentode/beam ✓")
        sock = rec.get("socket")
        if sock == spec["socket"]:
            s += 1.0
            reasons.append("noval socket — drop-in ✓")
        elif sock == "octal":
            s += 0.2
            reasons.append("octal — different socket, but electrically fine (e.g. 6V6)")
        else:
            reasons.append(f"{sock} socket — impractical for 9-pin layout")
            s -= 0.5
        h_bonus, h_reason, h_fail = self._heater_check(rec)
        if h_fail:
            return {"score": 0, "reasons": reasons + [h_reason]}
        s += h_bonus
        reasons.append(h_reason)
        va = rec.get("va_max") or 0
        va_lo, va_hi = spec["va_range"]
        if va >= va_lo:
            s += 0.5
            reasons.append(f"{va} V rating headroom vs 305 V B+")
        else:
            reasons.append(f"only {va} V rated — too fragile at 305 V")
            s -= 1.0
        pd = rec.get("pd") or 0
        pd_lo, pd_hi = spec["pd_range"]
        if pd_lo <= pd <= pd_hi:
            s += 1.0
            reasons.append(f"{pd} W diss — matches SE 4 W target")
        elif pd > pd_hi:
            s += 0.2
            reasons.append(f"{pd} W diss — overkill for 4 W SE, wastes heater budget")
        else:
            reasons.append(f"{pd} W diss — too small for 4 W SE")
        vb, vr = self._vibe_bonus(rec)
        s += vb
        if vr: reasons.append(vr)
        return {"score": round(self._cap(s), 1), "reasons": reasons}

    def _score_combo(self, rec, spec):
        reasons = []
        s = 0.0
        if rec.get("cat") != "combo":
            return {"score": 0, "reasons": ["not a triode-pentode combo"]}
        s += 2.0
        reasons.append("triode-pentode combo ✓")
        pd = rec.get("pd") or 0
        pd_lo, pd_hi = spec["pd_range"]
        if pd_lo <= pd <= pd_hi:
            s += 0.8
            reasons.append(f"pentode diss {pd} W within SE range")
        else:
            s -= 0.3
            reasons.append(f"pentode diss {pd} W outside SE-power window")
        sock = rec.get("socket")
        if sock == spec["socket"]:
            s += 1.0
            reasons.append("noval socket — drop-in ✓")
        else:
            reasons.append(f"{sock} socket — impractical")
            s -= 0.5
        h_bonus, h_reason, h_fail = self._heater_check(rec)
        if h_fail:
            return {"score": 0, "reasons": reasons + [h_reason]}
        s += h_bonus + (0.5 if rec.get("hv") == 13.0 else 0)
        reasons.append(h_reason + (" · matches PCL86 rail exactly" if rec.get("hv") == 13.0 else ""))
        mu = rec.get("mu")
        lo, hi = spec["mu_range"]
        if mu and lo <= mu <= hi:
            s += 0.5
            reasons.append(f"triode µ={mu} in range")
        vb, vr = self._vibe_bonus(rec)
        s += vb
        if vr: reasons.append(vr)
        return {"score": round(self._cap(s), 1), "reasons": reasons}

    def _score_PI(self, rec, spec):
        reasons = []
        s = 0.0
        cat = rec.get("cat")
        if cat not in ("triode_pre", "combo"):
            return {"score": 0, "reasons": ["not a triode — cathodyne needs one triode section"]}
        if cat == "combo":
            s += 1.2
            reasons.append("combo tube — triode section usable as cathodyne, but you'd waste the pentode")
        else:
            s += 2.0
            reasons.append("triode ✓ — one section serves as cathodyne split-load")
        sock = rec.get("socket")
        if sock in ("noval", "octal"):
            s += 1.0
            reasons.append(f"{sock} socket — fits standard PI layouts")
        else:
            reasons.append(f"{sock} socket — impractical here")
            s -= 0.5
        h_bonus, h_reason, h_fail = self._heater_check(rec)
        if h_fail:
            return {"score": 0, "reasons": reasons + [h_reason]}
        s += h_bonus
        reasons.append(h_reason)
        mu = rec.get("mu")
        if mu:
            if 15 <= mu <= 30:
                s += 1.2
                reasons.append(f"µ={mu} — ideal cathodyne (clean, linear split)")
            elif 31 <= mu <= 60:
                s += 0.9
                reasons.append(f"µ={mu} — solid cathodyne (12AY7/5751 territory)")
            elif 61 <= mu <= 90:
                s += 0.7
                reasons.append(f"µ={mu} — usable (12AT7-class)")
            elif mu > 90:
                s += 0.5
                reasons.append(f"µ={mu} — high gain, works (Fender-style 12AX7 PI) but lower headroom")
            else:
                s += 0.3
                reasons.append(f"µ={mu} — very low, may need extra gain upstream")
        vb, vr = self._vibe_bonus(rec)
        s += vb
        if vr: reasons.append(vr)
        return {"score": round(self._cap(s), 1), "reasons": reasons}

    def _score_V2p_B(self, rec, spec):
        # Second half of a push-pull output pair. Scoring rubric is identical to V2p
        # (must be same tube type), but the reasoning refers to PP context, and matched-
        # pair availability (in_stock) matters more.
        base = self._score_V2p(rec, spec)
        # rewrite the top-of-reasons line
        reasons = base["reasons"][:]
        for i, r in enumerate(reasons):
            if r == "power pentode/beam ✓":
                reasons[i] = "power pentode/beam ✓ — usable as PP mate (buy a matched pair)"
                break
            if r.startswith("combo tube — pentode section usable"):
                reasons[i] = "combo tube — pentode-section PP is unusual; V2p rubric applies but sockets get busy"
                break
        return {"score": base["score"], "reasons": reasons}

# ---------- scraper ----------

class RateLimitExceeded(Exception):
    pass

def _build_ssl_context():
    """Prefer verified TLS; fall back to unverified on macOS Python installs that
    can't locate a CA bundle. Only public HTML from a known host is fetched, so
    the fallback is acceptable for a personal utility."""
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where()), True
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    return ctx, True

class Scraper:
    def __init__(self, on_progress=None):
        self.on_progress = on_progress or (lambda msg: None)
        self.last_request_at = 0.0
        self._ctx, self._verified = _build_ssl_context()
        self._fallback_used = False

    def _fetch(self, path: str, accept="text/html,application/xhtml+xml") -> str:
        url = BASE_URL + path if path.startswith("/") else path
        wait = REQUEST_DELAY_S - (time.time() - self.last_request_at)
        if wait > 0:
            time.sleep(wait)
        self.last_request_at = time.time()
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        })
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S, context=self._ctx) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), ssl.SSLError) and not self._fallback_used:
                self._fallback_used = True
                self._ctx = ssl._create_unverified_context()
                self.on_progress("WARNING: system CA bundle unavailable — falling back to unverified TLS")
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S, context=self._ctx) as r:
                    return r.read().decode("utf-8", errors="replace")
            raise

    def _fetch_json(self, path: str):
        return json.loads(self._fetch(path, accept="application/json"))

    def _api_items(self, category_path: str, currency: str = "CAD"):
        """Use the NetSuite SuiteCommerce items API to enumerate every product under
        a leaf category with prices in the requested currency. Returns a list of
        {name, url, price, price_formatted, in_stock} dicts.
        """
        encoded = urllib.parse.quote(category_path, safe="")
        out = []
        offset = 0
        while True:
            q = (f"/api/items?fieldset=search&currency={currency}"
                 f"&commercecategoryurl={encoded}&limit=100&offset={offset}")
            try:
                d = self._fetch_json(q)
            except Exception as exc:
                self.on_progress(f"    API error at offset={offset}: {exc}")
                break
            items = d.get("items") or []
            total = d.get("total") or 0
            for it in items:
                url_comp = it.get("urlcomponent") or ""
                if not url_comp:
                    continue
                out.append({
                    "name": (it.get("displayname") or it.get("storedisplayname2") or "").strip(),
                    "url": "/" + url_comp.lstrip("/"),
                    "internalid": it.get("internalid"),
                    "price": it.get("onlinecustomerprice"),
                    "price_formatted": it.get("onlinecustomerprice_formatted") or "",
                    "currency": currency,
                    "in_stock": bool(it.get("isinstock")),
                    "rating": None,
                    "reviews": 0,
                })
            offset += len(items)
            if not items or offset >= total:
                break
        return out

    def _sub_links(self, html: str, prefix: str):
        """Extract category links directly under prefix (one level deeper)."""
        prefix_esc = re.escape(prefix.rstrip("/"))
        # links look like href="/other-tubes/by-leading-number/6-types" (one segment deeper)
        pat = re.compile(rf'href="({prefix_esc}/[a-z0-9-]+)(?:\?[^"]*)?"')
        found = []
        seen = set()
        for m in pat.finditer(html):
            u = m.group(1)
            if u == prefix.rstrip("/"):
                continue
            if u in seen:
                continue
            seen.add(u)
            found.append(u)
        return found

    def _product_cards(self, html: str):
        """Yield (name, url, price, in_stock, rating, reviews) tuples from a listing page."""
        # Split on the Product itemtype markers
        parts = re.split(
            r'itemscope="" itemtype="http://schema.org/Product"',
            html,
        )
        for chunk in parts[1:]:
            # each chunk starts with the card's inner HTML; stop at the next section boundary
            end = chunk.find('itemscope="" itemtype="http://schema.org/Product"')
            if end != -1:
                chunk = chunk[:end]
            name_m = re.search(r'<span itemprop="name">([^<]+)</span>', chunk)
            url_m = re.search(r'itemprop="url"[^>]*content="([^"]+)"', chunk)
            price_m = re.search(r'itemprop="price"[^>]*>([^<]+)</span>', chunk)
            avail_m = re.search(r'itemprop="availability"[^>]*href="([^"]+)"', chunk) \
                or re.search(r'itemprop="availability"[^>]*content="([^"]+)"', chunk)
            # aggregate rating meta appears first inside the star widget
            rating_m = re.search(r'itemprop="ratingValue"[^>]*content="([0-9.]+)"', chunk)
            reviews_m = re.search(r'itemprop="reviewCount"[^>]*content="([0-9]+)"', chunk)
            if not (name_m and url_m):
                continue
            name = name_m.group(1).strip()
            url = url_m.group(1).strip()
            price = parse_price(price_m.group(1) if price_m else "")
            avail = (avail_m.group(1) if avail_m else "").lower()
            in_stock = "instock" in avail.replace("/", "").replace(" ", "")
            yield {
                "name": name,
                "url": url,
                "price": price,
                "in_stock": in_stock,
                "rating": float(rating_m.group(1)) if rating_m else None,
                "reviews": int(reviews_m.group(1)) if reviews_m else 0,
            }

    def _has_next_page(self, html: str, current_page: int) -> bool:
        # look for a pagination anchor pointing at current_page + 1
        return bool(re.search(rf'href="[^"]*page={current_page + 1}[^"]*"', html))

    def crawl(self, currency: str = "CAD") -> list:
        """Breadth-first crawl.

        Per category path, one HTML fetch discovers sub-category links (for BFS) and
        signals whether the page holds products. If it does, one JSON API call
        (with the target currency baked in) returns every product on that leaf in
        one shot — no HTML card parsing, no pagination via HTML.
        """
        pages_seen = 0
        seen_products = {}
        queue = [ROOT_PATH]
        visited_paths = set()

        while queue:
            path = queue.pop(0)
            if path in visited_paths:
                continue
            visited_paths.add(path)
            if pages_seen >= MAX_PAGES_PER_REFRESH:
                self.on_progress(f"HIT MAX_PAGES_PER_REFRESH ({MAX_PAGES_PER_REFRESH}) — stopping")
                break
            self.on_progress(f"scan {path}")
            try:
                html = self._fetch(path)
            except Exception as exc:
                self.on_progress(f"  error: {exc}")
                continue
            pages_seen += 1

            for u in self._sub_links(html, path):
                if u not in visited_paths:
                    queue.append(u)

            if "schema.org/Product" in html:
                if pages_seen >= MAX_PAGES_PER_REFRESH:
                    break
                items = self._api_items(path, currency=currency)
                pages_seen += 1
                for p in items:
                    seen_products[p["url"]] = p
                self.on_progress(f"  +{len(items)} products (total {len(seen_products)})")

        return list(seen_products.values())

# ---------- snapshot (data + rate limit) ----------

class Snapshot:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text())
        else:
            self.data = {
                "generated_at": None,
                "products": [],
                "refresh_log": [],
                "progress": [],
            }

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))

    def prune_refresh_log(self):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        keep = []
        for iso in self.data.get("refresh_log", []):
            try:
                t = datetime.fromisoformat(iso)
            except Exception:
                continue
            if t >= cutoff:
                keep.append(iso)
        self.data["refresh_log"] = keep

    def refreshes_remaining(self) -> int:
        self.prune_refresh_log()
        return max(0, MAX_REFRESHES_PER_DAY - len(self.data.get("refresh_log", [])))

    def can_refresh(self) -> bool:
        return self.refreshes_remaining() > 0

    def record_refresh(self):
        self.data.setdefault("refresh_log", []).append(now_iso())

# ---------- combining scrape + classify + score ----------

PASSTHROUGH_FIELDS = ("name", "url", "internalid", "price", "price_formatted", "currency",
                      "in_stock", "rating", "reviews")

# The Voxy chassis only accepts B7G (7-pin) or noval (9-pin) — anything octal,
# UX4/5/6, loctal, magnoval etc. is bigger and won't fit. Unknown socket → also
# doesn't fit (safer default; user can un-hide via "All sockets").
CHASSIS_OK_SOCKETS = {"B7G", "noval"}

def fits_chassis(socket_):
    return socket_ in CHASSIS_OK_SOCKETS

def enrich(products, catalog: Catalog, ranker: VoxyRanker):
    """Return the same product dicts with classification + Voxy scores added."""
    out = []
    for p in products:
        key, rec = catalog.classify(p["name"])
        classified = bool(rec)
        cat = rec.get("cat") if rec else "unknown"
        socket_ = rec.get("socket") if rec else None
        hv = rec.get("hv") if rec else None
        ha = rec.get("ha") if rec else None
        pd = rec.get("pd") if rec else None
        mu = rec.get("mu") if rec else None
        gm = rec.get("gm") if rec else None
        vibe = rec.get("vibe", 0) if rec else 0
        notes = rec.get("notes") if rec else None
        elements = rec.get("elements") if rec else None
        chassis_ok = fits_chassis(socket_) if classified else None
        if classified and not chassis_ok:
            # Hard chassis fail — zero every Voxy slot score with a clear reason.
            reason = f"{socket_ or '?'} socket — doesn't fit Voxy chassis (7-pin B7G or 9-pin noval only)"
            scores = {slot: {"score": 0, "reasons": [reason]} for slot in ranker.slots}
        elif rec:
            scores = ranker.score_all(rec)
        else:
            scores = {slot: {"score": 0, "reasons": ["unknown tube"]} for slot in ranker.slots}
        overall = max((s["score"] for s in scores.values()), default=0)
        base = {k: p.get(k) for k in PASSTHROUGH_FIELDS}
        out.append({
            **base,
            "matched_key": key,
            "classified": classified,
            "category": cat,
            "socket": socket_,
            "fits_chassis": chassis_ok,
            "heater_v": hv,
            "heater_a": ha,
            "plate_diss": pd,
            "mu": mu,
            "gm": gm,
            "vibe": vibe,
            "notes": notes,
            "elements": elements,
            "scores": scores,
            "voxy_overall": overall,
        })
    return out

# ---------- background refresh ----------

class RefreshRunner:
    def __init__(self, snapshot: Snapshot, catalog: Catalog, ranker: VoxyRanker):
        self.snapshot = snapshot
        self.catalog = catalog
        self.ranker = ranker
        self._lock = threading.Lock()
        self._thread = None
        self._progress = []

    @property
    def in_progress(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def progress_snapshot(self):
        return list(self._progress)

    def start(self):
        with self._lock:
            if self.in_progress:
                return {"status": "already_running"}
            if not self.snapshot.can_refresh():
                return {"status": "rate_limited",
                        "remaining": 0,
                        "message": f"Daily limit ({MAX_REFRESHES_PER_DAY}) reached. Try again in a few hours."}
            self._progress = [f"{now_iso()}  refresh started"]
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return {"status": "started"}

    def _log(self, msg):
        stamp = f"{now_iso()}  {msg}"
        self._progress.append(stamp)
        # cap to last 400 lines
        if len(self._progress) > 400:
            self._progress = self._progress[-400:]
        print(stamp, file=sys.stderr)

    def _run(self):
        try:
            scraper = Scraper(on_progress=self._log)
            raw = scraper.crawl()
            self._log(f"crawl finished: {len(raw)} distinct products")
            enriched = enrich(raw, self.catalog, self.ranker)
            self.snapshot.data["products"] = enriched
            self.snapshot.data["generated_at"] = now_iso()
            self.snapshot.record_refresh()
            self.snapshot.data["progress"] = self._progress[-40:]
            self.snapshot.save()
            self._log("done")
        except Exception as exc:
            self._log(f"FAILED: {exc}")

# ---------- HTTP server ----------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TubeHunter — Voxy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --frame-bg: linear-gradient(180deg, #dfe3e8 0%, #b6bcc4 100%);
    --frame-shadow: inset 0 -1px 0 rgba(0,0,0,0.2);
    --sidebar-bg: #dee3ea;
    --sidebar-active: linear-gradient(180deg, #6c8bd6, #3a63b8);
    --row-alt: #f0f4fa;
    --row-hover: #d9e6ff;
    --row-sel: linear-gradient(180deg, #6c8bd6, #3a63b8);
    --border: #a8b0bb;
    --text: #1a1d21;
    --muted: #666;
    --star: #d9a900;
    --star-empty: #cbd2da;
    --pane-bg: #fbfcfe;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font: 12px/1.35 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    color: var(--text);
    background: #c9cfd7;
    display: flex; flex-direction: column;
    user-select: none;
  }
  /* toolbar */
  #frame {
    background: var(--frame-bg);
    border-bottom: 1px solid var(--border);
    box-shadow: var(--frame-shadow);
    padding: 8px 12px;
    display: flex; align-items: center; gap: 10px;
    flex: none;
  }
  #frame .title { font-weight: 600; font-size: 13px; }
  #frame .spacer { flex: 1; }
  #frame button {
    font: inherit; padding: 3px 10px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: linear-gradient(180deg, #fefefe, #d2d7de);
    cursor: pointer;
  }
  #frame button:disabled { color: #999; cursor: not-allowed; }
  #frame .meta { color: var(--muted); font-size: 11px; }
  #search {
    padding: 3px 8px; font: inherit; width: 180px;
    border: 1px solid var(--border); border-radius: 10px;
    background: #fff;
  }
  .chassis-toggle {
    font-size: 11px; color: #333; display: flex; align-items: center; gap: 4px;
    padding: 2px 4px; cursor: pointer;
  }
  .chassis-toggle input { margin: 0; }
  .filter-group {
    display: flex; flex-direction: column; gap: 2px;
    font-size: 10px; color: #333; padding: 0 6px;
  }
  .filter-label { white-space: nowrap; }
  .filter-label span { font-weight: 600; color: #17376b; }
  /* Slider layout — both sliders use the same wrapper so their tracks and thumbs
     sit at identical vertical positions. The visible track/fill are absolutely
     positioned divs; the <input>s have transparent runnable-tracks and only
     contribute their thumbs. */
  .dual-range {
    position: relative; width: 280px; height: 24px;
  }
  .dual-range.single-range-wrap { width: 200px; }
  .dual-range .track {
    position: absolute; left: 0; right: 0; top: 9px; height: 6px;
    background: #b6bcc4; border-radius: 3px;
    pointer-events: none;
  }
  .dual-range .track-fill {
    position: absolute; top: 9px; height: 6px;
    background: linear-gradient(180deg, #6c8bd6, #3a63b8);
    border-radius: 3px;
    pointer-events: none;
  }
  .dual-range input[type="range"] {
    position: absolute; left: 0; right: 0; top: 0; width: 100%;
    height: 24px; margin: 0; padding: 0;
    background: transparent;
    -webkit-appearance: none; appearance: none;
    pointer-events: none;
  }
  .dual-range input[type="range"]::-webkit-slider-runnable-track {
    background: transparent; height: 24px; border: 0;
  }
  .dual-range input[type="range"]::-moz-range-track {
    background: transparent; height: 24px; border: 0;
  }
  .dual-range input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none;
    width: 18px; height: 18px; border-radius: 50%;
    background: #fff; border: 1px solid #3a63b8;
    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    cursor: pointer; pointer-events: all;
    margin-top: 3px;   /* centers 18px thumb on 24px input; track sits at y=9..15, thumb at y=3..21, both centered on y=12 */
  }
  .dual-range input[type="range"]::-moz-range-thumb {
    width: 18px; height: 18px; border-radius: 50%;
    background: #fff; border: 1px solid #3a63b8;
    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    cursor: pointer; pointer-events: all;
  }
  .dual-range input[type="range"]::-webkit-slider-thumb:hover {
    background: #eef3ff; border-color: #2a4dab;
  }
  /* layout */
  #main {
    display: flex; flex: 1; min-height: 0;
  }
  /* sidebar */
  #sidebar {
    width: 200px; background: var(--sidebar-bg);
    border-right: 1px solid var(--border);
    padding: 8px 0;
    overflow: auto;
    flex: none;
  }
  .side-group { padding: 4px 12px; font-size: 10px; text-transform: uppercase;
    letter-spacing: 0.08em; color: #555; }
  .side-item {
    padding: 4px 22px; cursor: pointer; display: flex; align-items: center;
    justify-content: space-between; gap: 6px;
    border-radius: 4px; margin: 0 6px;
  }
  .side-item:hover { background: #cad2dc; }
  .side-item.active { background: var(--sidebar-active); color: #fff;
    text-shadow: 0 -1px 0 rgba(0,0,0,0.25); }
  .side-item .count { font-size: 10px; opacity: 0.7; }
  /* table */
  #tablewrap { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #tablescroll { flex: 1; overflow: auto; background: #fff; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  thead th {
    position: sticky; top: 0;
    background: linear-gradient(180deg, #eef1f5, #d8dee5);
    border-bottom: 1px solid var(--border);
    border-right: 1px solid var(--border);
    padding: 4px 8px; text-align: left; font-weight: 600;
    cursor: pointer; white-space: nowrap;
  }
  thead th:last-child { border-right: none; }
  thead th .arrow { color: #444; margin-left: 3px; font-size: 10px; }
  tbody td {
    padding: 3px 8px; border-bottom: 1px solid #e6ebf1;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  td.elements-cell { font-size: 11px; color: #333; }
  td.add-cell { text-align: center; padding: 3px 2px; cursor: pointer; user-select: none; }
  td.add-cell .add-btn {
    display: inline-block; width: 20px; height: 18px; line-height: 17px;
    border: 1px solid transparent; border-radius: 3px;
    font-weight: 600; font-size: 12px; color: #999;
  }
  td.add-cell:hover .add-btn { background: #fff; border-color: #3a63b8; color: #3a63b8; }
  td.add-cell .add-btn.in-build { background: linear-gradient(180deg,#6c8bd6,#3a63b8); color: #fff; border-color: #2a4dab; }
  td.add-cell.disabled { cursor: not-allowed; opacity: 0.4; }
  tr.selected td.add-cell .add-btn:not(.in-build) { color: #ddd; }

  .build-picker {
    display: flex; align-items: center; gap: 4px; font-size: 11px;
    background: #fff; border: 1px solid var(--border); border-radius: 4px;
    padding: 2px 6px;
  }
  .build-picker select {
    font: inherit; border: 0; background: transparent; outline: none;
    max-width: 160px;
  }
  .build-picker .badge {
    background: #3a63b8; color: #fff; padding: 0 5px; border-radius: 8px;
    font-size: 10px; font-weight: 600;
  }
  .build-picker button {
    font: inherit; border: 0; background: transparent; cursor: pointer;
    color: #2a4dab; padding: 0 2px;
  }
  .build-picker button:hover { color: #17376b; text-decoration: underline; }
  .cart-setup {
    font-size: 11px; color: #4c1c7a; padding: 3px 8px;
    background: #f0dcff; border: 1px solid #d0b8e8; border-radius: 4px;
    text-decoration: none;
  }
  .cart-setup:hover { background: #e0c8ff; text-decoration: none; }
  .export-btn {
    font: inherit; font-size: 11px; padding: 3px 10px;
    border: 1px solid var(--border); border-radius: 4px;
    background: linear-gradient(180deg, #fefefe, #d2d7de);
    cursor: pointer;
  }
  .export-btn:hover { background: linear-gradient(180deg, #fff, #e2e7ee); }

  /* Build drawer replaces the tube-detail drawer content when a build is being viewed */
  .build-drawer h2 { margin: 0 0 6px; font-size: 15px; }
  .build-drawer .actions { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 10px; }
  .build-drawer .actions button {
    font: inherit; font-size: 11px;
    padding: 3px 10px; border-radius: 4px;
    border: 1px solid var(--border);
    background: linear-gradient(180deg, #fefefe, #d2d7de);
    cursor: pointer;
  }
  .build-drawer .actions button.primary {
    background: linear-gradient(180deg, #6c8bd6, #3a63b8);
    color: #fff; border-color: #2a4dab;
  }
  .build-drawer .actions button.danger { color: #a03030; }
  .build-drawer .warn {
    background: #fff2c4; border: 1px solid #d1a833; color: #5a3d05;
    padding: 6px 10px; border-radius: 4px; margin: 4px 0 8px; font-size: 11px;
  }
  .build-drawer .ok {
    background: #dcf0dd; border: 1px solid #4c9c50; color: #1e5a2c;
    padding: 6px 10px; border-radius: 4px; margin: 4px 0 8px; font-size: 11px;
  }
  .build-drawer .rails { display: flex; gap: 8px; margin: 4px 0 8px; flex-wrap: wrap; }
  .build-drawer .rail {
    background: #eef3ff; border: 1px solid #b6c8e8;
    padding: 3px 8px; border-radius: 4px; font-size: 11px;
  }
  .build-drawer .rail b { color: #17376b; }
  .build-drawer .envelope-list { margin-top: 4px; }
  .build-drawer .envelope {
    border: 1px solid #dae0e8; border-radius: 5px;
    padding: 4px 8px; margin-bottom: 4px; background: #fff;
  }
  .build-drawer .envelope-head {
    display: grid; grid-template-columns: 22px 12px 1fr auto auto 20px;
    gap: 8px; align-items: center; font-size: 12px;
  }
  .build-drawer .envelope-num { color: #666; font-weight: 600; }
  .build-drawer .envelope-name a { color: #17376b; font-weight: 500; }
  .build-drawer .envelope-heater { color: #4c1c7a; font-variant-numeric: tabular-nums; }
  .build-drawer .envelope-price { font-variant-numeric: tabular-nums; }
  .build-drawer .envelope-head .remove {
    cursor: pointer; color: #a03030; font-weight: 600; text-align: center;
  }
  .build-drawer .envelope-head .remove:hover { color: #ff0000; }
  .build-drawer .envelope-sections {
    display: flex; flex-wrap: wrap; gap: 4px 12px; padding: 4px 0 2px 34px;
    font-size: 11px;
  }
  .build-drawer .section-tag {
    display: inline-flex; align-items: center; gap: 3px;
    background: #f0f4fa; border: 1px solid #d9dee5; border-radius: 3px;
    padding: 1px 6px;
  }
  .build-drawer .section-label { color: #333; font-weight: 500; }
  .build-drawer .section-role {
    font: inherit; font-size: 11px;
    border: 0; background: transparent; color: #2a4dab;
    padding: 0 2px; cursor: pointer;
  }
  .build-drawer .section-role.muted { color: #888; font-style: italic; }
  .build-drawer .summary-line { color: #444; margin: 6px 0; font-size: 12px; }
  .build-drawer .summary-line b { color: #000; }
  .build-drawer .shop-header {
    font-size: 11px; color: #4c1c7a; margin: 10px 0 4px; font-weight: 500;
  }
  .build-drawer .shop-list {
    border: 1px solid #dae0e8; border-radius: 5px;
    padding: 6px 10px; margin-bottom: 10px; background: #fafcff;
  }
  .build-drawer .shop-line {
    display: grid; grid-template-columns: 12px 34px 1fr auto auto 60px;
    gap: 10px; padding: 3px 0; align-items: center;
    font-size: 12px; border-bottom: 1px dashed #e6ebf1;
  }
  .build-drawer .shop-line:last-of-type { border-bottom: none; }
  .build-drawer .shop-qty { font-weight: 600; text-align: right; color: #17376b; }
  .build-drawer .shop-name { color: #333; }
  .build-drawer .shop-each { color: #777; font-variant-numeric: tabular-nums; }
  .build-drawer .shop-line-cost { font-variant-numeric: tabular-nums; font-weight: 500; }
  .build-drawer .shop-open {
    text-align: center; padding: 2px 8px;
    background: linear-gradient(180deg, #6c8bd6, #3a63b8);
    color: #fff; border-radius: 3px; text-decoration: none; font-size: 11px;
    border: 1px solid #2a4dab;
  }
  .build-drawer .shop-open:hover { background: linear-gradient(180deg, #7c9be6, #4a73c8); text-decoration: none; }
  .build-drawer .shop-total {
    text-align: right; margin-top: 4px; padding-top: 4px;
    border-top: 1px solid #b6c8e8; font-size: 12px;
  }

  /* Cart-copy modal — the two-step user-gesture workflow */
  .cart-overlay {
    position: fixed; inset: 0; z-index: 9999;
    background: rgba(20, 25, 32, 0.55);
    display: flex; align-items: center; justify-content: center;
    font: 13px/1.5 -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
  }
  .cart-modal {
    background: #fff; border-radius: 8px; box-shadow: 0 20px 60px rgba(0,0,0,0.4);
    padding: 24px 30px; max-width: 540px; width: 92%; color: #222;
  }
  .cart-modal h2 { margin: 0 0 12px; font-size: 17px; }
  .cart-modal-summary {
    background: #eef3ff; border-left: 3px solid #3a63b8;
    padding: 8px 12px; border-radius: 3px; margin-bottom: 10px;
  }
  .cart-modal-warn {
    background: #fff2c4; border-left: 3px solid #d1a833;
    padding: 8px 12px; border-radius: 3px; margin-bottom: 10px;
    color: #5a3d05;
  }
  .cart-modal-steps { padding-left: 22px; margin: 10px 0; }
  .cart-modal-steps li { margin-bottom: 6px; }
  .cart-modal-actions { display: flex; gap: 10px; margin: 16px 0 8px; }
  .cart-modal-actions button {
    padding: 10px 18px; font: inherit; font-size: 14px; font-weight: 600;
    border: 1px solid #a8b0bb; border-radius: 5px; cursor: pointer;
  }
  .cart-copy-btn {
    background: linear-gradient(180deg, #6c8bd6, #3a63b8); color: #fff;
    border-color: #2a4dab !important;
  }
  .cart-copy-btn.copied { background: #4c9c50 !important; border-color: #2d6b31 !important; }
  .cart-copy-btn.failed { background: #b03030 !important; border-color: #7d2020 !important; }
  .cart-copy-btn:disabled { opacity: 0.85; cursor: default; }
  .cart-cancel-btn { background: #eee; }
  .cart-modal-details { margin-top: 10px; font-size: 12px; color: #555; }
  .cart-modal-details summary { cursor: pointer; padding: 4px 0; }
  .cart-modal-details textarea {
    width: 100%; box-sizing: border-box; margin-top: 4px;
    font: 11px/1.4 SF Mono, Menlo, Consolas, monospace;
    padding: 6px; border: 1px solid #dae0e8; border-radius: 4px;
    background: #fafcff; resize: vertical;
  }
  tbody tr:nth-child(even) td { background: var(--row-alt); }
  tbody tr.selected td { background: var(--row-sel); color: #fff; }
  tbody tr:not(.selected):hover td { background: var(--row-hover); }
  tbody tr.selected td a { color: #fff; }
  tbody tr td a { color: #2a4dab; text-decoration: none; }
  tbody tr td a:hover { text-decoration: underline; }
  .stars { color: var(--star); letter-spacing: 1px; }
  .stars .empty { color: var(--star-empty); }
  tr.selected .stars .empty { color: rgba(255,255,255,0.3); }
  tr.selected .stars { color: #ffe07a; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.oos { color: #b03030; }
  tr.selected td.oos { color: #ffd0d0; }
  .cat-pill {
    display: inline-block; padding: 0 6px; font-size: 10px; border-radius: 8px;
    background: #e0e6ee; color: #333;
  }
  .cat-pentode_pre { background: #d9e8ff; color: #17376b; }
  .cat-triode_pre  { background: #dcf0dd; color: #1e5a2c; }
  .cat-power       { background: #ffe0d0; color: #7a3410; }
  .cat-combo       { background: #f0dcff; color: #4c1c7a; }
  .cat-rectifier   { background: #ffe6f2; color: #7a1c4c; }
  .cat-damper      { background: #e8dcf8; color: #4c1c7a; }
  .cat-regulator   { background: #fff2b3; color: #6a4c00; }
  .cat-deflection  { background: #d9d2b8; color: #4a3d10; }
  .cat-converter   { background: #cfefef; color: #164949; }
  .cat-magic_eye   { background: #b6f0c0; color: #123a1a; }
  .cat-diode       { background: #e6dcd0; color: #5a3e17; }
  .cat-other       { background: #e0dee6; color: #4a4657; }
  .cat-unknown     { background: #e7e7e7; color: #666; }
  /* detail drawer */
  #detail {
    height: 260px; background: var(--pane-bg);
    border-top: 1px solid var(--border);
    padding: 10px 16px; overflow: auto;
    flex: none;
  }
  #detail h2 { margin: 0 0 4px; font-size: 15px; }
  #detail .sub { color: var(--muted); font-size: 11px; margin-bottom: 8px; }
  #detail .slot {
    display: flex; align-items: flex-start; gap: 10px;
    padding: 4px 0; border-top: 1px dashed #d9dee5;
  }
  #detail .slot:first-of-type { border-top: none; }
  #detail .slot .label { width: 170px; font-weight: 600; flex: none; }
  #detail .slot .reasons { color: #333; font-size: 11px; }
  #detail .empty { color: var(--muted); font-style: italic; }
  #detail .specs { color: #333; margin-bottom: 6px; font-size: 11px; }
  #detail .specs b { color: #000; }
  #status {
    font-size: 11px; color: var(--muted); padding: 3px 12px;
    background: #ebeef2; border-top: 1px solid var(--border);
    display: flex; gap: 12px;
    flex: none;
  }
  #progress {
    position: fixed; right: 12px; bottom: 12px;
    background: rgba(20,25,32,0.9); color: #eee;
    border-radius: 6px; padding: 8px 10px; font-size: 11px;
    max-width: 460px; max-height: 220px; overflow: auto;
    box-shadow: 0 6px 18px rgba(0,0,0,0.3);
    display: none;
    white-space: pre-wrap;
  }
  #progress .close { float: right; cursor: pointer; padding-left: 8px; }
</style>
</head>
<body>
  <div id="frame">
    <div class="title">TubeHunter · Voxy</div>
    <input id="search" placeholder="Filter (e.g. EF86, noval, combo)">
    <label class="chassis-toggle"><input type="checkbox" id="chassisToggle" checked> Fits chassis</label>
    <label class="chassis-toggle"><input type="checkbox" id="stockToggle" checked> In stock only</label>
    <div class="filter-group" id="priceFilter">
      <span class="filter-label">Price <span id="priceReadout">— – —</span></span>
      <div class="dual-range">
        <div class="track"></div>
        <div class="track-fill" id="priceFill"></div>
        <input type="range" id="priceMinSlider" min="0" max="100" value="0">
        <input type="range" id="priceMaxSlider" min="0" max="100" value="100">
      </div>
    </div>
    <div class="filter-group" id="starsFilter">
      <span class="filter-label">Min fit <span id="starsReadout">★</span></span>
      <div class="dual-range single-range-wrap">
        <div class="track"></div>
        <div class="track-fill" id="starsFill"></div>
        <input type="range" id="starsSlider" min="0" max="5" step="0.5" value="0">
      </div>
    </div>
    <button id="refresh">Refresh from web</button>
    <div class="spacer"></div>
    <div class="build-picker" id="buildPicker"></div>
    <a href="/bookmarklet" target="_blank" rel="noopener" class="cart-setup" title="One-time setup for the store-cart bookmarklet">🛒 cart setup</a>
    <button id="exportCsv" class="export-btn" title="Download all crawled inventory as CSV (regardless of chassis fit, filters, or classification)">Export CSV</button>
    <div class="meta" id="meta">loading…</div>
  </div>
  <div id="main">
    <div id="sidebar"></div>
    <div id="tablewrap">
      <div id="tablescroll">
        <table>
          <thead><tr id="thead"></tr></thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
      <div id="detail"><div class="empty">Select a tube to see how it scores for each Voxy slot.</div></div>
    </div>
  </div>
  <div id="status"><span id="rowcount"></span></div>
  <div id="progress"><span class="close" onclick="document.getElementById('progress').style.display='none'">×</span><span id="progresstext"></span></div>

<script>
"use strict";

const COLS = [
  {key: "in_build",     label: "",             w: 34,  render: r => renderAddButton(r), sortVal: r => envelopesForTube(r.url), cls: "add-cell"},
  {key: "name",         label: "Tube",         w: 210, render: r => `<a href="https://www.thetubestore.com${r.url}" target="_blank" rel="noopener">${escapeHtml(r.name)}</a>`},
  {key: "category",     label: "Type",         w: 110, render: r => `<span class="cat-pill cat-${r.category}">${prettyCat(r.category)}</span>`},
  {key: "elements",     label: "Contents",     w: 150, render: r => renderElements(r.elements), sortVal: r => (r.elements || []).join(","), cls: "elements-cell"},
  {key: "socket",       label: "Socket",       w: 70,  render: r => escapeHtml(r.socket || "—")},
  {key: "heater_v",     label: "Heater",       w: 88,  render: r => r.heater_v ? `${r.heater_v} V${r.heater_a ? " / " + r.heater_a + " A" : ""}` : "—", cls: "num"},
  {key: "plate_diss",   label: "Diss (W)",     w: 60,  render: r => r.plate_diss ?? "—", cls: "num"},
  {key: "mu",           label: "µ",            w: 40,  render: r => r.mu ?? "—", cls: "num"},
  {key: "gm",           label: "gm",           w: 40,  render: r => r.gm ?? "—", cls: "num"},
  {key: "price",        label: "Price (CAD)",  w: 84,  render: r => renderPrice(r), sortVal: r => r.price, cls: "num"},
  {key: "in_stock",     label: "Stock",        w: 60,  render: r => r.in_stock ? "In stock" : "Out", cls: r => r.in_stock ? "" : "oos"},
  {key: "score_V1",     label: "V1 pent",      w: 74,  render: r => stars(r.scores.V1.score), sortVal: r => r.scores.V1.score, cls: "num"},
  {key: "score_V2t",    label: "V2 tri",       w: 74,  render: r => stars(r.scores.V2t.score), sortVal: r => r.scores.V2t.score, cls: "num"},
  {key: "score_V2p",    label: "V2 pow",       w: 74,  render: r => stars(r.scores.V2p.score), sortVal: r => r.scores.V2p.score, cls: "num"},
  {key: "score_combo",  label: "Combo",        w: 74,  render: r => stars(r.scores.combo.score), sortVal: r => r.scores.combo.score, cls: "num"},
  {key: "score_PI",     label: "PI",           w: 74,  render: r => stars(r.scores.PI.score), sortVal: r => r.scores.PI.score, cls: "num"},
  {key: "score_V2p_B",  label: "PP mate",      w: 74,  render: r => stars(r.scores.V2p_B.score), sortVal: r => r.scores.V2p_B.score, cls: "num"},
  {key: "voxy_overall", label: "Voxy Fit",     w: 84,  render: r => stars(r.voxy_overall), cls: "num"},
];

const ELEMENT_LABEL = {
  triode: "triode", pentode: "pentode",
  power_pentode: "power pentode", beam_power: "beam power", power_triode: "power triode",
  diode: "diode", pentagrid: "pentagrid", hexode: "hexode", heptode: "heptode",
  damper_diode: "damper", magic_eye: "magic-eye target", gas_regulator: "gas VR",
  gated_beam: "gated-beam", beam_deflection: "beam-deflection",
};

function renderElements(els) {
  if (!els || !els.length) return `<span style="color:#999">—</span>`;
  // group consecutive same elements: e.g. [triode,triode,triode] → "3×triode"
  const counts = new Map();
  for (const e of els) counts.set(e, (counts.get(e) || 0) + 1);
  const parts = [];
  for (const [k, n] of counts) {
    const label = ELEMENT_LABEL[k] || k;
    parts.push(n > 1 ? `${n}×${escapeHtml(label)}` : escapeHtml(label));
  }
  return parts.join(" + ");
}

function renderPrice(r) {
  if (r.price_formatted) return escapeHtml(r.price_formatted.replace(/‎/g, ""));
  if (r.price != null) return "$" + r.price.toFixed(2);
  return "—";
}

function detectCurrency() {
  for (const r of state.rows) {
    if (r.currency) return r.currency;
    if (r.price_formatted) {
      const m = r.price_formatted.match(/([A-Z]{3})/);
      if (m) return m[1];
    }
  }
  return "USD";
}

const state = {
  rows: [],
  filtered: [],
  selectedUrl: null,
  sortKey: "voxy_overall",
  sortDir: -1,
  filter: {kind: "all"},
  search: "",
  meta: null,
  chassisOnly: true,   // default: hide tubes that won't physically fit Voxy
  inStockOnly: true,   // default: hide out-of-stock listings
  priceMin: 0,
  priceMax: 0,       // set from data at boot
  priceCeil: 0,      // slider max (highest price in current snapshot, rounded up)
  minStars: 0,       // Voxy fit floor, 0–5
  // Multi-build carts (like playlists) persisted to localStorage.
  builds: {},          // {id: {id, name, envelopes: [{id, tubeUrl, roles: {sectionIdx: roleId}}], created}}
  activeBuildId: null, // "+" adds to this one
  viewingBuildId: null,// non-null → detail drawer shows the build summary instead of a tube
};

async function boot() {
  await load();
  loadBuilds();
  initPriceFilter();
  initStarsFilter();
  renderHead();
  renderBuildPicker();
  renderSidebar();
  applyFilter();
  document.getElementById("refresh").addEventListener("click", refresh);
  document.getElementById("search").addEventListener("input", e => {
    state.search = e.target.value.trim().toLowerCase();
    applyFilter();
  });
  document.getElementById("chassisToggle").addEventListener("change", e => {
    state.chassisOnly = e.target.checked;
    renderSidebar();
    applyFilter();
  });
  document.getElementById("stockToggle").addEventListener("change", e => {
    state.inStockOnly = e.target.checked;
    renderSidebar();
    applyFilter();
  });
  document.getElementById("exportCsv").addEventListener("click", exportInventoryCsv);
}

// Dump every tube in the current snapshot to CSV — no filters applied.
// In the native pywebview window, hand the text to Python via the JS API so
// Python can pop a Save… dialog; WKWebView doesn't handle blob downloads and
// clicking a download-link there would just navigate the window away.
async function exportInventoryCsv() {
  if (!state.rows.length) { flashToast("No inventory to export"); return; }
  const cols = [
    {label: "Name",        get: r => r.name},
    {label: "Matched key", get: r => r.matched_key || ""},
    {label: "Category",    get: r => prettyCat(r.category)},
    {label: "Contents",    get: r => (r.elements || []).join(" + ")},
    {label: "Socket",      get: r => r.socket || ""},
    {label: "Heater V",    get: r => r.heater_v ?? ""},
    {label: "Heater A",    get: r => r.heater_a ?? ""},
    {label: "Plate diss W",get: r => r.plate_diss ?? ""},
    {label: "µ",           get: r => r.mu ?? ""},
    {label: "gm mA/V",     get: r => r.gm ?? ""},
    {label: "Vibe",        get: r => r.vibe ?? ""},
    {label: "Price",       get: r => r.price ?? ""},
    {label: "Currency",    get: r => r.currency || ""},
    {label: "In stock",    get: r => r.in_stock ? "yes" : "no"},
    {label: "Fits chassis",get: r => r.fits_chassis === true ? "yes" : r.fits_chassis === false ? "no" : "unknown"},
    {label: "V1 score",    get: r => r.scores?.V1?.score ?? ""},
    {label: "V2t score",   get: r => r.scores?.V2t?.score ?? ""},
    {label: "V2p score",   get: r => r.scores?.V2p?.score ?? ""},
    {label: "Combo score", get: r => r.scores?.combo?.score ?? ""},
    {label: "PI score",    get: r => r.scores?.PI?.score ?? ""},
    {label: "V2p_B score", get: r => r.scores?.V2p_B?.score ?? ""},
    {label: "Voxy overall",get: r => r.voxy_overall ?? ""},
    {label: "URL",         get: r => r.url ? "https://www.thetubestore.com" + r.url : ""},
    {label: "Notes",       get: r => r.notes || ""},
  ];
  const esc = v => {
    if (v == null) return "";
    const s = String(v);
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [
    cols.map(c => esc(c.label)).join(","),
    ...state.rows.map(r => cols.map(c => esc(c.get(r))).join(",")),
  ];
  const csv = "﻿" + lines.join("\n");   // BOM → Excel opens UTF-8 cleanly
  const stamp = (state.meta?.generated_at || "").slice(0, 10) || "current";
  const filename = `thetubestore-inventory-${stamp}.csv`;

  // Native window: ask Python to open the Save… panel and write the file.
  if (window.pywebview && window.pywebview.api && window.pywebview.api.save_csv) {
    try {
      const r = await window.pywebview.api.save_csv(csv, filename);
      if (r && r.ok)             flashToast(`Saved: ${r.path}`);
      else if (r && r.cancelled) flashToast("Save cancelled");
      else                        flashToast("Save failed: " + (r && r.error || "unknown"));
    } catch (e) {
      flashToast("Save failed: " + e.message);
    }
    return;
  }

  // Browser fallback: standard blob-download link (works in Safari, Chrome, etc.)
  const blob = new Blob([csv], {type: "text/csv;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  flashToast(`Exported ${state.rows.length} tubes to CSV`);
}

// Slider position → price is piecewise linear so the low-price range gets most
// of the physical slider space. Rough calibration: 65% of the slider width
// resolves $0–$15 (fine tuning where the user actually filters), the next 25%
// covers $15–$50, and the last 10% sweeps up to the ceiling. First step down
// from 100% therefore drops the value close to the ceiling, not by pennies.
const PRICE_STOPS = [   // [slider_fraction, price]
  [0.00, 0],
  [0.65, 15],
  [0.90, 50],
  [1.00, null],   // last stop's price is set to state.priceCeil at boot
];
const SLIDER_MAX = 1000;

function sliderPosToPrice(pos) {
  const f = pos / SLIDER_MAX;
  const stops = PRICE_STOPS;
  for (let i = 1; i < stops.length; i++) {
    const [f1, p1] = stops[i - 1];
    const [f2, p2] = stops[i];
    if (f <= f2 + 1e-9) {
      const t = (f - f1) / (f2 - f1);
      const raw = p1 + t * (p2 - p1);
      // Round for a friendly readout: dollars at high prices, cents low down.
      return raw < 20 ? Math.round(raw * 10) / 10 : Math.round(raw);
    }
  }
  return stops[stops.length - 1][1];
}

function priceToSliderPos(price) {
  const stops = PRICE_STOPS;
  for (let i = 1; i < stops.length; i++) {
    const [f1, p1] = stops[i - 1];
    const [f2, p2] = stops[i];
    if (price <= p2 + 1e-9) {
      const t = (price - p1) / (p2 - p1);
      return Math.round((f1 + t * (f2 - f1)) * SLIDER_MAX);
    }
  }
  return SLIDER_MAX;
}

function initPriceFilter() {
  const prices = state.rows.map(r => r.price).filter(v => typeof v === "number");
  const ceil = prices.length ? Math.ceil(Math.max(...prices)) : 100;
  state.priceCeil = ceil;
  PRICE_STOPS[PRICE_STOPS.length - 1][1] = ceil;
  state.priceMin = 0;
  state.priceMax = ceil;
  const lo = document.getElementById("priceMinSlider");
  const hi = document.getElementById("priceMaxSlider");
  lo.min = hi.min = 0;
  lo.max = hi.max = SLIDER_MAX;
  lo.step = hi.step = 1;
  lo.value = 0;
  hi.value = SLIDER_MAX;
  const update = () => {
    let a = +lo.value, b = +hi.value;
    if (a > b) {
      if (document.activeElement === lo) { hi.value = String(a); b = a; }
      else                                { lo.value = String(b); a = b; }
    }
    state.priceMin = sliderPosToPrice(a);
    state.priceMax = sliderPosToPrice(b);
    paintPriceFill();
    updatePriceReadout();
    renderSidebar();
    applyFilter();
  };
  lo.addEventListener("input", update);
  hi.addEventListener("input", update);
  paintPriceFill();
  updatePriceReadout();
}

function paintPriceFill() {
  const fill = document.getElementById("priceFill");
  if (!fill) return;
  const lo = document.getElementById("priceMinSlider");
  const hi = document.getElementById("priceMaxSlider");
  const l = (+lo.value / SLIDER_MAX) * 100;
  const r = (+hi.value / SLIDER_MAX) * 100;
  fill.style.left = l + "%";
  fill.style.right = (100 - r) + "%";
}

function updatePriceReadout() {
  const el = document.getElementById("priceReadout");
  const fmt = v => (v < 20 ? "$" + v.toFixed(v % 1 ? 1 : 0) : "$" + Math.round(v));
  el.textContent = `${fmt(state.priceMin)} – ${fmt(state.priceMax)}`;
}

function initStarsFilter() {
  const s = document.getElementById("starsSlider");
  const fill = document.getElementById("starsFill");
  const update = () => {
    state.minStars = +s.value;
    // Star fill mirrors the same "filter is active from priceMin to priceMax" idea:
    // 0 stars → no fill; higher → fill from 0 to the current value.
    const pct = (state.minStars / 5) * 100;
    fill.style.left = "0%";
    fill.style.right = (100 - pct) + "%";
    document.getElementById("starsReadout").innerHTML = renderStarsCompact(state.minStars);
    renderSidebar();
    applyFilter();
  };
  s.addEventListener("input", update);
  update();
}

function renderStarsCompact(v) {
  if (!v) return "any";
  return stars(v) + `<span style="color:#666;font-weight:400"> ${v.toFixed(1)}+</span>`;
}

async function load() {
  const r = await fetch("/api/data");
  const j = await r.json();
  state.rows = j.products || [];
  state.meta = j;
  updateMeta();
}

function updateMeta() {
  const m = state.meta || {};
  const btn = document.getElementById("refresh");
  const remaining = m.refreshes_remaining;
  const ts = m.generated_at ? new Date(m.generated_at).toLocaleString() : "never";
  const cur = detectCurrency();
  document.getElementById("meta").textContent =
    `${state.rows.length} tubes · prices ${cur} · last sync ${ts} · ${remaining}/${m.max_per_day} refreshes left today` +
    (m.refresh_in_progress ? " · refreshing…" : "");
  btn.disabled = remaining === 0 || m.refresh_in_progress;
}

function renderHead() {
  const tr = document.getElementById("thead");
  tr.innerHTML = "";
  COLS.forEach(c => {
    const th = document.createElement("th");
    th.style.width = c.w + "px";
    const arrow = state.sortKey === c.key ? (state.sortDir > 0 ? " ▲" : " ▼") : "";
    th.innerHTML = `${c.label}<span class="arrow">${arrow}</span>`;
    th.addEventListener("click", () => {
      if (state.sortKey === c.key) state.sortDir = -state.sortDir;
      else { state.sortKey = c.key; state.sortDir = -1; }
      renderHead(); renderBody();
    });
    tr.appendChild(th);
  });
}

function renderSidebar() {
  const el = document.getElementById("sidebar");
  // Sidebar counts respect the toolbar filters so numbers on the left match what the user sees on the right.
  let pool = state.chassisOnly
    ? state.rows.filter(r => r.fits_chassis === true || r.fits_chassis === null)
    : state.rows;
  if (state.inStockOnly) pool = pool.filter(r => r.in_stock);
  if (state.priceMin > 0 || state.priceMax < state.priceCeil) {
    pool = pool.filter(r => r.price == null || (r.price >= state.priceMin && r.price <= state.priceMax));
  }
  if (state.minStars > 0) pool = pool.filter(r => (r.voxy_overall || 0) >= state.minStars);
  const counts = countBy(pool, r => r.category);
  const bestForSlot = slot => pool.filter(r => r.scores[slot].score >= 4).length;
  const groups = [
    {header: "Library"},
    {label: "All tubes", key: {kind: "all"}, count: pool.length},
    {label: "Classified", key: {kind: "classified"}, count: pool.filter(r => r.classified).length},
    {label: "Unknown / uncatalogued", key: {kind: "unknown"}, count: pool.filter(r => !r.classified).length},
    {header: "By type"},
    {label: "Pentode preamp", key: {kind: "cat", val: "pentode_pre"}, count: counts["pentode_pre"] || 0},
    {label: "Triode preamp",  key: {kind: "cat", val: "triode_pre"},  count: counts["triode_pre"] || 0},
    {label: "Power",          key: {kind: "cat", val: "power"},       count: counts["power"] || 0},
    {label: "Pre-power combo",key: {kind: "cat", val: "combo"},       count: counts["combo"] || 0},
    {label: "Rectifier",      key: {kind: "cat", val: "rectifier"},   count: counts["rectifier"] || 0},
    {label: "Damper diode",   key: {kind: "cat", val: "damper"},      count: counts["damper"] || 0},
    {label: "Regulator",      key: {kind: "cat", val: "regulator"},   count: counts["regulator"] || 0},
    {label: "Deflection / HO",key: {kind: "cat", val: "deflection"},  count: counts["deflection"] || 0},
    {label: "Converter (AM)", key: {kind: "cat", val: "converter"},   count: counts["converter"] || 0},
    {label: "Magic eye",      key: {kind: "cat", val: "magic_eye"},   count: counts["magic_eye"] || 0},
    {label: "Other / special",key: {kind: "cat", val: "other"},       count: counts["other"] || 0},
    {header: "Best for Voxy"},
    {label: "★★★★+ V1 pentode", key: {kind: "slot", val: "V1"},    count: bestForSlot("V1")},
    {label: "★★★★+ V2 triode",  key: {kind: "slot", val: "V2t"},   count: bestForSlot("V2t")},
    {label: "★★★★+ V2 power",   key: {kind: "slot", val: "V2p"},   count: bestForSlot("V2p")},
    {label: "★★★★+ Combo",      key: {kind: "slot", val: "combo"}, count: bestForSlot("combo")},
    {header: "Optional slots"},
    {label: "★★★★+ Cathodyne PI", key: {kind: "slot", val: "PI"},    count: bestForSlot("PI")},
    {label: "★★★★+ PP power mate", key: {kind: "slot", val: "V2p_B"}, count: bestForSlot("V2p_B")},
  ];
  // Builds section (playlists) — one entry per saved build, plus a "+ New" action.
  groups.push({header: "Builds"});
  for (const b of Object.values(state.builds)) {
    groups.push({
      label: (b.id === state.activeBuildId ? "★ " : "") + b.name,
      key: {kind: "build", val: b.id},
      count: b.envelopes.length,
    });
  }
  groups.push({label: "+ New build", key: {kind: "new-build"}, count: ""});
  el.innerHTML = "";
  groups.forEach(g => {
    if (g.header) {
      const h = document.createElement("div");
      h.className = "side-group"; h.textContent = g.header;
      el.appendChild(h);
      return;
    }
    const d = document.createElement("div");
    d.className = "side-item" + (JSON.stringify(g.key) === JSON.stringify(state.filter) ? " active" : "");
    d.innerHTML = `<span>${escapeHtml(g.label)}</span><span class="count">${g.count}</span>`;
    d.addEventListener("click", () => {
      if (g.key.kind === "new-build") {
        promptNewBuild();
        return;
      }
      if (g.key.kind === "build") {
        // clicking a build in the sidebar: activate it, show its tubes filtered, open the build drawer
        setActiveBuild(g.key.val);
        state.filter = g.key;
        viewBuild(g.key.val);
      } else {
        state.filter = g.key;
        state.viewingBuildId = null;
      }
      renderSidebar();
      applyFilter();
    });
    el.appendChild(d);
  });
}

function applyFilter() {
  const f = state.filter;
  let rows = state.rows.slice();
  if (state.chassisOnly) {
    // Keep tubes that physically fit; also keep unknowns so user can explore/classify them.
    rows = rows.filter(r => r.fits_chassis === true || r.fits_chassis === null);
  }
  if (state.inStockOnly) {
    rows = rows.filter(r => r.in_stock);
  }
  // price range: only apply once user narrows below the full ceiling. A tube
  // without a price is kept regardless — you can't reject what you don't know.
  if (state.priceMin > 0 || state.priceMax < state.priceCeil) {
    rows = rows.filter(r => r.price == null || (r.price >= state.priceMin && r.price <= state.priceMax));
  }
  if (state.minStars > 0) {
    rows = rows.filter(r => (r.voxy_overall || 0) >= state.minStars);
  }
  if (f.kind === "cat")        rows = rows.filter(r => r.category === f.val);
  else if (f.kind === "unknown") rows = rows.filter(r => !r.classified);
  else if (f.kind === "classified") rows = rows.filter(r => r.classified);
  else if (f.kind === "slot") rows = rows.filter(r => r.scores[f.val].score >= 4);
  else if (f.kind === "build") {
    const b = state.builds[f.val];
    const urls = new Set(b ? b.envelopes.map(e => e.tubeUrl) : []);
    rows = rows.filter(r => urls.has(r.url));
  }
  if (state.search) {
    const q = state.search;
    rows = rows.filter(r =>
      (r.name || "").toLowerCase().includes(q) ||
      (r.matched_key || "").toLowerCase().includes(q) ||
      (r.category || "").toLowerCase().includes(q) ||
      (r.socket || "").toLowerCase().includes(q) ||
      (r.notes || "").toLowerCase().includes(q)
    );
  }
  state.filtered = rows;
  renderBody();
}

function renderBody() {
  const rows = state.filtered.slice();
  const col = COLS.find(c => c.key === state.sortKey) || COLS[0];
  const getSort = col.sortVal || (r => r[col.key]);
  rows.sort((a, b) => {
    const va = getSort(a), vb = getSort(b);
    if (va == null && vb == null) return tieBreak(a, b);
    if (va == null) return 1;
    if (vb == null) return -1;
    if (va < vb) return -1 * state.sortDir;
    if (va > vb) return  1 * state.sortDir;
    return tieBreak(a, b);
  });
  // Secondary key: Voxy fit descending. That way when the primary column is
  // price ascending, two $5 tubes surface with the better Voxy fit on top.
  function tieBreak(a, b) {
    if (state.sortKey === "voxy_overall") return 0;
    return (b.voxy_overall || 0) - (a.voxy_overall || 0);
  }
  const tb = document.getElementById("tbody");
  tb.innerHTML = "";
  const frag = document.createDocumentFragment();
  rows.forEach(r => {
    const tr = document.createElement("tr");
    if (r.url === state.selectedUrl) tr.className = "selected";
    tr.dataset.url = r.url;
    COLS.forEach(c => {
      const td = document.createElement("td");
      const cls = typeof c.cls === "function" ? c.cls(r) : c.cls;
      if (cls) td.className = cls;
      td.innerHTML = c.render(r);
      if (c.key === "in_build") {
        td.addEventListener("click", ev => { ev.stopPropagation(); addEnvelopeToActiveBuild(r.url); });
      }
      tr.appendChild(td);
    });
    tr.addEventListener("click", () => selectRow(r));
    frag.appendChild(tr);
  });
  tb.appendChild(frag);
  document.getElementById("rowcount").textContent = `${rows.length} tubes shown`;
}

function selectRow(r) {
  state.selectedUrl = r.url;
  state.viewingBuildId = null;
  renderBody();
  renderDetail(r);
}

function renderDetail(r) {
  const slots = [
    {id: "V1",    label: "V1 · Pentode preamp"},
    {id: "V2t",   label: "V2 · Triode boost"},
    {id: "V2p",   label: "V2 · SE power"},
    {id: "combo", label: "Combo · full V2 drop-in"},
    {id: "PI",    label: "PI · Cathodyne (optional)"},
    {id: "V2p_B", label: "V2 · PP mate (optional)"},
  ];
  let html = `<h2>${escapeHtml(r.name)}
    ${r.matched_key ? `<span class="sub">matched as <b>${escapeHtml(r.matched_key)}</b></span>` : `<span class="sub">not in catalog</span>`}
  </h2>`;
  html += `<div class="specs">`;
  if (r.classified) {
    html += [
      `type: <b>${prettyCat(r.category)}</b>`,
      `contents: <b>${renderElements(r.elements)}</b>`,
      `socket: <b>${r.socket || "?"}</b>`,
      r.heater_v ? `heater: <b>${r.heater_v} V / ${r.heater_a || "?"} A</b>` : null,
      r.plate_diss ? `diss: <b>${r.plate_diss} W</b>` : null,
      r.mu ? `µ: <b>${r.mu}</b>` : null,
      r.gm ? `gm: <b>${r.gm} mA/V</b>` : null,
      r.price_formatted ? `price: <b>${escapeHtml(r.price_formatted.replace(/‎/g, ""))}</b>` : (r.price != null ? `price: <b>$${r.price.toFixed(2)}</b>` : null),
      r.in_stock ? `<b>in stock</b>` : `<b style="color:#b03030">out of stock</b>`,
    ].filter(Boolean).join(" · ");
  } else {
    html += `Not classified — add an entry to <code>data/catalog.json</code> for scoring.`;
  }
  html += `</div>`;
  if (r.notes) html += `<div class="specs" style="font-style:italic">${escapeHtml(r.notes)}</div>`;
  slots.forEach(s => {
    const sc = r.scores[s.id];
    if (!sc) return;
    html += `<div class="slot">
      <div class="label">${s.label}<div class="stars" style="font-size:14px">${stars(sc.score)}</div></div>
      <div class="reasons">${sc.reasons.map(escapeHtml).join(" · ")}</div>
    </div>`;
  });
  document.getElementById("detail").innerHTML = html;
}

async function refresh() {
  const r = await fetch("/api/refresh", {method: "POST"});
  const j = await r.json();
  if (j.status === "rate_limited") {
    alert(j.message || "Refresh limit reached for today.");
    return;
  }
  showProgress();
  pollProgress();
}

function showProgress() { document.getElementById("progress").style.display = "block"; }
async function pollProgress() {
  const r = await fetch("/api/status");
  const j = await r.json();
  document.getElementById("progresstext").textContent = (j.progress || []).slice(-25).join("\n");
  state.meta = {...state.meta, ...j};
  updateMeta();
  if (j.refresh_in_progress) {
    setTimeout(pollProgress, 1500);
  } else {
    await load();
    renderSidebar();
    applyFilter();
    setTimeout(() => document.getElementById("progress").style.display = "none", 6000);
  }
}

function stars(s) {
  if (s == null || isNaN(s)) return `<span class="stars"><span class="empty">·····</span></span>`;
  const full = Math.round(s);
  const empty = 5 - full;
  return `<span class="stars">${"★".repeat(full)}<span class="empty">${"★".repeat(empty)}</span></span>`;
}
function countBy(arr, fn) {
  const out = {};
  arr.forEach(x => { const k = fn(x); out[k] = (out[k] || 0) + 1; });
  return out;
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function prettyCat(c) {
  return {
    pentode_pre: "pentode pre",
    triode_pre: "triode pre",
    power: "power",
    combo: "pre/power combo",
    rectifier: "rectifier",
    damper: "damper diode",
    regulator: "voltage reg",
    deflection: "TV deflection",
    converter: "pentagrid conv",
    magic_eye: "magic eye",
    diode: "diode",
    other: "other",
    unknown: "unknown",
  }[c] || c;
}

/* ==================== BUILDS (playlist/cart) ==================== */

const BUILDS_STORAGE_KEY = "tubehunter-builds-v2";
const BUILDS_STORAGE_KEY_V1 = "tubehunter-builds-v1";  // legacy — migrated on load

/* Voxy roles that a section can be tagged as. Each section-type has a whitelist. */
const ROLES = [
  { id: "V1",    label: "V1 pentode pre",     accepts: ["pentode"] },
  { id: "V2t",   label: "V2 triode boost",    accepts: ["triode"] },
  { id: "V2p",   label: "V2 SE power / PP-A", accepts: ["power_pentode", "beam_power", "power_triode"] },
  { id: "V2p_B", label: "V2 PP-B mate",       accepts: ["power_pentode", "beam_power", "power_triode"] },
  { id: "PI",    label: "PI cathodyne",       accepts: ["triode"] },
];
const ROLE_BY_ID = Object.fromEntries(ROLES.map(r => [r.id, r]));

function rolesForSection(sectionType) {
  return ROLES.filter(r => r.accepts.includes(sectionType));
}

/* Given a tube's elements list, return per-section descriptors with human labels.
   Duplicate types get A/B/C suffixes so the two triodes inside a 12AX7 are labeled
   "triode A" and "triode B" but a 6BM8's single triode is just "triode". */
function sectionsOf(elements) {
  if (!elements || !elements.length) return [];
  const totalPerType = {};
  for (const e of elements) totalPerType[e] = (totalPerType[e] || 0) + 1;
  const running = {};
  return elements.map((e, i) => {
    running[e] = (running[e] || 0) + 1;
    const pretty = ELEMENT_LABEL[e] || e;
    const label = totalPerType[e] > 1 ? `${pretty} ${String.fromCharCode(64 + running[e])}` : pretty;
    return { idx: i, type: e, label };
  });
}

function newEnvelopeId() {
  return "env" + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36);
}

/* Old builds stored `tubeUrls: [url,url,...]`; new builds store
   `envelopes: [{id, tubeUrl, roles: {sectionIdx: roleId}}, ...]`. Migrate in place. */
function migrateBuild(b) {
  if (!b.envelopes) {
    if (b.tubeUrls && b.tubeUrls.length) {
      b.envelopes = b.tubeUrls.map((u) => ({id: newEnvelopeId(), tubeUrl: u, roles: {}}));
    } else {
      b.envelopes = [];
    }
    delete b.tubeUrls;
  }
  return b;
}

function saveBuilds() {
  try {
    localStorage.setItem(BUILDS_STORAGE_KEY, JSON.stringify({
      builds: state.builds,
      activeBuildId: state.activeBuildId,
    }));
  } catch (e) { /* private mode → in-memory only */ }
}

function loadBuilds() {
  try {
    let raw = localStorage.getItem(BUILDS_STORAGE_KEY);
    if (!raw) raw = localStorage.getItem(BUILDS_STORAGE_KEY_V1);
    if (!raw) return;
    const d = JSON.parse(raw);
    state.builds = d.builds || {};
    // migrate every build in place so the rest of the code only sees `envelopes`
    for (const id of Object.keys(state.builds)) migrateBuild(state.builds[id]);
    state.activeBuildId = d.activeBuildId && state.builds[d.activeBuildId] ? d.activeBuildId : null;
    saveBuilds();
  } catch (e) { /* ignore */ }
}

function newBuildId() {
  return "b" + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36);
}

function createBuild(name) {
  const id = newBuildId();
  const n = (name || "").trim() || `Build ${Object.keys(state.builds).length + 1}`;
  state.builds[id] = {id, name: n, envelopes: [], created: Date.now()};
  state.activeBuildId = id;
  saveBuilds();
  return id;
}

function deleteBuild(id) {
  if (!state.builds[id]) return;
  if (!confirm(`Delete "${state.builds[id].name}"? This can't be undone.`)) return;
  delete state.builds[id];
  if (state.activeBuildId === id) state.activeBuildId = Object.keys(state.builds)[0] || null;
  if (state.viewingBuildId === id) state.viewingBuildId = null;
  saveBuilds();
  renderBuildPicker();
  renderSidebar();
  clearDetail();
  applyFilter();
}

function renameBuild(id) {
  const b = state.builds[id];
  if (!b) return;
  const n = prompt("Rename build:", b.name);
  if (n && n.trim()) {
    b.name = n.trim();
    saveBuilds();
    renderBuildPicker();
    renderSidebar();
    if (state.viewingBuildId === id) renderBuildDrawer(id);
  }
}

function duplicateBuild(id) {
  const b = state.builds[id];
  if (!b) return;
  const newId = createBuild(`${b.name} (copy)`);
  // clone envelopes so role edits don't leak between the two builds
  state.builds[newId].envelopes = b.envelopes.map(e => ({
    id: newEnvelopeId(), tubeUrl: e.tubeUrl, roles: {...e.roles},
  }));
  saveBuilds();
  renderBuildPicker();
  renderSidebar();
}

function promptNewBuild() {
  const n = prompt("New build name:", `Voxy ${String.fromCharCode(65 + Object.keys(state.builds).length)}`);
  if (n === null) return;
  createBuild(n);
  renderBuildPicker();
  renderSidebar();
}

function setActiveBuild(id) {
  if (!id || !state.builds[id]) return;
  state.activeBuildId = id;
  saveBuilds();
  renderBuildPicker();
  renderSidebar();
  renderBody();
}

function envelopesForTube(url) {
  const b = state.builds[state.activeBuildId];
  return b ? b.envelopes.filter(e => e.tubeUrl === url).length : 0;
}

function ensureActiveBuild() {
  if (state.activeBuildId && state.builds[state.activeBuildId]) return;
  if (!Object.keys(state.builds).length) {
    createBuild("Voxy A");   // silent first-time create so the first "+" click just works
  } else {
    state.activeBuildId = Object.keys(state.builds)[0];
  }
  saveBuilds();
  renderBuildPicker();
}

function addEnvelopeToActiveBuild(url) {
  ensureActiveBuild();
  const b = state.builds[state.activeBuildId];
  b.envelopes.push({id: newEnvelopeId(), tubeUrl: url, roles: {}});
  saveBuilds();
  renderBuildPicker();
  renderSidebar();
  renderBody();
  if (state.viewingBuildId) renderBuildDrawer(state.viewingBuildId);
}

function removeEnvelope(buildId, envelopeId) {
  const b = state.builds[buildId];
  if (!b) return;
  b.envelopes = b.envelopes.filter(e => e.id !== envelopeId);
  saveBuilds();
  renderBuildPicker();
  renderSidebar();
  renderBody();
  renderBuildDrawer(buildId);
}

function setEnvelopeRole(buildId, envelopeId, sectionIdx, roleId) {
  const b = state.builds[buildId];
  if (!b) return;
  const env = b.envelopes.find(e => e.id === envelopeId);
  if (!env) return;
  if (!roleId) delete env.roles[sectionIdx];
  else         env.roles[sectionIdx] = roleId;
  saveBuilds();
  renderBuildDrawer(buildId);
}

/* ---- rendering ---- */

function renderAddButton(r) {
  const n = envelopesForTube(r.url);
  const inBuild = n > 0;
  const tooltip = inBuild
    ? `${n} envelope${n === 1 ? "" : "s"} in active build — click to add another (remove in the build drawer)`
    : "Add an envelope to the active build";
  return `<span class="add-btn ${inBuild ? "in-build" : ""}" title="${tooltip}">+</span>`;
}

function renderBuildPicker() {
  const el = document.getElementById("buildPicker");
  const builds = Object.values(state.builds);
  if (!builds.length) {
    el.innerHTML = `<button onclick="promptNewBuild()">+ New build</button>`;
    return;
  }
  const options = builds.map(b => `<option value="${b.id}" ${b.id === state.activeBuildId ? "selected" : ""}>${escapeHtml(b.name)} (${b.envelopes.length})</option>`).join("");
  el.innerHTML = `
    <span>Build:</span>
    <select id="buildSelect">${options}</select>
    <span class="badge" title="Envelopes in active build">${state.builds[state.activeBuildId]?.envelopes.length ?? 0}</span>
    <button onclick="viewActiveBuild()" title="View build details">view</button>
    <button onclick="promptNewBuild()" title="Create a new build">+</button>
  `;
  document.getElementById("buildSelect").addEventListener("change", e => setActiveBuild(e.target.value));
}

function viewActiveBuild() {
  if (state.activeBuildId) viewBuild(state.activeBuildId);
}

function viewBuild(id) {
  state.viewingBuildId = id;
  state.selectedUrl = null;
  renderBuildDrawer(id);
}

function clearDetail() {
  state.viewingBuildId = null;
  document.getElementById("detail").innerHTML = `<div class="empty">Select a tube to see how it scores for each Voxy slot.</div>`;
}

function buildEnvelopeData(id) {
  const b = state.builds[id];
  if (!b) return [];
  const byUrl = new Map(state.rows.map(r => [r.url, r]));
  return b.envelopes.map(env => ({
    envelope: env,
    tube: byUrl.get(env.tubeUrl) || {url: env.tubeUrl, name: env.tubeUrl, in_stock: false, price: null, heater_v: null, elements: []},
  }));
}

function hasAnyRole(env) {
  return env && env.roles && Object.keys(env.roles).length > 0;
}

function railSummary(envelopeData) {
  // Only envelopes that are actually going into the amp count against the
  // 3-rail heater budget. An envelope with no role assigned is treated as a
  // shopping-list extra — priced and shown in the cart, but doesn't fight the
  // amp for a heater voltage.
  const bins = new Map();
  for (const {envelope, tube} of envelopeData) {
    if (!hasAnyRole(envelope)) continue;
    if (tube.heater_v == null) continue;
    const k = tube.heater_v;
    if (!bins.has(k)) bins.set(k, {v: k, count: 0, amps: 0, names: []});
    const bin = bins.get(k);
    bin.count += 1;
    bin.amps += (tube.heater_a || 0);
    bin.names.push(tube.matched_key || tube.name);
  }
  return [...bins.values()].sort((a,b) => a.v - b.v);
}

function assignedRoleSet(envelopeData) {
  // What roles are already claimed anywhere in the build? Used to grey out duplicates.
  const claimed = new Map();  // roleId → [envId, sectionIdx]
  for (const {envelope} of envelopeData) {
    for (const [si, rid] of Object.entries(envelope.roles || {})) {
      if (!claimed.has(rid)) claimed.set(rid, []);
      claimed.get(rid).push({envId: envelope.id, sectionIdx: +si});
    }
  }
  return claimed;
}

function renderBuildDrawer(id) {
  const b = state.builds[id];
  if (!b) return;
  const envData = buildEnvelopeData(id);
  const total = envData.reduce((s, e) => s + (typeof e.tube.price === "number" ? e.tube.price : 0), 0);
  // Split by whether the envelope has any role assigned. Assigned envelopes go
  // into the amp — they count against the heater budget and must fit the chassis.
  // Unassigned envelopes are pure shopping-list extras (parts you want to buy
  // alongside the build) — they don't consume rails or trigger fit warnings.
  const assigned = envData.filter(e => hasAnyRole(e.envelope));
  const unassigned = envData.filter(e => !hasAnyRole(e.envelope));
  const rails = railSummary(envData);
  const overRailBudget = rails.length > 3;
  const overRailWarn = rails.length === 3;
  const tooBig = assigned.filter(e => e.tube.fits_chassis === false);
  const outOfStock = envData.filter(e => e.tube.in_stock === false);
  const claimed = assignedRoleSet(envData);

  let html = `<div class="build-drawer">`;
  const subBits = [`${envData.length} envelope${envData.length===1?"":"s"}`];
  if (unassigned.length) subBits.push(`${assigned.length} assigned · ${unassigned.length} cart-only`);
  subBits.push(total ? "CAD $"+total.toFixed(2) : "no price");
  html += `<h2>${escapeHtml(b.name)} <span class="sub">· ${subBits.join(" · ")}</span></h2>`;
  html += `<div class="actions">`;
  html += `<button class="primary" onclick="pushBuildToStoreCart('${b.id}')" title="Sync this build to TubeHunter's pending cart so the store-cart bookmarklet can add them in one click">Push to store cart ↗</button>`;
  html += `<button onclick="copyBuildSummary('${b.id}')">Copy summary</button>`;
  html += `<button onclick="renameBuild('${b.id}')">Rename</button>`;
  html += `<button onclick="duplicateBuild('${b.id}')">Duplicate</button>`;
  html += `<button class="danger" onclick="deleteBuild('${b.id}')">Delete</button>`;
  html += `</div>`;

  if (!assigned.length) {
    html += `<div class="ok">No tubes assigned to amp slots yet — heater rails and chassis-fit only count assigned tubes.</div>`;
  } else if (overRailBudget) {
    html += `<div class="warn"><b>Over budget:</b> assigned tubes need ${rails.length} distinct heater rails but the amp only supports 3. Swap something to a shared voltage or drop an assignment.</div>`;
  } else if (overRailWarn) {
    html += `<div class="warn">Using the maximum 3 heater rails. Any additional assigned tube on a fresh voltage will push over-budget.</div>`;
  } else if (rails.length && !tooBig.length) {
    html += `<div class="ok">Fits: ${rails.length} heater rail${rails.length===1?"":"s"} used (of 3 available).</div>`;
  }

  if (tooBig.length) {
    const names = [...new Set(tooBig.map(e => e.tube.name))];
    html += `<div class="warn"><b>Assigned tube won't fit chassis:</b> ${names.map(escapeHtml).join(", ")}. Voxy only accepts 7-pin or 9-pin sockets.</div>`;
  }
  if (outOfStock.length) {
    const names = [...new Set(outOfStock.map(e => e.tube.name))];
    html += `<div class="warn">Out of stock right now: ${names.map(escapeHtml).join(", ")}.</div>`;
  }

  if (rails.length) {
    html += `<div class="rails">`;
    for (const r of rails) {
      const amps = r.amps ? ` · ${r.amps.toFixed(2)} A` : "";
      html += `<div class="rail"><b>${r.v} V</b> · ${r.count}× env${r.count===1?"":"s"}${amps}</div>`;
    }
    html += `</div>`;
  }

  if (!envData.length) {
    html += `<div class="empty" style="color:#666; font-style:italic; padding: 10px 0">No envelopes yet. Click + on any tube row to add one.</div>`;
  } else {
    // ---- shopping list (consolidated for the store cart) ----
    const cart = consolidateEnvelopes(envData);
    html += `<div class="shop-header">Shopping list — click each tube to open its product page with quantity pre-filled, then hit "Add to Cart" on the store:</div>`;
    html += `<div class="shop-list">`;
    let shopTotal = 0;
    for (const item of cart) {
      const t = item.tube;
      const stockDot = t.in_stock ? '<span style="color:#1e5a2c" title="In stock">●</span>' : '<span style="color:#a03030" title="Out of stock">●</span>';
      const each = typeof t.price === "number" ? t.price : null;
      const lineCost = each != null ? each * item.qty : null;
      if (lineCost != null) shopTotal += lineCost;
      const eachStr = each != null ? `CAD $${each.toFixed(2)}` : "—";
      const lineStr = lineCost != null ? `CAD $${lineCost.toFixed(2)}` : "—";
      const url = `https://www.thetubestore.com${t.url}?quantity=${item.qty}`;
      html += `<div class="shop-line">
        <span>${stockDot}</span>
        <span class="shop-qty">${item.qty}×</span>
        <span class="shop-name">${escapeHtml(t.name)}</span>
        <span class="shop-each">${eachStr}</span>
        <span class="shop-line-cost">${lineStr}</span>
        <a class="shop-open" href="${url}" target="_blank" rel="noopener" title="Open product page with quantity ${item.qty} pre-filled">Open →</a>
      </div>`;
    }
    html += `<div class="shop-total">Cart total: <b>CAD $${shopTotal.toFixed(2)}</b></div>`;
    html += `</div>`;

    // ---- envelope-level view (role tagging) ----
    html += `<div class="envelope-list">`;
    envData.forEach((ed, idx) => {
      const t = ed.tube;
      const env = ed.envelope;
      const priceCol = t.price != null ? "CAD $" + t.price.toFixed(2) : "—";
      const stockDot = t.in_stock ? '<span style="color:#1e5a2c" title="In stock">●</span>' : '<span style="color:#a03030" title="Out of stock">●</span>';
      const cat = t.category ? `<span class="cat-pill cat-${t.category}" style="font-size:9px">${prettyCat(t.category)}</span>` : "";
      const heater = t.heater_v ? `${t.heater_v} V${t.heater_a ? " · " + t.heater_a + " A" : ""}` : "—";
      const sections = sectionsOf(t.elements);

      html += `<div class="envelope">`;
      html += `<div class="envelope-head">
        <span class="envelope-num">${idx + 1}.</span>
        <span>${stockDot}</span>
        <span class="envelope-name"><a href="https://www.thetubestore.com${t.url}" target="_blank" rel="noopener">${escapeHtml(t.name)}</a> ${cat}</span>
        <span class="envelope-heater">${heater}</span>
        <span class="envelope-price">${priceCol}</span>
        <span class="remove" onclick="removeEnvelope('${b.id}','${env.id}')" title="Remove this envelope">×</span>
      </div>`;

      if (sections.length === 0) {
        html += `<div class="envelope-sections"><span style="color:#888">unclassified — no sections known</span></div>`;
      } else {
        html += `<div class="envelope-sections">`;
        for (const s of sections) {
          html += renderSectionSelector(b.id, env, s, claimed);
        }
        html += `</div>`;
      }
      html += `</div>`;
    });
    html += `</div>`;
  }
  html += `</div>`;
  document.getElementById("detail").innerHTML = html;
}

function renderSectionSelector(buildId, env, section, claimed) {
  const currentRole = env.roles[section.idx] || "";
  const options = rolesForSection(section.type);
  if (!options.length) {
    return `<span class="section-tag"><span class="section-label">${escapeHtml(section.label)}</span><span class="section-role muted">n/a</span></span>`;
  }
  let opts = `<option value="">— unassigned —</option>`;
  for (const r of options) {
    const also = (claimed.get(r.id) || []).filter(c => !(c.envId === env.id && c.sectionIdx === section.idx));
    const dup = also.length ? " ⚠" : "";
    const sel = r.id === currentRole ? " selected" : "";
    opts += `<option value="${r.id}"${sel}>${escapeHtml(r.label)}${dup}</option>`;
  }
  return `<span class="section-tag">
    <span class="section-label">${escapeHtml(section.label)}</span>
    →
    <select class="section-role" onchange="setEnvelopeRole('${buildId}','${env.id}',${section.idx}, this.value)">${opts}</select>
  </span>`;
}

/* ---- actions ---- */

function buildToMarkdown(id) {
  const b = state.builds[id];
  if (!b) return "";
  const envData = buildEnvelopeData(id);
  const assigned = envData.filter(e => hasAnyRole(e.envelope));
  const unassigned = envData.filter(e => !hasAnyRole(e.envelope));
  const total = envData.reduce((s, e) => s + (typeof e.tube.price === "number" ? e.tube.price : 0), 0);
  const rails = railSummary(envData);

  const lines = [];
  lines.push(`# ${b.name}`);
  lines.push("");
  const summaryBits = [`${envData.length} envelope${envData.length===1?"":"s"}`];
  if (unassigned.length) summaryBits.push(`${assigned.length} assigned to amp · ${unassigned.length} cart-only`);
  summaryBits.push(`CAD $${total.toFixed(2)}`);
  summaryBits.push(`${rails.length} heater rail${rails.length===1?"":"s"}${rails.length ? " (" + rails.map(r=>r.v+"V").join(", ") + ")" : ""}`);
  lines.push(summaryBits.join(" · "));
  lines.push("");

  // Roles → envelope table: shows how each Voxy slot gets filled.
  lines.push("## Role assignments");
  const roleTable = [];
  for (const r of ROLES) {
    const claimants = [];
    envData.forEach((ed, i) => {
      for (const [si, rid] of Object.entries(ed.envelope.roles || {})) {
        if (rid !== r.id) continue;
        const s = sectionsOf(ed.tube.elements)[+si];
        claimants.push(`envelope ${i+1} (${ed.tube.matched_key || ed.tube.name}) · ${s ? s.label : "section " + si}`);
      }
    });
    if (claimants.length) {
      for (const c of claimants) roleTable.push(`- **${r.label}** — ${c}`);
    }
  }
  if (roleTable.length) roleTable.forEach(l => lines.push(l));
  else                  lines.push("_no roles assigned yet_");
  lines.push("");

  // Envelope list with per-section role annotations
  lines.push("## Envelopes");
  lines.push("| # | Tube | Sections → role | Socket | Heater | Diss | Price | Stock |");
  lines.push("|---|---|---|---|---|---|---|---|");
  envData.forEach((ed, i) => {
    const t = ed.tube;
    const env = ed.envelope;
    const sections = sectionsOf(t.elements);
    const sectRoles = sections.length
      ? sections.map(s => {
          const rid = env.roles[s.idx];
          const roleLabel = rid ? ROLE_BY_ID[rid]?.label || rid : "—";
          return `${s.label} → ${roleLabel}`;
        }).join(", ")
      : "—";
    const heater = t.heater_v ? `${t.heater_v} V / ${t.heater_a ?? "?"} A` : "—";
    const priceMd = t.price != null ? "CAD $" + t.price.toFixed(2) : "—";
    lines.push(`| ${i+1} | [${t.name}](https://www.thetubestore.com${t.url}) | ${sectRoles} | ${t.socket || "?"} | ${heater} | ${t.plate_diss ?? "—"} | ${priceMd} | ${t.in_stock ? "in stock" : "OUT"} |`);
  });
  lines.push("");

  lines.push("## Heater rails");
  for (const r of rails) {
    lines.push(`- **${r.v} V** — ${r.count}× envelope${r.count===1?"":"s"}${r.amps ? " · " + r.amps.toFixed(2) + " A" : ""} (${r.names.join(", ")})`);
  }
  if (rails.length > 3) lines.push(`- ⚠ **Over 3-rail budget** — Voxy amp supports 3 distinct heater voltages max.`);
  return lines.join("\n");
}

async function copyBuildSummary(id) {
  const md = buildToMarkdown(id);
  try {
    await navigator.clipboard.writeText(md);
    flashToast("Build summary copied to clipboard");
  } catch (e) {
    // clipboard requires HTTPS or user-gesture in some browsers; fall back to a textarea
    const ta = document.createElement("textarea");
    ta.value = md; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); flashToast("Copied (fallback)"); }
    catch (_) { alert(md); }
    finally { ta.remove(); }
  }
}

/* Consolidate envelopes by tube URL so 2× 11BM8 becomes one product page (qty=2)
   instead of two separate tabs / duplicate cart lines. */
function consolidateEnvelopes(envData) {
  const by = new Map();
  for (const ed of envData) {
    const key = ed.tube.url;
    if (!by.has(key)) by.set(key, {url: key, tube: ed.tube, qty: 0});
    by.get(key).qty += 1;
  }
  return [...by.values()];
}

/* addBuildToStoreCart used to open every product page in a batch of tabs, but
   thetubestore's add-to-cart modal makes that flow noisy. The shopping list in
   the drawer now shows one "Open →" link per unique tube instead — user opens
   one, adds to cart, closes, moves on. */

async function pushBuildToStoreCart(id) {
  const b = state.builds[id];
  if (!b) return;
  const envData = buildEnvelopeData(id);
  if (!envData.length) { flashToast("No tubes in this build"); return; }

  const rawItems = consolidateEnvelopes(envData).map(it => ({
    url: it.url,
    qty: it.qty,
    name: it.tube.name,
    internalid: it.tube.internalid,
  }));

  flashToast("Resolving internal IDs…");
  let resolved;
  try {
    const r = await fetch("/api/pending-cart", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({build_name: b.name, items: rawItems, return_items: true}),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    resolved = await r.json();
  } catch (e) {
    alert("Push failed: " + e.message);
    return;
  }

  const items = (resolved.resolved_items || []).map(it => ({
    internalid: it.internalid,
    qty: it.qty,
    name: it.name,
  }));
  if (!items.length) {
    alert("Couldn't resolve any tubes to add. Use 'Open →' on each tube in the shopping list instead.");
    return;
  }

  const payload = { _tubehunter: true, build_name: b.name, items };
  const json = JSON.stringify(payload);
  showCartCopyModal(json, items, resolved.missing || [], b.name);
}

// Explicit modal + user-clicked Copy button. Safari (and, for that matter, all
// modern browsers) require *fresh* user activation for clipboard writes, and
// the activation from the original 'Push' click is consumed by the async fetch.
// Requiring the user to click Copy inside the modal gives us new activation.
function showCartCopyModal(json, items, missing, buildName) {
  const total = items.reduce((s, it) => s + it.qty, 0);
  const overlay = document.createElement("div");
  overlay.className = "cart-overlay";
  overlay.innerHTML = `
    <div class="cart-modal">
      <h2>Ready to push · ${escapeHtml(buildName)}</h2>
      <div class="cart-modal-summary">
        <b>${total}</b> tube${total===1?"":"s"} in <b>${items.length}</b> line${items.length===1?"":"s"} ready to add to your tubestore cart.
      </div>
      ${missing.length ? `<div class="cart-modal-warn">Couldn't resolve: ${missing.map(m => escapeHtml(m.name || m.url)).join(", ")} — these will be skipped.</div>` : ""}
      <ol class="cart-modal-steps">
        <li><b>Click Copy below</b> to put the cart on your clipboard.</li>
        <li>Switch to a tab open on <b>thetubestore.com</b>.</li>
        <li>Click your <b>🛒 TubeHunter Cart</b> bookmarklet. (Safari may show a Paste prompt — click it.)</li>
      </ol>
      <div class="cart-modal-actions">
        <button class="cart-copy-btn" id="cart-copy-btn">📋 Copy cart to clipboard</button>
        <button class="cart-cancel-btn" id="cart-cancel-btn">Close</button>
      </div>
      <details class="cart-modal-details">
        <summary>Manual: select all text below and copy (Cmd-C) if the button doesn't work</summary>
        <textarea readonly rows="4">${escapeHtml(json)}</textarea>
      </details>
    </div>
  `;
  document.body.appendChild(overlay);

  const copyBtn = overlay.querySelector("#cart-copy-btn");
  const cancelBtn = overlay.querySelector("#cart-cancel-btn");

  function closeModal() { overlay.remove(); }
  cancelBtn.addEventListener("click", closeModal);
  overlay.addEventListener("click", e => { if (e.target === overlay) closeModal(); });

  copyBtn.addEventListener("click", async () => {
    // This click has fresh user activation. Try both clipboard mechanisms.
    let ok = false;
    try {
      await navigator.clipboard.writeText(json);
      ok = true;
    } catch (e) { /* fall through to execCommand */ }
    if (!ok) {
      const ta = document.createElement("textarea");
      ta.value = json;
      ta.style.position = "fixed"; ta.style.top = "-9999px";
      document.body.appendChild(ta);
      ta.focus(); ta.select(); ta.setSelectionRange(0, json.length);
      try { ok = document.execCommand("copy"); } catch (_) {}
      ta.remove();
    }
    if (ok) {
      copyBtn.textContent = "✓ Copied — now switch to a tubestore tab";
      copyBtn.classList.add("copied");
      copyBtn.disabled = true;
    } else {
      copyBtn.textContent = "Couldn't copy automatically — use the textarea below";
      copyBtn.classList.add("failed");
    }
  });
}

/* NOTE: I tried a bulk cart URL (?additems=ID1,ID2&qty=1,2) but this SC-Advanced
   deployment doesn't intercept those params — the page loads with an empty cart.
   The reliable path is opening the individual product pages in tabs (with the
   quantity pre-filled via ?quantity=N) and having the user click Add to Cart on
   each. Cookie-based session keeps the cart accumulating across tabs. */

function flashToast(msg) {
  let t = document.getElementById("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    t.style.cssText = "position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#3a63b8;color:#fff;padding:8px 14px;border-radius:6px;font-size:12px;box-shadow:0 4px 12px rgba(0,0,0,0.25);z-index:9999;transition:opacity 0.3s;";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.opacity = "1";
  clearTimeout(t._h);
  t._h = setTimeout(() => t.style.opacity = "0", 2000);
}

/* wire keyboard: 'v' opens active build, 'n' creates one */
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
  if (e.key === "v" && state.activeBuildId) { viewActiveBuild(); }
  if (e.key === "n" && !e.ctrlKey && !e.metaKey) { promptNewBuild(); }
});

boot();
</script>
</body>
</html>
"""

CART_BOOKMARKLET_ORIGIN = "https://www.thetubestore.com"

# ---------------------------------------------------------------------------
# BOOKMARKLET — click while on any thetubestore.com page to sync the pending
# cart from TubeHunter to the real store cart.
#
# Written here as readable JS with comments so the source-of-truth is greppable;
# the minified single-line form (for the href="javascript:…" link in the setup
# page) is produced by _minify_bookmarklet() below.
# ---------------------------------------------------------------------------

BOOKMARKLET_JS = r"""
(async function tubehunterCart() {
  const TAG = 'TubeHunter →';
  function note(msg, isError) {
    // Discreet floating banner, top-right, dismisses after a few seconds.
    let el = document.getElementById('tubehunter-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'tubehunter-toast';
      el.style.cssText =
        'position:fixed;top:16px;right:16px;z-index:2147483647;' +
        'padding:10px 14px;border-radius:6px;font:13px/1.4 -apple-system,Segoe UI,Helvetica,sans-serif;' +
        'box-shadow:0 6px 18px rgba(0,0,0,0.25);max-width:340px;';
      document.body.appendChild(el);
    }
    el.style.background = isError ? '#b03030' : '#2a4dab';
    el.style.color = '#fff';
    el.textContent = TAG + ' ' + msg;
    clearTimeout(el._t);
    el._t = setTimeout(() => el.remove(), 8000);
  }

  try {
    // Read the pending cart directly from the clipboard. TubeHunter's
    // 'Push to store cart' button writes a JSON payload there. Safari and
    // Chrome both allow navigator.clipboard.readText() from a bookmarklet click
    // because the click counts as a user gesture. Safari will pop a small
    // 'Paste' confirmation the first time — click it once.
    let raw;
    try {
      raw = await navigator.clipboard.readText();
    } catch (e) {
      throw new Error('Clipboard blocked — allow the paste prompt if Safari shows one, or copy the cart from TubeHunter first.');
    }
    let payload;
    try { payload = JSON.parse(raw); }
    catch (e) { throw new Error('Clipboard is not a TubeHunter cart. Click "Push to store cart" in TubeHunter first.'); }
    if (!payload || payload._tubehunter !== true || !Array.isArray(payload.items)) {
      throw new Error('Clipboard is not a TubeHunter cart. Click "Push to store cart" in TubeHunter first.');
    }
    const items = payload.items;
    if (!items.length) { note('Cart is empty.', true); return; }

    // Locate the store's AMD registry. Their production build exposes either
    // window.require or window.SCM, depending on load state.
    const req = window.require || (window.SCM ? function (n) { return window.SCM[n]; } : null);
    if (!req) throw new Error("Couldn't find the store's module registry (require/SCM). Reload the tubestore tab and try again.");

    // Cart model singleton — same instance the mini-cart in the header uses.
    let cart;
    try {
      const CartModel = req('LiveOrder.Model');
      cart = CartModel.getInstance();
      if (cart.loadCart && (!cart.get('lines') || cart.isLoading)) {
        await cart.loadCart();
      }
    } catch (e) {
      throw new Error("Couldn't grab LiveOrder.Model — is this a tubestore.com page?");
    }
    const LineModel = req('LiveOrder.Line.Model');

    // Build Line models for every item, then hand them to addLines() so the
    // store does its usual bulk-add flow (a single confirmation modal, cart
    // state updates, mini-cart re-renders).
    const lines = [];
    for (const it of items) {
      lines.push(new LineModel({
        item: { internalid: String(it.internalid) },
        quantity: it.qty,
      }));
    }

    const promise = cart.addLines(lines);
    promise.done(function () {
      const totalQty = items.reduce((s, it) => s + it.qty, 0);
      note('Added ' + totalQty + ' tube' + (totalQty === 1 ? '' : 's') + ' to your cart.');
    }).fail(function (err) {
      let msg = 'Add-to-cart failed';
      if (err && err.responseText) {
        try { msg += ': ' + (JSON.parse(err.responseText).errorMessage || err.responseText.slice(0, 200)); }
        catch (_) { msg += ': ' + err.responseText.slice(0, 200); }
      }
      note(msg, true);
    });
  } catch (e) {
    note(e.message || String(e), true);
  }
})();
"""

def _minify_bookmarklet(js: str) -> str:
    """Squash the readable JS into a single line for the javascript: URL.
    Strips block comments, line comments, and collapses runs of whitespace.

    Aggressively percent-encodes everything that could break inside an
    `href="…"` attribute — most notably `"`, `<`, `>`, and `&`. Keeps a wide
    set of URL-legal punctuation in raw form so the URL stays readable when
    the user peeks at it, but the encoded bytes still form a valid href."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    js = re.sub(r"(^|\s)//[^\n]*", "", js)
    js = re.sub(r"\s+", " ", js).strip()
    # Note: '"', '<', '>', '&', '#', '%' are DELIBERATELY not in the safe set —
    # they either break the HTML attribute (") or the URL parser (%, #), or make
    # the anchor text harder for the browser to detect during drag (< > &).
    return "javascript:" + urllib.parse.quote(js, safe="(){}[]:;,.=+-*/?!|'` ")

BOOKMARKLET_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TubeHunter — Store Cart Bookmarklet</title>
<style>
  body { font: 14px/1.5 -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
         max-width: 760px; margin: 40px auto; padding: 0 20px; color: #222; }
  h1 { margin: 0 0 4px; font-size: 22px; }
  h2 { margin: 24px 0 6px; font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; color: #555; }
  ol, ul { padding-left: 20px; }
  ol li, ul li { margin-bottom: 6px; }
  .drag-me {
    display: inline-block; padding: 10px 20px; margin: 10px 0;
    background: linear-gradient(180deg, #6c8bd6, #3a63b8);
    color: #fff; text-decoration: none; border-radius: 6px; font-weight: 600;
    box-shadow: 0 3px 8px rgba(0,0,0,0.2); cursor: grab;
  }
  .drag-me:hover { background: linear-gradient(180deg, #7c9be6, #4a73c8); }
  .drag-me:active { cursor: grabbing; }
  .callout {
    background: #eef3ff; border-left: 3px solid #3a63b8;
    padding: 10px 14px; margin: 10px 0; border-radius: 3px;
  }
  .warn-box {
    background: #fff2c4; border-left: 3px solid #d1a833;
    padding: 10px 14px; margin: 10px 0; border-radius: 3px;
  }
  .method {
    border: 1px solid #dae0e8; border-radius: 6px;
    padding: 14px 18px; margin: 10px 0;
    background: #fafcff;
  }
  .method h3 { margin: 0 0 6px; font-size: 15px; color: #17376b; }
  code {
    background: #f4f6fa; border: 1px solid #dae0e8; border-radius: 3px;
    padding: 1px 4px; font: 12px/1.5 SF Mono, Menlo, Consolas, monospace;
  }
  pre {
    background: #f4f6fa; border: 1px solid #dae0e8; border-radius: 4px;
    padding: 12px; font: 11px/1.5 SF Mono, Menlo, Consolas, monospace;
    overflow-x: auto; white-space: pre-wrap; word-break: break-all;
  }
  textarea.copy-target {
    width: 100%; height: 120px; box-sizing: border-box;
    font: 11px/1.5 SF Mono, Menlo, Consolas, monospace;
    padding: 8px; border: 1px solid #dae0e8; border-radius: 4px;
    background: #fafcff; resize: vertical;
  }
  button.copy-btn {
    background: linear-gradient(180deg, #fefefe, #d2d7de);
    border: 1px solid #a8b0bb; border-radius: 4px;
    padding: 4px 12px; font: 13px inherit; cursor: pointer; margin-top: 4px;
  }
  button.copy-btn:hover { background: linear-gradient(180deg, #fff, #e2e7ee); }
  kbd {
    background: #eee; border: 1px solid #bbb; border-bottom-width: 2px;
    border-radius: 3px; padding: 0 5px; font: 11px inherit;
  }
  .footnote { color: #666; font-size: 12px; margin-top: 30px; }
</style>
</head>
<body>
<h1>TubeHunter → Store Cart</h1>
<p>A one-click bookmarklet that syncs a TubeHunter build into your real
   <a href="https://www.thetubestore.com/" target="_blank">thetubestore.com</a> cart.</p>

<h2>Step 1 — Install the bookmarklet</h2>

<div class="method">
  <h3>Method A · Drag the button</h3>
  <p>Show your bookmarks bar first (<kbd>⌘⇧B</kbd> in Chrome/Safari toggles it). Then drag this button onto the bar:</p>
  <a class="drag-me" href="__BOOKMARKLET_URL__" title="TubeHunter Cart">🛒 TubeHunter Cart</a>

  <div class="warn-box">
    <b>If the bookmark shows a long line of code as its name</b>, that's just Chrome/Safari using the URL as the title when the drag happens fast. To fix it: right-click the bookmark on your bar → <b>Edit</b> (or <b>Rename</b>) → change the <b>Name</b> to <code>TubeHunter Cart</code> (or whatever you want). The bookmark itself still works. This is a one-time cleanup.
  </div>
</div>

<div class="method">
  <h3>Method B · Copy the URL manually</h3>
  <p>If dragging isn't working, install it by hand:</p>
  <ol>
    <li>Copy the code below (there's a button).</li>
    <li>Right-click on your bookmarks bar → <b>Add page…</b> (Chrome) or <b>Add Bookmark for This Page</b> (Safari — then edit it).</li>
    <li>Set the <b>Name</b> to <code>TubeHunter Cart</code>.</li>
    <li>Paste the copied code into the <b>URL</b> field.</li>
    <li>Save.</li>
  </ol>
  <textarea class="copy-target" id="bookmarklet-url" readonly>__BOOKMARKLET_URL_TEXT__</textarea>
  <button class="copy-btn" onclick="
    const ta = document.getElementById('bookmarklet-url');
    ta.select(); ta.setSelectionRange(0, 99999);
    document.execCommand('copy');
    this.textContent = '✓ Copied';
    setTimeout(() => this.textContent = 'Copy to clipboard', 1500);
  ">Copy to clipboard</button>
</div>

<h2>Step 2 — Use it</h2>
<ol>
  <li>Open a build in TubeHunter, hit <b>Push to store cart ↗</b>. The cart gets copied to your clipboard as a small JSON blob.</li>
  <li>Switch to a browser tab open on <b>thetubestore.com</b> (any page — homepage, a product, or the cart).</li>
  <li>Click the <b>🛒 TubeHunter Cart</b> bookmark in your bookmarks bar.</li>
  <li>Safari may pop a small "Paste" confirmation the first time — click it. (Chrome/Firefox skip this.)</li>
  <li>A small blue banner appears at the top-right confirming how many tubes got added.</li>
  <li>Click the store's cart icon to review and check out.</li>
</ol>

<h2>How it works (nerd corner)</h2>
<p>The bookmarklet runs <i>inside thetubestore.com</i>'s page, so it has your session cookie
   and can talk to their cart. To get the list of tubes to add, it reads them from the
   clipboard — that avoids any cross-origin request to localhost (which Safari blocks even
   over HTTPS when the cert isn't in the system Keychain). TubeHunter's "Push to store cart"
   writes a JSON payload like <code>{"_tubehunter":true,"items":[{"internalid":2845,"qty":1}]}</code>
   to your clipboard. The bookmarklet reads that with <code>navigator.clipboard.readText()</code>
   (allowed because the bookmarklet click counts as user activation), then calls the store's own
   <code>LiveOrder.Model.getInstance().addLines(...)</code> to add each line.</p>

<h2>Bookmarklet source (readable)</h2>
<p>The URL above is the minified single-line form. Here's the readable source in case you want to eyeball what it does before installing:</p>
<pre>__BOOKMARKLET_SOURCE__</pre>

<p class="footnote">
  If TubeHunter isn't running when you click the bookmarklet, or you haven't pushed a build,
  the banner will say so and nothing bad happens. If <a href="https://www.thetubestore.com/">thetubestore.com</a>
  changes their JS internals and the bookmarklet stops working, edit
  <code>BOOKMARKLET_JS</code> in <code>tubehunter.py</code>.
</p>
</body>
</html>
"""

# Rendered lazily so we don't have to recompute per request; the minify pass
# is cheap enough to do inline the first time.
_bookmarklet_cache = {"url": None, "src": BOOKMARKLET_JS}

def _rendered_bookmarklet_page() -> str:
    if _bookmarklet_cache["url"] is None:
        _bookmarklet_cache["url"] = _minify_bookmarklet(BOOKMARKLET_JS)
    href = _bookmarklet_cache["url"]
    # Textarea content: replace HTML special chars so the URL renders literally,
    # not as parsed markup. Copy-to-clipboard reads from the textarea's .value
    # which is the decoded string, so the paste result is the raw URL.
    href_text = (href.replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))
    source_esc = (BOOKMARKLET_JS.strip()
                  .replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;"))
    return (BOOKMARKLET_PAGE
            .replace("__BOOKMARKLET_URL__", href)
            .replace("__BOOKMARKLET_URL_TEXT__", href_text)
            .replace("__BOOKMARKLET_SOURCE__", source_esc))

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TubeHunter/1.0"

    def _send_json(self, obj, status=200, cors=False):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            # Only permit the tubestore origin; the bookmarklet runs there.
            self.send_header("Access-Control-Allow-Origin", CART_BOOKMARKLET_ORIGIN)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _cors_preflight(self):
        origin = self.headers.get("Origin", "")
        self.send_response(204)
        if origin == CART_BOOKMARKLET_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", CART_BOOKMARKLET_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self):
        self._cors_preflight()

    def do_GET(self):
        app = self.server.app  # type: ignore[attr-defined]
        if self.path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/data":
            snap = app.snapshot
            self._send_json({
                "generated_at": snap.data.get("generated_at"),
                "products": snap.data.get("products", []),
                "refreshes_remaining": snap.refreshes_remaining(),
                "max_per_day": MAX_REFRESHES_PER_DAY,
                "refresh_in_progress": app.runner.in_progress,
            })
            return
        if self.path == "/api/pending-cart":
            with app.pending_cart_lock:
                snap = dict(app.pending_cart)
            self._send_json(snap, cors=True)
            return
        if self.path == "/bookmarklet":
            # Serve a friendly setup page with the current bookmarklet source and
            # instructions. Handy as a shareable link — no login needed.
            body = _rendered_bookmarklet_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/lookup"):
            # On-demand internalid lookup so the store-cart URL works even for
            # snapshots that predate the internalid field. One request per unique
            # tube in a build — a handful, well under any daily budget.
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            slug = (qs.get("url") or [""])[0]
            if not slug:
                self._send_json({"error": "missing url"}, status=400)
                return
            try:
                scraper = Scraper()
                encoded = urllib.parse.quote(slug, safe="")
                data = scraper._fetch_json(f"/api/items?url={encoded}&fieldset=search&currency=CAD")
                items = data.get("items") or []
                if items and items[0].get("internalid") is not None:
                    self._send_json({"internalid": items[0]["internalid"]})
                else:
                    self._send_json({"error": "not found"}, status=404)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=502)
            return
        if self.path == "/api/status":
            self._send_json({
                "refresh_in_progress": app.runner.in_progress,
                "progress": app.runner.progress_snapshot(),
                "refreshes_remaining": app.snapshot.refreshes_remaining(),
                "max_per_day": MAX_REFRESHES_PER_DAY,
                "generated_at": app.snapshot.data.get("generated_at"),
            })
            return
        self.send_error(404)

    def do_POST(self):
        app = self.server.app  # type: ignore[attr-defined]
        if self.path == "/api/refresh":
            result = app.runner.start()
            self._send_json(result)
            return
        if self.path == "/api/pending-cart":
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._send_json({"error": "invalid JSON"}, status=400)
                return
            raw_items = payload.get("items") or []
            # Fetch any missing internalids so the bookmarklet doesn't have to.
            scraper_singleton = getattr(app, "_lookup_scraper", None)
            if scraper_singleton is None:
                scraper_singleton = Scraper()
                app._lookup_scraper = scraper_singleton
            resolved = []
            missing = []
            for it in raw_items:
                url = it.get("url")
                if not url:
                    continue
                iid = it.get("internalid")
                if iid is None:
                    try:
                        encoded = urllib.parse.quote(url, safe="")
                        data = scraper_singleton._fetch_json(
                            f"/api/items?url={encoded}&fieldset=search&currency=CAD"
                        )
                        items_r = data.get("items") or []
                        iid = items_r[0].get("internalid") if items_r else None
                    except Exception as exc:
                        iid = None
                if iid is None:
                    missing.append({"url": url, "name": it.get("name")})
                    continue
                resolved.append({
                    "internalid": int(iid),
                    "qty": int(it.get("qty") or 1),
                    "url": url,
                    "name": it.get("name") or "",
                })
            with app.pending_cart_lock:
                app.pending_cart = {
                    "items": resolved,
                    "updated_at": now_iso(),
                    "build_name": payload.get("build_name"),
                }
            # Reply to the frontend. When return_items is set, echo the resolved
            # list so the frontend can hand it straight to the clipboard.
            reply = {
                "count": sum(it["qty"] for it in resolved),
                "unique": len(resolved),
                "missing": missing,
            }
            if payload.get("return_items"):
                reply["resolved_items"] = resolved
            self._send_json(reply)
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        # quieter default logging
        sys.stderr.write(f"[http] {fmt % args}\n")

class TubeHunterApp:
    def __init__(self):
        self.catalog = Catalog(CATALOG_PATH)
        self.ranker = VoxyRanker(VOXY_PATH)
        self.snapshot = Snapshot(SNAPSHOT_PATH)
        # Ephemeral cart shared with the tubestore bookmarklet. The frontend POSTs
        # a build's shopping list here; the bookmarklet (running on tubestore.com)
        # GETs it and adds each line to the user's real cart.
        self.pending_cart = {"items": [], "updated_at": None}
        self.pending_cart_lock = threading.Lock()
        # re-score existing snapshot against current catalog/voxy configs on boot,
        # preserving the raw store fields so the UI still shows price/stock/etc.
        prods = self.snapshot.data.get("products") or []
        if prods:
            raw = [{k: p.get(k) for k in PASSTHROUGH_FIELDS} for p in prods]
            self.snapshot.data["products"] = enrich(raw, self.catalog, self.ranker)
        self.runner = RefreshRunner(self.snapshot, self.catalog, self.ranker)

def ensure_ssl_cert():
    """Generate a long-lived self-signed cert for localhost the first time we
    boot, cache it under data/. Safari refuses HTTPS→HTTP-localhost fetches from
    tubestore.com even though the spec permits loopback, so the bookmarklet needs
    an HTTPS endpoint to talk to. Chrome/Firefox tolerate it but this way the
    tool works the same in every browser.

    Returns (cert_path, key_path) as strings.
    """
    cert_path = DATA / "tubehunter.crt"
    key_path = DATA / "tubehunter.key"
    if cert_path.exists() and key_path.exists():
        return str(cert_path), str(key_path)

    # Write an OpenSSL config with the SANs and EKUs Safari expects for a
    # server-auth cert. macOS ships /usr/bin/openssl (LibreSSL) which is fine
    # for this — no third-party CA involvement, we're just signing our own leaf.
    cfg = DATA / "openssl.tmp.cnf"
    cfg.write_text(
        "[req]\n"
        "distinguished_name = dn\n"
        "req_extensions = ext\n"
        "prompt = no\n\n"
        "[dn]\n"
        "CN = TubeHunter Local\n"
        "O = TubeHunter\n\n"
        "[ext]\n"
        "subjectAltName = IP:127.0.0.1,DNS:localhost\n"
        "keyUsage = digitalSignature, keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
    )
    print("[tubehunter] first launch — generating a self-signed TLS cert for localhost", file=sys.stderr)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048",
             "-keyout", str(key_path), "-out", str(cert_path),
             "-days", "3650", "-nodes",
             "-config", str(cfg), "-extensions", "ext"],
            check=True, capture_output=True,
        )
    except FileNotFoundError:
        raise SystemExit(
            "openssl not found — install it (macOS: it ships with the OS; Homebrew: brew install openssl) or "
            "run TubeHunter over plain HTTP for browsers other than Safari."
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"openssl failed to build the cert: {exc.stderr.decode('utf-8', 'replace')[:400]}")
    finally:
        cfg.unlink(missing_ok=True)

    print(f"[tubehunter] cert written to {cert_path}.", file=sys.stderr)
    print("[tubehunter] Safari will warn 'not private' the first time — click 'Show Details' → "
          "'visit this website' → 'Continue' and enter your password to trust it. One-time step.",
          file=sys.stderr)
    return str(cert_path), str(key_path)


def find_open_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("no free port")

def _arg(name, default=None, cast=str):
    """Tiny CLI arg helper for --key value or --key=value."""
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
        if a.startswith(name + "="):
            return cast(a.split("=", 1)[1])
    return default

def crawl_and_save(max_pages: int | None):
    """CLI-only path: run scraper, enrich, save snapshot. Skips server + rate limit
    so we can pre-seed data (or verify without burning the user's daily budget)."""
    global MAX_PAGES_PER_REFRESH
    if max_pages is not None:
        MAX_PAGES_PER_REFRESH = max_pages
    catalog = Catalog(CATALOG_PATH)
    ranker = VoxyRanker(VOXY_PATH)
    snap = Snapshot(SNAPSHOT_PATH)
    def log(msg): print(f"[crawl] {msg}", file=sys.stderr)
    raw = Scraper(on_progress=log).crawl()
    log(f"crawl done: {len(raw)} products")
    enriched = enrich(raw, catalog, ranker)
    snap.data["products"] = enriched
    snap.data["generated_at"] = now_iso()
    snap.data["progress"] = [f"CLI crawl at {now_iso()} — {len(enriched)} products"]
    snap.save()
    log(f"saved to {SNAPSHOT_PATH}")

def main():
    if "--crawl-now" in sys.argv:
        crawl_and_save(_arg("--max-pages", cast=int))
        return
    app = TubeHunterApp()
    port = find_open_port(PORT)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
    server.app = app  # type: ignore[attr-defined]
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"TubeHunter serving on {url}", file=sys.stderr)

    if "--refresh-now" in sys.argv:
        app.runner.start()

    # Preferred UX: open a native macOS window via pywebview (WKWebView under the
    # hood). No Safari chrome, own Dock icon (from the .app bundle), single
    # process. Falls back to launching the default browser if pywebview is not
    # installed. Pass --no-window to force browser mode.
    force_browser = "--no-window" in sys.argv or "--no-browser" in sys.argv
    try:
        import webview  # type: ignore
        have_webview = not force_browser
    except ImportError:
        have_webview = False

    if have_webview:
        # HTTP server runs in a background thread; the webview owns the main
        # thread (WKWebView requires it on macOS). Closing the window exits the
        # process, which stops the server.
        threading.Thread(target=server.serve_forever, daemon=True).start()

        # JS-callable API: WKWebView has no downloads handler, so clicking a
        # blob-download link navigates the window instead of saving. Route the
        # CSV export through pywebview.api.save_csv() which pops a native
        # macOS "Save…" panel and writes the file — window stays open.
        class WebviewApi:
            def save_csv(self, csv_text, default_filename):
                win = webview.windows[0]
                path = win.create_file_dialog(
                    webview.SAVE_DIALOG,
                    directory=str(Path.home() / "Downloads"),
                    save_filename=default_filename or "export.csv",
                    file_types=("CSV Files (*.csv)", "All files (*.*)"),
                )
                if not path:
                    return {"ok": False, "cancelled": True}
                # In save-mode the return type varies across pywebview versions —
                # sometimes a string, sometimes a tuple/list of one path.
                if isinstance(path, (list, tuple)):
                    path = path[0]
                try:
                    with open(path, "w", encoding="utf-8", newline="") as f:
                        f.write(csv_text)
                    return {"ok": True, "path": path}
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}

        webview.create_window(
            "TubeHunter · Voxy",
            url,
            width=1440, height=900,
            resizable=True,
            confirm_close=False,
            js_api=WebviewApi(),
        )
        # private_mode defaults to True in pywebview (WKWebView non-persistent
        # data store) which nukes localStorage every launch — our builds live
        # there. Point at a real on-disk directory so the WKWebView writes
        # cookies + localStorage + IndexedDB under ~/Library/Application Support.
        storage = Path.home() / "Library" / "Application Support" / "TubeHunter"
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(private_mode=False, storage_path=str(storage))
        return

    # Fallback: run the server in the foreground and open the URL in the
    # user's default browser. Kept for headless / CLI use and when pywebview
    # isn't available.
    if not force_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)

if __name__ == "__main__":
    main()
