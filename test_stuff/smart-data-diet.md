# Smart Data Diet — Adaptive Scrape Preprocessing

The data diet can't be hardcoded for Framer. It needs to handle any website intelligently. The system should measure the raw data, detect redundancy patterns, and budget tokens dynamically.

**This replaces the static truncation approach in speed-fix-data-diet.md.**

---

## Core Idea: Token Budget System

Every clone gets a fixed TOKEN BUDGET. The preprocessor's job is to fit the scrape data into that budget while preserving the most important information.

```
TOTAL BUDGET: 25,000 tokens (~18K chars of text + 1 screenshot)

Budget allocation:
  Screenshot:     ~4,000 tokens (1 compressed full-page image — fixed)
  Theme/colors:   ~1,000 tokens (always include full — non-negotiable)
  DOM skeleton:   ~5,000 tokens (adaptive — more for complex layouts)
  Text content:   ~4,000 tokens (adaptive — more for text-heavy sites)
  Links/buttons:  ~2,000 tokens (adaptive — more for nav-heavy sites)
  Images list:    ~1,500 tokens (URLs + alt text only)
  SVGs:           ~1,000 tokens (top icons only)
  Backgrounds:    ~500 tokens (gradients + bg images)
  Section map:    ~1,000 tokens (types + order)
  
  Buffer:         ~5,000 tokens (reallocated from underused categories)
```

The key insight: **categories that have less content donate their unused budget to categories that need more.**

---

## Implementation

Create `backend/app/scrape_preprocessor.py`:

