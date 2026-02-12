"""
Section generator — generates ONE React component per Claude call.
All calls receive the same cached system prompt + design tokens.
"""

import json
import os
import re
import time

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


SECTION_SYSTEM_PROMPT = """You are a React component generator for a Next.js website clone. You generate ONE component at a time.

## CRITICAL: Shared Imports
ALWAYS import and use these shared utilities from `@/lib/utils`:
```jsx
import { cn, containerClass, sectionClass, fadeUp, staggerDelay, buttonPrimaryClass, buttonSecondaryClass, cardClass } from "@/lib/utils";
```

- `cn(...)` — merge Tailwind classes (replaces clsx/twMerge). Use it for ALL className combinations.
- `containerClass` — standard section container (max-width + padding). Wrap all section content in it.
- `sectionClass` — consistent vertical padding for sections. Apply to the outer `<section>`.
- `fadeUp` — framer-motion props for scroll-triggered fade-up. Spread onto `<motion.div {...fadeUp}>`.
- `staggerDelay(index)` — spread onto child `<motion.div>` for staggered entrance.
- `buttonPrimaryClass` / `buttonSecondaryClass` — pre-styled button classes from design tokens.
- `cardClass` — pre-styled card classes from design tokens.

## NEVER do these:
- NEVER define your own `cn()` function — import it from `@/lib/utils`
- NEVER define animation variant objects — use `fadeUp` and `staggerDelay` from utils
- NEVER use named Tailwind colors (bg-blue-500, text-gray-600)

## Rules

1. Output ONLY the raw JSX file content. No markdown fences. No explanation.
2. Start with `"use client";` if the component uses hooks, events, or browser APIs.
3. Use `className` (not `class`), self-close void elements (`<img />`, `<br />`).
4. Use semantic Tailwind classes from @theme (defined in globals.css):
   - Colors: `bg-primary`, `bg-secondary`, `bg-bg-page`, `bg-bg-alt`, `bg-bg-dark`, `bg-bg-card`
   - Text: `text-text-heading`, `text-text-body`, `text-text-muted`, `text-text-on-dark`, `text-text-link`
   - Borders: `border-border`, `border-divider`
   - Fonts: `font-heading`, `font-body`
   - For colors NOT in the theme, use arbitrary hex: `bg-[#hex]`, `text-[#hex]`
5. Use EXACT image URLs from the section data. No placeholders.
6. Use EXACT text content from headings, paragraphs, buttons.
7. Use EXACT link hrefs. No `href="#"` when a real URL exists.
8. Paste SVG markup directly for icons when provided.
9. Make layouts responsive: use `sm:`, `md:`, `lg:` prefixes.
10. For scroll animations, use the shared utils:
    ```jsx
    import { motion } from 'framer-motion';
    import { fadeUp, staggerDelay } from '@/lib/utils';
    <motion.div {...fadeUp}>  // fade-up on scroll
    <motion.div {...fadeUp} {...staggerDelay(index)}>  // staggered children
    ```

## Available packages (pre-installed, import freely):
- framer-motion, gsap
- swiper, swiper/react, swiper/modules, swiper/css/*
- embla-carousel-react
- @headlessui/react
- @radix-ui/react-accordion, @radix-ui/react-dialog, @radix-ui/react-dropdown-menu, @radix-ui/react-tabs, @radix-ui/react-tooltip, @radix-ui/react-popover
- lucide-react, react-icons/*, @heroicons/react/*
- react-intersection-observer, react-scroll, react-countup, react-type-animation
- clsx, tailwind-merge, class-variance-authority (also available via cn() from @/lib/utils)

## Component structure:
```jsx
"use client";
import { motion } from 'framer-motion';
import { cn, containerClass, sectionClass, fadeUp, staggerDelay } from '@/lib/utils';

export default function ComponentName() {
  return (
    <section className={cn(sectionClass, "bg-bg-page")}>
      <div className={containerClass}>
        <motion.div {...fadeUp}>
          {/* component content */}
        </motion.div>
      </div>
    </section>
  );
}
```"""


