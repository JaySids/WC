"""
Section planner — splits scrape data into per-section packages.
Pure Python, no AI. Each section gets only its own data.
"""

import base64


# Section type → component name mapping
TYPE_TO_COMPONENT = {
    "navbar": "Navbar",
    "header": "Navbar",
    "hero": "Hero",
    "features": "Features",
    "pricing": "Pricing",
    "testimonials": "Testimonials",
    "faq": "FAQ",
    "cta": "CTA",
    "footer": "Footer",
    "stats": "Stats",
    "logos": "LogoCloud",
    "team": "Team",
    "contact": "Contact",
    "about": "About",
    "blog": "Blog",
}


def plan_sections(scrape_data: dict) -> dict:
    """
    Take the full scrape result and produce per-section packages.

    Returns:
        {
            "shared_context": {
                "url": str,
                "title": str,
                "theme": dict,
                "google_font_urls": list,
                "font_families": list,
                "nav_links": list,
                "footer_links": list,
            },
            "sections": [
                {
                    "id": "section-0",
                    "component_name": "Navbar",
                    "type": "navbar",
                    "order": 0,
                    "data": {
                        "headings": [...],
                        "paragraphs": [...],
                        "images": [...],
                        "links": [...],
                        "buttons": [...],
                        "svgs": [...],
                        "elements": [...],
                        "background_color": str,
                        "gradient": str or None,
                        "background_image_url": str or None,
                        "layout": dict,
                        "bounding_rect": dict,
                        "screenshot_b64": str or None,
                    }
                },
                ...
            ]
        }
    """
    sections = scrape_data.get("sections", [])
    theme = scrape_data.get("theme", {})
    clickables = scrape_data.get("clickables", {})
    scroll_chunks = scrape_data.get("screenshots", {}).get("scroll_chunks", [])

    # Shared context — same for all sections
    shared_context = {
        "url": scrape_data.get("url", ""),
        "title": scrape_data.get("title", ""),
        "theme": theme,
        "google_font_urls": theme.get("fonts", {}).get("google_font_urls", []),
        "font_families": theme.get("fonts", {}).get("custom_fonts", []),
        "nav_links": [
            {"text": l.get("text", ""), "href": l.get("href", "#")}
            for l in clickables.get("nav_links", [])[:15]
        ],
        "footer_links": [
            {"text": l.get("text", ""), "href": l.get("href", "#")}
            for l in clickables.get("footer_links", [])[:15]
        ],
        "animations": scrape_data.get("animations", {}),
        "ui_patterns": scrape_data.get("ui_patterns", []),
        "button_behaviors": scrape_data.get("button_behaviors", []),
    }

    # Build section packages
    planned = []
    used_names = {}

    for i, sec in enumerate(sections):
        sec_type = sec.get("type", "section")
        component_name = _get_component_name(sec_type, i, used_names)

        # Find the best screenshot for this section
        screenshot_b64 = _find_section_screenshot(sec, scroll_chunks)

        # Build per-section data
        data = {
            "headings": sec.get("headings", []),
            "paragraphs": sec.get("paragraphs", []),
            "images": [
                {
                    "url": img.get("url", ""),
                    "alt": img.get("alt", ""),
                    "role": img.get("role", "content"),
                    "width": img.get("width"),
                    "height": img.get("height"),
                }
                for img in sec.get("images", [])[:10]
            ],
            "links": sec.get("links", [])[:10],
            "buttons": [
                {
                    "text": b.get("text", ""),
                    "bg": b.get("bg"),
                    "color": b.get("color"),
                    "border_radius": b.get("border_radius"),
                    "padding": b.get("padding"),
                    "href": b.get("href", "#"),
                }
                for b in sec.get("buttons", [])[:5]
            ],
            "svgs": [
                {
                    "id": s.get("id", f"svg-{j}"),
                    "markup": s.get("markup", "")[:1500],
                    "width": s.get("width"),
                    "height": s.get("height"),
                }
                for j, s in enumerate(sec.get("svgs", [])[:5])
            ],
            "elements": sec.get("elements", [])[:30],
            "background_color": sec.get("background_color"),
            "gradient": sec.get("gradient"),
            "background_image_url": sec.get("background_image_url"),
            "layout": sec.get("layout", {}),
            "bounding_rect": sec.get("bounding_rect", {}),
            "screenshot_b64": screenshot_b64,
        }

        # Type-specific enrichment
        if sec_type in ("navbar", "header"):
            data["nav_links"] = shared_context["nav_links"]
            # Find logo SVG from elements
            for elem in sec.get("elements", []):
                if elem.get("type") == "svg" and elem.get("role") == "logo":
                    data["logo_svg"] = elem.get("markup", "")[:2000]
                    break
            # Find logo image
            for img in sec.get("images", []):
                if img.get("role") == "logo":
                    data["logo_image"] = img.get("url", "")
                    break

        if sec_type == "footer":
            data["footer_links"] = shared_context["footer_links"]

        planned.append({
            "id": f"section-{i}",
            "component_name": component_name,
            "type": sec_type,
            "order": i,
            "data": data,
        })

    return {
        "shared_context": shared_context,
        "sections": planned,
    }


def _get_component_name(sec_type: str, index: int, used_names: dict) -> str:
    """Get a unique component name for a section type."""
    base_name = TYPE_TO_COMPONENT.get(sec_type, f"Section{index}")

    if base_name in used_names:
        used_names[base_name] += 1
        return f"{base_name}{used_names[base_name]}"
    else:
        used_names[base_name] = 1
        return base_name


def _find_section_screenshot(section: dict, scroll_chunks: list) -> str | None:
    """
    Find the scroll screenshot that best covers this section.
    Returns the base64 string, or None if no match.
    """
    if not scroll_chunks:
        return None

    rect = section.get("bounding_rect", {})
    section_top = rect.get("top", 0)
    section_height = rect.get("height", 0)
    section_mid = section_top + section_height / 2

    # Each scroll chunk has a "y" offset and covers ~1080px viewport height
    viewport_height = 1080
    best_chunk = None
    best_overlap = 0

    for chunk in scroll_chunks:
        chunk_y = chunk.get("y", 0)
        chunk_bottom = chunk_y + viewport_height

        # Calculate overlap between section and this chunk's viewport
        overlap_top = max(section_top, chunk_y)
        overlap_bottom = min(section_top + section_height, chunk_bottom)
        overlap = max(0, overlap_bottom - overlap_top)

        if overlap > best_overlap:
            best_overlap = overlap
            best_chunk = chunk

    if best_chunk and best_overlap > 0:
        return best_chunk.get("b64")

    return None
