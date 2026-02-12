"""
Smart preprocessing pipeline for scrape data.
Detects site characteristics, deduplicates content,
and fits everything into a fixed token budget.

Adapted to work with the section-based scrape format from scraper.py.
"""

import re
import json
import hashlib
from dataclasses import dataclass
from typing import Optional

from app.tool_handlers import build_asset_map

# Rough token estimation: 1 token ≈ 3.5 chars for mixed text/code
CHARS_PER_TOKEN = 3.5
TOTAL_TOKEN_BUDGET = 25000
SCREENSHOT_TOKEN_COST = 4000  # Fixed — one image always costs roughly this


@dataclass
class BudgetAllocation:
    """Dynamic token budget per category."""
    theme: int = 1000
    sections: int = 8000
    text_content: int = 3000
    links: int = 2000
    images: int = 1500
    svgs: int = 1000
    backgrounds: int = 500
    interactive: int = 2000
    asset_map: int = 1500

    @property
    def total_text(self) -> int:
        return (self.theme + self.sections + self.text_content +
                self.links + self.images + self.svgs +
                self.backgrounds + self.interactive + self.asset_map)

    @property
    def available(self) -> int:
        return TOTAL_TOKEN_BUDGET - SCREENSHOT_TOKEN_COST


def estimate_tokens(text: str) -> int:
    """Rough token count for a string."""
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN)


# ============================================================
# STEP 1: Detect Site Characteristics
# ============================================================

@dataclass
class SiteProfile:
    """Detected characteristics of the scraped site."""
    framework: str = "unknown"
    duplication_ratio: float = 1.0
    content_density: str = "normal"     # minimal, normal, heavy
    page_length: str = "normal"         # short (<2 viewports), normal (2-6), long (6+)
    has_code_blocks: bool = False
    has_pricing_table: bool = False
    has_carousel: bool = False
    has_faq: bool = False
    nav_complexity: str = "simple"      # simple, dropdown, mega-menu
    image_count: int = 0
    section_count: int = 0
    raw_text_tokens: int = 0


def detect_site_profile(data: dict) -> SiteProfile:
    """Analyze raw scrape data and build a site profile."""
    profile = SiteProfile()

    text = data.get("text_content", "")
    sections = data.get("sections", [])
    images = data.get("assets", {}).get("images", [])
    clickables = data.get("clickables", {})
    page_height = data.get("page_height", 0)
    react_info = data.get("react_info", {})
    ui_patterns = data.get("ui_patterns", [])

    profile.raw_text_tokens = estimate_tokens(text)
    profile.image_count = len(images)
    profile.section_count = len(sections)

    # --- Detect framework ---
    data_str = str(data)
    data_lower = data_str.lower()

    if "framerusercontent.com" in data_str or "data-framer" in data_lower:
        profile.framework = "framer"
    elif "wp-content" in data_str or "wordpress" in data_lower:
        profile.framework = "wordpress"
    elif "webflow" in data_lower or "wf-" in data_lower:
        profile.framework = "webflow"
    elif react_info.get("meta_framework") == "nextjs" or "__next" in data_str:
        profile.framework = "nextjs"
    elif "wix" in data_lower:
        profile.framework = "wix"
    elif "squarespace" in data_lower:
        profile.framework = "squarespace"
    elif react_info.get("framework"):
        profile.framework = react_info["framework"]
    else:
        profile.framework = "unknown"

    # --- Detect duplication ratio ---
    if text:
        lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 20]
        if lines:
            unique_lines = set(lines)
            profile.duplication_ratio = round(len(lines) / max(len(unique_lines), 1), 1)

    # --- Detect page length ---
    viewport_height = 900
    viewports = page_height / viewport_height if viewport_height else 1
    if viewports < 2:
        profile.page_length = "short"
    elif viewports < 6:
        profile.page_length = "normal"
    else:
        profile.page_length = "long"

    # --- Detect content density ---
    unique_text_tokens = estimate_tokens(deduplicate_text(text))
    if unique_text_tokens < 500:
        profile.content_density = "minimal"
    elif unique_text_tokens > 3000:
        profile.content_density = "heavy"
    else:
        profile.content_density = "normal"

    # --- Detect features ---
    text_lower = text.lower()
    section_types = [s.get("type", "") for s in sections]
    pattern_types = [p.get("type", "") for p in ui_patterns]

    profile.has_code_blocks = bool(re.search(r"```|<code|<pre", data_str[:50000]))
    profile.has_pricing_table = "pricing" in section_types or "pricing" in text_lower
    profile.has_carousel = "carousel" in pattern_types or any(
        kw in data_lower[:50000] for kw in ["swiper", "carousel", "slider"]
    )
    profile.has_faq = "faq" in section_types or "frequently asked" in text_lower

    # --- Detect nav complexity ---
    nav_links = clickables.get("nav_links", [])
    if len(nav_links) > 15:
        profile.nav_complexity = "mega-menu"
    elif len(nav_links) > 8:
        profile.nav_complexity = "dropdown"
    else:
        profile.nav_complexity = "simple"

    return profile


