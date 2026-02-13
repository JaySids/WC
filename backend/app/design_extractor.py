"""
Design token extractor — single Claude call to extract a structured
design system from scrape data. The tokens become the contract that
all parallel section generators follow.
"""

import json
import os

import anthropic


_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            from app.config import get_settings
            api_key = get_settings().anthropic_api_key
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


DESIGN_SYSTEM_PROMPT = """You are a design system extractor. Given screenshots and CSS data from a website, extract a precise design token JSON.

RULES:
- Use ONLY exact hex values from the CSS data. Never approximate. Never invent colors.
- Use ONLY font names that appear in the CSS data or Google Font URLs.
- Use ONLY exact px/rem sizes from the computed styles.
- If a value isn't clearly present, use a reasonable default and mark it with "_inferred": true.

Output ONLY valid JSON (no markdown fences, no explanation). The schema:

{
  "colors": {
    "primary": "#hex",
    "secondary": "#hex",
    "backgrounds": {
      "page": "#hex",
      "alt": "#hex",
      "dark": "#hex",
      "card": "#hex"
    },
    "text": {
      "heading": "#hex",
      "body": "#hex",
      "muted": "#hex",
      "on_dark": "#hex",
      "link": "#hex"
    },
    "border": "#hex",
    "divider": "#hex"
  },
  "typography": {
    "fonts": {
      "heading": "font-family string",
      "body": "font-family string",
      "mono": "font-family string or null"
    },
    "google_font_urls": ["url1", "url2"],
    "scale": {
      "hero_heading": { "size": "px", "weight": "number", "line_height": "ratio", "letter_spacing": "value" },
      "section_heading": { "size": "px", "weight": "number", "line_height": "ratio" },
      "card_heading": { "size": "px", "weight": "number" },
      "body_large": { "size": "px", "weight": "number" },
      "body": { "size": "px", "weight": "number", "line_height": "ratio" },
      "body_small": { "size": "px" },
      "label": { "size": "px", "weight": "number", "letter_spacing": "value" },
      "nav_link": { "size": "px", "weight": "number" }
    }
  },
  "spacing": {
    "section_padding_y": "px",
    "section_padding_y_mobile": "px",
    "container_max_width": "px",
    "container_padding_x": "px",
    "card_gap": "px",
    "heading_to_content": "px"
  },
  "components": {
    "button_primary": { "bg": "#hex", "text": "#hex", "padding": "px px", "border_radius": "px", "hover": { "bg": "#hex" }, "font_weight": "number" },
    "button_secondary": { "bg": "#hex or transparent", "text": "#hex", "border": "1px solid #hex", "padding": "px px", "border_radius": "px" },
    "card": { "bg": "#hex", "border": "1px solid #hex or none", "border_radius": "px", "shadow": "css shadow or none", "padding": "px", "hover": {} },
    "badge": { "bg": "#hex", "text": "#hex", "padding": "px px", "border_radius": "px", "font_size": "px" },
    "nav": { "bg": "#hex", "height": "px", "border_bottom": "css or none" }
  },
  "animation": {
    "scroll_entrance": { "initial_opacity": 0, "initial_y": 40, "duration": 0.6, "ease": "easeOut", "stagger": 0.1 },
    "hover_lift": { "y": -4, "shadow_increase": true },
    "transition_default": "all 0.3s ease"
  },
  "borders": {
    "radius_sm": "px",
    "radius_md": "px",
    "radius_lg": "px",
    "radius_xl": "px",
    "radius_full": "9999px",
    "default_color": "#hex"
  },
  "layout": {
    "hero_layout": "centered | split-left | split-right | image-bg",
    "grid_columns": {
      "features": 3,
      "pricing": 3,
      "testimonials": 3,
      "logos": 6,
      "team": 4,
      "stats": 4
    }
  }
}"""


