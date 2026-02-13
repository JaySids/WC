"""
Project assembler — builds the final project from parallel generation results.

Functions:
  assemble_project()         — pure string concatenation, no AI
  review_and_fix_assembly()  — single Claude call for cross-component fixes
  claude_code_review()       — Claude code error review (legacy, still usable)
"""

import asyncio
import json
import os

import anthropic


# ---------------------------------------------------------------
# assemble_project — pure string concatenation, NO AI
# ---------------------------------------------------------------

def assemble_project(
    results: list[dict],
    shared: dict,
    sections: list[dict],
    design_tokens: dict,
) -> dict:
    """
    Assemble the final project files from parallel generation results.
    Pure string concatenation — no AI calls.

    Builds:
      - app/page.jsx       — imports + renders all components in order
      - app/layout.jsx      — Google Font links + metadata + body font
      - app/globals.css     — @import fonts + @tailwind + base styles + CSS vars
      - lib/utils.js        — cn, containerClass, sectionClass, fadeUp, staggerDelay
      - components/ErrorBoundary.jsx — client error boundary

    Returns: {filepath: content} dict
    """
    files = {}

    # Collect successful component files, sorted by section order
    successful = [r for r in results if r.get("success")]
    order_map = {s["component_name"]: s["order"] for s in sections}
    successful.sort(key=lambda r: order_map.get(r["component_name"], 99))

    # Add component files
    for r in successful:
        files[r["filepath"]] = r["content"]

    # ---- app/page.jsx ----
    imports = ['import ErrorBoundary from "../components/ErrorBoundary";']
    elements = []
    for r in successful:
        name = r["component_name"]
        imports.append(f'import {name} from "../components/{name}";')
        elements.append(f'      <ErrorBoundary name="{name}"><{name} /></ErrorBoundary>')

    page_jsx = (
        '"use client";\n'
        + "\n".join(imports) + "\n\n"
        + "export default function Home() {\n"
        + "  return (\n"
        + '    <main className="min-h-screen">\n'
        + "\n".join(elements) + "\n"
        + "    </main>\n"
        + "  );\n"
        + "}\n"
    )
    files["app/page.jsx"] = page_jsx

    # ---- app/layout.jsx ----
    files["app/layout.jsx"] = _build_layout_jsx(shared, design_tokens)

    # ---- app/globals.css ----
    files["app/globals.css"] = _build_globals_css(design_tokens)

    # ---- lib/utils.js ----
    files["lib/utils.js"] = _build_utils_js(design_tokens)

    # ---- components/ErrorBoundary.jsx ----
    files["components/ErrorBoundary.jsx"] = _build_error_boundary()

    return files


def _build_layout_jsx(shared: dict, design_tokens: dict) -> str:
    """Build app/layout.jsx with Google Font links and metadata."""
    fonts = design_tokens.get("typography", {}).get("fonts", {})
    google_urls = design_tokens.get("typography", {}).get("google_font_urls", [])
    title = shared.get("title", "Website Clone").replace('"', '\\"')
    body_font = fonts.get("body", "Inter, system-ui, sans-serif")
    font_name = body_font.split(",")[0].strip().strip("'\"").replace(" ", "_")

    font_links = (
        '        <link rel="preconnect" href="https://fonts.googleapis.com" />\n'
        '        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />\n'
    )
    for url in google_urls:
        safe_url = url.replace('"', '&quot;')
        font_links += f'        <link href="{safe_url}" rel="stylesheet" />\n'

    return f'''import "./globals.css";

export const metadata = {{
  title: "{title}",
  description: "Cloned website",
}};

export default function RootLayout({{ children }}) {{
  return (
    <html lang="en">
      <head>
{font_links}      </head>
      <body className="font-['{font_name}'] antialiased">{{children}}</body>
    </html>
  );
}}
'''


