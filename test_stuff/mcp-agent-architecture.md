# Website Cloner — MCP Agent Architecture

We're rebuilding the website cloner as an MCP server that exposes tools to Claude. Claude orchestrates the cloning process, decides what tools to use, self-corrects, and handles errors intelligently.

**Read this ENTIRE prompt before writing any code.**

---

## Why Network Interception > DOM Scraping

Instead of parsing the DOM for assets (unreliable — misses lazy-loaded stuff, CSS-loaded fonts, JS-injected images), we intercept ALL network requests the browser makes. This captures everything the browser actually downloaded.

```
Browser loads https://stripe.com
    → GET https://stripe.com/                              (HTML)
    → GET https://fonts.googleapis.com/css2?family=...     (font CSS)
    → GET https://fonts.gstatic.com/s/inter/v13/xxx.woff2  (font file)
    → GET https://images.stripe.com/hero.png               (image)
    → GET https://stripe.com/assets/style.css              (stylesheet)
    → GET https://js.stripe.com/v3/                        (script)
    ... every single request
```

We intercept all of these, categorize them, and hand them to Claude as structured data. Claude then has EXACT URLs for every asset — no guessing.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│                  FastAPI Backend                  │
│                                                   │
│  POST /clone { url, mode }                       │
│       │                                           │
│       ▼                                           │
│  ┌─────────────────────────────────┐             │
│  │     Anthropic API Call          │             │
│  │     with MCP Tools              │             │
│  │                                 │             │
│  │  Claude decides:                │             │
│  │   1. "Let me scrape this URL"   │             │
│  │   2. "Let me analyze the data"  │             │
│  │   3. "Let me generate code"     │             │
│  │   4. "Let me deploy it"         │             │
│  │   5. "Let me check my work"     │             │
│  │   6. "Let me fix the issues"    │             │
│  └─────────────────────────────────┘             │
│       │                                           │
│       ▼                                           │
│  MCP Tools (local Python functions):              │
│   • scrape_url     — network intercept + extract  │
│   • screenshot_url — take viewport screenshots    │
│   • generate_file  — write code to sandbox        │
│   • deploy_sandbox — start Daytona sandbox        │
│   • screenshot_sandbox — screenshot the clone     │
│   • get_sandbox_logs — check for errors           │
│   • update_file    — edit a file in the sandbox   │
│                                                   │
│  Result: preview_url returned to frontend         │
└─────────────────────────────────────────────────┘
```

Claude is the orchestrator. It calls tools, reads results, decides next steps. If the clone looks wrong, it fixes it. If React compilation fails, it reads the error and corrects the code.

---

## Step 1: Network Interception Extraction

This is the foundation. Create `backend/app/scraper.py`:

```python
import asyncio
import base64
import json
from urllib.parse import urlparse, urljoin
from playwright.async_api import async_playwright, Route, Request