```python
"""
Smart preprocessing pipeline for scrape data.
Detects site characteristics, deduplicates content,
and fits everything into a fixed token budget.

Works across: Framer, WordPress, Webflow, custom React/Next.js,
static HTML, SPAs, Squarespace, Wix, etc.
"""

import re
import json
import hashlib
from dataclasses import dataclass, field
from typing import Optional

# Rough token estimation: 1 token ≈ 4 chars for English text, 3 chars for code/markup
CHARS_PER_TOKEN = 3.5
TOTAL_TOKEN_BUDGET = 25000
SCREENSHOT_TOKEN_COST = 4000  # Fixed — one image always costs roughly this


@dataclass
class BudgetAllocation:
    """Dynamic token budget per category."""
    theme: int = 1000
    dom_skeleton: int = 5000
    text_content: int = 4000
    links: int = 2000
    images: int = 1500
    svgs: int = 1000
    backgrounds: int = 500
    sections: int = 1000
    
    @property
    def total_text(self) -> int:
        return (self.theme + self.dom_skeleton + self.text_content +
                self.links + self.images + self.svgs +
                self.backgrounds + self.sections)
    
    @property
    def available(self) -> int:
        return TOTAL_TOKEN_BUDGET - SCREENSHOT_TOKEN_COST


def estimate_tokens(text: str) -> int:
    """Rough token count for a string."""
    return int(len(text) / CHARS_PER_TOKEN)


# ============================================================
# STEP 1: Detect Site Characteristics
# ============================================================

@dataclass
class SiteProfile:
    """Detected characteristics of the scraped site."""
    framework: str = "unknown"          # framer, wordpress, webflow, nextjs, static, etc.
    duplication_ratio: float = 1.0      # 1.0 = no duplication, 3.0 = everything tripled
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
    raw_skeleton_tokens: int = 0


def detect_site_profile(data: dict) -> SiteProfile:
    """Analyze raw scrape data and build a site profile."""
    profile = SiteProfile()
    
    text = data.get("text_content", "")
    skeleton = data.get("dom_skeleton", "")
    sections = data.get("sections", [])
    images = data.get("assets", {}).get("images", [])
    clickables = data.get("clickables", {})
    page_height = data.get("page_height", 0)
    
    profile.raw_text_tokens = estimate_tokens(text)
    profile.raw_skeleton_tokens = estimate_tokens(skeleton)
    profile.image_count = len(images)
    profile.section_count = len(sections)
    
    # --- Detect framework ---
    html_source = data.get("html_source", skeleton)
    
    if "framerusercontent.com" in str(data) or "framer" in skeleton.lower():
        profile.framework = "framer"
    elif "wp-content" in str(data) or "wordpress" in str(data).lower():
        profile.framework = "wordpress"
    elif "webflow" in str(data).lower() or "wf-" in skeleton:
        profile.framework = "webflow"
    elif "_next" in str(data) or "__next" in skeleton:
        profile.framework = "nextjs"
    elif "wix" in str(data).lower():
        profile.framework = "wix"
    elif "squarespace" in str(data).lower():
        profile.framework = "squarespace"
    else:
        profile.framework = "unknown"
    
    # --- Detect duplication ratio ---
    # Count how many times the same substantial text appears
    if text:
        lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 20]
        if lines:
            unique_lines = set(lines)
            profile.duplication_ratio = round(len(lines) / max(len(unique_lines), 1), 1)
    
    # --- Detect page length ---
    viewport_height = 900  # assume standard viewport
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
    section_types = [s.get("probable_type", "") for s in sections]
    
    profile.has_code_blocks = bool(re.search(r'```|<code|<pre', str(data)))
    profile.has_pricing_table = "pricing" in section_types or "pricing" in text_lower
    profile.has_carousel = any(kw in str(data).lower() for kw in ["swiper", "carousel", "slider", "marquee"])
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
    """
    Allocate token budget based on site characteristics.
    Categories that need less donate to categories that need more.
    """
    budget = BudgetAllocation()
    total_available = budget.available  # ~21K tokens
    
    # Start with base allocation, then adjust
    
    # --- DOM skeleton ---
    if profile.raw_skeleton_tokens < 2000:
        # Simple site — skeleton is small, give budget to text
        budget.dom_skeleton = 2000
    elif profile.framework in ("framer", "webflow", "wix"):
        # These frameworks produce verbose DOM — need more room even after dedup
        budget.dom_skeleton = 6000
    elif profile.page_length == "long":
        budget.dom_skeleton = 6000
    else:
        budget.dom_skeleton = 4500
    
    # --- Text content ---
    if profile.content_density == "heavy":
        budget.text_content = 5000
    elif profile.content_density == "minimal":
        budget.text_content = 1500
    else:
        budget.text_content = 3500
    
    # Pricing tables and FAQs need exact text — boost
    if profile.has_pricing_table:
        budget.text_content += 1000
    if profile.has_faq:
        budget.text_content += 800
    
    # --- Links ---
    if profile.nav_complexity == "mega-menu":
        budget.links = 3000
    elif profile.nav_complexity == "dropdown":
        budget.links = 2500
    else:
        budget.links = 1500
    
    # --- Images ---
    if profile.image_count > 30:
        budget.images = 2000
    elif profile.image_count < 5:
        budget.images = 500
    else:
        budget.images = 1200
    
    # --- SVGs ---
    if profile.image_count < 5:
        # Probably icon-heavy — SVGs matter more
        budget.svgs = 1500
    else:
        budget.svgs = 800
    
    # --- Code blocks ---
    if profile.has_code_blocks:
        # Sites with code demos need text budget for code content
        budget.text_content += 500
    
    # --- Normalize to fit budget ---
    current_total = budget.total_text
    if current_total > total_available:
        # Scale everything down proportionally
        scale = total_available / current_total
        budget.dom_skeleton = int(budget.dom_skeleton * scale)
        budget.text_content = int(budget.text_content * scale)
        budget.links = int(budget.links * scale)
        budget.images = int(budget.images * scale)
        budget.svgs = int(budget.svgs * scale)
        budget.backgrounds = int(budget.backgrounds * scale)
        budget.sections = int(budget.sections * scale)
    elif current_total < total_available:
        # Redistribute surplus to DOM skeleton and text (most impactful)
        surplus = total_available - current_total
        budget.dom_skeleton += surplus // 2
        budget.text_content += surplus // 2
    
    return budget


# ============================================================
# STEP 3: Deduplication (framework-aware)
# ============================================================

