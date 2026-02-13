# Unified Implementation — Claude Code Prompt

You are building a website cloner backend. Read claude.md for project context.

This prompt consolidates everything into ONE system. Do not reference the old separate prompts (react-cloner-ultimate.md, runtime-optimizations.md, code-validation-prompt.md, parallel-generation.md, combiner-review.md, style-locked-generation.md). This document is the single source of truth.

---

## THE PIPELINE (every clone follows this exact flow)

```
USER SUBMITS URL
      │
      ├──→ [A] scrape_website(url)                    5-8s
      │         Playwright: network interception,
      │         DOM skeleton, theme extraction,
      │         section detection, screenshots
      │
      ├──→ [B] sandbox_pool.acquire()                 0s (pre-warmed)
      │         Pre-provisioned Next.js 14 sandbox
      │         with all npm packages installed
      │
      │    (A and B run in PARALLEL)
      │
      ▼
[C] extract_design_system(scrape_data)                3-4s
      │  Single Claude call → design_tokens.json
      │  Colors, typography, spacing, button styles,
      │  card styles, animation patterns, borders
      │
      ▼
[D] plan_sections(scrape_data)                        0s (no AI)
      │  Split scrape data into per-section packages
      │  Each section gets: its screenshot, its DOM slice,
      │  its text/images/links, the shared design_tokens
      │
      ▼
[E] generate_sections_parallel(sections, tokens)      8-15s
      │  N parallel Claude calls (one per section)
      │  Each call: screenshot + section data + design tokens
      │  Each returns: one complete .jsx component file
      │  All calls use SAME cached system prompt
      │
      ▼
[F] assemble_project(results, tokens)                 0s (no AI)
      │  String concatenation only:
      │  - page.jsx: imports + renders in order
      │  - layout.jsx: font links + metadata
      │  - globals.css: font imports + tailwind + base styles
      │
      ▼
[G] review_assembly(all_files)                        3-5s
      │  Single Claude call: read all files, fix:
      │  - duplicate helper functions across components
      │  - z-index conflicts
      │  - missing swiper CSS imports
      │  - inconsistent spacing between sections
      │  Returns ONLY changed files (or {} if clean)
      │
      ▼
[H] validate_files(all_files)                         0s (no AI)
      │  Regex/AST checks:
      │  - bad imports, missing "use client"
      │  - class= vs className=, style strings
      │  - truncated code, duplicate blocks
      │  - missing components, orphan files
      │
      ├── valid? → continue
      └── errors? → fix_validation_errors() (one Claude call, 3-5s)
                    re-validate, continue
      │
      ▼
[I] upload_files_to_sandbox(sandbox, files)            1-2s
      │  Upload all files to pre-warmed Daytona sandbox
      │  Next.js hot-reload picks them up
      │
      ▼
[J] check_compilation(sandbox)                         2-3s
      │  Read Next.js dev server logs
      │  Parse with nextjs_error_parser
      │
      ├── compiled? → continue
      └── errors? → fix_compilation_errors() (one Claude call, 3-5s)
                    re-upload, re-check (up to 3 attempts)
      │
      ▼
[K] DONE → stream preview_url to frontend

TOTAL: ~22-35 seconds
```

Every step streams SSE events to the frontend so the user sees real-time progress.

---

## FILE STRUCTURE TO CREATE

```
backend/app/
├── main.py                  # FastAPI app, lifespan, endpoints
├── config.py                # Settings, env vars
├── agent.py                 # run_clone_streaming() — orchestrates the full pipeline
├── scraper.py               # Playwright extraction (exists, may need updates)
├── design_extractor.py      # [NEW] Extract design tokens from scrape data
├── section_planner.py       # [NEW] Split scrape into per-section packages
├── section_generator.py     # [NEW] Generate one component per Claude call
├── project_assembler.py     # [NEW] Assemble files + combiner review
├── code_validator.py        # [NEW] Static JSX validation
├── nextjs_error_parser.py   # [NEW] Parse Next.js compiler errors
├── sandbox_template.py      # [NEW] Next.js project template files
├── sandbox_pool.py          # [NEW] Pre-warmed sandbox pool
├── sandbox.py               # Daytona SDK wrapper (exists, may need updates)
├── image_utils.py           # [NEW] Screenshot compression
├── database.py              # Supabase client (exists)
└── sse_utils.py             # [NEW] SSE event helpers
```