def _build_globals_css(design_tokens: dict) -> str:
    """Build app/globals.css with font imports, Tailwind, CSS variables, and base styles."""
    google_urls = design_tokens.get("typography", {}).get("google_font_urls", [])
    colors = design_tokens.get("colors", {})
    fonts = design_tokens.get("typography", {}).get("fonts", {})
    bgs = colors.get("backgrounds", {})
    text = colors.get("text", {})

    import_lines = ""
    for url in google_urls:
        import_lines += f"@import url('{url}');\n"

    return f"""{import_lines}
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {{
  :root {{
    --color-primary: {colors.get("primary", "#3b82f6")};
    --color-secondary: {colors.get("secondary", "#6366f1")};
    --color-bg-page: {bgs.get("page", "#ffffff")};
    --color-bg-alt: {bgs.get("alt", "#f8fafc")};
    --color-bg-dark: {bgs.get("dark", "#0f172a")};
    --color-bg-card: {bgs.get("card", "#ffffff")};
    --color-text-heading: {text.get("heading", "#1a1a1a")};
    --color-text-body: {text.get("body", "#374151")};
    --color-text-muted: {text.get("muted", "#6b7280")};
    --color-text-on-dark: {text.get("on_dark", "#ffffff")};
    --color-text-link: {text.get("link", "#3b82f6")};
    --color-border: {colors.get("border", "#e5e7eb")};
    --color-divider: {colors.get("divider", "#e5e7eb")};
  }}
}}

*, *::before, *::after {{
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}}

html {{
  scroll-behavior: smooth;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}

body {{
  color: {text.get("body", "#374151")};
  background: {bgs.get("page", "#ffffff")};
  font-family: {fonts.get("body", "Inter, system-ui, sans-serif")};
}}

img, video {{
  max-width: 100%;
  height: auto;
  display: block;
}}

.scrollbar-hide::-webkit-scrollbar {{
  display: none;
}}
.scrollbar-hide {{
  -ms-overflow-style: none;
  scrollbar-width: none;
}}
"""


def _build_utils_js(design_tokens: dict) -> str:
    """Build lib/utils.js with shared utilities that all components import."""
    spacing = design_tokens.get("spacing", {})
    components = design_tokens.get("components", {})
    colors = design_tokens.get("colors", {})
    btn_primary = components.get("button_primary", {})
    btn_secondary = components.get("button_secondary", {})
    card = components.get("card", {})

    container_max = spacing.get("container_max_width", "1200px")
    container_px = spacing.get("container_padding_x", "24px")
    section_py = spacing.get("section_padding_y", "80px")
    section_py_mobile = spacing.get("section_padding_y_mobile", "48px")

    # Card border handling
    card_border_raw = card.get("border", "none")
    card_has_border = card_border_raw and card_border_raw != "none"
    if card_has_border:
        # Extract color from "1px solid #hex"
        parts = card_border_raw.split()
        card_border_color = parts[-1] if parts else colors.get("border", "#e5e7eb")
        card_border_class = f'"border border-[{card_border_color}]",'
    else:
        card_border_class = ""

    card_shadow = card.get("shadow", "none")
    card_shadow_class = f'"shadow-[{card_shadow}]",' if card_shadow and card_shadow != "none" else ""

    return f'''import {{ clsx }} from "clsx";
import {{ twMerge }} from "tailwind-merge";

export function cn(...inputs) {{
  return twMerge(clsx(inputs));
}}

export const containerClass = "max-w-[{container_max}] mx-auto px-[{container_px}]";

export const sectionClass = "py-[{section_py_mobile}] md:py-[{section_py}]";

export const fadeUp = {{
  initial: {{ opacity: 0, y: 40 }},
  whileInView: {{ opacity: 1, y: 0 }},
  viewport: {{ once: true, margin: "-100px" }},
  transition: {{ duration: 0.6, ease: "easeOut" }},
}};

export function staggerDelay(index) {{
  return {{
    initial: {{ opacity: 0, y: 40 }},
    whileInView: {{ opacity: 1, y: 0 }},
    viewport: {{ once: true, margin: "-100px" }},
    transition: {{ duration: 0.6, ease: "easeOut", delay: index * 0.1 }},
  }};
}}

export const buttonPrimaryClass = cn(
  "inline-flex items-center justify-center",
  "bg-[{btn_primary.get("bg", "#3b82f6")}]",
  "text-[{btn_primary.get("text", "#ffffff")}]",
  "rounded-[{btn_primary.get("border_radius", "8px")}]",
  "px-6 py-3",
  "font-semibold",
  "transition-all duration-200",
  "hover:opacity-90"
);

export const buttonSecondaryClass = cn(
  "inline-flex items-center justify-center",
  "bg-transparent",
  "text-[{btn_secondary.get("text", colors.get("primary", "#3b82f6"))}]",
  "border border-[{colors.get("border", "#e5e7eb")}]",
  "rounded-[{btn_secondary.get("border_radius", "8px")}]",
  "px-6 py-3",
  "font-semibold",
  "transition-all duration-200",
  "hover:bg-[{colors.get("backgrounds", {{}}).get("alt", "#f8fafc")}]"
);

export const cardClass = cn(
  "bg-[{card.get("bg", "#ffffff")}]",
  "rounded-[{card.get("border_radius", "12px")}]",
  "p-[{card.get("padding", "24px")}]",
  {card_border_class}
  {card_shadow_class}
  "transition-all duration-300"
);
'''