# Type-specific instructions appended to the user prompt
TYPE_INSTRUCTIONS = {
    "navbar": """NAVBAR RULES:
- Fixed/sticky positioning with backdrop blur
- Logo on the left, nav links center or right
- Mobile hamburger menu with useState toggle
- Use the exact nav_links provided
- Use lucide-react Menu and X icons for mobile toggle""",

    "hero": """HERO RULES:
- Full viewport height or near it (min-h-[80vh] or min-h-screen)
- Use framer-motion for entrance animations (fade-up on heading, stagger on subtext/buttons)
- If there's a background image, use it as a CSS background with overlay
- Primary CTA button uses design_tokens.components.button_primary styles
- Secondary CTA uses design_tokens.components.button_secondary styles""",

    "features": """FEATURES RULES:
- Grid layout: use design_tokens.layout.grid_columns.features (typically 3 cols)
- Each feature card uses design_tokens.components.card styles
- Use framer-motion whileInView with stagger for card entrance
- Place icons/images exactly as shown in the screenshot""",

    "pricing": """PRICING RULES:
- Grid layout: typically 3 columns with a highlighted/recommended plan
- Highlighted plan should have a different background or border (primary color)
- CTA buttons in each card use button_primary or button_secondary from design_tokens
- Price text should be prominent (larger font, bold)""",

    "testimonials": """TESTIMONIALS RULES:
- If >3 testimonials, consider a carousel using Swiper
- Each testimonial card: avatar image, quote text, name, title
- Use design_tokens.components.card for card styles
- Quotation marks or quote icon for visual flair""",

    "faq": """FAQ RULES:
- Use @radix-ui/react-accordion or a simple useState toggle
- Each FAQ item: question (clickable) + answer (collapsible)
- Show/hide with smooth animation
- Use chevron icon that rotates on open""",

    "footer": """FOOTER RULES:
- Use the exact footer_links provided
- Multi-column layout on desktop, stacked on mobile
- Include copyright text
- Use muted text colors from design_tokens
- Dark background if the original footer is dark""",

    "cta": """CTA RULES:
- Strong visual impact — may use primary or dark background
- Large heading, compelling subtext
- Prominent CTA button using design_tokens.components.button_primary
- Consider framer-motion entrance animation""",

    "stats": """STATS RULES:
- Use react-countup for animated numbers
- Grid layout (typically 4 columns)
- Each stat: large number + label
- Trigger animation on scroll with react-intersection-observer""",

    "logos": """LOGO CLOUD RULES:
- Grid of company logos — use exact image URLs
- Logos should be grayscale/muted, optionally with hover color effect
- Responsive grid: 3 cols mobile, 6 cols desktop
- Optional: infinite scroll marquee with CSS animation""",
}