---

## IMPLEMENTATION (file by file)

### 1. `backend/app/sse_utils.py`

```python
import json

def sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    payload = {"type": event_type, **data}
    return f"data: {json.dumps(payload)}\n\n"
```

### 2. `backend/app/image_utils.py`

```python
from PIL import Image
import io
import base64


def optimize_screenshot(screenshot_bytes: bytes, max_width: int = 1280, quality: int = 75) -> bytes:
    img = Image.open(io.BytesIO(screenshot_bytes))
    w, h = img.size
    if w > max_width:
        ratio = max_width / w
        img = img.resize((max_width, int(h * ratio)), Image.LANCZOS)
    if img.mode == 'RGBA':
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality, optimize=True)
    return buf.getvalue()


def screenshot_to_b64(screenshot_bytes: bytes, compress: bool = True) -> tuple[str, str]:
    if compress:
        optimized = optimize_screenshot(screenshot_bytes)
        return base64.b64encode(optimized).decode(), "image/jpeg"
    return base64.b64encode(screenshot_bytes).decode(), "image/png"
```

### 3. `backend/app/sandbox_template.py`

Contains the full Next.js 14 project template. All packages pre-installed. See the PACKAGE_JSON, TAILWIND_CONFIG, POSTCSS_CONFIG, NEXT_CONFIG, GLOBALS_CSS, ROOT_LAYOUT, PAGE_TEMPLATE constants and the TEMPLATE_FILES dict.

The provision_react_sandbox() function:
1. Creates a Daytona sandbox with language="javascript"
2. Uploads all template files
3. Runs `npm install --legacy-peer-deps`
4. Starts the dev server in a background session
5. Returns { sandbox_id, preview_url }

Pre-installed packages (Claude writes components using these, never installs anything):
- react, react-dom, next 14.2.21
- framer-motion, gsap
- swiper, embla-carousel-react
- @headlessui/react, @radix-ui/react-* (accordion, dialog, dropdown-menu, tabs, tooltip, popover)
- lucide-react, react-icons, @heroicons/react
- react-intersection-observer, react-scroll, react-countup, react-type-animation
- clsx, tailwind-merge, class-variance-authority
- tailwindcss, postcss, autoprefixer

### 4. `backend/app/sandbox_pool.py`

Pre-warms Daytona sandboxes on server startup so npm install never blocks a clone request.

```python
import asyncio
from collections import deque

class SandboxPool:
    def __init__(self, pool_size=2):
        self.pool_size = pool_size
        self.available: deque = deque()
        self.lock = asyncio.Lock()
        self._provisioning = False

    async def initialize(self):
        print(f"Pre-warming {self.pool_size} sandboxes...")
        tasks = [self._provision_one() for _ in range(self.pool_size)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, dict) and "sandbox_id" in r:
                self.available.append(r)
        print(f"Pool ready: {len(self.available)} sandboxes")

    async def acquire(self) -> dict:
        async with self.lock:
            if self.available:
                sandbox = self.available.popleft()
                asyncio.create_task(self._replenish())
                return sandbox
        return await self._provision_one()

    async def _replenish(self):
        async with self.lock:
            if len(self.available) >= self.pool_size or self._provisioning:
                return
            self._provisioning = True
        try:
            sandbox = await self._provision_one()
            async with self.lock:
                if len(self.available) < self.pool_size:
                    self.available.append(sandbox)
        except Exception as e:
            print(f"Replenish failed: {e}")
        finally:
            async with self.lock:
                self._provisioning = False

    async def _provision_one(self):
        from app.sandbox_template import provision_react_sandbox
        return await provision_react_sandbox()

sandbox_pool = SandboxPool(pool_size=2)
```

### 5. `backend/app/design_extractor.py`

Single Claude call that reads the full scrape data (viewport + full-page screenshots, CSS theme, DOM skeleton, button styles, backgrounds, section types) and outputs a structured design_tokens.json.