async def extract_design_system(scrape_data: dict) -> dict:
    """
    Single Claude call to extract design tokens from scrape data.
    Falls back to _fallback_design_tokens() if the call fails.
    """
    try:
        client = _get_client()

        # Build the content with screenshots + CSS data
        content = []

        # Add viewport screenshot
        viewport_b64 = scrape_data.get("screenshots", {}).get("viewport")
        if viewport_b64:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": viewport_b64},
            })

        # Add full page screenshot
        full_b64 = scrape_data.get("screenshots", {}).get("full_page")
        if full_b64:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": full_b64},
            })

        # Build the data payload for Claude
        theme = scrape_data.get("theme", {})
        sections = scrape_data.get("sections", [])

        data_payload = {
            "url": scrape_data.get("url", ""),
            "title": scrape_data.get("title", ""),
            "theme": theme,
            "google_font_urls": theme.get("fonts", {}).get("google_font_urls", []),
            "sections_summary": [
                {
                    "type": sec.get("type", "section"),
                    "background_color": sec.get("background_color"),
                    "gradient": sec.get("gradient"),
                    "headings": [
                        {"text": h.get("text", "")[:80], "color": h.get("color"), "font_size": h.get("font_size"), "font_weight": h.get("font_weight")}
                        for h in sec.get("headings", [])[:3]
                    ],
                    "paragraphs": [
                        {"color": p.get("color"), "font_size": p.get("font_size")}
                        for p in sec.get("paragraphs", [])[:2]
                    ],
                    "buttons": [
                        {"text": b.get("text", ""), "bg": b.get("bg"), "color": b.get("color"), "border_radius": b.get("border_radius"), "padding": b.get("padding")}
                        for b in sec.get("buttons", [])[:3]
                    ],
                    "layout": sec.get("layout", {}),
                }
                for sec in sections[:15]
            ],
        }

        content.append({
            "type": "text",
            "text": (
                "Extract the design system from this website. Here is the CSS/computed style data:\n\n"
                f"```json\n{json.dumps(data_payload, indent=2)[:15000]}\n```\n\n"
                "Output ONLY the design_tokens JSON object. No markdown fences. No explanation."
            ),
        })

        text = ""
        async with client.messages.stream(
            model="claude-sonnet-4-5-20250929",
            max_tokens=4000,
            system=[{
                "type": "text",
                "text": DESIGN_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": content}],
        ) as stream:
            async for chunk in stream.text_stream:
                text += chunk

        text = text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]

        tokens = json.loads(text)
        tokens["_fallback"] = False
        print(f"  [design-extractor] Extracted tokens — primary={tokens.get('colors', {}).get('primary', '?')}")
        return tokens

    except Exception as e:
        print(f"  [design-extractor] Claude call failed: {e} — using fallback")
        return _fallback_design_tokens(scrape_data)


def _fallback_design_tokens(scrape_data: dict) -> dict:
    """Build minimal design tokens directly from raw scrape data."""
    theme = scrape_data.get("theme", {})
    colors = theme.get("colors", {})
    fonts = theme.get("fonts", {})

    # Find primary color from accent colors or buttons
    accent_colors = colors.get("accent_colors", [])
    primary = accent_colors[0] if accent_colors else "#3b82f6"

    # Find most common background colors
    backgrounds = colors.get("backgrounds", [])
    page_bg = colors.get("body_bg", "#ffffff")

    # Find a dark background for contrast sections
    dark_bg = None
    for bg in backgrounds:
        if isinstance(bg, str) and bg.startswith("#"):
            # Crude darkness check
            try:
                r = int(bg[1:3], 16)
                g = int(bg[3:5], 16)
                b = int(bg[5:7], 16)
                if (r + g + b) / 3 < 80:
                    dark_bg = bg
                    break
            except (ValueError, IndexError):
                pass
    if not dark_bg:
        dark_bg = "#0f172a"

    heading_color = (colors.get("heading_colors", [None])[0]
                     if colors.get("heading_colors") else colors.get("body_text", "#1a1a1a"))
    body_text = colors.get("body_text", "#374151")

    google_font_urls = fonts.get("google_font_urls", [])
    heading_font = fonts.get("heading", "Inter, system-ui, sans-serif")
    body_font = fonts.get("body", "Inter, system-ui, sans-serif")

    # Extract button styles from sections
    btn_bg = primary
    btn_text = "#ffffff"
    btn_radius = "8px"
    sections = scrape_data.get("sections", [])
    for sec in sections:
        for btn in sec.get("buttons", []):
            if btn.get("bg"):
                btn_bg = btn["bg"]
                if btn.get("color"):
                    btn_text = btn["color"]
                if btn.get("border_radius"):
                    btn_radius = btn["border_radius"]
                break
        if btn_bg != primary:
            break

    return {
        "_fallback": True,
        "colors": {
            "primary": primary,
            "secondary": accent_colors[1] if len(accent_colors) > 1 else primary,
            "backgrounds": {
                "page": page_bg,
                "alt": backgrounds[1] if len(backgrounds) > 1 and isinstance(backgrounds[1], str) and backgrounds[1].startswith("#") else "#f8fafc",
                "dark": dark_bg,
                "card": "#ffffff",
            },
            "text": {
                "heading": heading_color,
                "body": body_text,
                "muted": "#6b7280",
                "on_dark": "#ffffff",
                "link": primary,
            },
            "border": (colors.get("border_colors", ["#e5e7eb"])[0]
                       if colors.get("border_colors") else "#e5e7eb"),
            "divider": "#e5e7eb",
        },
        "typography": {
            "fonts": {
                "heading": heading_font,
                "body": body_font,
                "mono": "monospace",
            },
            "google_font_urls": google_font_urls,
            "scale": {
                "hero_heading": {"size": fonts.get("heading_size", "48px"), "weight": fonts.get("heading_weight", "700"), "line_height": "1.1", "letter_spacing": fonts.get("heading_letter_spacing", "-0.02em")},
                "section_heading": {"size": "36px", "weight": "700", "line_height": "1.2"},
                "card_heading": {"size": "20px", "weight": "600"},
                "body_large": {"size": "18px", "weight": "400"},
                "body": {"size": fonts.get("body_size", "16px"), "weight": fonts.get("body_weight", "400"), "line_height": "1.6"},
                "body_small": {"size": "14px"},
                "label": {"size": "12px", "weight": "600", "letter_spacing": "0.05em"},
                "nav_link": {"size": "14px", "weight": "500"},
            },
        },
        "spacing": {
            "section_padding_y": "80px",
            "section_padding_y_mobile": "48px",
            "container_max_width": "1200px",
            "container_padding_x": "24px",
            "card_gap": "24px",
            "heading_to_content": "48px",
        },
        "components": {
            "button_primary": {"bg": btn_bg, "text": btn_text, "padding": "12px 24px", "border_radius": btn_radius, "hover": {"bg": primary}, "font_weight": "600"},
            "button_secondary": {"bg": "transparent", "text": primary, "border": f"1px solid {primary}", "padding": "12px 24px", "border_radius": btn_radius},
            "card": {"bg": "#ffffff", "border": "1px solid #e5e7eb", "border_radius": "12px", "shadow": "0 1px 3px rgba(0,0,0,0.1)", "padding": "24px", "hover": {}},
            "badge": {"bg": f"{primary}15", "text": primary, "padding": "4px 12px", "border_radius": "9999px", "font_size": "12px"},
            "nav": {"bg": page_bg, "height": "64px", "border_bottom": "1px solid #e5e7eb"},
        },
        "animation": {
            "scroll_entrance": {"initial_opacity": 0, "initial_y": 40, "duration": 0.6, "ease": "easeOut", "stagger": 0.1},
            "hover_lift": {"y": -4, "shadow_increase": True},
            "transition_default": "all 0.3s ease",
        },
        "borders": {
            "radius_sm": "4px",
            "radius_md": "8px",
            "radius_lg": "12px",
            "radius_xl": "16px",
            "radius_full": "9999px",
            "default_color": "#e5e7eb",
        },
        "layout": {
            "hero_layout": "centered",
            "grid_columns": {
                "features": 3,
                "pricing": 3,
                "testimonials": 3,
                "logos": 6,
                "team": 4,
                "stats": 4,
            },
        },
    }
