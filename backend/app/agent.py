"""
Agent orchestrator — one-shot generation pipeline.

Pipeline:
  [A+B] scrape + sandbox (parallel)
  [C]   one-shot generation (single Claude call → all files as JSON)
  [D]   upload to sandbox
  [E]   check compilation + fix loop
  [F]   done
"""

import asyncio
import json
import os
import re
import time
from typing import AsyncGenerator

import anthropic

from app.sse_utils import sse_event
from app.scraper import scrape_website
from app.nextjs_error_parser import (
    parse_nextjs_errors,
    parse_runtime_errors,
    format_nextjs_errors,
)
from app.sandbox import PROJECT_PATH, BUN_BIN, get_daytona_client, create_react_boilerplate_sandbox
from app.sandbox_template import upload_files_to_sandbox, get_sandbox_logs


OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")

# In-memory chat session storage (clone_id → session data)
_chat_sessions: dict = {}

# Active sandbox tracking (sandbox_id → sandbox info)
active_sandboxes: dict = {}

# Claude model
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

# Sandbox auto-stop after N minutes of inactivity
SANDBOX_TTL_MINUTES = 30


def _get_client():
    """Get an async Anthropic client."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        from app.config import get_settings
        api_key = get_settings().anthropic_api_key
    return anthropic.AsyncAnthropic(api_key=api_key)


def _file_language(filepath: str) -> str:
    """Infer language label from file extension."""
    if filepath.endswith((".tsx", ".jsx")):
        return "tsx"
    if filepath.endswith((".ts", ".mts")):
        return "typescript"
    if filepath.endswith((".js", ".mjs")):
        return "javascript"
    if filepath.endswith(".css"):
        return "css"
    if filepath.endswith(".json"):
        return "json"
    return "text"


def _save_file_locally(clone_id: str, filepath: str, content: str):
    """Save a file to backend/output/{clone_id}/{filepath} for debugging."""
    if not clone_id:
        return
    dest = os.path.join(OUTPUT_DIR, clone_id, filepath)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content)


async def _clear_sandbox_logs(sandbox_id: str, project_root: str):
    """Write a timestamp marker to server.log so we can find fresh output."""
    def _clear():
        try:
            daytona = get_daytona_client()
            sb = daytona.get(sandbox_id)
            sb.process.exec(
                f"echo '=== LOG_MARKER_'$(date +%s)' ===' >> {project_root}/server.log",
                timeout=5,
            )
        except Exception:
            pass
    await asyncio.to_thread(_clear)


async def _touch_sandbox_files(sandbox_id: str, filepaths: list[str], project_root: str):
    """Touch files in sandbox to trigger Next.js filesystem watcher."""
    def _touch():
        try:
            daytona = get_daytona_client()
            sb = daytona.get(sandbox_id)
            paths = " ".join(f"{project_root}/{fp}" for fp in filepaths)
            sb.process.exec(f"touch {paths}", timeout=10)
        except Exception as e:
            print(f"  [touch] Failed: {e}")
    await asyncio.to_thread(_touch)


async def _restart_dev_server(sandbox_id: str, project_root: str):
    """Kill any running Next.js/bun processes and restart the dev server."""
    def _restart():
        try:
            daytona = get_daytona_client()
            sb = daytona.get(sandbox_id)
            # Kill existing dev server
            sb.process.exec("pkill -f next || true; pkill -f bun || true", timeout=5)
            import time as _t
            _t.sleep(1)
            # Clear old logs
            log_file = f"{project_root}/server.log"
            sb.process.exec(f"> {log_file}", timeout=5)
            # Start fresh
            start_cmd = (
                f"nohup {BUN_BIN} --cwd {project_root} --bun next dev -p 3000 -H 0.0.0.0 "
                f"> {log_file} 2>&1 &"
            )
            sb.process.exec(start_cmd, timeout=10)
        except Exception as e:
            print(f"  [restart-dev] Failed: {e}")
    await asyncio.to_thread(_restart)


async def _check_sandbox_http(sandbox_id: str) -> dict:
    """Fetch the page from the sandbox and check for errors."""
    def _check():
        try:
            daytona = get_daytona_client()
            sb = daytona.get(sandbox_id)
            result = sb.process.exec(
                "curl -s -w '\\n__HTTP_CODE__%{http_code}' http://localhost:3000/ 2>/dev/null",
                timeout=15,
            )
            output = result.result or ""
            if "__HTTP_CODE__" in output:
                body, code_part = output.rsplit("__HTTP_CODE__", 1)
                status_code = int(code_part.strip())
            else:
                body = output
                status_code = 0

            errors = []
            # Only match indicators that unambiguously signal a runtime
            # error page.  Avoid substrings that appear in normal Next.js
            # HTML (e.g. script chunk names like "next-error-*.js", or
            # hydration "digest=" attributes).
            error_indicators = [
                "Application error: a client-side exception",
                "Application error: a server-side exception",
                "Unhandled Runtime Error",
                "Internal Server Error",
                "Error: Minified React error",
                "nextjs__container_errors__",
            ]
            for indicator in error_indicators:
                if indicator in body:
                    errors.append(indicator)

            if status_code == 200 and len(body.strip()) < 200:
                errors.append("page_too_small")
            if status_code >= 500:
                errors.append(f"http_{status_code}")

            return {
                "ok": status_code in (200, 304) and len(errors) == 0,
                "status_code": status_code,
                "errors": errors,
                "body_length": len(body),
                "body": body[:3000],
            }
        except Exception as e:
            return {"ok": False, "status_code": 0, "errors": [str(e)], "body_length": 0, "body": ""}
    return await asyncio.to_thread(_check)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences from Claude output."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _filter_errors_to_generated(errors: list, generated_files: dict) -> list:
    """Keep only errors that map to generated files."""
    if not errors:
        return []
    allowed = set(generated_files.keys())
    filtered = []
    for err in errors:
        fp = err.get("file")
        if not fp:
            continue
        if fp in allowed:
            filtered.append(err)
            continue
        # Fallback: match by suffix
        match = next((a for a in allowed if a.endswith(fp) or fp.endswith(a)), None)
        if match:
            err = {**err, "file": match}
            filtered.append(err)
    return filtered


# ---------------------------------------------------------------
# ONE-SHOT GENERATION
# ---------------------------------------------------------------

GENERATE_SYSTEM_PROMPT = """You are a pixel-perfect website cloner. Your ONLY job is to faithfully replicate the original website exactly as it appears. Do NOT enhance, improve, or add anything that isn't on the original site.

## Output Format
Output ONLY a JSON object mapping file paths to their complete file contents:
{"app/globals.css": "...", "app/layout.jsx": "...", "app/page.jsx": "...", "components/Navbar.jsx": "...", ...}

No markdown fences around the JSON. No explanation. ONLY the JSON object.

## Required Files
1. `app/globals.css` — Tailwind v4 CSS (see CSS Rules below). MUST start with `@import "tailwindcss";` as the VERY FIRST LINE.
2. `app/layout.jsx` — MUST have `import "./globals.css";` as the FIRST import (THIS IS CRITICAL — without it NO CSS loads and the page is unstyled). Root layout with <html>, <head> with Google Font <link> tags, metadata export, body with font className. Must NOT have "use client".
3. `app/page.jsx` — "use client" directive, imports and renders ALL section components in visual order top-to-bottom
4. `components/*.jsx` — One file per major page section (Navbar, Hero, Features, Pricing, Testimonials, FAQ, Footer, etc.)