async def scrape_website(url: str) -> dict:
    """
    Load a URL in Playwright, intercept ALL network requests,
    and extract a complete asset manifest + page data.
    
    Returns:
    {
        "url": "https://stripe.com",
        "title": "Stripe - Financial Infrastructure",
        "assets": {
            "images": [
                {"url": "https://images.stripe.com/hero.png", "type": "image/png", "size": 45230},
                ...
            ],
            "fonts": [
                {"url": "https://fonts.gstatic.com/s/inter/xxx.woff2", "family": "Inter", "type": "font/woff2"},
                ...
            ],
            "stylesheets": [
                {"url": "https://stripe.com/assets/main.css", "type": "text/css"},
                ...
            ],
            "scripts": [
                {"url": "https://js.stripe.com/v3/", "type": "application/javascript"},
                ...
            ]
        },
        "theme": {
            "colors": {
                "body_bg": "#ffffff",
                "body_text": "#425466",
                "backgrounds": ["#0a2540", "#ffffff", "#f6f9fc"],
                "text_colors": ["#0a2540", "#425466", "#697386"],
                "heading_colors": ["#0a2540"]
            },
            "fonts": {
                "body": "sohne-var, -apple-system, BlinkMacSystemFont, sans-serif",
                "heading": "sohne-var, -apple-system, BlinkMacSystemFont, sans-serif",
                "body_size": "16px",
                "heading_size": "64px",
                "heading_weight": "600",
                "google_font_urls": ["https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700"]
            }
        },
        "clickables": {
            "nav_links": [
                {"text": "Products", "href": "https://stripe.com/products"},
                ...
            ],
            "cta_buttons": [
                {"text": "Start now", "href": "https://dashboard.stripe.com/register", "bg": "#635bff", "color": "#fff", "border_radius": "9999px"},
                ...
            ],
            "footer_links": [...]
        },
        "text_content": "Financial infrastructure for the internet...",
        "svgs": [
            {"id": "logo", "markup": "<svg viewBox='0 0 60 25'>...</svg>"},
            ...
        ],
        "page_height": 8500,
        "screenshots": {
            "viewport": "base64...",  # first viewport (1920x1080)
            "full_page_thumbnail": "base64..."  # full page resized to fit
        },
        "meta": {
            "description": "...",
            "og_image": "...",
            "favicon": "..."
        }
    }
    """
    
    # Collect all network requests
    network_requests = []
    
    async def handle_route(route: Route):
        """Intercept and log all network requests, then continue."""
        request = route.request
        
        resource_type = request.resource_type  # document, stylesheet, image, font, script, xhr, fetch, etc.
        url = request.url
        
        network_requests.append({
            "url": url,
            "resource_type": resource_type,
            "method": request.method
        })
        
        await route.continue_()
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        # Intercept ALL requests
        await page.route("**/*", handle_route)
        
        # Navigate
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            # Try with domcontentloaded if networkidle times out
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(3000)
            except Exception as e2:
                await browser.close()
                raise Exception(f"Failed to load {url}: {e2}")
        
        # Run prepare_page — dismiss cookie banners, unlock scroll, trigger lazy loading
        await prepare_page(page)
        
        # Wait for any additional assets triggered by prepare_page
        await page.wait_for_timeout(2000)
        
        # === CATEGORIZE NETWORK REQUESTS ===
        assets = {
            "images": [],
            "fonts": [],
            "stylesheets": [],
            "scripts": []
        }
        
        seen_urls = set()
        for req in network_requests:
            req_url = req["url"]
            if req_url in seen_urls:
                continue
            seen_urls.add(req_url)
            
            rtype = req["resource_type"]
            
            if rtype == "image":
                assets["images"].append({
                    "url": req_url,
                    "type": guess_mime(req_url, "image")
                })
            elif rtype == "font":
                assets["fonts"].append({
                    "url": req_url,
                    "type": guess_mime(req_url, "font")
                })
            elif rtype == "stylesheet":
                assets["stylesheets"].append({
                    "url": req_url,
                    "type": "text/css"
                })
            elif rtype == "script":
                assets["scripts"].append({
                    "url": req_url,
                    "type": "application/javascript"
                })
        
        # Also catch images loaded via CSS or lazy loading that might be in <img> tags
        dom_images = await page.evaluate('''() => {
            return [...document.querySelectorAll('img')]
                .filter(img => img.src && img.offsetWidth > 0)
                .map(img => ({
                    url: img.src,
                    alt: img.alt || '',
                    width: img.naturalWidth,
                    height: img.naturalHeight
                }));
        }''')
        
        # Merge DOM images with network images (dedup by URL)
        image_urls = set(a["url"] for a in assets["images"])
        for img in dom_images:
            if img["url"] not in image_urls and not img["url"].startswith("data:"):
                assets["images"].append({
                    "url": img["url"],
                    "type": "image",
                    "alt": img.get("alt", ""),
                    "width": img.get("width"),
                    "height": img.get("height")
                })
        
        # === EXTRACT GOOGLE FONTS from network requests ===
        google_font_urls = [
            req["url"] for req in network_requests
            if "fonts.googleapis.com" in req["url"] and req["resource_type"] == "stylesheet"
        ]
        
        # === EXTRACT FONT FAMILIES from font file URLs ===
        # Map font file URLs back to @font-face family names
        font_families = await page.evaluate('''() => {
            const families = new Set();
            for (const sheet of document.styleSheets) {
                try {
                    for (const rule of sheet.cssRules) {
                        if (rule instanceof CSSFontFaceRule) {
                            families.add(rule.style.fontFamily.replace(/['"]/g, ''));
                        }
                    }
                } catch(e) {}
            }
            return [...families];
        }''')
        
        # Attach family names to font assets where possible
        for font_asset in assets["fonts"]:
            font_url = font_asset["url"].lower()
            for family in font_families:
                if family.lower().replace(' ', '') in font_url.lower().replace(' ', ''):
                    font_asset["family"] = family
                    break
        
        # === EXTRACT THEME (computed styles) ===
        theme = await page.evaluate('''() => {
            const result = { colors: {}, fonts: {} };
            
            function rgbToHex(rgb) {
                if (!rgb || rgb === 'transparent' || rgb === 'rgba(0, 0, 0, 0)') return null;
                const match = rgb.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                if (!match) return rgb;
                return '#' + [match[1], match[2], match[3]]
                    .map(x => parseInt(x).toString(16).padStart(2, '0'))
                    .join('');
            }
            
            const body = document.body;
            const bs = getComputedStyle(body);
            result.colors.body_bg = rgbToHex(bs.backgroundColor) || '#ffffff';
            result.colors.body_text = rgbToHex(bs.color);
            
            // Sample backgrounds from sections
            const bgSet = new Set();
            const textSet = new Set();
            const headingColorSet = new Set();
            
            document.querySelectorAll('section, header, footer, nav, main, [class*="hero"], [class*="section"], body > div > div').forEach(el => {
                const s = getComputedStyle(el);
                const bg = rgbToHex(s.backgroundColor);
                if (bg) bgSet.add(bg);
                const tc = rgbToHex(s.color);
                if (tc) textSet.add(tc);
            });
            
            document.querySelectorAll('h1, h2, h3').forEach(h => {
                const hc = rgbToHex(getComputedStyle(h).color);
                if (hc) headingColorSet.add(hc);
            });
            
            result.colors.backgrounds = [...bgSet].slice(0, 10);
            result.colors.text_colors = [...textSet].slice(0, 10);
            result.colors.heading_colors = [...headingColorSet].slice(0, 5);
            
            // Fonts
            result.fonts.body = bs.fontFamily;
            result.fonts.body_size = bs.fontSize;
            result.fonts.body_weight = bs.fontWeight;
            
            const h1 = document.querySelector('h1');
            if (h1) {
                const h1s = getComputedStyle(h1);
                result.fonts.heading = h1s.fontFamily;
                result.fonts.heading_size = h1s.fontSize;
                result.fonts.heading_weight = h1s.fontWeight;
                result.fonts.heading_letter_spacing = h1s.letterSpacing;
            }
            
            return result;
        }''')
        
        theme["fonts"]["google_font_urls"] = google_font_urls
        theme["fonts"]["custom_fonts"] = font_families
        
        # === EXTRACT CLICKABLES ===
        base_domain = urlparse(url).scheme + "://" + urlparse(url).netloc
        
        clickables = await page.evaluate('''(baseDomain) => {
            const result = { nav_links: [], cta_buttons: [], footer_links: [], all_links: [] };
            
            function rgbToHex(rgb) {
                if (!rgb || rgb === 'transparent' || rgb === 'rgba(0, 0, 0, 0)') return null;
                const match = rgb.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
                if (!match) return rgb;
                return '#' + [match[1], match[2], match[3]]
                    .map(x => parseInt(x).toString(16).padStart(2, '0'))
                    .join('');
            }
            
            const seen = new Set();
            
            document.querySelectorAll('a[href]').forEach(a => {
                if (!a.offsetWidth || !a.innerText.trim()) return;
                const text = a.innerText.trim().slice(0, 100);
                const href = a.href;
                const key = text + '|' + href;
                if (seen.has(key)) return;
                seen.add(key);
                
                const s = getComputedStyle(a);
                const entry = {
                    text: text,
                    href: href,
                    color: rgbToHex(s.color),
                    is_external: !href.startsWith(baseDomain),
                    opens_new_tab: a.target === '_blank'
                };
                
                if (a.closest('nav, header, [class*="nav"]')) {
                    result.nav_links.push(entry);
                } else if (a.closest('footer, [class*="footer"]')) {
                    result.footer_links.push(entry);
                }
                
                // Check if CTA button
                const looksLikeButton = (
                    s.display === 'inline-flex' || s.display === 'flex' ||
                    s.borderRadius !== '0px' ||
                    a.className.match(/btn|button|cta/i) ||
                    (rgbToHex(s.backgroundColor) !== null)
                );
                if (looksLikeButton) {
                    entry.bg = rgbToHex(s.backgroundColor);
                    entry.border_radius = s.borderRadius;
                    entry.padding = s.padding;
                    entry.font_weight = s.fontWeight;
                    result.cta_buttons.push(entry);
                }
                
                result.all_links.push(entry);
            });
            
            // Also get buttons
            document.querySelectorAll('button').forEach(btn => {
                if (!btn.offsetWidth || !btn.innerText.trim()) return;
                const s = getComputedStyle(btn);
                const parentLink = btn.closest('a');
                result.cta_buttons.push({
                    text: btn.innerText.trim().slice(0, 100),
                    href: parentLink ? parentLink.href : '#',
                    bg: rgbToHex(s.backgroundColor),
                    color: rgbToHex(s.color),
                    border_radius: s.borderRadius,
                    padding: s.padding
                });
            });
            
            return result;
        }''', base_domain)
        
        # === EXTRACT SVGs ===
        svgs = await page.evaluate('''() => {
            return [...document.querySelectorAll('svg')]
                .filter(svg => svg.offsetWidth > 10 && svg.offsetHeight > 10)
                .slice(0, 20)
                .map((svg, i) => ({
                    id: svg.id || svg.getAttribute('aria-label') || `svg-${i}`,
                    markup: svg.outerHTML.slice(0, 2000),
                    width: svg.getAttribute('width') || svg.getBoundingClientRect().width,
                    height: svg.getAttribute('height') || svg.getBoundingClientRect().height
                }));
        }''')
        
        # === EXTRACT TEXT CONTENT ===
        text_content = await page.evaluate('''() => {
            return document.body.innerText.slice(0, 8000);
        }''')
        
        # === EXTRACT META ===
        meta = await page.evaluate('''() => {
            return {
                title: document.title,
                description: document.querySelector('meta[name="description"]')?.content || '',
                og_image: document.querySelector('meta[property="og:image"]')?.content || '',
                favicon: document.querySelector('link[rel="icon"]')?.href || 
                         document.querySelector('link[rel="shortcut icon"]')?.href || ''
            };
        }''')
        
        # === SCREENSHOTS ===
        page_height = await page.evaluate('document.body.scrollHeight')
        
        # Viewport screenshot (first fold)
        await page.evaluate('window.scrollTo(0, 0)')
        await page.wait_for_timeout(300)
        viewport_screenshot = await page.screenshot()
        viewport_b64 = base64.b64encode(viewport_screenshot).decode()
        
        # Full page thumbnail (resized to fit API limits)
        full_screenshot = await page.screenshot(full_page=True)
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(full_screenshot))
        w, h = img.size
        if h > 5000:
            scale = 5000 / h
            img = img.resize((int(w * scale), 5000), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        full_b64 = base64.b64encode(buf.getvalue()).decode()
        
        # Viewport-scroll screenshots for chunked generation
        scroll_screenshots = []
        scroll_step = 880  # 1080 - 200 overlap
        y = 0
        while y < page_height:
            await page.evaluate(f'window.scrollTo(0, {y})')
            await page.wait_for_timeout(300)
            chunk_bytes = await page.screenshot()
            scroll_screenshots.append({
                "y": y,
                "b64": base64.b64encode(chunk_bytes).decode()
            })
            y += scroll_step
        
        await browser.close()
    
    return {
        "url": url,
        "title": meta.get("title", ""),
        "assets": assets,
        "theme": theme,
        "clickables": clickables,
        "text_content": text_content,
        "svgs": svgs,
        "page_height": page_height,
        "screenshots": {
            "viewport": viewport_b64,
            "full_page": full_b64,
            "scroll_chunks": scroll_screenshots
        },
        "meta": meta
    }


def guess_mime(url: str, category: str) -> str:
    """Guess MIME type from URL extension."""
    url_lower = url.lower().split('?')[0]
    if category == "image":
        if url_lower.endswith('.png'): return 'image/png'
        if url_lower.endswith('.jpg') or url_lower.endswith('.jpeg'): return 'image/jpeg'
        if url_lower.endswith('.gif'): return 'image/gif'
        if url_lower.endswith('.svg'): return 'image/svg+xml'
        if url_lower.endswith('.webp'): return 'image/webp'
        if url_lower.endswith('.ico'): return 'image/x-icon'
        return 'image/unknown'
    if category == "font":
        if url_lower.endswith('.woff2'): return 'font/woff2'
        if url_lower.endswith('.woff'): return 'font/woff'
        if url_lower.endswith('.ttf'): return 'font/ttf'
        if url_lower.endswith('.otf'): return 'font/otf'
        return 'font/unknown'
    return 'unknown'


async def prepare_page(page):
    """Clean up page before extraction — dismiss banners, unlock scroll, trigger lazy loading."""
    
    # Dismiss cookie banners
    await page.evaluate('''() => {
        const btns = document.querySelectorAll(
            '[class*="cookie"] button, [id*="cookie"] button, ' +
            '[class*="consent"] button, [aria-label*="accept"], ' +
            '[aria-label*="Accept"], [class*="gdpr"] button'
        );
        for (const btn of btns) {
            if (btn.innerText.match(/accept|agree|got it|ok|close|dismiss/i)) {
                btn.click();
                break;
            }
        }
    }''')
    await page.wait_for_timeout(500)
    
    # Remove overlays
    await page.evaluate('''() => {
        document.querySelectorAll(
            '[class*="cookie"], [id*="cookie"], [class*="consent"], ' +
            '[class*="gdpr"], [class*="popup"], [class*="overlay"], [class*="modal"]'
        ).forEach(el => {
            if (el.innerText.toLowerCase().match(/cookie|consent|privacy|gdpr/)) {
                el.remove();
            }
        });
        
        document.querySelectorAll('*').forEach(el => {
            const s = getComputedStyle(el);
            if ((s.position === 'fixed' || s.position === 'sticky') && 
                parseInt(s.zIndex) > 999 &&
                el.tagName !== 'NAV' && el.tagName !== 'HEADER') {
                el.remove();
            }
        });
    }''')
    
    # Unlock scroll
    await page.evaluate('''() => {
        const unlock = (el) => {
            el.style.overflow = 'visible';
            el.style.overflowY = 'auto';
            el.style.height = 'auto';
            el.style.maxHeight = 'none';
        };
        unlock(document.documentElement);
        unlock(document.body);
        document.querySelectorAll('#__next, #app, #root, main').forEach(el => {
            const s = getComputedStyle(el);
            if (s.overflow === 'hidden' || s.overflowY === 'hidden') unlock(el);
        });
        document.body.classList.remove('no-scroll', 'overflow-hidden', 'modal-open');
    }''')
    
    # Scroll to trigger lazy loading
    await page.evaluate('''async () => {
        await new Promise(resolve => {
            let total = 0;
            const distance = 400;
            const timer = setInterval(() => {
                window.scrollBy(0, distance);
                total += distance;
                if (total >= document.body.scrollHeight) {
                    clearInterval(timer);
                    window.scrollTo(0, 0);
                    resolve();
                }
            }, 100);
        });
    }''')
    await page.wait_for_timeout(1500)
    
    # Force images
    await page.evaluate('''() => {
        document.querySelectorAll('img[loading="lazy"]').forEach(img => {
            img.loading = 'eager';
            if (img.dataset.src) img.src = img.dataset.src;
            if (img.dataset.srcset) img.srcset = img.dataset.srcset;
        });
    }''')
    await page.wait_for_timeout(1000)
```

---

## Step 2: MCP Tool Definitions

Create `backend/app/mcp_tools.py`. These are the tools Claude can call:

```python
"""
MCP-style tool definitions for the website cloner agent.
These aren't a real MCP server — they're tool definitions we pass
to the Anthropic API's tool_use feature. Same effect, simpler setup.
"""

TOOLS = [
    {
        "name": "scrape_url",
        "description": "Load a URL in a headless browser, intercept all network requests, and extract comprehensive page data including: all asset URLs (images, fonts, stylesheets) captured from actual network traffic, exact CSS theme values (colors, fonts, spacing), all clickable elements with their real hrefs, SVG markup, text content, and viewport screenshots. This is the FIRST tool you should call — it gives you everything you need to clone the site.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to scrape"
                }
            },
            "required": ["url"]
        }
    },
    {
        "name": "generate_and_deploy_html",
        "description": "Generate a single self-contained HTML+Tailwind file from scraped data and deploy it to a Daytona sandbox. Use this for simple HTML output mode. Returns a live preview URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "html_content": {
                    "type": "string",
                    "description": "The complete HTML content to deploy. Must be a full HTML document with <!DOCTYPE html>, Tailwind CDN, etc."
                }
            },
            "required": ["html_content"]
        }
    },
    {
        "name": "generate_and_deploy_react",
        "description": "Deploy a React+Tailwind project to a Daytona sandbox. Upload multiple component files, install dependencies, and start Vite dev server. Returns a live preview URL. Use this for React output mode.",
        "input_schema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "object",
                    "description": "Map of filepath to file content. Must include at least 'src/App.jsx'. Example: {'src/App.jsx': 'import...', 'src/components/Navbar.jsx': '...'}",
                    "additionalProperties": {"type": "string"}
                }
            },
            "required": ["files"]
        }
    },
    {
        "name": "screenshot_preview",
        "description": "Take a screenshot of the currently deployed clone at its preview URL. Use this to check your work — compare the screenshot to the original site visually. Returns a base64 PNG image.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preview_url": {
                    "type": "string",
                    "description": "The Daytona preview URL to screenshot"
                }
            },
            "required": ["preview_url"]
        }
    },
    {
        "name": "get_sandbox_logs",
        "description": "Get the console output and error logs from the Daytona sandbox. Use this to check for React compilation errors, runtime errors, or server startup issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sandbox_id": {
                    "type": "string",
                    "description": "The Daytona sandbox ID"
                }
            },
            "required": ["sandbox_id"]
        }
    },
    {
        "name": "update_sandbox_file",
        "description": "Update a single file in an existing Daytona sandbox. The Vite dev server will hot-reload automatically. Use this to fix errors or make corrections without redeploying the entire project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sandbox_id": {
                    "type": "string",
                    "description": "The Daytona sandbox ID"
                },
                "filepath": {
                    "type": "string",
                    "description": "Path of the file to update (e.g. 'src/components/Hero.jsx')"
                },
                "content": {
                    "type": "string",
                    "description": "The new file content"
                }
            },
            "required": ["sandbox_id", "filepath", "content"]
        }
    }
]
```

---

## Step 3: Tool Implementation

Create `backend/app/tool_handlers.py`:

```python
"""
Handlers for each MCP tool. These execute the actual logic
when Claude calls a tool.
"""
import asyncio
import base64
import json
import os
from app.scraper import scrape_website
from app.sandbox import deploy_html_to_sandbox, deploy_react_to_sandbox