The design tokens contain:
- **colors**: primary, secondary, backgrounds (page, alt, dark, card), text (heading, body, muted, on_dark, link), border, divider
- **typography**: font families (heading, body, mono), google_font_urls, scale (hero_heading, section_heading, card_heading, body_large, body, body_small, label, nav_link — each with size, weight, line_height, letter_spacing)
- **spacing**: section_padding_y, section_padding_y_mobile, container_max_width, container_padding_x, card_gap, heading_to_content
- **components**: button_primary, button_secondary, card, badge, input, nav — each with bg, text, padding, border_radius, hover, transition
- **animation**: scroll_entrance (initial, animate, duration, ease, stagger), hover_lift, transition_default
- **borders**: radius_sm/md/lg/xl/full, default_color
- **layout**: grid_columns per section type, hero_layout, responsive breakpoints

The system prompt tells Claude to use ONLY exact hex values from the CSS data. Never approximate. Never invent.

Include a _fallback_design_tokens() function that builds minimal tokens from raw scrape data if the Claude call fails.

### 6. `backend/app/section_planner.py`

Pure Python, no AI. Takes the full scrape result and produces:

```python
{
    "shared_context": { "url", "title", "theme", "google_font_urls", "font_families" },
    "sections": [
        {
            "id": "section-0",
            "component_name": "Navbar",
            "type": "navbar",
            "order": 0,
            "data": {
                "dom_skeleton_slice": "...",    # just this section's portion
                "screenshot_b64": "...",         # just this section's screenshot
                "headings": [...],
                "paragraphs": [...],
                "images": [...],                 # just this section's images
                "links": [...],
                "buttons": [...],
                "text_content": "...",
                "nav_links": [...],              # type-specific enrichment
                "logo_svg": "...",               # type-specific
                "background": {...},             # if applicable
            }
        },
        ...
    ]
}
```

Section type detection maps to component names: navbar→Navbar, hero→Hero, features→Features, pricing→Pricing, testimonials→Testimonials, faq→FAQ, cta→CTA, footer→Footer, stats→Stats, content→Section{N}.

Each section gets ONLY its own data (its portion of DOM skeleton, its screenshot, its images, its links). This keeps parallel calls focused and small.

### 7. `backend/app/section_generator.py`

Generates ONE React component per Claude call. Every call receives:
1. The SECTION_SYSTEM_PROMPT (cached via cache_control)
2. The section's screenshot (as image content block)
3. The section's data (headings, text, images, links, buttons, DOM skeleton slice)
4. The design_tokens JSON (the style contract)
5. Section-type-specific instructions (navbar gets mobile menu rules, hero gets animation rules, etc.)

The system prompt enforces design token usage:
- Colors: use bg-[{tokens.colors.primary}], never named Tailwind colors
- Typography: use text-[{tokens.typography.scale.hero_heading.size}], never text-6xl
- Spacing: use py-[{tokens.spacing.section_padding_y}], use max-w-[{tokens.spacing.container_max_width}]
- Buttons: use exact bg/text/radius/padding from tokens.components.button_primary
- Cards: use exact bg/border/radius/shadow/hover from tokens.components.card
- Animations: use exact initial/animate/duration/ease from tokens.animation.scroll_entrance
- All images use real URLs, all links use real hrefs, all text is verbatim

Output: raw JSX file content (no markdown fences). Cleaned with _strip_code_fences().

Returns:
```python
{
    "component_name": "Hero",
    "filepath": "components/Hero.jsx",
    "content": "\"use client\";\n...",
    "success": True/False,
    "error": None or str,
    "tokens_in": int,
    "tokens_out": int,
}
```

### 8. `backend/app/project_assembler.py`

Two functions:

**assemble_project(results, shared, sections, design_tokens)** — pure string concatenation, no AI:
- Collects successful component files
- Sorts by section order
- Generates page.jsx: imports + renders in order
- Generates layout.jsx: Google Font links + metadata + body font className (from design_tokens)
- Generates globals.css: @import fonts + @tailwind + base styles using design_tokens colors/typography
- Returns complete file map

**review_and_fix_assembly(files)** — single Claude call:
- Reads ALL assembled files
- Checks for: duplicate function declarations, import conflicts, z-index issues, missing swiper CSS, spacing gaps between sections, globals.css duplication, page.jsx ordering
- Does NOT rewrite components, change colors, add content, or remove sections
- Returns ONLY changed files (or {} if clean)
- Prompt is cached via cache_control
- Takes 3-5 seconds