## layout.jsx Template (FOLLOW THIS EXACTLY)
```jsx
import "./globals.css";

export const metadata = { title: "...", description: "..." };

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        {/* Google Font <link> tags here */}
      </head>
      <body className="font-sans">{children}</body>
    </html>
  );
}
```

## FAITHFUL REPLICATION RULES (MOST IMPORTANT)
- Clone the website EXACTLY as it appears in the screenshots and scraped data
- Do NOT add animations, hover effects, or transitions unless the original site clearly has them
- Do NOT add features like mobile hamburger menus unless the original site has one
- Do NOT add scroll animations (framer-motion, fade-ins, slide-ups) unless the original site uses them
- Use the EXACT text content, headings, paragraphs, and link text from the scraped data
- Match the EXACT colors, font sizes, font weights, spacing, and layout from the scraped data
- Replicate the original site's section structure, order, and visual hierarchy precisely
- If something is unclear from the screenshots, use the scraped data (DOM skeleton, theme, sections) as the source of truth

## Tailwind CSS v4 Rules (CRITICAL — wrong syntax = no styling)
This project uses **Tailwind CSS v4**. The CSS syntax is different from v3:

- Start `app/globals.css` with `@import "tailwindcss";` (NOT `@tailwind base/components/utilities` — that is v3 and WILL BREAK)
- Use `@theme { }` blocks to define custom CSS variables (colors, fonts from scraped theme data)
- Use `@layer base { }` for base/reset styles
- `@apply` still works inside `@layer` blocks
- All Tailwind utility classes (bg-[#hex], text-[#hex], flex, grid, etc.) work the same as v3

Example `app/globals.css`:
```
@import "tailwindcss";

@theme {
  --color-primary: #3b82f6;
  --color-secondary: #6366f1;
  --font-body: "Inter", system-ui, sans-serif;
}

@layer base {
  html { scroll-behavior: smooth; }
  body {
    font-family: var(--font-body);
    color: #374151;
    background: #ffffff;
  }
}
```

## JSX Rules (CRITICAL — violations cause "Parsing ecmascript source code failed" and crash the build)
Any HTML-style syntax in JSX will CRASH the build with "Parsing ecmascript source code failed". You MUST convert ALL HTML to valid JSX:

### HTML → JSX conversions (EVERY ONE IS MANDATORY):
| WRONG (crashes build)          | CORRECT                              |
|-------------------------------|--------------------------------------|
| `<div class="foo">`           | `<div className="foo">`              |
| `<label for="x">`            | `<label htmlFor="x">`                |
| `<img src="x">`              | `<img src="x" alt="" />`             |
| `<br>`                        | `<br />`                             |
| `<hr>`                        | `<hr />`                             |
| `<input type="text">`        | `<input type="text" />`              |
| `style="color: red"`         | `style={{ color: 'red' }}`           |
| `<!-- comment -->`            | `{/* comment */}`                    |
| `onclick="fn()"`             | `onClick={fn}`                       |
| `tabindex="0"`               | `tabIndex={0}`                       |
| `colspan="2"`                | `colSpan={2}`                        |
| `&nbsp;`                     | `{'\u00A0'}` or just use a space     |
| `&mdash;`                    | `—` (use the actual character)       |
| `&amp;`                      | `&` (ampersands are fine in JSX text)|
| `&lt;` / `&gt;`             | Use the actual `<` `>` chars in text, or `{'<'}` `{'>'}` |

### Other required JSX rules:
- `"use client"` at the TOP of every component file that uses hooks, events, or browser APIs
- `app/layout.jsx` must NOT have "use client"
- Every `.map()` needs a unique `key` prop
- Export default function from every component file

### PRE-OUTPUT CHECKLIST — verify before outputting ANY JSX:
1. Search your output for the word `class=` — if found, replace with `className=`
2. Search for `for=` on labels — if found, replace with `htmlFor=`
3. Search for `style="` — if found, convert to `style={{ }}`
4. Search for `<!--` — if found, convert to `{/* */}`
5. Search for `<br>`, `<hr>`, `<img `, `<input ` without self-closing `/>` — add `/>`
6. Search for `&nbsp;`, `&mdash;`, `&amp;`, `&quot;` — replace with actual characters

## Color Rules
NEVER use named Tailwind colors (bg-blue-500, text-gray-600).
ALWAYS use exact hex values from the scraped data: `bg-[#0a2540]`, `text-[#425466]`, `border-[#e5e7eb]`

## Image Rules
NEVER use placeholder images (placeholder.com, via.placeholder, placehold.co).
ALWAYS use the real image URLs provided in the scrape data.
Use `<img>` tags (NOT Next.js `<Image>`) to avoid domain configuration issues.

## Available Packages (pre-installed, use ONLY when the original site has the corresponding feature)
- framer-motion — ONLY if the original site has visible animations/transitions
- lucide-react — for icons (Menu, X, ChevronDown, ArrowRight, Check, Star, etc.)
- @radix-ui/react-accordion, dialog, tabs — for interactive UI if the original has it
- react-intersection-observer — for lazy loading if needed
- clsx + tailwind-merge — className merging utility

## Architecture
- Create a `lib/utils.js` with: `import { clsx } from "clsx"; import { twMerge } from "tailwind-merge"; export const cn = (...inputs) => twMerge(clsx(inputs));`
- Make ALL layouts responsive with `sm:`, `md:`, `lg:` breakpoint prefixes
- Match the visual layout, colors, spacing, and typography EXACTLY as shown in screenshots
- Use exact text content, headings, link hrefs, and image URLs from the scraped data"""


def _extract_json_from_response(raw: str) -> dict:
    """Try multiple strategies to extract a JSON dict from Claude's response."""
    text = raw.strip()

    # Strategy 1: direct parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # Strategy 2: strip code fences
    cleaned = _strip_code_fences(text)
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Strategy 3: find outermost { ... } with string-awareness
    start = text.find("{")
    if start >= 0:
        # Try parsing from the first { to end — progressively trim
        # First try full text from first {
        candidate = text[start:]
        try:
            return json.loads(candidate)
        except Exception:
            pass

        # Try trimming to last }
        last_brace = candidate.rfind("}")
        if last_brace > 0:
            try:
                return json.loads(candidate[:last_brace + 1])
            except Exception:
                pass

    # Strategy 4: regex extraction of key-value pairs for truncated JSON
    # If output was truncated mid-file, extract whatever complete file entries exist
    if start is not None and start >= 0:
        files = {}
        # Match "filepath": "content" pairs — the content may contain escaped quotes
        # Use a simpler approach: split on the pattern that starts a new file entry
        file_pattern = re.compile(r'"((?:app|components|lib)/[^"]+)":\s*"')
        matches = list(file_pattern.finditer(text))
        for i, m in enumerate(matches):
            filepath = m.group(1)
            content_start = m.end()
            # Find the end of this string value — look for unescaped "
            # followed by either , or }
            j = content_start
            while j < len(text):
                if text[j] == "\\" and j + 1 < len(text):
                    j += 2  # Skip escaped character
                    continue
                if text[j] == '"':
                    # Check if followed by , or } or whitespace then , or }
                    rest = text[j + 1:].lstrip()
                    if rest and rest[0] in (",", "}"):
                        # Found end of this value
                        try:
                            content = json.loads('"' + text[content_start:j].replace('\n', '\\n') + '"')
                            files[filepath] = content
                        except Exception:
                            # Try raw unescape
                            content = text[content_start:j].replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
                            if content.strip():
                                files[filepath] = content
                        break
                j += 1
        if files:
            print(f"  [generate] Recovered {len(files)} files from truncated JSON")
            return files

    return {}


async def _generate_all(scrape_data: dict) -> tuple[dict, int, int]:
    """
    Single Claude call to generate all project files from scrape data.
    Returns (files_dict, tokens_in, tokens_out).
    """
    client = _get_client()
    content = []

    # Add screenshots as vision input
    # All screenshots are compressed to JPEG by image_utils.screenshot_to_b64
    screenshots = scrape_data.get("screenshots", {})

    # Full page screenshot
    full_ss = screenshots.get("full_page")
    if full_ss:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": full_ss},
        })

    # Viewport screenshot
    viewport_ss = screenshots.get("viewport")
    if viewport_ss and not full_ss:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": viewport_ss},
        })

    # Scroll chunk screenshots (up to 6)
    scroll_chunks = screenshots.get("scroll_chunks", [])
    for chunk in scroll_chunks[:6]:
        b64 = chunk.get("b64") or chunk if isinstance(chunk, str) else chunk.get("b64", "")
        if b64:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            })

    # Section screenshots (up to 6)
    sections = scrape_data.get("sections", [])
    section_ss_count = 0
    for sec in sections:
        if section_ss_count >= 6:
            break
        ss = sec.get("screenshot_b64")
        if ss:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": ss},
            })
            section_ss_count += 1

    # Build structured data for the text prompt
    theme = scrape_data.get("theme", {})
    clickables = scrape_data.get("clickables", {})
    assets = scrape_data.get("assets", {})

    sections_data = []
    for i, sec in enumerate(sections[:15]):
        sec_data = {
            "order": i,
            "type": sec.get("type", "content"),
            "background_color": sec.get("bg_color") or sec.get("background_color"),
            "headings": sec.get("headings", [])[:10],
            "paragraphs": sec.get("paragraphs", [])[:10],
            "images": sec.get("images", [])[:15],
            "links": sec.get("links", [])[:15],
            "buttons": sec.get("buttons", [])[:10],
            "layout": sec.get("layout", {}),
        }
        svgs = sec.get("svgs", [])
        if svgs:
            sec_data["svgs"] = [
                {"id": s.get("id", ""), "markup": s.get("markup", "")[:800]}
                for s in svgs[:5]
            ]
        if sec.get("gradient"):
            sec_data["gradient"] = sec["gradient"]
        if sec.get("background_image_url"):
            sec_data["background_image_url"] = sec["background_image_url"]
        sections_data.append(sec_data)

    structured = {
        "url": scrape_data.get("url", ""),
        "title": scrape_data.get("title", ""),
        "page_height": scrape_data.get("page_height", 0),
        "theme": {
            "colors": theme.get("colors", {}),
            "fonts": theme.get("fonts", {}),
        },
        "nav_links": clickables.get("nav_links", [])[:20],
        "footer_links": clickables.get("footer_links", [])[:30],
        "cta_buttons": clickables.get("cta_buttons", [])[:10],
        "sections": sections_data,
        "image_urls": [
            img.get("url") if isinstance(img, dict) else img
            for img in assets.get("images", [])[:30]
        ],
    }

    # Add Google font URLs if available
    font_urls = theme.get("google_font_urls", []) or assets.get("fonts", [])
    if font_urls:
        structured["google_font_urls"] = font_urls

    data_json = json.dumps(structured, indent=2, default=str)
    if len(data_json) > 30000:
        data_json = data_json[:30000] + "\n... (truncated)"

    content.append({
        "type": "text",
        "text": (
            f"Clone this website: {scrape_data.get('url', '')}\n"
            f"Page title: {scrape_data.get('title', '')}\n\n"
            f"SCRAPED DATA:\n{data_json}\n\n"
            "Generate a complete Next.js 14 clone with ALL sections shown in the screenshots.\n"
            "Use EXACT colors, fonts, images, text, and links from the scraped data.\n"
            "Output ONLY the JSON object mapping filepath to file content."
        ),
    })

    # Make API call with retry
    max_retries = 3
    raw = ""
    tokens_in = 0
    tokens_out = 0

    for attempt in range(max_retries):
        try:
            raw = ""
            async with client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=32000,
                system=[{
                    "type": "text",
                    "text": GENERATE_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": content}],
            ) as stream:
                async for chunk in stream.text_stream:
                    raw += chunk
                response = await stream.get_final_message()

            usage = getattr(response, "usage", None)
            tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
            tokens_out = getattr(usage, "output_tokens", 0) if usage else 0

            # Warn on truncation
            if getattr(response, "stop_reason", None) == "max_tokens":
                print("  [generate] WARNING: Output truncated (max_tokens reached)")

            break
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt * 3
                print(f"  [generate] Attempt {attempt+1} failed: {e}, retrying in {wait}s...")
                await asyncio.sleep(wait)
            else:
                raise

    # Parse output
    files = _extract_json_from_response(raw)

    if not files:
        # Log the raw output for debugging
        print(f"  [generate] JSON parse FAILED. Raw output length: {len(raw)} chars")
        print(f"  [generate] Raw output starts with: {raw[:200]!r}")
        print(f"  [generate] Raw output ends with: {raw[-200:]!r}")

    # Filter to only valid paths
    valid_prefixes = ("app/", "components/", "lib/")
    files = {
        fp: content for fp, content in files.items()
        if any(fp.startswith(p) for p in valid_prefixes)
    }

    # ── Post-generation safety checks ──────────────────────────────────────
    # 1. Ensure layout.jsx imports globals.css (without it → zero CSS)
    layout_key = next((k for k in files if k in ("app/layout.jsx", "app/layout.tsx")), None)
    if layout_key and 'import "./globals.css"' not in files[layout_key] and "import './globals.css'" not in files[layout_key]:
        print("  [generate] WARNING: layout missing globals.css import — injecting")
        content = files[layout_key]
        # Insert after "use client" if present, otherwise at top
        if content.strip().startswith('"use client"') or content.strip().startswith("'use client'"):
            lines = content.split("\n", 1)
            files[layout_key] = lines[0] + '\nimport "./globals.css";\n' + (lines[1] if len(lines) > 1 else "")
        else:
            files[layout_key] = 'import "./globals.css";\n' + content

    # 2. Ensure globals.css starts with @import "tailwindcss" (without it → no Tailwind)
    css_key = next((k for k in files if k in ("app/globals.css",)), None)
    if css_key:
        css = files[css_key]
        if '@import "tailwindcss"' not in css and "@import 'tailwindcss'" not in css:
            if "@tailwind base" in css:
                # Claude used v3 syntax — replace with v4
                print("  [generate] WARNING: globals.css uses Tailwind v3 syntax — converting to v4")
                css = css.replace("@tailwind base;\n@tailwind components;\n@tailwind utilities;", '@import "tailwindcss";')
                css = css.replace("@tailwind base;\n@tailwind components;\n@tailwind utilities", '@import "tailwindcss";')
                files[css_key] = css
            else:
                print("  [generate] WARNING: globals.css missing @import tailwindcss — injecting")
                files[css_key] = '@import "tailwindcss";\n\n' + css

    # 3. Ensure globals.css exists at all
    if "app/globals.css" not in files:
        print("  [generate] WARNING: No globals.css generated — creating minimal")
        files["app/globals.css"] = '@import "tailwindcss";\n\n@layer base {\n  html { scroll-behavior: smooth; }\n  body { -webkit-font-smoothing: antialiased; }\n}\n'

    # 4. Sanitize JSX — fix HTML-isms that cause "Parsing ecmascript source code failed"
    for fp in list(files.keys()):
        if not fp.endswith((".jsx", ".tsx")):
            continue
        src = files[fp]
        original = src

        # class= → className=  (but not className= which is already correct)
        src = re.sub(r'\bclass=(["\'{])', r'className=\1', src)

        # for= on labels → htmlFor=  (but not htmlFor= already)
        src = re.sub(r'\bfor=(["\'{}])', r'htmlFor=\1', src)

        # HTML comments → JSX comments
        src = re.sub(r'<!--\s*(.*?)\s*-->', r'{/* \1 */}', src)

        # Void elements without self-closing slash
        src = re.sub(r'<(br|hr|img|input|meta|link|source|area|col|embed|wbr)(\s[^>]*)?\s*(?<!/)>',
                     lambda m: f'<{m.group(1)}{m.group(2) or ""} />', src)

        # HTML entities → actual characters
        src = src.replace('&nbsp;', ' ')
        src = src.replace('&mdash;', '\u2014')
        src = src.replace('&ndash;', '\u2013')
        src = src.replace('&laquo;', '\u00AB')
        src = src.replace('&raquo;', '\u00BB')
        src = src.replace('&bull;', '\u2022')
        src = src.replace('&hellip;', '\u2026')
        src = src.replace('&copy;', '\u00A9')
        src = src.replace('&reg;', '\u00AE')
        src = src.replace('&trade;', '\u2122')
        # &amp; &lt; &gt; &quot; are trickier — only replace in text contexts,
        # not inside JSX expressions.  Simple heuristic: skip them to be safe.

        if src != original:
            files[fp] = src
            fixes_applied = []
            if 'class=' in original and 'class=' not in src:
                fixes_applied.append('class→className')
            if re.search(r'\bfor=["\']', original):
                fixes_applied.append('for→htmlFor')
            if '<!--' in original:
                fixes_applied.append('HTML comments→JSX')
            if '&nbsp;' in original or '&mdash;' in original:
                fixes_applied.append('HTML entities→chars')
            print(f"  [generate] JSX sanitized {fp}: {', '.join(fixes_applied) or 'void elements'}")

    print(f"  [generate] Generated {len(files)} files: {list(files.keys())}")
    return files, tokens_in, tokens_out


# ---------------------------------------------------------------
# MAIN PIPELINE — run_clone_streaming
# ---------------------------------------------------------------

async def run_clone_streaming(url: str) -> AsyncGenerator[str, None]:
    """
    One-shot clone pipeline (React only).

    Steps:
    [A+B] Scrape + sandbox acquire (parallel)
    [C]   One-shot generation (single Claude call → all files)
    [D]   Upload to sandbox
    [E]   Check compilation + fix loop
    [F]   Done
    """
    state = {
        "preview_url": None,
        "sandbox_id": None,
        "project_root": None,
        "files": {},
        "clone_id": None,
        "output_format": "react",
    }
    start = time.time()
    tokens_total_in = 0
    tokens_total_out = 0
    gen_time = 0.0

    def _elapsed():
        return f"{time.time() - start:.1f}s"

    def _log(msg):
        print(f"  [{_elapsed()}] {msg}")

    _log(f"=== CLONE START: {url} ===")

    # Save to DB
    try:
        from app.database import save_clone
        record = await save_clone({"url": url, "status": "processing", "output_format": "react"})
        state["clone_id"] = record.get("id")
        _log(f"DB: clone_id={state['clone_id']}")
        yield sse_event("clone_created", {"clone_id": state["clone_id"]})
    except Exception as e:
        _log(f"DB skip: {e}")
        yield sse_event("warning", {"message": f"DB skip: {e}"})

    # ============================================================
    # [A + B] SCRAPE + SANDBOX in parallel
    # Emit `deployed` as soon as sandbox is ready so the user
    # sees the default Next.js page in the iframe immediately.
    # ============================================================
    yield sse_event("step", {"step": "scraping", "message": f"Scraping {url}..."})

    scrape_task = asyncio.create_task(scrape_website(url))
    sandbox_task = asyncio.create_task(create_react_boilerplate_sandbox())

    pending = {scrape_task, sandbox_task}
    scrape_data = None
    sandbox_info = None

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is sandbox_task:
                try:
                    sandbox_info = task.result()
                except Exception as e:
                    _log(f"Sandbox acquisition failed: {e}")
                    # Cancel the other task and bail out
                    for t in pending:
                        t.cancel()
                    yield sse_event("error", {"message": f"Sandbox failed: {e}"})
                    yield sse_event("done", {"preview_url": None, "error": str(e)})
                    return

                state["sandbox_id"] = sandbox_info["sandbox_id"]
                state["preview_url"] = sandbox_info["preview_url"]
                state["project_root"] = sandbox_info.get("project_root", PROJECT_PATH)

                active_sandboxes[sandbox_info["sandbox_id"]] = {
                    **sandbox_info,
                    "created_at": time.time(),
                }

                _log(f"Sandbox ready: {sandbox_info['sandbox_id'][:12]} — {sandbox_info['preview_url']}")

                yield sse_event("deployed", {
                    "preview_url": sandbox_info["preview_url"],
                    "sandbox_id": sandbox_info["sandbox_id"],
                })

            elif task is scrape_task:
                try:
                    scrape_data = task.result()
                except Exception as e:
                    _log(f"Scrape failed: {e}")
                    for t in pending:
                        t.cancel()
                    yield sse_event("error", {"message": f"Scrape failed: {e}"})
                    yield sse_event("done", {"preview_url": state.get("preview_url"), "error": str(e)})
                    return

    sections_raw = scrape_data.get("sections", [])
    images_raw = scrape_data.get("assets", {}).get("images", [])
    fonts_raw = scrape_data.get("assets", {}).get("fonts", [])
    theme_data = scrape_data.get("theme", {})
    clickables_data = scrape_data.get("clickables", {})
    screenshots_data = scrape_data.get("screenshots", {})

    _log(f"Scrape done — {len(sections_raw)} sections, {len(images_raw)} images, "
         f"{scrape_data.get('page_height', 0)}px tall")

    # Build section type summary
    section_types = [s.get("type", "content") for s in sections_raw]

    # Build color palette summary
    colors = theme_data.get("colors", {})
    color_palette = []
    if colors.get("body_bg"):
        color_palette.append(colors["body_bg"])
    if colors.get("body_text"):
        color_palette.append(colors["body_text"])
    for c in colors.get("backgrounds", [])[:4]:
        if c not in color_palette and not c.startswith("linear"):
            color_palette.append(c)
    for c in colors.get("accent_colors", [])[:3]:
        if c not in color_palette:
            color_palette.append(c)

    # Font info
    fonts_info = theme_data.get("fonts", {})
    font_families = []
    if fonts_info.get("body"):
        font_families.append(fonts_info["body"].split(",")[0].strip().strip('"').strip("'"))
    if fonts_info.get("heading") and fonts_info.get("heading") != fonts_info.get("body"):
        font_families.append(fonts_info["heading"].split(",")[0].strip().strip('"').strip("'"))

    # Screenshot count
    scroll_count = len(screenshots_data.get("scroll_chunks", []))
    has_full = bool(screenshots_data.get("full_page"))
    has_viewport = bool(screenshots_data.get("viewport"))
    total_screenshots = scroll_count + (1 if has_full else 0) + (1 if has_viewport else 0)

    yield sse_event("scrape_done", {
        "title": scrape_data.get("title", ""),
        "sections": len(sections_raw),
        "section_types": section_types,
        "images": len(images_raw),
        "image_urls": [
            (img.get("url") if isinstance(img, dict) else img)
            for img in images_raw[:8]
        ],
        "fonts": len(fonts_raw),
        "font_families": font_families,
        "colors": color_palette[:8],
        "nav_links": len(clickables_data.get("nav_links", [])),
        "cta_buttons": len(clickables_data.get("cta_buttons", [])),
        "page_height": scrape_data.get("page_height", 0),
        "screenshots": total_screenshots,
        "svgs": len(scrape_data.get("svgs", [])),
    })

    # Quality gate: no sections at all
    if not sections_raw:
        _log("No sections detected — aborting")
        yield sse_event("error", {
            "message": "No content sections detected on the page. "
                       "The site may be behind a login, use heavy JavaScript rendering, "
                       "or block automated access."
        })
        yield sse_event("done", {"preview_url": None, "error": "No sections detected"})
        return

    # Quality gate: scrape too thin
    text_len = len(scrape_data.get("text_content", ""))
    if len(sections_raw) <= 1 and len(images_raw) <= 3 and text_len < 500:
        _log(f"Scrape too thin — {len(sections_raw)} sections, {len(images_raw)} images, "
             f"{text_len} chars — aborting")
        yield sse_event("error", {
            "message": (
                f"Scrape returned too little content ({len(sections_raw)} section, "
                f"{len(images_raw)} images, {text_len} chars). "
                "The site likely blocked automated access (CAPTCHA/bot detection). "
                "Try a different URL."
            ),
        })
        yield sse_event("done", {"preview_url": None, "error": "Scrape too thin"})
        return

    # ============================================================
    # [C] ONE-SHOT GENERATION
    # ============================================================
    yield sse_event("step", {"step": "generating", "message": "Generating React clone..."})

    gen_start = time.time()
    try:
        files, tokens_in, tokens_out = await _generate_all(scrape_data)
        tokens_total_in += tokens_in
        tokens_total_out += tokens_out
    except Exception as e:
        _log(f"Generation failed: {e}")
        yield sse_event("error", {"message": f"Code generation failed: {e}"})
        yield sse_event("done", {"preview_url": None, "error": str(e)})
        return

    gen_time = round(time.time() - gen_start, 1)
    _log(f"Generation done in {gen_time}s — {len(files)} files, "
         f"{tokens_total_in}in/{tokens_total_out}out tokens")

    if not files:
        _log("No files generated — aborting")
        yield sse_event("error", {"message": "Code generation produced no files."})
        yield sse_event("done", {"preview_url": None, "error": "No files generated"})
        return

    yield sse_event("generation_complete", {
        "time": gen_time,
        "file_count": len(files),
        "files": list(files.keys()),
    })

    # Stream generated files to frontend
    for fp, content in files.items():
        yield sse_event("file", {"path": fp, "content": content, "language": _file_language(fp)})

    state["files"] = files

    # Save all files locally
    for fp, content in files.items():
        _save_file_locally(state.get("clone_id"), fp, content)

    # ============================================================
    # [D] UPLOAD TO SANDBOX
    # ============================================================
    yield sse_event("step", {"step": "deploying", "message": "Uploading files to sandbox..."})

    # Remove conflicting .tsx scaffold files before uploading .jsx
    tsx_to_remove = []
    for fp in files:
        if fp.endswith(".jsx"):
            tsx_equiv = fp.replace(".jsx", ".tsx")
            tsx_to_remove.append(tsx_equiv)

    if tsx_to_remove:
        def _remove_tsx():
            try:
                daytona = get_daytona_client()
                sb = daytona.get(sandbox_info["sandbox_id"])
                for tsx_path in tsx_to_remove:
                    full_path = f"{state['project_root']}/{tsx_path}"
                    try:
                        sb.process.exec(f"rm -f {full_path}", timeout=5)
                    except Exception:
                        pass
            except Exception as e:
                print(f"  [tsx-cleanup] Failed: {e}")
        await asyncio.to_thread(_remove_tsx)
        _log(f"Removed {len(tsx_to_remove)} conflicting .tsx scaffold files")

    upload_start = time.time()
    try:
        await upload_files_to_sandbox(
            sandbox_info["sandbox_id"],
            files,
            project_root=state["project_root"],
        )
        _log(f"Uploaded {len(files)} files in {time.time() - upload_start:.1f}s")
    except Exception as upload_err:
        _log(f"Upload failed: {upload_err}")
        yield sse_event("error", {"message": f"Failed to upload files: {upload_err}"})
        yield sse_event("done", {"preview_url": state.get("preview_url"), "error": str(upload_err)})
        return

    # Restart the dev server — tsx removal + file replacement can crash it
    _log("Restarting Next.js dev server...")
    await _restart_dev_server(sandbox_info["sandbox_id"], state["project_root"])

    # ============================================================
    # [E] CHECK COMPILATION + FIX
    # ============================================================
    _log("Waiting 20s for Next.js to compile...")
    yield sse_event("step", {"step": "checking", "message": "Waiting for compilation..."})
    await asyncio.sleep(20)

    all_errors_seen = []   # Cumulative error tracking
    prior_fixes = {}       # Track files modified in previous attempts
    fix_iterations = 0     # Count actual fix attempts applied

    max_fix_attempts = 4
    for attempt in range(max_fix_attempts):
        _log(f"Checking compilation (attempt {attempt + 1}/{max_fix_attempts})...")

        # Step 1: Check server logs
        logs = ""
        try:
            logs = await get_sandbox_logs(
                sandbox_info["sandbox_id"],
                state["project_root"],
                lines=200,
            )
        except Exception as log_err:
            _log(f"Failed to fetch logs: {log_err}")
            yield sse_event("warning", {"message": f"Sandbox connection error: {log_err}"})
            if attempt < max_fix_attempts - 1:
                _log("Retrying in 10s...")
                await asyncio.sleep(10)
                continue
            else:
                yield sse_event("warning", {"message": "Could not verify compilation"})
                break

        _save_file_locally(state.get("clone_id"),
                           f"_diagnostics/server_logs_attempt{attempt+1}.txt", logs)
        parsed = parse_nextjs_errors(logs)
        runtime = parse_runtime_errors(logs)

        # Track compilation and runtime errors separately
        compilation_errors = _filter_errors_to_generated(parsed["errors"], state["files"])
        runtime_errors_list = _filter_errors_to_generated(runtime["errors"], state["files"])

        # Combined for internal fix logic
        combined_errors = compilation_errors + runtime_errors_list
        parsed["errors"] = combined_errors
        has_errors = len(combined_errors) > 0 or parsed["has_errors"] or runtime["has_errors"]

        # Step 2: If compiled with no log errors, verify via HTTP
        http_runtime_errors = []
        if parsed["compiled"] and not has_errors:
            _log("Server logs say compiled — verifying page renders...")
            http_result = await _check_sandbox_http(sandbox_info["sandbox_id"])

            if http_result["ok"]:
                _log(f"Page verified OK (HTTP {http_result['status_code']}, "
                     f"{http_result['body_length']} bytes)")
                yield sse_event("compiled", {"message": "Compiled and verified successfully"})
                break
            else:
                _log(f"Page has runtime errors: {http_result['errors']}")
                body_snippet = http_result.get("body", "")[:500]
                for err_indicator in http_result["errors"]:
                    http_runtime_errors.append({
                        "type": "runtime_error",
                        "file": "app/page.jsx",
                        "line": None,
                        "message": f"Runtime error on page: {err_indicator}\nPage HTML snippet: {body_snippet}",
                    })
                has_errors = True
                combined_errors.extend(http_runtime_errors)

        # If nothing detected yet (not compiled or inconclusive)
        if not has_errors and not http_runtime_errors:
            if attempt < max_fix_attempts - 1:
                _log("Not compiled yet, waiting 15s...")
                await asyncio.sleep(15)
                continue
            else:
                _log("Logs inconclusive — trying HTTP check...")
                http_result = await _check_sandbox_http(sandbox_info["sandbox_id"])
                if http_result["ok"]:
                    yield sse_event("compiled", {"message": "Server responding correctly"})
                    break
                else:
                    # HTTP error on last attempt — feed it into the fix path
                    # instead of just warning and giving up
                    _log(f"HTTP check failed: {http_result['errors']}")
                    body_snippet = http_result.get("body", "")[:500]
                    for err_indicator in http_result["errors"]:
                        combined_errors.append({
                            "type": "runtime_error",
                            "file": "app/page.jsx",
                            "line": None,
                            "message": f"Runtime error on page: {err_indicator}\nPage HTML snippet: {body_snippet}",
                        })
                    has_errors = True

        # Emit separate events for compilation and runtime errors
        if compilation_errors:
            comp_report = format_nextjs_errors({**parsed, "errors": compilation_errors, "has_errors": True})
            _log(f"Compilation errors ({len(compilation_errors)}): {comp_report[:200]}")
            yield sse_event("compile_errors", {
                "attempt": attempt + 1,
                "error_count": len(compilation_errors),
                "report": comp_report[:500],
                "errors": [
                    {"file": e.get("file"), "line": e.get("line"), "message": e.get("message", "")[:200]}
                    for e in compilation_errors[:8]
                ],
            })

        all_runtime = runtime_errors_list + http_runtime_errors
        if all_runtime:
            _log(f"Runtime errors ({len(all_runtime)})")
            yield sse_event("runtime_errors", {
                "attempt": attempt + 1,
                "error_count": len(all_runtime),
                "errors": [
                    {"file": e.get("file"), "line": e.get("line"), "message": e.get("message", "")[:200]}
                    for e in all_runtime[:8]
                ],
            })

        # Fallback: has_errors flag set but no individually parsed errors
        if not compilation_errors and not all_runtime:
            error_report = format_nextjs_errors(parsed)
            _log(f"Errors detected: {error_report[:200]}")
            yield sse_event("compile_errors", {
                "attempt": attempt + 1,
                "error_count": 0,
                "report": error_report[:500],
            })

        # If we only have HTTP runtime errors (no log-parsed errors),
        # fetch fresh logs — the dev server often logs the actual stack trace
        if http_runtime_errors and not compilation_errors and not runtime_errors_list:
            try:
                fresh_logs = await get_sandbox_logs(
                    sandbox_info["sandbox_id"], state["project_root"], lines=200
                )
                fresh_parsed = parse_nextjs_errors(fresh_logs)
                fresh_runtime = parse_runtime_errors(fresh_logs)
                fresh_errors = _filter_errors_to_generated(
                    fresh_parsed["errors"] + fresh_runtime["errors"], state["files"]
                )
                if fresh_errors:
                    _log(f"Fresh logs revealed {len(fresh_errors)} additional errors")
                    combined_errors.extend(fresh_errors)
            except Exception:
                pass

        if attempt < max_fix_attempts - 1:
            # Handle missing module imports
            missing_fixed = {}
            remaining_errors = []
            for err in combined_errors:
                if err.get("type") == "module_not_found" and err.get("module"):
                    mod = err["module"]
                    comp_path = mod.lstrip("./").lstrip("../")
                    if comp_path.startswith("components/") and f"{comp_path}.jsx" not in files:
                        comp_name = comp_path.split("/")[-1]
                        _log(f"Missing component '{comp_name}' — removing from page.jsx")
                        page = files.get("app/page.jsx", "")
                        page = re.sub(
                            rf'import\s+{re.escape(comp_name)}\s+from\s+"[^"]*";\n?',
                            "", page
                        )
                        page = re.sub(
                            rf'^\s*<ErrorBoundary[^>]*><{re.escape(comp_name)}\s*/><\/ErrorBoundary>\n?',
                            "", page, flags=re.MULTILINE
                        )
                        page = re.sub(
                            rf'^\s*<{re.escape(comp_name)}\s*/>\n?',
                            "", page, flags=re.MULTILINE
                        )
                        missing_fixed["app/page.jsx"] = page
                        continue
                remaining_errors.append(err)

            if missing_fixed:
                files.update(missing_fixed)
                state["files"].update(missing_fixed)
                for fp, content in missing_fixed.items():
                    yield sse_event("file_updated", {"path": fp, "content": content})

            # Fix remaining errors with Claude
            fixed = {}
            if remaining_errors:
                prior_context = ""
                if prior_fixes:
                    prior_context = (
                        "\nPrevious fix attempts did NOT resolve these errors. "
                        "Files already modified:\n"
                    )
                    for fp in prior_fixes:
                        prior_context += f"  - {fp}\n"
                    if all_errors_seen:
                        prior_context += "Errors that persisted:\n"
                        for prev_err in all_errors_seen[-5:]:
                            prior_context += f"  - {prev_err.get('message', '')[:150]}\n"
                    prior_context += "Try a DIFFERENT approach this time.\n"

                try:
                    fixed = await fix_targeted(
                        state["files"], remaining_errors, "compilation/runtime",
                        prior_context=prior_context,
                        all_files=state["files"] if http_runtime_errors else None,
                    )
                except Exception as fix_err:
                    _log(f"Fix agent failed: {fix_err}")
                    break

            # Merge all fixes
            fixed.update(missing_fixed)
            all_errors_seen.extend(remaining_errors)
            prior_fixes.update(fixed)

            if fixed:
                fix_iterations += 1
                for fp, content in fixed.items():
                    files[fp] = content
                    state["files"][fp] = content
                    yield sse_event("file_updated", {"path": fp, "content": content})

                await _clear_sandbox_logs(sandbox_info["sandbox_id"], state["project_root"])
                await upload_files_to_sandbox(
                    sandbox_info["sandbox_id"],
                    fixed,
                    project_root=state["project_root"],
                )
                await _touch_sandbox_files(
                    sandbox_info["sandbox_id"],
                    list(fixed.keys()),
                    state["project_root"],
                )

                yield sse_event("step", {"step": "verifying", "message": f"Verifying fix (attempt {fix_iterations})..."})
                _log(f"Fixed {len(fixed)} files, waiting 15s for recompilation...")
                await asyncio.sleep(15)
            else:
                _log("No fixes applied")
                break

    # ============================================================
    # [F] DONE
    # ============================================================
    total_time = round(time.time() - start, 1)
    final_status = "success" if state.get("preview_url") else "failed"
    file_list = list(state.get("files", {}).keys())

    _log(f"=== CLONE COMPLETE ===")
    _log(f"  Status: {final_status}")
    _log(f"  Total: {total_time}s")
    _log(f"  Preview: {state.get('preview_url', 'NONE')}")
    _log(f"  Files: {len(file_list)}")
    _log(f"  Fix iterations: {fix_iterations}")
    _log(f"  Tokens: {tokens_total_in} in / {tokens_total_out} out in {gen_time}s")

    yield sse_event("done", {
        "preview_url": state.get("preview_url"),
        "sandbox_id": state.get("sandbox_id"),
        "clone_id": state.get("clone_id"),
        "files": file_list,
        "iterations": fix_iterations + 1,  # +1 for the initial generation
        "time": total_time,
    })

    # Store session for chat follow-ups
    session_key = state.get("clone_id") or state.get("sandbox_id") or ""
    if session_key:
        _chat_sessions[session_key] = {
            "files": files,
            "state": state,
            "scrape_data": scrape_data,
        }

    # Update DB
    try:
        from app.database import update_clone
        if state.get("clone_id"):
            await update_clone(state["clone_id"], {
                "status": final_status,
                "preview_url": state.get("preview_url"),
                "sandbox_id": state.get("sandbox_id"),
                "output_format": "react",
                "metadata": {
                    "files": files,
                    "output_format": "react",
                    "gen_time": gen_time,
                    "total_time": total_time,
                    "tokens_in": tokens_total_in,
                    "tokens_out": tokens_total_out,
                },
            })
            _log("DB updated")
    except Exception as e:
        _log(f"DB update failed: {e}")


# ---------------------------------------------------------------
# TARGETED FIX — shared by compilation fix path
# ---------------------------------------------------------------

async def fix_targeted(
    files: dict, errors: list, error_source: str, prior_context: str = "",
    all_files: dict | None = None,
) -> dict:
    """
    Single Claude call to fix specific errors in specific files.
    Returns only the fixed files (filepath -> content), or {} on failure.
    """
    client = _get_client()

    by_file = {}
    for e in errors:
        fp = e.get("file") or "unknown"
        by_file.setdefault(fp, []).append(e)

    parts = [
        f"Fix these {error_source} errors. Return ONLY a JSON object mapping "
        f"filepath to corrected full file content. No explanation.\n"
    ]

    if prior_context:
        parts.append(prior_context)

    has_runtime_errors = any(e.get("type") == "runtime_error" for e in errors)

    for fp, errs in by_file.items():
        parts.append(f"\n--- {fp} ---")
        for e in errs:
            line = e.get("line", 0)
            msg = e.get("message", "")
            hint = e.get("fix_hint", "")
            parts.append(f"  Line {line}: {msg}" + (f" -> {hint}" if hint else ""))

        match = None
        for key in files:
            if key == fp or key.endswith(fp) or fp.endswith(key):
                match = key
                break
        if match:
            parts.append(f"CODE:\n```\n{files[match]}\n```")

    # For runtime errors, include ALL generated files so Claude can trace the issue
    if has_runtime_errors and all_files:
        already_shown = set(by_file.keys())
        extra_files = {
            fp: content for fp, content in all_files.items()
            if fp not in already_shown and fp.endswith((".jsx", ".tsx", ".css", ".js"))
        }
        if extra_files:
            parts.append("\n--- ALL PROJECT FILES (for runtime error context) ---")
            for fp, content in extra_files.items():
                parts.append(f"\n--- {fp} ---\nCODE:\n```\n{content}\n```")

    try:
        text = ""
        async with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=12000,
            system=(
                "Fix React/JSX errors in a Next.js 15 App Router project using Tailwind CSS v4. "
                "This project uses the app/ directory (NOT pages/). "
                "NEVER create pages/_app.tsx, pages/_document.tsx, or any file under pages/. "
                "NEVER create styles/globals.css — the CSS file is at app/globals.css. "
                "Only fix files under app/, components/, and lib/. "
                "IMPORTANT: This project uses Tailwind CSS v4. "
                "app/globals.css MUST start with `@import \"tailwindcss\";` (NOT @tailwind base/components/utilities). "
                "Use `@theme { }` for custom variables and `@layer base { }` for base styles. "
                "\n"
                "COMMON CAUSE of 'Parsing ecmascript source code failed': HTML syntax in JSX. "
                "Fix ALL of these: class= → className=, for= → htmlFor=, "
                "style=\"...\" → style={{ }}, <!-- --> → {/* */}, "
                "<br>/<hr>/<img>/<input> → <br />/<hr />/<img />/<input />, "
                "&nbsp; → {' '} or actual space, &mdash; → —, &amp; → &, "
                "onclick → onClick, tabindex → tabIndex, colspan → colSpan. "
                "Scan the ENTIRE file for these patterns, not just the reported error line. "
                "\n"
                "Output ONLY JSON: {\"filepath\": \"corrected content\"}. "
                "No markdown fences around the JSON. Return complete file contents, not patches."
            ),
            messages=[{"role": "user", "content": "\n".join(parts)}],
        ) as stream:
            async for chunk in stream.text_stream:
                text += chunk
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(text)
        # Filter out files outside allowed paths
        allowed_prefixes = ("app/", "components/", "lib/")
        filtered = {
            fp: content for fp, content in result.items()
            if any(fp.startswith(p) for p in allowed_prefixes)
        }
        rejected = set(result.keys()) - set(filtered.keys())
        if rejected:
            print(f"  [fix-targeted] Rejected invalid paths: {rejected}")
        print(f"  [fix-targeted] Fixed {len(filtered)} files for {error_source} errors")
        return filtered
    except Exception as e:
        print(f"  [fix-targeted] Failed: {e}")
        return {}