# Store active sandbox state
active_sandboxes = {}


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
    }
    
    handler = handlers.get(tool_name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    
    try:
        result = await handler(tool_input)
        return result
    except Exception as e:
        return json.dumps({"error": str(e)})


async def handle_scrape_url(input: dict) -> str:
    """Scrape a URL and return structured data."""
    url = input["url"]
    data = await scrape_website(url)
    
    # Don't send full screenshots in text — too large
    # Instead, note how many scroll chunks we got
    summary = {
        "url": data["url"],
        "title": data["title"],
        "page_height": data["page_height"],
        "num_scroll_chunks": len(data["screenshots"]["scroll_chunks"]),
        "assets": {
            "images": [{"url": img["url"], "alt": img.get("alt", "")} for img in data["assets"]["images"][:30]],
            "fonts": data["assets"]["fonts"][:10],
            "google_font_urls": data["theme"]["fonts"].get("google_font_urls", []),
            "stylesheets_count": len(data["assets"]["stylesheets"]),
        },
        "theme": data["theme"],
        "clickables": {
            "nav_links": data["clickables"]["nav_links"][:20],
            "cta_buttons": data["clickables"]["cta_buttons"][:15],
            "footer_links": data["clickables"]["footer_links"][:20],
        },
        "svgs_count": len(data["svgs"]),
        "svgs": [{"id": s["id"], "markup": s["markup"][:500]} for s in data["svgs"][:10]],
        "text_content": data["text_content"][:5000],
        "meta": data["meta"]
    }
    
    # Store full data for later use (screenshots etc)
    # Use a simple in-memory cache keyed by URL
    _scrape_cache[url] = data
    
    return json.dumps(summary, indent=2)


# Simple in-memory cache
_scrape_cache = {}


async def handle_deploy_html(input: dict) -> str:
    """Deploy HTML to a Daytona sandbox."""
    html = input["html_content"]
    
    result = await deploy_html_to_sandbox(html)
    active_sandboxes[result["sandbox_id"]] = result
    
    return json.dumps({
        "preview_url": result["preview_url"],
        "sandbox_id": result["sandbox_id"],
        "status": "deployed",
        "message": "HTML site deployed. Use screenshot_preview to check how it looks."
    })


async def handle_deploy_react(input: dict) -> str:
    """Deploy React project to a Daytona sandbox."""
    files = input["files"]
    
    result = await deploy_react_to_sandbox(files)
    active_sandboxes[result["sandbox_id"]] = result
    
    return json.dumps({
        "preview_url": result["preview_url"],
        "sandbox_id": result["sandbox_id"],
        "status": "deployed",
        "message": "React app deployed. Wait 5-10 seconds for npm install + Vite startup, then use screenshot_preview to check it or get_sandbox_logs to check for errors."
    })


async def handle_screenshot_preview(input: dict) -> str:
    """Screenshot a deployed preview URL."""
    from playwright.async_api import async_playwright
    
    preview_url = input["preview_url"]
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        
        try:
            await page.goto(preview_url, wait_until="networkidle", timeout=20000)
        except:
            await page.goto(preview_url, wait_until="domcontentloaded", timeout=10000)
            await page.wait_for_timeout(2000)
        
        screenshot = await page.screenshot()
        await browser.close()
    
    b64 = base64.b64encode(screenshot).decode()
    
    return json.dumps({
        "screenshot_b64": b64,
        "message": "Screenshot taken. Compare this to the original page visually. Look for: wrong colors, missing sections, layout differences, missing images, broken buttons."
    })


async def handle_get_logs(input: dict) -> str:
    """Get logs from a Daytona sandbox."""
    from daytona import Daytona, DaytonaConfig
    
    sandbox_id = input["sandbox_id"]
    
    def _get_logs():
        daytona = Daytona(DaytonaConfig(
            api_key=os.getenv("DAYTONA_API_KEY"),
            target="us"
        ))
        sandbox = daytona.get(sandbox_id)
        
        info = active_sandboxes.get(sandbox_id, {})
        session_id = info.get("session_id", f"dev-{sandbox_id[:8]}")
        
        # Get recent output
        result = sandbox.process.execute_session_command(session_id, {
            "command": "tail -50 /tmp/vite-output.log 2>/dev/null; echo '---STDERR---'; cat /tmp/vite-error.log 2>/dev/null || echo 'no error log'",
            "var_async": False
        })
        
        return result.result or "No log output available"
    
    logs = await asyncio.to_thread(_get_logs)
    
    return json.dumps({
        "logs": logs[:3000],
        "message": "Check for errors. Common React issues: 'class' instead of 'className', unclosed tags, missing imports. If you see errors, use update_sandbox_file to fix the broken file."
    })


async def handle_update_file(input: dict) -> str:
    """Update a file in an existing sandbox."""
    from daytona import Daytona, DaytonaConfig
    
    sandbox_id = input["sandbox_id"]
    filepath = input["filepath"]
    content = input["content"]
    
    def _update():
        daytona = Daytona(DaytonaConfig(
            api_key=os.getenv("DAYTONA_API_KEY"),
            target="us"
        ))
        sandbox = daytona.get(sandbox_id)
        
        # Ensure path is within the project
        if not filepath.startswith("src/"):
            full_path = f"/home/daytona/clone-app/{filepath}"
        else:
            full_path = f"/home/daytona/clone-app/{filepath}"
        
        sandbox.fs.upload_file(content.encode(), full_path)
        return True
    
    await asyncio.to_thread(_update)
    
    return json.dumps({
        "status": "updated",
        "filepath": filepath,
        "message": f"File {filepath} updated. Vite will hot-reload automatically. Use screenshot_preview to verify the fix."
    })
```

---

## Step 4: Agent Orchestrator

Create `backend/app/agent.py`. This is the core — it sends the system prompt and tools to Claude via the Anthropic API (through OpenRouter), then handles the tool call loop:

```python
"""
Agent orchestrator. Sends system prompt + tools to Claude,
handles the tool-use loop until Claude produces a final result.
"""
import httpx
import json
import os
import base64
from app.mcp_tools import TOOLS
from app.tool_handlers import handle_tool_call, _scrape_cache

# Max iterations to prevent infinite loops
MAX_ITERATIONS = 8


SYSTEM_PROMPT = """You are an expert website cloner. Your job is to perfectly recreate websites.

You have access to tools that let you:
1. Scrape any URL (getting exact theme data, assets, screenshots, text, links)
2. Deploy HTML or React apps to live sandboxes
3. Screenshot your deployed clone to check your work
4. Read error logs from the sandbox
5. Update individual files to fix issues

## Your Workflow

### Step 1: Scrape
Call scrape_url to get comprehensive data about the target site. You'll receive:
- Exact hex colors, fonts, spacing from computed CSS
- All image URLs captured from network traffic
- All link hrefs and button destinations
- SVG markup for icons
- Text content
- Page structure information

### Step 2: Generate Code
Using ALL the scraped data, generate the website code. You have two options:

**HTML Mode (default, more reliable):**
Generate a single self-contained HTML file using Tailwind CSS CDN. Include:
- Tailwind CDN: <script src="https://cdn.tailwindcss.com"></script>
- Font Awesome: <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.3/css/all.min.css">
- Google Font links from the scraped data
- CSS reset: * { box-sizing: border-box; margin: 0; padding: 0; } img { display: block; max-width: 100%; height: auto; }

**React Mode (if user requests):**
Generate a Vite + React + Tailwind project with proper component files:
- src/App.jsx (root component, imports all sections)
- src/components/Navbar.jsx, Hero.jsx, Features.jsx, Footer.jsx, etc.
- Use className (NOT class), self-close all void elements, style={{}} objects

### Step 3: Deploy
Call generate_and_deploy_html or generate_and_deploy_react to push your code to a live sandbox.

### Step 4: Verify
Call screenshot_preview to see how your clone looks. Compare it mentally to the original scrape data. Look for:
- Wrong or missing colors (compare to the exact hex values from scrape)
- Missing sections or elements
- Wrong layout (number of columns, alignment)
- Missing images
- Broken or unstyled buttons/links
- Duplicate sections (this is a common issue — remove them)
- For React: call get_sandbox_logs first to check for compilation errors

### Step 5: Fix (if needed)
If you spot issues:
- For HTML: generate a corrected HTML file and call generate_and_deploy_html again
- For React: call update_sandbox_file to fix the specific broken component
- Then screenshot again to verify the fix

## CRITICAL RULES

ACCURACY:
- Use the EXACT hex colors from the scraped theme data. Do NOT approximate.
- Use the EXACT image URLs from the scraped assets. Do NOT use placeholders if real URLs exist.
- Use the EXACT link hrefs from the scraped clickables. Do NOT use href="#" when a real URL exists.
- Use the EXACT text content from the scraped data. Do NOT paraphrase.
- Use SVG markup directly for icons when provided.
- Include Google Font links from the scraped data.

COMPLETENESS:
- Write EVERY visible element. Do NOT leave comments like "<!-- more items -->" — write all items.
- If there are 15 cards, write all 15. Do NOT abbreviate.
- Recreate the ENTIRE page, not just the visible viewport.

NO DUPLICATION:
- Each section should appear exactly once. Do NOT duplicate headers, footers, or content sections.

RESPONSIVENESS:
- Make the layout responsive using Tailwind's sm:, md:, lg: prefixes.
- Mobile-first approach.

You should aim to get a good result in 2-3 iterations max. Don't over-iterate — a 90% accurate clone deployed to a live URL is the goal."""


async def run_clone_agent(url: str, output_format: str = "html") -> dict:
    """
    Run the agent loop. Claude calls tools until it has a deployed clone.
    
    Returns:
    {
        "preview_url": "https://3000-xxx.proxy.daytona.works",
        "sandbox_id": "xxx",
        "html": "..." (if HTML mode),
        "files": {...} (if React mode),
        "iterations": 3,
        "status": "success"
    }
    """
    
    # Initial user message
    format_instruction = "Use HTML mode (single HTML file with Tailwind)." if output_format == "html" else "Use React mode (Vite + React + Tailwind with separate component files)."
    
    user_message = f"Clone this website: {url}\n\n{format_instruction}\n\nStart by scraping the URL to get all the data you need."
    
    messages = [
        {"role": "user", "content": user_message}
    ]
    
    result = {
        "preview_url": None,
        "sandbox_id": None,
        "html": None,
        "files": None,
        "iterations": 0,
        "status": "processing"
    }
    
    for iteration in range(MAX_ITERATIONS):
        result["iterations"] = iteration + 1
        print(f"\n🤖 Agent iteration {iteration + 1}/{MAX_ITERATIONS}")
        
        # Call Claude via OpenRouter
        response = await call_claude_with_tools(messages)
        
        if not response:
            result["status"] = "failed"
            break
        
        # Process the response
        assistant_message = response["choices"][0]["message"]
        
        # Add assistant message to conversation
        messages.append({"role": "assistant", "content": assistant_message.get("content", ""), "tool_calls": assistant_message.get("tool_calls")})
        
        # Check if Claude wants to use tools
        tool_calls = assistant_message.get("tool_calls", [])
        
        if not tool_calls:
            # Claude is done — extract final result
            print("✅ Agent finished (no more tool calls)")
            result["status"] = "success"
            break
        
        # Execute each tool call
        tool_results = []
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            tool_input = json.loads(tc["function"]["arguments"])
            
            print(f"  🔧 Calling tool: {tool_name}")
            
            tool_result = await handle_tool_call(tool_name, tool_input)
            
            # Track sandbox/preview state
            try:
                parsed_result = json.loads(tool_result)
                if "preview_url" in parsed_result:
                    result["preview_url"] = parsed_result["preview_url"]
                if "sandbox_id" in parsed_result:
                    result["sandbox_id"] = parsed_result["sandbox_id"]
            except:
                pass
            
            # Store HTML/files from deploy calls
            if tool_name == "generate_and_deploy_html":
                result["html"] = tool_input.get("html_content")
            elif tool_name == "generate_and_deploy_react":
                result["files"] = tool_input.get("files")
            
            tool_results.append({
                "tool_call_id": tc["id"],
                "role": "tool",
                "content": tool_result
            })
        
        # Add tool results to conversation
        messages.extend(tool_results)
        
        # Handle screenshots specially — send as images in next message
        for tc, tr in zip(tool_calls, tool_results):
            if tc["function"]["name"] in ("screenshot_preview", "scrape_url"):
                try:
                    parsed = json.loads(tr["content"])
                    if "screenshot_b64" in parsed:
                        # Add screenshot as image for Claude to see
                        messages.append({
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{parsed['screenshot_b64']}"}},
                                {"type": "text", "text": "Here's the screenshot. Analyze it and decide if fixes are needed, or if the clone looks good enough to finalize."}
                            ]
                        })
                except:
                    pass
    
    if not result["preview_url"]:
        result["status"] = "failed"
    
    return result