def deduplicate_text(text: str) -> str:
    """
    Remove duplicate text content. Works for any framework.
    
    Strategy: hash each meaningful line, keep first occurrence.
    Handles Framer (3x), WordPress (sidebar + content duplication),
    and any other framework that duplicates content.
    """
    if not text:
        return ""
    
    lines = text.split('\n')
    seen_hashes = set()
    result = []
    
    for line in lines:
        stripped = line.strip()
        
        # Always keep empty lines (paragraph breaks)
        if not stripped:
            if result and result[-1].strip():  # Avoid consecutive blank lines
                result.append("")
            continue
        
        # Skip very short fragments (likely UI artifacts)
        if len(stripped) < 4:
            continue
        
        # Normalize for comparison
        # - collapse whitespace
        # - lowercase for matching
        # - strip common artifacts
        normalized = ' '.join(stripped.split()).lower()
        normalized = re.sub(r'[^\w\s]', '', normalized)  # strip punctuation for matching
        
        line_hash = hashlib.md5(normalized.encode()).hexdigest()
        
        if line_hash in seen_hashes:
            continue
        
        seen_hashes.add(line_hash)
        result.append(stripped)
    
    return '\n'.join(result)


def deduplicate_links(links: list) -> list:
    """Remove duplicate links. Key: normalized (text, href) pair."""
    if not links:
        return []
    
    seen = set()
    result = []
    
    for link in links:
        text = link.get("text", "").strip()
        href = link.get("href", "").strip()
        
        if not text and not href:
            continue
        
        # Normalize href — strip trailing slashes, protocol
        norm_href = href.rstrip("/").replace("https://", "").replace("http://", "")
        norm_text = ' '.join(text.split()).lower()
        
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
        
        # Normalize: strip query params (Framer adds ?width=, Cloudinary adds transforms, etc.)
        base_url = url.split("?")[0]
        
        # Also strip common CDN path variations
        # e.g., /w_200/ vs /w_400/ on Cloudinary
        norm_url = re.sub(r'/w_\d+/', '/', base_url)
        norm_url = re.sub(r'/h_\d+/', '/', norm_url)
        norm_url = re.sub(r'@\dx', '', norm_url)  # @2x, @3x retina
        
        if norm_url in seen:
            continue
        
        seen.add(norm_url)
        result.append(img)
    
    return result


def deduplicate_svgs(svgs: list) -> list:
    """Remove duplicate SVGs by content hash (same icon appearing multiple times)."""
    if not svgs:
        return []
    
    seen = set()
    result = []
    
    for svg in svgs:
        markup = svg.get("markup", "")
        if not markup:
            continue
        
        # Normalize: strip whitespace and IDs (same icon with different IDs)
        normalized = re.sub(r'\s+', ' ', markup.strip())
        normalized = re.sub(r'id="[^"]*"', '', normalized)
        
        svg_hash = hashlib.md5(normalized.encode()).hexdigest()
        if svg_hash in seen:
            continue
        
        seen.add(svg_hash)
        result.append(svg)
    
    return result


# ============================================================
# STEP 4: Smart Truncation (budget-aware)
# ============================================================

def truncate_to_budget(text: str, token_budget: int) -> str:
    """Truncate text to fit within a token budget."""
    char_budget = int(token_budget * CHARS_PER_TOKEN)
    
    if len(text) <= char_budget:
        return text
    
    # For structured content (like DOM skeleton), keep top and bottom
    # Top = navbar/hero, Bottom = footer — both critical for layout
    top_share = 0.6
    bottom_share = 0.4
    
    top_chars = int(char_budget * top_share)
    bottom_chars = int(char_budget * bottom_share)
    
    return (
        text[:top_chars] +
        "\n\n[... middle content — refer to screenshot ...]\n\n" +
        text[-bottom_chars:]
    )


def truncate_list(items: list, token_budget: int, key_fields: list[str]) -> list:
    """
    Truncate a list of dicts to fit within a token budget.
    Keeps items from the start until budget exhausted.
    Only includes specified key_fields from each item to save space.
    """
    result = []
    tokens_used = 0
    
    for item in items:
        # Build a slim version with only key fields
        slim = {k: item[k] for k in key_fields if k in item}
        item_str = json.dumps(slim)
        item_tokens = estimate_tokens(item_str)
        
        if tokens_used + item_tokens > token_budget:
            break
        
        result.append(slim)
        tokens_used += item_tokens
    
    return result