# ---------------------------------------------------------------
# HOT-FIX — upload a single file edit to the sandbox
# ---------------------------------------------------------------

async def hot_fix_file(clone_id: str, filepath: str, content: str) -> dict:
    """
    Upload a single file to the sandbox and trigger hot-reload.
    Falls back to Supabase for sandbox_id if no in-memory session exists.
    Returns {"status": "updated"} or {"status": "error", "message": "..."}.
    """
    # Find sandbox info from session or DB
    session = _chat_sessions.get(clone_id)
    sandbox_id = None
    project_root = None

    if session:
        sandbox_id = session["state"].get("sandbox_id")
        project_root = session["state"].get("project_root")
        # Update in-memory session
        session["state"]["files"][filepath] = content
        session["files"][filepath] = content

    if not sandbox_id:
        try:
            from app.database import get_clone
            clone = await get_clone(clone_id)
            if clone:
                sandbox_id = clone.get("sandbox_id")
        except Exception:
            pass

    if not sandbox_id:
        return {"status": "error", "message": "No sandbox found for this clone"}

    if not project_root:
        project_root = PROJECT_PATH

    # Upload file to sandbox
    def _upload():
        daytona = get_daytona_client()
        sb = daytona.get(sandbox_id)
        full_path = f"{project_root}/{filepath}"
        dir_path = "/".join(full_path.split("/")[:-1])
        sb.process.exec(f"mkdir -p {dir_path}", timeout=5)
        sb.fs.upload_file(content.encode(), full_path)

    try:
        await asyncio.to_thread(_upload)
    except Exception as e:
        return {"status": "error", "message": f"Upload failed: {e}"}

    # Touch to trigger HMR
    await _touch_sandbox_files(sandbox_id, [filepath], project_root)

    # Sync latest files to Supabase so rebuilds use the latest version
    if session and session.get("files"):
        try:
            from app.database import sync_files_to_supabase
            await sync_files_to_supabase(clone_id, session["files"])
        except Exception as e:
            print(f"  [hot_fix_file] Supabase sync failed: {e}")

    return {"status": "updated", "filepath": filepath, "sandbox_id": sandbox_id}