### 9. `backend/app/code_validator.py`

Pure Python, no AI. Validates all generated JSX files.

**validate_files(files)** runs these checks:

| Check | What It Catches |
|-------|----------------|
| missing_use_client | Components/page.jsx missing "use client" |
| bad_import | Import from package not in VALID_PACKAGES set |
| unclosed_void_element | <img>, <br>, <input> not self-closed |
| class_not_classname | Using class= instead of className= |
| for_not_htmlfor | Using for= instead of htmlFor= on labels |
| style_string | style="..." instead of style={{}} |
| html_comment | <!-- --> instead of {/* */} |
| truncation_comment | "// ...", "// rest of", "// more items", "// etc" |
| duplicate_block | 4+ consecutive identical lines repeated |
| missing_default_export | No export default in component file |
| empty_component | Component returns null or empty element |
| missing_key_prop | .map() without key (warning) |
| missing_component_file | page.jsx imports component that doesn't exist |
| orphan_component | Component file exists but not imported (warning) |
| missing_layout | app/layout.jsx not generated (warning) |
| missing_globals | app/globals.css not generated (warning) |

VALID_PACKAGES is a set of all packages installed in the sandbox (react, framer-motion, swiper, etc.) plus their subpaths.

Returns: { valid: bool, errors: [...], warnings: [...], stats: {...} }

**format_error_report(validation)** — formats errors into a human-readable string with file, line, type, message, and fix_hint for each error.

### 10. `backend/app/nextjs_error_parser.py`

Parses Next.js dev server log output for compilation errors.

**parse_nextjs_errors(log_output)** extracts:
- Build errors (file + error message)
- Module not found (which module, which file)
- Syntax errors with line numbers
- Hydration mismatches
- Bad default exports
- Generic "Failed to compile"

Returns: { has_errors: bool, compiled: bool, errors: [...] }

**format_nextjs_errors(parsed)** — formats for Claude to fix.

### 11. `backend/app/agent.py`

The main orchestrator. Contains:

**SECTION_SYSTEM_PROMPT** — the per-section generation prompt (cached, used by all parallel calls)
**COMBINER_PROMPT** — the review pass prompt (cached)

**run_clone_streaming(url, output_format)** — async generator that yields SSE events:

```python
import asyncio
import json
import time
from typing import AsyncGenerator

import anthropic

from app.sse_utils import sse_event
from app.scraper import scrape_website
from app.design_extractor import extract_design_system
from app.section_planner import plan_sections
from app.section_generator import generate_section
from app.project_assembler import assemble_project, review_and_fix_assembly
from app.code_validator import validate_files, format_error_report
from app.nextjs_error_parser import parse_nextjs_errors, format_nextjs_errors
from app.sandbox_pool import sandbox_pool
from app.image_utils import screenshot_to_b64

client = anthropic.AsyncAnthropic()

# Store for chat follow-ups
_sessions = {}


async def run_clone_streaming(url: str, output_format: str = "react") -> AsyncGenerator[str, None]:
    
    state = {"preview_url": None, "sandbox_id": None, "files": {}, "clone_id": None}
    start = time.time()
    
    # Save to DB
    try:
        from app.database import save_clone
        record = await save_clone({"url": url, "status": "processing"})
        state["clone_id"] = record["id"]
        yield sse_event("clone_created", {"clone_id": record["id"]})
    except Exception as e:
        yield sse_event("warning", {"message": f"DB skip: {e}"})
    
    # ============================================================
    # [A + B] SCRAPE + SANDBOX in parallel
    # ============================================================
    yield sse_event("step", {"step": "scraping", "message": f"Scraping {url}...", "icon": "🌐"})
    
    scrape_task = asyncio.create_task(scrape_website(url))
    sandbox_task = asyncio.create_task(sandbox_pool.acquire())
    
    scrape_data = await scrape_task
    
    yield sse_event("scrape_done", {
        "title": scrape_data.get("title", ""),
        "sections": len(scrape_data.get("sections", [])),
        "images": len(scrape_data.get("assets", {}).get("images", [])),
        "page_height": scrape_data.get("page_height", 0)
    })
    
    # ============================================================
    # [C] EXTRACT DESIGN SYSTEM
    # ============================================================
    yield sse_event("step", {"step": "design", "message": "Extracting design system...", "icon": "🎨"})
    
    design_tokens = await extract_design_system(scrape_data)
    
    yield sse_event("design_extracted", {
        "fallback": design_tokens.get("_fallback", False),
        "primary_color": design_tokens.get("colors", {}).get("primary", ""),
        "body_font": design_tokens.get("typography", {}).get("fonts", {}).get("body", ""),
    })
    
    # ============================================================
    # [D] PLAN SECTIONS
    # ============================================================
    plan = plan_sections(scrape_data)
    sections = plan["sections"]
    shared = plan["shared_context"]
    
    yield sse_event("planned", {
        "section_count": len(sections),
        "sections": [{"name": s["component_name"], "type": s["type"]} for s in sections]
    })
    
    # ============================================================
    # [E] PARALLEL GENERATION
    # ============================================================
    yield sse_event("step", {"step": "generating", "message": f"Generating {len(sections)} components...", "icon": "⚡"})
    
    gen_start = time.time()
    
    tasks = [
        asyncio.create_task(generate_section(section, shared, design_tokens))
        for section in sections
    ]
    
    results = []
    pending = dict(zip(tasks, sections))
    
    while pending:
        done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED, timeout=60)
        for task in done:
            section = pending.pop(task)
            try:
                result = task.result()
                results.append(result)
                yield sse_event("section_done", {
                    "section": result["component_name"],
                    "success": result["success"],
                    "remaining": len(pending),
                })
                if result["success"]:
                    yield sse_event("file", {
                        "path": result["filepath"],
                        "content": result["content"],
                        "language": "jsx"
                    })
            except Exception as e:
                results.append({
                    "component_name": section["component_name"],
                    "filepath": f"components/{section['component_name']}.jsx",
                    "content": "", "success": False, "error": str(e),
                })
                yield sse_event("section_failed", {"section": section["component_name"], "error": str(e)})
        if not done:
            for t in pending:
                t.cancel()
            break
    
    gen_time = round(time.time() - gen_start, 1)
    yield sse_event("generation_complete", {
        "time": gen_time,
        "successful": sum(1 for r in results if r["success"]),
        "failed": sum(1 for r in results if not r["success"]),
    })
    
    # ============================================================
    # [F] ASSEMBLE
    # ============================================================
    yield sse_event("step", {"step": "assembling", "message": "Assembling project...", "icon": "🔗"})
    
    files = assemble_project(results, shared, sections, design_tokens)
    
    # Stream the assembled infrastructure files
    for fp in ["app/page.jsx", "app/layout.jsx", "app/globals.css"]:
        if fp in files:
            yield sse_event("file", {"path": fp, "content": files[fp], "language": "jsx" if fp.endswith(".jsx") else "css"})
    
    # ============================================================
    # [G] COMBINER REVIEW
    # ============================================================
    yield sse_event("step", {"step": "reviewing", "message": "Checking compatibility...", "icon": "🔍"})
    
    changes = await review_and_fix_assembly(files)
    if changes:
        for fp, content in changes.items():
            files[fp] = content
            yield sse_event("file_updated", {"path": fp, "content": content})
        yield sse_event("review_fixed", {"files_changed": list(changes.keys())})
    else:
        yield sse_event("review_clean", {"message": "No cross-component issues"})
    
    # ============================================================
    # [H] VALIDATE
    # ============================================================
    yield sse_event("step", {"step": "validating", "message": "Validating code...", "icon": "✅"})
    
    validation = validate_files(files)
    
    if not validation["valid"]:
        yield sse_event("validation_failed", {
            "error_count": len(validation["errors"]),
            "report": format_error_report(validation)[:500]
        })
        
        # One Claude call to fix validation errors
        fixed = await fix_targeted(files, validation["errors"], "validation")
        if fixed:
            for fp, content in fixed.items():
                files[fp] = content
                yield sse_event("file_updated", {"path": fp, "content": content})
            
            # Re-validate
            validation = validate_files(files)
            if validation["valid"]:
                yield sse_event("validation_passed", {"message": "Fixed"})
    else:
        yield sse_event("validation_passed", {"message": "All checks passed"})
    
    state["files"] = files
    
    # ============================================================
    # [I] DEPLOY
    # ============================================================
    yield sse_event("step", {"step": "deploying", "message": "Deploying...", "icon": "🚀"})
    
    sandbox_info = await sandbox_task  # Already done (was running in parallel since step A)
    state["sandbox_id"] = sandbox_info["sandbox_id"]
    state["preview_url"] = sandbox_info["preview_url"]
    
    await upload_files(sandbox_info["sandbox_id"], files)
    
    yield sse_event("deployed", {
        "preview_url": sandbox_info["preview_url"],
        "sandbox_id": sandbox_info["sandbox_id"]
    })
    
    # ============================================================
    # [J] CHECK COMPILATION
    # ============================================================
    await asyncio.sleep(4)  # Let Next.js compile
    yield sse_event("step", {"step": "checking", "message": "Checking compilation...", "icon": "📋"})
    
    for attempt in range(3):
        logs = await get_sandbox_logs(sandbox_info["sandbox_id"])
        parsed = parse_nextjs_errors(logs)
        
        if not parsed["has_errors"]:
            yield sse_event("compiled", {"message": "Compiled successfully"})
            break
        
        yield sse_event("compile_errors", {
            "attempt": attempt + 1,
            "error_count": len(parsed["errors"]),
            "report": format_nextjs_errors(parsed)[:500]
        })
        
        if attempt < 2:
            fixed = await fix_targeted(files, parsed["errors"], "compilation")
            if fixed:
                for fp, content in fixed.items():
                    files[fp] = content
                    state["files"][fp] = content
                    yield sse_event("file_updated", {"path": fp, "content": content})
                await upload_files(sandbox_info["sandbox_id"], fixed)
                await asyncio.sleep(3)
            else:
                break
    
    # ============================================================
    # [K] DONE
    # ============================================================
    total_time = round(time.time() - start, 1)
    
    yield sse_event("done", {
        "preview_url": state.get("preview_url"),
        "sandbox_id": state.get("sandbox_id"),
        "clone_id": state.get("clone_id"),
        "files": list(files.keys()),
        "time": total_time,
    })
    
    # Store session for chat follow-ups
    _sessions[state.get("clone_id", "")] = {
        "files": files,
        "state": state,
        "scrape_data": scrape_data,
        "design_tokens": design_tokens,
    }
    
    # Update DB
    try:
        from app.database import update_clone
        if state.get("clone_id"):
            await update_clone(state["clone_id"], {
                "status": "success" if state.get("preview_url") else "failed",
                "preview_url": state.get("preview_url"),
                "sandbox_id": state.get("sandbox_id"),
            })
    except:
        pass


# ---------------------------------------------------------------
# TARGETED FIX (shared by validation + compilation fix paths)
# ---------------------------------------------------------------

async def fix_targeted(files: dict, errors: list, error_source: str) -> dict:
    """
    Single Claude call to fix specific errors in specific files.
    Returns only the fixed files.
    """
    # Group errors by file
    by_file = {}
    for e in errors:
        fp = e.get("file", "unknown")
        if fp not in by_file:
            by_file[fp] = []
        by_file[fp].append(e)
    
    parts = [f"Fix these {error_source} errors. Return ONLY a JSON object mapping filepath to corrected file content. No explanation.\n"]
    
    for fp, errs in by_file.items():
        parts.append(f"\n--- {fp} ---")
        for e in errs:
            line = e.get("line", 0)
            msg = e.get("message", "")
            hint = e.get("fix_hint", "")
            parts.append(f"  Line {line}: {msg}" + (f" → {hint}" if hint else ""))
        
        # Find matching file content
        match = None
        for key in files:
            if key == fp or key.endswith(fp) or fp.endswith(key):
                match = key
                break
        if match:
            parts.append(f"CODE:\n```\n{files[match]}\n```")
    
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=12000,
            system="Fix React/JSX errors. Output ONLY JSON: {\"filepath\": \"corrected content\"}. No markdown fences around the JSON.",
            messages=[{"role": "user", "content": '\n'.join(parts)}]
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        return json.loads(text)
    except:
        return {}


# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------

async def upload_files(sandbox_id: str, files: dict):
    """Upload files to Daytona sandbox."""
    import os
    from daytona import Daytona, DaytonaConfig
    
    def _do():
        d = Daytona(DaytonaConfig(api_key=os.getenv("DAYTONA_API_KEY"), target="us"))
        sb = d.get(sandbox_id)
        for fp, content in files.items():
            full = f"/home/daytona/clone-app/{fp}"
            dir_path = '/'.join(full.split('/')[:-1])
            sb.process.exec(f"mkdir -p {dir_path}")
            sb.fs.upload_file(content.encode(), full)
    
    await asyncio.to_thread(_do)


async def get_sandbox_logs(sandbox_id: str) -> str:
    """Get Next.js dev server output."""
    import os
    from daytona import Daytona, DaytonaConfig
    
    def _do():
        d = Daytona(DaytonaConfig(api_key=os.getenv("DAYTONA_API_KEY"), target="us"))
        sb = d.get(sandbox_id)
        r = sb.process.exec("cat /tmp/next-output.log 2>/dev/null | tail -100", timeout=10)
        return r.result or ""
    
    return await asyncio.to_thread(_do)
```

