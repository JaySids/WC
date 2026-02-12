"""
Handlers for each MCP tool. These execute the actual logic
when Claude calls a tool.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import time as _time

from app.scraper import scrape_website
from app.sandbox import deploy_html_to_sandbox, create_react_boilerplate_sandbox, get_daytona_client
from app.image_utils import screenshot_to_b64

# Store active sandbox state
active_sandboxes = {}

# Scrape cache with TTL
_scrape_cache = {}
_SCRAPE_CACHE_TTL = 300  # 5 minutes


async def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """
    Route a tool call to the appropriate handler.
    Returns a string result that gets sent back to Claude.
    """
    handlers = {
        "scrape_url": handle_scrape_url,
        "generate_and_deploy_html": handle_deploy_html,
        "generate_and_deploy_react": handle_deploy_react,
        "screenshot_preview": handle_screenshot_preview,
        "get_sandbox_logs": handle_get_logs,
        "update_sandbox_file": handle_update_file,
        "create_react_sandbox": handle_create_react_sandbox,
        "install_package": handle_install_package,
    }

    handler = handlers.get(tool_name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    try:
        result = await handler(tool_input)
        return result
    except Exception as e:
        return json.dumps({"error": str(e)})


def _slugify(text: str) -> str:
    """Turn heading text into a short slug for asset_map keys."""
    if not text:
        return ""
    slug = re.sub(r'[^a-z0-9]+', '_', text.lower().strip())
    return slug[:30].strip('_')


def _image_key(section_type: str, img: dict, counters: dict) -> str:
    """Generate a descriptive asset_map key for an image element."""
    role = img.get("role", "content")

    if role == "logo":
        return f"{section_type}.logo"
    if role == "hero":
        return f"{section_type}.main_image"
    if role == "avatar":
        n = counters.get(f"{section_type}.avatar", 0)
        counters[f"{section_type}.avatar"] = n + 1
        return f"{section_type}.avatar_{n}"
    if role == "company-logo":
        n = counters.get(f"{section_type}.company_logo", 0)
        counters[f"{section_type}.company_logo"] = n + 1
        return f"{section_type}.company_logo_{n}"
    if role == "icon":
        gi = img.get("group_index")
        if gi is not None:
            return f"{section_type}.card_{gi}.icon"
        heading = _slugify(img.get("near_heading", ""))
        if heading:
            return f"{section_type}.{heading}.icon"
        n = counters.get(f"{section_type}.icon", 0)
        counters[f"{section_type}.icon"] = n + 1
        return f"{section_type}.icon_{n}"

    # content / screenshot — try group-based key first
    gi = img.get("group_index")
    if gi is not None:
        return f"{section_type}.card_{gi}.image"

    heading = _slugify(img.get("near_heading", ""))
    if heading:
        n = counters.get(f"{section_type}.{heading}.img", 0)
        counters[f"{section_type}.{heading}.img"] = n + 1
        return f"{section_type}.{heading}.image_{n}" if n > 0 else f"{section_type}.{heading}.image"

    n = counters.get(f"{section_type}.image", 0)
    counters[f"{section_type}.image"] = n + 1
    return f"{section_type}.image_{n}"


def _svg_key(section_type: str, svg: dict, counters: dict) -> str:
    """Generate a descriptive asset_map key for an SVG element."""
    role = svg.get("role", "decorative")

    if role == "logo":
        return f"{section_type}.logo_svg"
    if role == "icon":
        gi = svg.get("group_index")
        if gi is not None:
            return f"{section_type}.card_{gi}.icon_svg"
        heading = _slugify(svg.get("near_heading", ""))
        if heading:
            return f"{section_type}.{heading}.icon_svg"
        n = counters.get(f"{section_type}.icon_svg", 0)
        counters[f"{section_type}.icon_svg"] = n + 1
        return f"{section_type}.icon_svg_{n}"

    n = counters.get(f"{section_type}.svg", 0)
    counters[f"{section_type}.svg"] = n + 1
    return f"{section_type}.svg_{n}"


def _build_from_elements(section_type: str, elements: list, counters: dict, asset_map: dict, bg_url: str | None):
    """Build asset_map entries from the ordered element stream."""
    if bg_url:
        key = f"{section_type}.background"
        if key not in asset_map:
            asset_map[key] = {"type": "bg-image", "url": bg_url}

    for elem in elements:
        etype = elem.get("type")
        if etype == "image":
            key = _image_key(section_type, elem, counters)
            if key not in asset_map:
                asset_map[key] = {
                    "type": "img",
                    "url": elem.get("url", ""),
                    "role": elem.get("role", "content"),
                }
        elif etype == "svg":
            key = _svg_key(section_type, elem, counters)
            if key not in asset_map:
                markup = elem.get("markup", "")
                asset_map[key] = {
                    "type": "svg",
                    "markup": markup[:800],
                    "role": elem.get("role", "decorative"),
                }


def _build_from_flat_lists(section_type: str, images: list, svgs: list, counters: dict, asset_map: dict, bg_url: str | None):
    """Fallback: build asset_map entries from flat images/svgs lists."""
    if bg_url:
        key = f"{section_type}.background"
        if key not in asset_map:
            asset_map[key] = {"type": "bg-image", "url": bg_url}

    for img in images:
        key = _image_key(section_type, img, counters)
        if key not in asset_map:
            asset_map[key] = {
                "type": "img",
                "url": img.get("url", ""),
                "role": img.get("role", "content"),
            }

    for svg_item in svgs:
        n = counters.get(f"{section_type}.svg", 0)
        counters[f"{section_type}.svg"] = n + 1
        key = f"{section_type}.svg_{n}"
        if key not in asset_map:
            markup = svg_item if isinstance(svg_item, str) else svg_item.get("markup", "")
            asset_map[key] = {
                "type": "svg",
                "markup": markup[:800],
                "role": "decorative",
            }


def build_asset_map(sections: list) -> dict:
    """
    Walk each section's elements stream and build a flat dict with descriptive keys
    mapping to exact asset URLs/markup. Claude uses these keys to deterministically
    place every image and SVG in the clone.
    """
    asset_map = {}
    counters = {}
    type_counts = {}  # Track duplicate section types

    for sec in sections:
        raw_type = sec.get("type", "section")
        # Handle duplicate section types: features, features_1, features_2, ...
        if raw_type in type_counts:
            type_counts[raw_type] += 1
            section_type = f"{raw_type}_{type_counts[raw_type]}"
        else:
            type_counts[raw_type] = 0
            section_type = raw_type

        bg_url = sec.get("background_image_url")
        elements = sec.get("elements", [])

        if elements:
            _build_from_elements(section_type, elements, counters, asset_map, bg_url)
        else:
            # Fallback to flat lists
            images = sec.get("images", [])
            svgs = sec.get("svgs", [])
            _build_from_flat_lists(section_type, images, svgs, counters, asset_map, bg_url)

    return asset_map


def _build_label_sheet(data: dict) -> str:
    """Build a right-sized label sheet JSON from raw scrape data.
    Reused for both fresh scrapes and cache hits."""

    # Build asset_map from structured sections
    asset_map = build_asset_map(data.get("sections", []))

    # Per-section labels — no DOM structure, just the key values
    section_labels = []
    for sec in data.get("sections", [])[:20]:
        label = {
            "index": sec["index"],
            "type": sec["type"],
            "background_color": sec.get("background_color"),
            "gradient": sec.get("gradient"),
            "background_image": sec.get("background_image"),
            "headings": [
                {"text": h["text"], "color": h.get("color"), "font_size": h.get("font_size")}
                for h in sec["headings"]
            ],
            "images": [
                {"url": img["url"], "role": img.get("role", "content"), "alt": img.get("alt", "")}
                for img in sec["images"][:10]
            ],
            "buttons": [
                {
                    "text": b.get("text", ""),
                    "bg": b.get("bg"),
                    "color": b.get("color"),
                    "border_radius": b.get("border_radius"),
                    "href": b.get("href", "#"),
                }
                for b in sec["buttons"][:5]
            ],
            "svgs": [s["markup"][:500] for s in sec.get("svgs", [])[:5]],
        }
        section_labels.append(label)

    # Animation summary
    raw_anims = data.get("animations", {})
    animations = {
        "libraries_detected": raw_anims.get("libraries_detected", []),
        "scroll_triggers": raw_anims.get("scroll_triggers", [])[:8],
        "scroll_animations": raw_anims.get("scroll_animations", [])[:20],
        "keyframes": [
            {"name": kf.get("name", ""), "css": kf.get("css", "")[:300]}
            for kf in raw_anims.get("keyframes", [])[:5]
        ],
    }

    ui_patterns = data.get("ui_patterns", [])
    button_behaviors = [
        {"text": b.get("text", ""), "behavior": b.get("behavior"), "controls": b.get("controls")}
        for b in data.get("button_behaviors", [])[:20]
    ]
    react_info = data.get("react_info", {})

    # DOM skeleton — truncated to 10000 chars
    dom_skeleton = data.get("dom_skeleton", "")
    if len(dom_skeleton) > 10000:
        dom_skeleton = dom_skeleton[:10000] + "\n... (truncated)"

    # Background images and gradients — top 15
    backgrounds = data.get("backgrounds", [])[:15]

    label_sheet = {
        "url": data["url"],
        "title": data["title"],
        "page_height": data["page_height"],
        "num_screenshots": len(data.get("screenshots", {}).get("scroll_chunks", [])),
        "theme": data["theme"],
        "google_font_urls": data["theme"]["fonts"].get("google_font_urls", []),
        "all_images": [
            {"url": img["url"], "alt": img.get("alt", "")}
            for img in data["assets"]["images"][:30]
        ],
        "dom_skeleton": dom_skeleton,
        "backgrounds": backgrounds,
        "sections": section_labels,
        "animations": animations,
        "ui_patterns": ui_patterns,
        "button_behaviors": button_behaviors,
        "framework_info": react_info,
        "nav_links": [
            {"text": l.get("text", ""), "href": l.get("href", "#")}
            for l in data["clickables"]["nav_links"][:15]
        ],
        "footer_links": [
            {"text": l.get("text", ""), "href": l.get("href", "#")}
            for l in data["clickables"]["footer_links"][:15]
        ],
        "asset_map": asset_map,
    }

    return json.dumps(label_sheet, indent=2)


async def handle_scrape_url(input: dict) -> str:
    """Scrape a URL and return a label sheet — exact values that
    supplement the screenshots Claude will receive separately."""
    url = input["url"]

    # Check cache (TTL-based)
    if url in _scrape_cache:
        entry = _scrape_cache[url]
        if isinstance(entry, dict) and "_cache_ts" in entry:
            if _time.time() - entry["_cache_ts"] < _SCRAPE_CACHE_TTL:
                print(f"  [scraper] Cache hit for {url}")
                # Rebuild label sheet from cached data
                return _build_label_sheet(entry)

    t0 = _time.time()
    print(f"  [scraper] Starting scrape of {url}")
    data = await scrape_website(url)
    print(f"  [scraper] Scrape completed in {_time.time() - t0:.1f}s — "
          f"{len(data.get('sections', []))} sections, "
          f"{len(data.get('assets', {}).get('images', []))} images, "
          f"{len(data.get('screenshots', {}).get('scroll_chunks', []))} scroll chunks")

    # Store full data with cache timestamp, then build label sheet
    data["_cache_ts"] = _time.time()
    _scrape_cache[url] = data

    return _build_label_sheet(data)


async def handle_deploy_html(input: dict) -> str:
    """Deploy HTML to a Daytona sandbox."""
    html = input["html_content"]

    result = await deploy_html_to_sandbox(html)
    active_sandboxes[result["sandbox_id"]] = result

    return json.dumps({
        "preview_url": result["preview_url"],
        "sandbox_id": result["sandbox_id"],
        "status": "deployed",
        "message": "HTML site deployed. Use get_sandbox_logs to check for errors.",
    })


async def handle_deploy_react(input: dict) -> str:
    """
    Deploy React project to a Daytona sandbox.
    Provisions sandbox with template (if not already created),
    then uploads Claude's generated files.
    """
    from app.sandbox_template import provision_react_sandbox

    files = input.get("files", {})
    if not files:
        return json.dumps({"error": "No files provided"})

    # Check if we already have a sandbox for this session
    existing_sandbox_id = input.get("sandbox_id")
    if existing_sandbox_id and existing_sandbox_id in active_sandboxes:
        sandbox_id = existing_sandbox_id
        preview_url = active_sandboxes[sandbox_id].get("preview_url", "")
        project_root = active_sandboxes[sandbox_id].get("project_root", "/home/daytona/my-app")
    else:
        # Provision sandbox with template (installs all packages)
        sandbox_info = await provision_react_sandbox()
        sandbox_id = sandbox_info["sandbox_id"]
        preview_url = sandbox_info["preview_url"]
        project_root = sandbox_info.get("project_root", "/home/daytona/my-app")
        active_sandboxes[sandbox_id] = sandbox_info

    # Upload Claude's generated files
    def _upload():
        daytona = get_daytona_client()
        sandbox = daytona.get(sandbox_id)

        for filepath, content in files.items():
            full_path = f"{project_root}/{filepath}"
            # Ensure directory exists
            dir_path = "/".join(full_path.split("/")[:-1])
            sandbox.process.exec(f"mkdir -p {dir_path}", timeout=5)
            sandbox.fs.upload_file(content.encode("utf-8"), full_path)

    await asyncio.to_thread(_upload)

    # Wait for Next.js to pick up the new files
    await asyncio.sleep(3)

    return json.dumps({
        "preview_url": preview_url,
        "sandbox_id": sandbox_id,
        "status": "deployed",
        "files_uploaded": list(files.keys()),
        "message": "React project deployed. Use get_sandbox_logs to check for errors.",
    })


async def handle_create_react_sandbox(input: dict) -> str:
    """Create a boilerplate Next.js App Router sandbox with Tailwind CSS v4."""
    result = await create_react_boilerplate_sandbox()
    active_sandboxes[result["sandbox_id"]] = result

    # initial_files contains all key project files read from the sandbox
    initial_files = result.get("initial_files", {})

    return json.dumps({
        "preview_url": result["preview_url"],
        "sandbox_id": result["sandbox_id"],
        "project_root": result["project_root"],
        "status": "sandbox_ready",
        "message": (
            "Next.js App Router sandbox is live with TypeScript + Tailwind CSS v4 + bun. "
            "Use `update_sandbox_file` to write .tsx components and pages. "
            "Next.js hot-reloads after each file save. "
            "Do NOT edit app/layout.tsx or app/globals.css — they are pre-configured. "
            "Pre-installed: framer-motion, lucide-react, react-icons."
        ),
        "initial_files": initial_files,
    })


async def handle_screenshot_preview(input: dict) -> str:
    """Screenshot a deployed preview URL (compressed to JPEG)."""
    from playwright.async_api import async_playwright

    preview_url = input["preview_url"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})

        try:
            await page.goto(preview_url, wait_until="networkidle", timeout=20000)
        except Exception:
            await page.goto(preview_url, wait_until="domcontentloaded", timeout=10000)
            await page.wait_for_timeout(2000)

        screenshot_bytes = await page.screenshot()
        await browser.close()

    # Compress: ~2MB PNG → ~200KB JPEG
    b64, media_type = screenshot_to_b64(screenshot_bytes, compress=True)

    return json.dumps({
        "screenshot_b64": b64,
        "media_type": media_type,
        "message": (
            "Screenshot taken. Compare this to the original page visually. "
            "Look for: wrong colors, missing sections, layout differences, "
            "missing images, broken buttons."
        ),
    })


async def handle_get_logs(input: dict) -> str:
    """Get logs from a Daytona sandbox."""
    sandbox_id = input["sandbox_id"]

    def _get_logs():
        # Reuse cached sandbox object
        cached = active_sandboxes.get(sandbox_id, {})
        sandbox = cached.get("_sandbox_obj")
        if sandbox is None:
            daytona = get_daytona_client()
            sandbox = daytona.get(sandbox_id)
            if sandbox_id in active_sandboxes:
                active_sandboxes[sandbox_id]["_sandbox_obj"] = sandbox

        project_root = cached.get("project_root", "/home/daytona/clone-app")
        log_file = f"{project_root}/server.log"

        result = sandbox.process.exec(
            f"tail -80 {log_file} 2>/dev/null; "
            f"echo '---STDERR---'; "
            f"tail -50 {log_file} 2>/dev/null || echo 'no error log'",
            timeout=30,
        )

        return result.result if result.result else "No log output available"

    try:
        logs = await asyncio.wait_for(asyncio.to_thread(_get_logs), timeout=30)
    except asyncio.TimeoutError:
        logs = "Timed out fetching logs. The sandbox may still be starting up."

    return json.dumps({
        "logs": logs[:3000],
        "message": (
            "Check for errors. Common Next.js issues: 'class' instead of "
            "'className', unclosed tags, missing imports, missing 'use client' "
            "directive on components with hooks/events. If you see errors, "
            "use update_sandbox_file to fix the broken file."
        ),
    })


async def handle_update_file(input: dict) -> str:
    """Update a file in an existing sandbox."""
    sandbox_id = input["sandbox_id"]
    filepath = input["filepath"]
    content = input["content"]

    print(f"  [update_file] Uploading {filepath} ({len(content)} chars) to sandbox {sandbox_id[:12]}")

    def _update():
        t0 = _time.time()
        # Reuse cached sandbox object if available to avoid extra API round-trip
        cached = active_sandboxes.get(sandbox_id, {})
        sandbox_obj = cached.get("_sandbox_obj")
        if sandbox_obj is None:
            daytona = get_daytona_client()
            sandbox_obj = daytona.get(sandbox_id)
            # Cache the sandbox object for reuse
            if sandbox_id in active_sandboxes:
                active_sandboxes[sandbox_id]["_sandbox_obj"] = sandbox_obj

        project_root = cached.get("project_root", "/home/daytona/clone-app")

        full_path = f"{project_root}/{filepath}"
        sandbox_obj.fs.upload_file(content.encode(), full_path)
        print(f"  [update_file] {filepath} uploaded in {_time.time() - t0:.1f}s to {full_path}")
        return True

    await asyncio.to_thread(_update)

    return json.dumps({
        "status": "updated",
        "filepath": filepath,
        "message": (
            f"File {filepath} updated. The dev server will hot-reload automatically. "
            "Use get_sandbox_logs to verify no errors."
        ),
    })


async def handle_update_files_batch(files: list[dict]) -> list[str]:
    """Upload multiple files to a sandbox in parallel. Each item: {sandbox_id, filepath, content}."""
    if not files:
        return []

    sandbox_id = files[0]["sandbox_id"]
    cached = active_sandboxes.get(sandbox_id, {})
    project_root = cached.get("project_root", "/home/daytona/clone-app")

    def _upload_all():
        t0 = _time.time()
        # Single client + single sandbox.get() for ALL files
        sandbox_obj = cached.get("_sandbox_obj")
        if sandbox_obj is None:
            daytona = get_daytona_client()
            sandbox_obj = daytona.get(sandbox_id)
            if sandbox_id in active_sandboxes:
                active_sandboxes[sandbox_id]["_sandbox_obj"] = sandbox_obj

        for f in files:
            fp = f["filepath"]
            content = f["content"]
            full_path = f"{project_root}/{fp}"
            sandbox_obj.fs.upload_file(content.encode(), full_path)
            print(f"  [batch-upload] {fp} ({len(content)} chars)")

        elapsed = _time.time() - t0
        print(f"  [batch-upload] {len(files)} files uploaded in {elapsed:.1f}s")
        return True

    await asyncio.to_thread(_upload_all)

    return [
        json.dumps({"status": "updated", "filepath": f["filepath"]})
        for f in files
    ]


async def handle_install_package(input: dict) -> str:
    """Install an npm package in a Daytona sandbox using bun."""
    sandbox_id = input["sandbox_id"]
    package_name = input["package_name"]

    # Validate package name to prevent command injection
    if not re.match(r'^(@[a-z0-9\-~][a-z0-9\-._~]*/)?[a-z0-9\-~][a-z0-9\-._~]*(@[^\s]+)?$', package_name):
        return json.dumps({"error": f"Invalid package name: {package_name}"})

    def _install():
        daytona = get_daytona_client()
        sandbox = daytona.get(sandbox_id)

        info = active_sandboxes.get(sandbox_id, {})
        project_root = info.get("project_root", "/home/daytona/clone-app")

        result = sandbox.process.exec(
            f"bun add {package_name} 2>&1 || npm install {package_name} 2>&1",
            cwd=project_root,
            timeout=60,
        )

        return result.result if result.result else "Install completed (no output)"

    output = await asyncio.to_thread(_install)

    return json.dumps({
        "status": "installed",
        "package": package_name,
        "output": output[:2000],
        "message": f"Package {package_name} installed. Vite will pick it up on next import.",
    })