# ---------------------------------------------------------------
# CHAT FOLLOW-UP
# ---------------------------------------------------------------

CHAT_SYSTEM_PROMPT = """You are a website clone assistant. The user has an existing cloned website and wants to make changes.

You have access to:
1. `update_sandbox_file` — modify a file in the running sandbox (Next.js hot-reloads)
2. `get_sandbox_logs` — check for compilation errors

Rules:
- Use className, not class
- Use "use client" for components with hooks/events
- Use Tailwind arbitrary values for colors: bg-[#hex], text-[#hex]
- This project uses Tailwind CSS v4. app/globals.css uses `@import "tailwindcss";` (NOT @tailwind directives)
- Use `@theme { }` for custom CSS variables and `@layer base { }` for base styles
- Keep changes minimal — only modify what the user asks for
- After making changes, check logs to verify no errors"""

CHAT_TOOLS = [
    {
        "name": "update_sandbox_file",
        "description": "Update a file in the sandbox. Next.js hot-reloads automatically.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
                "filepath": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["sandbox_id", "filepath", "content"],
        },
    },
    {
        "name": "get_sandbox_logs",
        "description": "Get dev server logs to check for compilation errors.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sandbox_id": {"type": "string"},
            },
            "required": ["sandbox_id"],
        },
    },
]