### 12. `backend/app/main.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from app.sandbox_pool import sandbox_pool
from app.agent import run_clone_streaming

@asynccontextmanager
async def lifespan(app: FastAPI):
    await sandbox_pool.initialize()
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/clone/stream")
async def clone_stream(request: Request):
    body = await request.json()
    url = body["url"]
    output_format = body.get("output_format", "react")
    
    return StreamingResponse(
        run_clone_streaming(url, output_format),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
```

Add the other endpoints (chat follow-up, file read/write, etc.) based on what exists in the current codebase.

---

## ENVIRONMENT VARIABLES

```bash
ANTHROPIC_API_KEY=sk-ant-...      # Direct Anthropic SDK (not OpenRouter)
DAYTONA_API_KEY=...
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJ...
```

## DEPENDENCIES

```
# requirements.txt
anthropic>=0.42.0
fastapi>=0.115.0
uvicorn>=0.32.0
playwright>=1.49.0
Pillow>=11.0.0
python-dotenv>=1.0.0
daytona-sdk>=0.10.0
supabase>=2.0.0
```

Run after creating: `pip install -r requirements.txt --break-system-packages && playwright install chromium`

---

## IMPLEMENTATION ORDER

1. Create sse_utils.py, image_utils.py (standalone, no deps)
2. Create sandbox_template.py (standalone)
3. Create sandbox_pool.py (depends on sandbox_template)
4. Create code_validator.py (standalone)
5. Create nextjs_error_parser.py (standalone)
6. Create design_extractor.py (depends on anthropic)
7. Create section_planner.py (standalone)
8. Create section_generator.py (depends on anthropic, design_extractor)
9. Create project_assembler.py (depends on anthropic for review)
10. Create agent.py (depends on everything above)
11. Update main.py (depends on agent, sandbox_pool)
12. Test: `curl -N -X POST http://localhost:8000/clone/stream -H "Content-Type: application/json" -d '{"url":"https://stripe.com","output_format":"react"}'`

---

## HOW THE STYLE STAYS CONSISTENT

The design_tokens are the contract. Here's the flow:

1. **extract_design_system** reads the FULL page — all screenshots, all CSS, all sections — and produces ONE design_tokens.json with exact hex colors, exact px sizes, exact spacing values, exact button/card/animation specs.

2. **Every parallel section call** receives the SAME design_tokens. The section prompt says: "Use bg-[{tokens.colors.primary}], not your own blue. Use text-[{tokens.typography.scale.hero_heading.size}], not your guess. Use py-[{tokens.spacing.section_padding_y}], not whatever feels right."

3. **assemble_project** uses design_tokens for globals.css (base body styles, colors) and layout.jsx (font links). These are the files that affect ALL components.

4. **review_and_fix_assembly** catches any remaining inconsistencies — if one component used a different spacing or animation duration than the tokens specified.

Result: 6 components generated by 6 separate API calls, all using identical spacing, colors, typography, animations, button styles, card styles, and border radii. No visual fragmentation.