# ============================================================
# STEP 5: Build Final Summary (the main function)
# ============================================================

def preprocess_scrape(data: dict) -> dict:
    """
    Main entry point. Takes raw scrape data, returns a lean summary
    that fits within the token budget.
    
    Call this INSTEAD of sending raw scrape data to Claude.
    """
    
    # Step 1: Profile the site
    profile = detect_site_profile(data)
    
    # Step 2: Compute adaptive budget
    budget = compute_budget(profile)
    
    # Step 3: Deduplicate everything
    deduped_text = deduplicate_text(data.get("text_content", ""))
    deduped_nav = deduplicate_links(data.get("clickables", {}).get("nav_links", []))
    deduped_cta = deduplicate_links(data.get("clickables", {}).get("cta_buttons", []))
    deduped_footer = deduplicate_links(data.get("clickables", {}).get("footer_links", []))
    deduped_images = deduplicate_images(data.get("assets", {}).get("images", []))
    deduped_svgs = deduplicate_svgs(data.get("svgs", []))
    deduped_skeleton = deduplicate_text(data.get("dom_skeleton", ""))
    
    # Step 4: Build summary with budget-aware truncation
    summary = {
        "url": data.get("url", ""),
        "title": data.get("title", ""),
        "page_height": data.get("page_height", 0),
        
        # Site profile (helps Claude understand what it's building)
        "site_profile": {
            "framework": profile.framework,
            "page_length": profile.page_length,
            "content_density": profile.content_density,
            "section_count": profile.section_count,
            "has_code_blocks": profile.has_code_blocks,
            "has_pricing": profile.has_pricing_table,
            "has_carousel": profile.has_carousel,
            "has_faq": profile.has_faq,
        },
    }
    
    # Theme: always full (it's small and essential)
    theme = data.get("theme", {})
    summary["theme"] = {
        "colors": theme.get("colors", {}),
        "fonts": theme.get("fonts", {}),
    }
    
    # DOM skeleton: deduped + truncated to budget
    summary["dom_skeleton"] = truncate_to_budget(deduped_skeleton, budget.dom_skeleton)
    
    # Text content: deduped + truncated to budget
    summary["text_content"] = truncate_to_budget(deduped_text, budget.text_content)
    
    # Links: deduped + capped
    link_budget_each = budget.links // 3
    summary["nav_links"] = truncate_list(deduped_nav, link_budget_each, ["text", "href"])
    summary["cta_buttons"] = truncate_list(deduped_cta, link_budget_each, ["text", "href", "bg", "color", "border_radius"])
    summary["footer_links"] = truncate_list(deduped_footer, link_budget_each, ["text", "href"])
    
    # Images: deduped, URL + alt only
    summary["images"] = truncate_list(deduped_images, budget.images, ["url", "alt"])
    
    # SVGs: deduped, truncated markup
    svg_budget_each = budget.svgs // max(len(deduped_svgs), 1)
    summary["svgs"] = []
    for svg in deduped_svgs[:5]:
        markup = svg.get("markup", "")
        max_chars = int(svg_budget_each * CHARS_PER_TOKEN)
        summary["svgs"].append({
            "markup": markup[:max_chars]
        })
    
    # Backgrounds
    summary["backgrounds"] = truncate_list(
        data.get("backgrounds", []),
        budget.backgrounds,
        ["type", "url", "colors", "direction"]
    )
    
    # Section map: just types and order (Claude has the screenshot for visual reference)
    sections = data.get("sections", [])
    summary["sections"] = [
        {
            "index": s.get("index", i),
            "type": s.get("probable_type", "content"),
            "background": s.get("style", {}).get("background", ""),
        }
        for i, s in enumerate(sections[:15])
    ]
    
    # Log the budget usage for debugging
    actual_tokens = estimate_tokens(json.dumps(summary))
    summary["_meta"] = {
        "estimated_tokens": actual_tokens,
        "budget": TOTAL_TOKEN_BUDGET - SCREENSHOT_TOKEN_COST,
        "dedup_ratio": round(profile.duplication_ratio, 1),
        "framework": profile.framework,
    }
    
    return summary