async def generate_section(
    section: dict,
    shared_context: dict,
    design_tokens: dict,
    component_manifest: list | None = None,
) -> dict:
    """
    Generate one React component for a section.

    Args:
        section: Per-section package from section_planner
        shared_context: Shared data (theme, fonts, nav_links, etc.)
        design_tokens: The design system contract
        component_manifest: List of all components being generated (for awareness)

    Returns:
        {
            "component_name": str,
            "filepath": str,
            "content": str,
            "success": bool,
            "error": str or None,
            "tokens_in": int,
            "tokens_out": int,
        }
    """
    component_name = section["component_name"]
    sec_type = section["type"]
    data = section["data"]
    t0 = time.time()

    try:
        client = _get_client()

        # Build the user message content
        content = []

        # Add section screenshot if available
        screenshot = data.get("screenshot_b64")
        if screenshot:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": screenshot},
            })

        # Build section data JSON
        section_data = {
            "component_name": component_name,
            "section_type": sec_type,
            "order": section["order"],
            "data": {
                "headings": data.get("headings", []),
                "paragraphs": data.get("paragraphs", []),
                "images": data.get("images", []),
                "links": data.get("links", []),
                "buttons": data.get("buttons", []),
                "svgs": [{"id": s["id"], "markup": s["markup"][:1000], "width": s.get("width"), "height": s.get("height")} for s in data.get("svgs", [])],
                "background_color": data.get("background_color"),
                "gradient": data.get("gradient"),
                "background_image_url": data.get("background_image_url"),
                "layout": data.get("layout", {}),
                "elements": data.get("elements", [])[:20],
            },
            "shared": {
                "nav_links": data.get("nav_links", shared_context.get("nav_links", [])),
                "footer_links": data.get("footer_links", shared_context.get("footer_links", [])),
                "google_font_urls": shared_context.get("google_font_urls", []),
            },
        }

        # Type-specific instructions
        type_instructions = TYPE_INSTRUCTIONS.get(sec_type, "")

        # Build component manifest awareness
        manifest_text = ""
        if component_manifest:
            siblings = [
                f"  - {c['name']} ({c['type']}, order {c['order']})"
                for c in component_manifest
                if c["name"] != component_name
            ]
            manifest_text = (
                "\nCOMPONENT MANIFEST — other components being generated in parallel:\n"
                + "\n".join(siblings)
                + "\nDo NOT duplicate content that belongs to another component. "
                "Stay focused on YOUR section's content only.\n"
            )

        user_text = (
            f"Generate the `{component_name}` component (section type: {sec_type}).\n\n"
            f"DESIGN TOKENS (use these EXACT values for all styling):\n"
            f"```json\n{json.dumps(design_tokens, indent=2)[:6000]}\n```\n\n"
            f"SECTION DATA:\n"
            f"```json\n{json.dumps(section_data, indent=2)[:8000]}\n```\n"
            f"{manifest_text}\n"
        )

        if type_instructions:
            user_text += f"\n{type_instructions}\n"

        user_text += (
            "\nREMINDER: Import cn, containerClass, sectionClass, fadeUp, staggerDelay from '@/lib/utils'. "
            "Do NOT define your own cn() or animation variants.\n"
            "\nOutput ONLY the raw file content. No markdown fences. No explanation. "
            f"The component should export default function {component_name}()."
        )

        content.append({"type": "text", "text": user_text})

        raw = ""
        async with client.messages.stream(
            model="claude-sonnet-4-5-20250929",
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": SECTION_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": content}],
        ) as stream:
            async for chunk in stream.text_stream:
                raw += chunk
            response = await stream.get_final_message()

        cleaned = _strip_code_fences(raw)

        elapsed = time.time() - t0
        usage = getattr(response, "usage", None)
        tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
        tokens_out = getattr(usage, "output_tokens", 0) if usage else 0

        print(f"  [section-gen] {component_name} ({sec_type}) — "
              f"{elapsed:.1f}s, {tokens_in}in/{tokens_out}out, {len(cleaned)} chars")

        return {
            "component_name": component_name,
            "filepath": f"components/{component_name}.jsx",
            "content": cleaned,
            "success": True,
            "error": None,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }

    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [section-gen] {component_name} FAILED in {elapsed:.1f}s: {e}")
        return {
            "component_name": component_name,
            "filepath": f"components/{component_name}.jsx",
            "content": _fallback_component(component_name, sec_type),
            "success": False,
            "error": str(e),
            "tokens_in": 0,
            "tokens_out": 0,
        }


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences from Claude output."""
    text = text.strip()
    if text.startswith("```"):
        # Remove first line (```jsx or ```typescript etc.)
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _fallback_component(name: str, sec_type: str) -> str:
    """Generate a minimal placeholder component when generation fails."""
    return f'''"use client";

export default function {name}() {{
  return (
    <section className="py-16 px-4">
      <div className="max-w-6xl mx-auto text-center">
        <p className="text-gray-500">Section: {name} ({sec_type})</p>
      </div>
    </section>
  );
}}
'''