# ============================================================
# STEP 2: Compute Adaptive Budget
# ============================================================

def compute_budget(profile: SiteProfile) -> BudgetAllocation:
    """Allocate token budget based on site characteristics."""
    budget = BudgetAllocation()
    total_available = budget.available  # ~21K tokens

    # --- Sections (per-section structured data replaces DOM skeleton) ---
    if profile.section_count > 15:
        budget.sections = 9000
    elif profile.framework in ("framer", "webflow", "wix"):
        budget.sections = 8000
    elif profile.page_length == "short":
        budget.sections = 5000
    else:
        budget.sections = 7000

    # --- Text content ---
    if profile.content_density == "heavy":
        budget.text_content = 4000
    elif profile.content_density == "minimal":
        budget.text_content = 1000
    else:
        budget.text_content = 2500

    if profile.has_pricing_table:
        budget.text_content += 800
    if profile.has_faq:
        budget.text_content += 600

    # --- Links ---
    if profile.nav_complexity == "mega-menu":
        budget.links = 2500
    elif profile.nav_complexity == "dropdown":
        budget.links = 2000
    else:
        budget.links = 1200

    # --- Images ---
    if profile.image_count > 30:
        budget.images = 2000
    elif profile.image_count < 5:
        budget.images = 400
    else:
        budget.images = 1200

    # --- SVGs ---
    if profile.image_count < 5:
        budget.svgs = 1500
    else:
        budget.svgs = 800

    # --- Code blocks ---
    if profile.has_code_blocks:
        budget.text_content += 400

    # --- Normalize to fit budget ---
    current_total = budget.total_text
    if current_total > total_available:
        scale = total_available / current_total
        budget.sections = int(budget.sections * scale)
        budget.text_content = int(budget.text_content * scale)
        budget.links = int(budget.links * scale)
        budget.images = int(budget.images * scale)
        budget.svgs = int(budget.svgs * scale)
        budget.backgrounds = int(budget.backgrounds * scale)
        budget.interactive = int(budget.interactive * scale)
        budget.asset_map = int(budget.asset_map * scale)
    elif current_total < total_available:
        surplus = total_available - current_total
        budget.sections += surplus // 2
        budget.text_content += surplus // 2

    return budget


# ============================================================
# STEP 3: Deduplication
# ============================================================

def deduplicate_text(text: str) -> str:
    """Remove duplicate text lines by content hash."""
    if not text:
        return ""

    lines = text.split("\n")
    seen_hashes = set()
    result = []

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if result and result[-1].strip():
                result.append("")
            continue

        if len(stripped) < 4:
            continue

        normalized = " ".join(stripped.split()).lower()
        normalized = re.sub(r"[^\w\s]", "", normalized)

        line_hash = hashlib.md5(normalized.encode()).hexdigest()
        if line_hash in seen_hashes:
            continue

        seen_hashes.add(line_hash)
        result.append(stripped)

    return "\n".join(result)


def deduplicate_links(links: list) -> list:
    """Remove duplicate links by normalized (text, href) pair."""
    if not links:
        return []

    seen = set()
    result = []

    for link in links:
        text = link.get("text", "").strip()
        href = link.get("href", "").strip()
        if not text and not href:
            continue

        norm_href = href.rstrip("/").replace("https://", "").replace("http://", "")
        norm_text = " ".join(text.split()).lower()

        key = (norm_text, norm_href)
        if key in seen:
            continue

        seen.add(key)
        result.append(link)

    return result


def deduplicate_images(images: list) -> list:
    """Remove duplicate images by normalized URL."""
    if not images:
        return []

    seen = set()
    result = []

    for img in images:
        url = img.get("url", img.get("src", ""))
        if not url:
            continue

        base_url = url.split("?")[0]
        norm_url = re.sub(r"/w_\d+/", "/", base_url)
        norm_url = re.sub(r"/h_\d+/", "/", norm_url)
        norm_url = re.sub(r"@\dx", "", norm_url)

        if norm_url in seen:
            continue

        seen.add(norm_url)
        result.append(img)

    return result


def deduplicate_svgs(svgs: list) -> list:
    """Remove duplicate SVGs by content hash (ignoring IDs)."""
    if not svgs:
        return []

    seen = set()
    result = []

    for svg in svgs:
        markup = svg.get("markup", "") if isinstance(svg, dict) else svg
        if not markup:
            continue

        normalized = re.sub(r"\s+", " ", markup.strip())
        normalized = re.sub(r'id="[^"]*"', "", normalized)

        svg_hash = hashlib.md5(normalized.encode()).hexdigest()
        if svg_hash in seen:
            continue

        seen.add(svg_hash)
        result.append(svg)

    return result