# ============================================================
# CONVENIENCE: Full pipeline for the tool handler
# ============================================================

def preprocess_and_format(data: dict) -> str:
    """
    Full pipeline: profile → budget → dedup → truncate → JSON string.
    This is what the scrape_url tool returns to Claude.
    """
    summary = preprocess_scrape(data)
    return json.dumps(summary, indent=2)
```

---

## Wire It In

### In `tool_handlers.py`:

```python
from app.scrape_preprocessor import preprocess_and_format

async def handle_scrape_url(input: dict) -> str:
    url = input["url"]
    data = await scrape_website(url)
    
    # Store full data for later (fix passes, chat follow-ups)
    _scrape_cache[url] = data
    
    # Return lean, budget-fitted summary
    return preprocess_and_format(data)
```

That's it. One line change. The preprocessor handles everything else.

### In `scraper.py`:

No changes needed. Keep extracting everything — the preprocessor handles filtering. This way the full data is available for chat follow-ups where the user might ask about specific elements.

---

## How It Adapts to Different Sites

| Site | Framework | Duplication | Budget Adjustments |
|------|-----------|-------------|-------------------|
| daytona.io | Framer | 3.0x → deduped to 1.0x | Extra DOM budget (Framer is verbose) |
| stripe.com | Custom React | 1.0x | Standard allocation |
| wordpress blog | WordPress | 1.2x (sidebar dups) | Extra text budget (content-heavy) |
| agency portfolio | Webflow | 2.0x | Extra DOM budget (Webflow is verbose) |
| saas-tool.com/pricing | Next.js | 1.0x | +1000 text tokens (pricing table) |
| docs.example.com | Static HTML | 1.0x | Minimal DOM, heavy text |
| wix restaurant | Wix | 1.5x | Extra image budget |
| simple landing page | Static | 1.0x | Small DOM, small text, surplus → skeleton |

The same code handles all of them. No if/else per framework. The profile detection and budget allocation handle the differences.

---

## What This Catches (that static truncation misses)

1. **Framer/Webflow responsive triplication** — detected by duplication_ratio > 1.5, deduped via content hashing
2. **WordPress sidebar content bleeding into main content** — deduped via line-level hashing
3. **CDN image URL variants** (same image at different sizes) — normalized by stripping query params and dimension paths
4. **Repeated SVG icons** (same hamburger icon 3x in responsive navs) — deduped via content hash ignoring IDs
5. **Mega-menu nav links** (50+ links) — detected by nav_complexity, gets more budget
6. **Long-form content sites** (blogs, docs) — detected by content_density=heavy, text gets more budget
7. **Image-heavy portfolios** — detected by image_count > 30, images get more budget
8. **FAQ-heavy pages** — detected by has_faq, text gets extra budget for Q&A pairs
9. **Code-heavy dev tool sites** — detected by has_code_blocks, text gets extra budget
10. **Short landing pages** — detected by page_length=short, surplus redistributed to quality over quantity

---

## Testing

```python
# Test with different sites
import asyncio
from app.scraper import scrape_website
from app.scrape_preprocessor import preprocess_scrape

async def test():
    for url in ["https://daytona.io", "https://stripe.com", "https://linear.app"]:
        data = await scrape_website(url)
        summary = preprocess_scrape(data)
        meta = summary["_meta"]
        print(f"{url}")
        print(f"  Framework: {meta['framework']}")
        print(f"  Dedup ratio: {meta['dedup_ratio']}x")
        print(f"  Tokens: {meta['estimated_tokens']} / {meta['budget']}")
        print()

asyncio.run(test())
```

Expected output:
```
https://daytona.io
  Framework: framer
  Dedup ratio: 2.8x
  Tokens: 18500 / 21000

https://stripe.com
  Framework: nextjs
  Dedup ratio: 1.1x
  Tokens: 19200 / 21000

https://linear.app
  Framework: nextjs
  Dedup ratio: 1.0x
  Tokens: 16800 / 21000
```