async def call_claude_with_tools(messages: list) -> dict:
    """Call Claude via OpenRouter with tool definitions."""
    
    # Convert tools to OpenAI function calling format (OpenRouter uses this)
    functions = [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"]
            }
        }
        for tool in TOOLS
    ]
    
    # Need to handle the messages carefully — some have images
    formatted_messages = []
    for msg in messages:
        if isinstance(msg.get("content"), list):
            # Multi-part message (text + images)
            formatted_messages.append(msg)
        elif msg.get("tool_calls"):
            formatted_messages.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": msg["tool_calls"]
            })
        else:
            formatted_messages.append(msg)
    
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
                "Content-Type": "application/json"
            },
            json={
                "model": "anthropic/claude-sonnet-4-20250514",
                "max_tokens": 16000,
                "system": SYSTEM_PROMPT,
                "messages": formatted_messages,
                "tools": functions,
                "tool_choice": "auto"
            }
        )
        
        if response.status_code != 200:
            print(f"❌ Claude API error: {response.status_code} {response.text[:500]}")
            return None
        
        return response.json()
```

---

## Step 5: Wire Into FastAPI

Update `backend/app/main.py` to add the agent endpoint alongside the existing pipeline:

```python
@app.post("/clone")
async def clone_website_endpoint(request: CloneRequest):
    """
    Clone a website. Supports two modes:
    - pipeline (default): deterministic screenshot → LLM → deploy
    - agent: Claude orchestrates the cloning with tools
    """
    url = request.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    
    # Save pending record
    from app.database import save_clone, update_clone
    record = await save_clone({"url": url, "status": "processing"})
    clone_id = record["id"]
    
    try:
        # Use agent mode
        from app.agent import run_clone_agent
        agent_result = await asyncio.wait_for(
            run_clone_agent(url, output_format=request.output_format),
            timeout=300  # 5 minutes max for agent loop
        )
        
        # Update Supabase
        await update_clone(clone_id, {
            "status": agent_result["status"],
            "preview_url": agent_result.get("preview_url"),
            "sandbox_id": agent_result.get("sandbox_id"),
            "html": agent_result.get("html"),
            "metadata": {
                "iterations": agent_result.get("iterations"),
                "output_format": request.output_format,
                "files": agent_result.get("files")
            }
        })
        
        return {
            "clone_id": clone_id,
            "preview_url": agent_result.get("preview_url"),
            "status": agent_result["status"],
            "iterations": agent_result.get("iterations"),
            "delivery": "sandbox" if agent_result.get("preview_url") else "failed"
        }
    
    except asyncio.TimeoutError:
        await update_clone(clone_id, {"status": "failed", "error_message": "Timed out"})
        raise HTTPException(status_code=504, detail="Clone timed out")
    except Exception as e:
        await update_clone(clone_id, {"status": "failed", "error_message": str(e)})
        raise HTTPException(status_code=500, detail=str(e))