async def _handle_chat_tool(
    tool_name: str, tool_input: dict, project_root: str
) -> str:
    """Handle tool calls during chat follow-up."""
    if tool_name == "update_sandbox_file":
        sandbox_id = tool_input["sandbox_id"]
        filepath = tool_input["filepath"]
        content = tool_input["content"]

        def _update():
            daytona = get_daytona_client()
            sb = daytona.get(sandbox_id)
            full_path = f"{project_root}/{filepath}"
            dir_path = "/".join(full_path.split("/")[:-1])
            sb.process.exec(f"mkdir -p {dir_path}", timeout=5)
            sb.fs.upload_file(content.encode(), full_path)

        await asyncio.to_thread(_update)
        return json.dumps({
            "status": "updated",
            "filepath": filepath,
            "message": f"File {filepath} updated. Dev server will hot-reload.",
        })

    elif tool_name == "get_sandbox_logs":
        sandbox_id = tool_input["sandbox_id"]
        logs = await get_sandbox_logs(sandbox_id, project_root, lines=200)
        return json.dumps({"logs": logs[:3000]})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


async def run_chat_followup(
    clone_id: str, user_message: str
) -> AsyncGenerator[str, None]:
    """
    Handle a user follow-up message about an existing clone.
    Uses tool-use loop with update_sandbox_file + get_sandbox_logs.
    """
    session = _chat_sessions.get(clone_id)
    if not session:
        # Try to restore session from Supabase
        try:
            from app.database import get_clone
            clone = await get_clone(clone_id)
            if clone and clone.get("sandbox_id"):
                metadata = clone.get("metadata") or {}
                saved_files = metadata.get("files") or {}
                session = {
                    "files": saved_files,
                    "state": {
                        "sandbox_id": clone["sandbox_id"],
                        "preview_url": clone.get("preview_url"),
                        "project_root": PROJECT_PATH,
                        "files": saved_files,
                        "clone_id": clone_id,
                        "output_format": "react",
                    },
                    "scrape_data": {},
                }
                _chat_sessions[clone_id] = session
        except Exception:
            pass

    if not session:
        yield sse_event("error", {"message": "No active session. Try cloning again."})
        return

    state = session["state"]
    files = session["files"]
    sandbox_id = state.get("sandbox_id")
    project_root = state.get("project_root", PROJECT_PATH)

    if not sandbox_id:
        yield sse_event("error", {"message": "No sandbox for this clone."})
        return

    client = _get_client()

    file_listing = "\n".join(
        f"--- {fp} ---\n{content[:3000]}"
        for fp, content in files.items()
        if fp.endswith((".jsx", ".tsx", ".css", ".js"))
    )

    messages = [
        {
            "role": "user",
            "content": (
                f"Current files in the project:\n{file_listing[:20000]}\n\n"
                f"sandbox_id: {sandbox_id}\n\n"
                f"User request: {user_message}"
            ),
        }
    ]

    yield sse_event("user_message", {"text": user_message})

    for iteration in range(4):
        try:
            response = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=32000,
                system=CHAT_SYSTEM_PROMPT,
                messages=messages,
                tools=CHAT_TOOLS,
            )
        except Exception as e:
            yield sse_event("error", {"message": f"API error: {e}"})
            break

        for block in response.content:
            if hasattr(block, "text") and block.text:
                yield sse_event("agent_message", {"text": block.text})

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            break

        tool_results = []
        for block in tool_use_blocks:
            tool_name = block.name
            tool_input = block.input

            if tool_name == "update_sandbox_file":
                fp = tool_input.get("filepath", "")
                content = tool_input.get("content", "")
                yield sse_event("step", {"step": "fixing", "message": f"Updating {fp}..."})
                yield sse_event("file_updated", {
                    "path": fp,
                    "content": content,
                    "language": _file_language(fp),
                })
                files[fp] = content
                state["files"][fp] = content

            result = await _handle_chat_tool(tool_name, tool_input, project_root)

            try:
                parsed_result = json.loads(result)
                if "preview_url" in parsed_result:
                    yield sse_event("deployed", {"preview_url": parsed_result["preview_url"]})
            except Exception:
                pass

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": [{"type": "text", "text": result}],
            })

        messages.append({"role": "user", "content": tool_results})

    yield sse_event("done", {
        "preview_url": state.get("preview_url"),
        "files": list(files.keys()),
    })

    _chat_sessions[clone_id] = session

    # Sync latest files to Supabase so rebuilds use the latest version
    try:
        from app.database import sync_files_to_supabase
        await sync_files_to_supabase(clone_id, files)
    except Exception as e:
        print(f"  [chat_followup] Supabase sync failed: {e}")