def _deduplicate_section_headings(sections: list) -> None:
    """Deduplicate heading text across sections (modifies in place).
    Framer sites often repeat headings in hidden responsive variants."""
    seen = set()
    for sec in sections:
        deduped = []
        for h in sec.get("headings", []):
            text = h.get("text", "").strip()
            if not text:
                continue
            norm = " ".join(text.split()).lower()
            if norm in seen:
                continue
            seen.add(norm)
            deduped.append(h)
        sec["headings"] = deduped


def _deduplicate_section_paragraphs(sections: list) -> None:
    """Deduplicate paragraph text across sections."""
    seen = set()
    for sec in sections:
        deduped = []
        for p in sec.get("paragraphs", []):
            text = p.get("text", "").strip()
            if not text:
                continue
            norm = " ".join(text.split()).lower()[:100]
            h = hashlib.md5(norm.encode()).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            deduped.append(p)
        sec["paragraphs"] = deduped


# ============================================================
# STEP 4: Smart Truncation
# ============================================================

def truncate_to_budget(text: str, token_budget: int) -> str:
    """Truncate text to fit within a token budget, keeping top and bottom."""
    char_budget = int(token_budget * CHARS_PER_TOKEN)
    if len(text) <= char_budget:
        return text

    top_chars = int(char_budget * 0.6)
    bottom_chars = int(char_budget * 0.4)

    return (
        text[:top_chars]
        + "\n\n[... middle content — refer to screenshot ...]\n\n"
        + text[-bottom_chars:]
    )


def truncate_list(items: list, token_budget: int, key_fields: list[str]) -> list:
    """Truncate a list of dicts to fit within a token budget."""
    result = []
    tokens_used = 0

    for item in items:
        slim = {k: item[k] for k in key_fields if k in item}
        item_str = json.dumps(slim)
        item_tokens = estimate_tokens(item_str)

        if tokens_used + item_tokens > token_budget:
            break

        result.append(slim)
        tokens_used += item_tokens

    return result


# ============================================================
# STEP 5: Build Final Preprocessed Output
# ============================================================