```

---

## Step 6: Frontend Toggle

Add output format selector to the frontend:

- Two buttons: "HTML" and "React" 
- Send `output_format` in the POST body
- Show iteration count during loading: "Agent iteration 2 of 8..."
- Everything else stays the same — iframe shows the preview_url

---

## File Structure

```
backend/
├── app/
│   ├── main.py            ← FastAPI with /clone endpoint
│   ├── scraper.py         ← Network interception + extraction (Step 1)
│   ├── mcp_tools.py       ← Tool definitions for Claude (Step 2)
│   ├── tool_handlers.py   ← Tool execution logic (Step 3)
│   ├── agent.py           ← Claude orchestrator loop (Step 4)
│   ├── sandbox.py         ← Daytona sandbox management
│   ├── database.py        ← Supabase client
│   └── config.py          ← Settings
├── requirements.txt
├── Dockerfile
└── .env
```

---

## Implementation Order

1. **scraper.py** — network interception extraction. Test it standalone: `python -c "import asyncio; from app.scraper import scrape_website; print(asyncio.run(scrape_website('https://example.com')))"`. Verify you get images, fonts, colors, links.
2. **mcp_tools.py** — just the tool definitions (pure data, nothing to test)
3. **sandbox.py** — both HTML and React deploy functions. Test with a hardcoded HTML string.
4. **tool_handlers.py** — wire tools to real functions. Test each handler individually.
5. **agent.py** — the orchestrator. Test: does Claude call scrape_url first? Does it generate code? Does it deploy?
6. **main.py** — wire it all together. Full end-to-end test.
7. **Frontend** — add output_format toggle.

Test EACH step before moving on. Don't write everything then debug.

---

## Gotchas To Watch For

1. **OpenRouter tool calling format** — OpenRouter uses OpenAI's function calling format, not Anthropic's native tool_use format. The tools array uses `{"type": "function", "function": {...}}` wrapper. Test this works before building the full loop.

2. **Context window** — The scraped data can be huge. Truncate text_content to 5000 chars, limit images to 30, limit SVGs to 10. If the context gets too large, Claude's code output will be truncated.

3. **Screenshot as image** — When sending the screenshot_preview result back to Claude, you MUST send it as an image_url content part, not as text. Claude can't "see" a base64 string — it needs the image format.

4. **Agent spinning** — Set MAX_ITERATIONS = 8. If Claude keeps fixing without converging, cut it off and return whatever's deployed.

5. **Cost** — Each iteration is a Claude API call with images. 8 iterations × ~$0.10 = ~$0.80 per clone. That's fine for a demo but track it.

6. **Daytona rate limits** — Don't create a new sandbox on every iteration. Deploy once, then use update_sandbox_file for fixes. Only create a new sandbox if the first deploy fails.

7. **React in Daytona** — The sandbox needs Node.js. Use `language="javascript"` when creating the sandbox for React mode, `language="python"` for HTML mode.