def _build_error_boundary() -> str:
    """Build components/ErrorBoundary.jsx — client-side error boundary."""
    return '''"use client";
import { Component } from "react";

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }
  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }
  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: "1.5rem", background: "#1e1e2e", color: "#f38ba8", fontFamily: "monospace", borderRadius: "8px", margin: "0.5rem 0", border: "1px solid #45475a" }}>
          <strong>{this.props.name || "Section"} failed to render:</strong>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: "0.8rem", color: "#cdd6f4", marginTop: "0.5rem" }}>{this.state.error?.toString()}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}
'''


# ---------------------------------------------------------------
# review_and_fix_assembly — single Claude call
# ---------------------------------------------------------------

ASSEMBLY_REVIEW_PROMPT = """You are a senior React/Next.js developer reviewing a set of independently-generated components that need to work together as one cohesive website.

All components were generated in parallel from scraped website data. They share a design token system but may have inconsistencies.

## Fix These Issues:
1. **Duplicate utility functions**: If multiple components define their own `cn()` or animation helpers, remove them and ensure they import from `@/lib/utils`.
2. **z-index conflicts**: Navbar should be z-50, dropdowns z-40, modals z-50+.
3. **Missing Swiper CSS**: If any component uses Swiper, ensure it imports `swiper/css`, `swiper/css/pagination`, etc.
4. **Inconsistent spacing**: Section padding should use the shared sectionClass from utils, not hardcoded values.
5. **Import errors**: Remove imports from packages that aren't installed. Fix wrong import paths.
6. **"use client" directive**: Add it to every component file that uses hooks, events, or browser APIs.
7. **className not class**: Replace every `class=` with `className=`.
8. **Self-closing void elements**: `<img />`, `<br />`, `<input />` — never `<img>`.
9. **htmlFor not for**: In `<label>` elements.
10. **key props**: Every `.map()` call must have a `key` prop.
11. **alt props**: Every `<img>` must have an `alt` prop.
12. **JSX comments**: `{/* comment */}` not `<!-- comment -->`.

## DO NOT:
- Rewrite component logic or structure
- Remove sections or content
- Add new sections or features
- Change text content, image URLs, or link hrefs
- Make the output shorter or simpler

## Output Format
Output ONLY a JSON object mapping filepath to COMPLETE corrected file content.
Only include files that actually need changes. If no changes needed, output: {}
No markdown fences. No explanation."""


def _get_anthropic_client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        from app.config import get_settings
        api_key = get_settings().anthropic_api_key
    return anthropic.AsyncAnthropic(api_key=api_key)


async def review_and_fix_assembly(files: dict) -> dict:
    """
    Single Claude call to review all assembled files for cross-component issues.
    Returns only the changed files (or empty dict if clean).
    """
    try:
        client = _get_anthropic_client()

        # Build file listing
        file_listing = []
        for fp in sorted(files.keys()):
            if fp.endswith((".jsx", ".js", ".css")):
                file_listing.append(f"=== {fp} ===\n{files[fp]}")
        all_files_text = "\n\n".join(file_listing)

        if len(all_files_text) > 80000:
            all_files_text = all_files_text[:80000] + "\n\n... (truncated)"

        text = ""
        async with client.messages.stream(
            model="claude-sonnet-4-5-20250929",
            max_tokens=32000,
            system=[{
                "type": "text",
                "text": ASSEMBLY_REVIEW_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": f"Review these files for cross-component issues:\n\n{all_files_text}",
            }],
        ) as stream:
            async for chunk in stream.text_stream:
                text += chunk

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]

        changes = json.loads(text.strip())

        if changes:
            print(f"  [review-assembly] Fixed {len(changes)} files: {list(changes.keys())}")
        else:
            print("  [review-assembly] Clean — no issues found")

        return changes

    except Exception as e:
        print(f"  [review-assembly] Failed: {e}")
        return {}


# ---------------------------------------------------------------
# Legacy review functions (kept for backwards compatibility)
# ---------------------------------------------------------------

async def claude_code_review(files: dict) -> dict:
    """Claude reviews all files for JSX/React code errors."""
    return await review_and_fix_assembly(files)