def preprocess_scrape(data: dict) -> dict:
    """
    Main entry point. Takes raw scrape data from scraper.py,
    returns a lean, structured summary that fits within the token budget.
    """
    # Step 1: Profile the site
    profile = detect_site_profile(data)

    # Step 2: Compute adaptive budget
    budget = compute_budget(profile)

    # Step 3: Build asset map from sections (reuse tool_handlers logic)
    sections_raw = data.get("sections", [])
    asset_map = build_asset_map(sections_raw)

    # Step 4: Deduplicate text content
    deduped_text = deduplicate_text(data.get("text_content", ""))
    deduped_nav = deduplicate_links(data.get("clickables", {}).get("nav_links", []))
    deduped_footer = deduplicate_links(data.get("clickables", {}).get("footer_links", []))
    deduped_images = deduplicate_images(data.get("assets", {}).get("images", []))

    # Step 5: Process sections — deduplicate across sections
    processed_sections = []
    for sec in sections_raw[:25]:
        processed_sections.append({
            "index": sec.get("index", 0),
            "type": sec.get("type", "section"),
            "background_color": sec.get("background_color"),
            "gradient": sec.get("gradient"),
            "background_image_url": sec.get("background_image_url"),
            "layout": sec.get("layout", {}),
            "headings": sec.get("headings", [])[:5],
            "paragraphs": sec.get("paragraphs", [])[:4],
            "buttons": [
                {
                    "text": b.get("text", ""),
                    "bg": b.get("bg"),
                    "color": b.get("color"),
                    "border_radius": b.get("border_radius"),
                    "href": b.get("href", "#"),
                }
                for b in sec.get("buttons", [])[:5]
            ],
            "images": [
                {
                    "url": img.get("url", ""),
                    "alt": img.get("alt", ""),
                    "role": img.get("role", "content"),
                }
                for img in sec.get("images", [])[:8]
            ],
            "svgs": [
                s.get("markup", "")[:500] if isinstance(s, dict) else str(s)[:500]
                for s in sec.get("svgs", [])[:3]
            ],
            "links": [
                {"text": l.get("text", ""), "href": l.get("href", "#")}
                for l in sec.get("links", [])[:8]
            ],
        })

    # Cross-section deduplication
    _deduplicate_section_headings(processed_sections)
    _deduplicate_section_paragraphs(processed_sections)

    # Step 6: Build summary
    theme = data.get("theme", {})
    summary = {
        "url": data.get("url", ""),
        "title": data.get("title", ""),
        "page_height": data.get("page_height", 0),
        "site_profile": {
            "framework": profile.framework,
            "page_length": profile.page_length,
            "content_density": profile.content_density,
            "section_count": profile.section_count,
            "has_pricing": profile.has_pricing_table,
            "has_carousel": profile.has_carousel,
            "has_faq": profile.has_faq,
        },
        "theme": {
            "colors": theme.get("colors", {}),
            "fonts": theme.get("fonts", {}),
        },
        "sections": processed_sections,
        "nav_links": truncate_list(deduped_nav, budget.links // 2, ["text", "href"]),
        "footer_links": truncate_list(deduped_footer, budget.links // 2, ["text", "href"]),
        "asset_map": asset_map,
    }

    # Animations summary (compact)
    raw_anims = data.get("animations", {})
    if raw_anims:
        summary["animations"] = {
            "libraries_detected": raw_anims.get("libraries_detected", []),
            "scroll_animations": [
                {"type": a["type"], "section_hint": a.get("section_hint", "")}
                for a in raw_anims.get("scroll_animations", [])[:10]
            ],
        }

    # UI patterns
    ui_patterns = data.get("ui_patterns", [])
    if ui_patterns:
        summary["ui_patterns"] = [
            {"type": p["type"], "library": p.get("library", ""), "count": p.get("count", 0)}
            for p in ui_patterns[:8]
        ]

    # Button behaviors
    button_behaviors = data.get("button_behaviors", [])
    if button_behaviors:
        summary["button_behaviors"] = [
            {"text": b.get("text", ""), "behavior": b.get("behavior")}
            for b in button_behaviors[:15]
        ]

    # Interactive elements (tabs, accordions, toggles, dropdowns)
    interactives = data.get("interactives", [])
    if interactives:
        interactive_summary = []
        tokens_used = 0

        for item in interactives:
            item_str = json.dumps(item)
            item_tokens = estimate_tokens(item_str)

            if tokens_used + item_tokens > budget.interactive:
                # Truncate content within items to fit
                if item.get("type") == "tabs":
                    for tab in item.get("tabs", []):
                        tab["content_text"] = tab.get("content_text", "")[:300]
                elif item.get("type") == "accordion":
                    for acc in item.get("items", []):
                        acc["answer"] = acc.get("answer", "")[:200]
                elif item.get("type") == "toggle":
                    for state_val in item.get("states", {}).values():
                        state_val["content_text"] = state_val.get("content_text", "")[:300]

            interactive_summary.append(item)
            tokens_used += estimate_tokens(json.dumps(item))

            if tokens_used > budget.interactive:
                break

        summary["interactives"] = interactive_summary

    # Truncate sections to budget
    sections_json = json.dumps(summary["sections"])
    sections_tokens = estimate_tokens(sections_json)
    if sections_tokens > budget.sections:
        # Remove elements/paragraphs from later sections first
        for sec in reversed(summary["sections"]):
            if sections_tokens <= budget.sections:
                break
            sec.pop("links", None)
            sec["paragraphs"] = sec.get("paragraphs", [])[:2]
            sec["svgs"] = sec.get("svgs", [])[:1]
            sections_tokens = estimate_tokens(json.dumps(summary["sections"]))

    # Truncate asset map to budget
    asset_map_json = json.dumps(summary["asset_map"])
    if estimate_tokens(asset_map_json) > budget.asset_map:
        # Keep only the most important assets (logos, heroes, backgrounds first)
        priority_keys = []
        other_keys = []
        for key in summary["asset_map"]:
            if any(p in key for p in ["logo", "hero", "background", "main_image"]):
                priority_keys.append(key)
            else:
                other_keys.append(key)

        trimmed = {}
        tokens_used = 0
        for key in priority_keys + other_keys:
            entry = summary["asset_map"][key]
            entry_tokens = estimate_tokens(json.dumps({key: entry}))
            if tokens_used + entry_tokens > budget.asset_map:
                break
            trimmed[key] = entry
            tokens_used += entry_tokens
        summary["asset_map"] = trimmed

    # Diagnostics metadata
    actual_tokens = estimate_tokens(json.dumps(summary))
    summary["_meta"] = {
        "estimated_tokens": actual_tokens,
        "budget": total_available_budget(budget),
        "dedup_ratio": round(profile.duplication_ratio, 1),
        "framework": profile.framework,
        "sections_in": len(sections_raw),
        "sections_out": len(summary["sections"]),
        "images_raw": profile.image_count,
        "images_deduped": len(deduped_images),
    }

    return summary


def total_available_budget(budget: BudgetAllocation) -> int:
    return TOTAL_TOKEN_BUDGET - SCREENSHOT_TOKEN_COST


def preprocess_and_format(data: dict) -> str:
    """Full pipeline: returns JSON string ready for Claude."""
    summary = preprocess_scrape(data)
    return json.dumps(summary, indent=2)