# ---------------------------------------------------------------
# LEGACY COMPATIBILITY
# ---------------------------------------------------------------

async def run_clone_agent(url: str, output_format: str = "react") -> dict:
    """Synchronous-style wrapper for the streaming pipeline."""
    result = {
        "preview_url": None,
        "sandbox_id": None,
        "files": {},
        "iterations": 1,
        "status": "processing",
    }

    async for event_str in run_clone_streaming(url):
        try:
            if event_str.startswith("data: "):
                data = json.loads(event_str[6:].strip())
                event_type = data.get("type")

                if event_type == "deployed":
                    result["preview_url"] = data.get("preview_url")
                    result["sandbox_id"] = data.get("sandbox_id")
                elif event_type == "done":
                    result["preview_url"] = data.get("preview_url")
                    result["sandbox_id"] = data.get("sandbox_id")
                    result["status"] = "success" if data.get("preview_url") else "failed"
                elif event_type == "file":
                    result["files"][data.get("path", "")] = data.get("content", "")
                elif event_type == "file_updated":
                    result["files"][data.get("path", "")] = data.get("content", "")
                elif event_type == "error":
                    result["status"] = "failed"
        except Exception:
            pass

    if not result["preview_url"]:
        result["status"] = "failed"

    return result


async def run_clone_agent_streaming(
    url: str, output_format: str = "react"
) -> AsyncGenerator[str, None]:
    """Alias for run_clone_streaming (backwards compatibility)."""
    async for event in run_clone_streaming(url):
        yield event
