"""
Network-interception based website scraper.

Instead of parsing the DOM for assets (unreliable — misses lazy-loaded stuff,
CSS-loaded fonts, JS-injected images), we intercept ALL network requests the
browser makes. This captures everything the browser actually downloaded.
"""

import asyncio
import base64
import io
import json
from urllib.parse import urlparse, urljoin

from PIL import Image
from playwright.async_api import async_playwright, Route, Request

from app.image_utils import screenshot_to_b64

try:
    from playwright_stealth import Stealth
    _stealth = Stealth()
except ImportError:
    _stealth = None


async def scrape_website(url: str) -> dict:
    """
    Load a URL in Playwright, intercept ALL network requests,
    and extract a complete asset manifest + page data.
    """

    # Collect all network requests
    network_requests = []

    async def handle_route(route: Route):
        """Intercept and log all network requests, then continue."""
        request = route.request
        resource_type = request.resource_type
        req_url = request.url

        network_requests.append({
            "url": req_url,
            "resource_type": resource_type,
            "method": request.method,
        })

        await route.continue_()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Apply stealth to avoid bot detection
        if _stealth:
            await _stealth.apply_stealth_async(page)

        # Intercept ALL requests
        await page.route("**/*", handle_route)

        # Navigate — use shorter timeout, fallback quickly
        try:
            await page.goto(url, wait_until="networkidle", timeout=15000)
        except Exception:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                await page.wait_for_timeout(2000)
            except Exception as e2:
                await browser.close()
                raise Exception(f"Failed to load {url}: {e2}")

        # Clean up page
        await prepare_page(page)

        # Brief wait for additional assets triggered by prepare_page
        await page.wait_for_timeout(1000)

        # === CATEGORIZE NETWORK REQUESTS ===
        assets = {
            "images": [],
            "fonts": [],
            "stylesheets": [],
            "scripts": [],
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
                    "type": guess_mime(req_url, "image"),
                })
            elif rtype == "font":
                assets["fonts"].append({
                    "url": req_url,
                    "type": guess_mime(req_url, "font"),
                })
            elif rtype == "stylesheet":
                assets["stylesheets"].append({
                    "url": req_url,
                    "type": "text/css",
                })
            elif rtype == "script":
                assets["scripts"].append({
                    "url": req_url,
                    "type": "application/javascript",
                })

        # Also catch images in <img> tags (CSS/lazy-loaded)
        try:
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
        except Exception as e:
            print(f"  [scrape] DOM images extraction failed: {e}")
            dom_images = []

        # Merge DOM images with network images (dedup by URL)
        image_urls = set(a["url"] for a in assets["images"])
        for img in dom_images:
            if img["url"] not in image_urls and not img["url"].startswith("data:"):
                assets["images"].append({
                    "url": img["url"],
                    "type": "image",
                    "alt": img.get("alt", ""),
                    "width": img.get("width"),
                    "height": img.get("height"),
                })

        # === EXTRACT GOOGLE FONTS from network requests ===
        google_font_urls = [
            req["url"] for req in network_requests
            if "fonts.googleapis.com" in req["url"] and req["resource_type"] == "stylesheet"
        ]

        # === EXTRACT FONT FAMILIES from @font-face rules ===
        try:
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
        except Exception as e:
            print(f"  [scrape] Font families extraction failed: {e}")
            font_families = []

        # Attach family names to font assets where possible
        for font_asset in assets["fonts"]:
            font_url = font_asset["url"].lower()
            for family in font_families:
                if family.lower().replace(" ", "") in font_url.lower().replace(" ", ""):
                    font_asset["family"] = family
                    break

        # === EXTRACT THEME (computed styles) ===
        try:
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

            // Walk up to find effective background
            function getEffectiveBg(el) {
                let current = el;
                while (current && current !== document.documentElement) {
                    const hex = rgbToHex(getComputedStyle(current).backgroundColor);
                    if (hex) return hex;
                    current = current.parentElement;
                }
                return null;
            }

            const body = document.body;
            const bs = getComputedStyle(body);
            // Effective body background: walk up from body to html
            result.colors.body_bg = getEffectiveBg(body) || rgbToHex(getComputedStyle(document.documentElement).backgroundColor) || '#ffffff';

            // Sample REAL text colors from visible elements instead of body.color
            const textColorMap = {};
            // Target elements that actually contain readable text
            const textSamples = document.querySelectorAll('p, span, li, td, a, h1, h2, h3, h4, h5, h6, label, blockquote');
            for (const el of textSamples) {
                if (!el.offsetWidth || !el.innerText.trim()) continue;
                // Only leaf-ish elements — skip containers with many children
                if (el.children.length > 5) continue;
                // Must have actual visible text (not just whitespace)
                const text = el.innerText.trim();
                if (text.length < 2) continue;
                const color = rgbToHex(getComputedStyle(el).color);
                if (color) textColorMap[color] = (textColorMap[color] || 0) + 1;
            }
            // Most common text color is the "body text" color
            const sortedTextColors = Object.entries(textColorMap).sort((a, b) => b[1] - a[1]);
            result.colors.body_text = sortedTextColors.length > 0 ? sortedTextColors[0][0] : rgbToHex(bs.color);

            const bgSet = new Set();
            const textSet = new Set();
            const headingColorSet = new Set();

            // Broader element search for backgrounds — include nested containers
            document.querySelectorAll('section, header, footer, nav, main, [class*="hero"], [class*="section"], body > div > div, [class*="card"], [class*="container"]').forEach(el => {
                const s = getComputedStyle(el);
                const bg = rgbToHex(s.backgroundColor);
                if (bg) bgSet.add(bg);
                // Also check for gradients
                if (s.backgroundImage && s.backgroundImage !== 'none' && s.backgroundImage.includes('gradient')) {
                    bgSet.add(s.backgroundImage.slice(0, 200));
                }
            });

            // Text colors from real visible text
            for (const [color] of sortedTextColors.slice(0, 10)) {
                textSet.add(color);
            }

            document.querySelectorAll('h1, h2, h3, h4').forEach(h => {
                if (!h.offsetWidth) return;
                const hc = rgbToHex(getComputedStyle(h).color);
                if (hc) headingColorSet.add(hc);
            });

            // Also capture link/button accent colors
            const accentSet = new Set();
            document.querySelectorAll('a, button, [role="button"]').forEach(el => {
                if (!el.offsetWidth) return;
                const s = getComputedStyle(el);
                const bg = rgbToHex(s.backgroundColor);
                if (bg) accentSet.add(bg);
                const color = rgbToHex(s.color);
                if (color) accentSet.add(color);
            });

            result.colors.backgrounds = [...bgSet].slice(0, 10);
            result.colors.text_colors = [...textSet].slice(0, 10);
            result.colors.heading_colors = [...headingColorSet].slice(0, 5);
            result.colors.accent_colors = [...accentSet].slice(0, 8);

            // Border and shadow colors (for cards)
            const borderSet = new Set();
            document.querySelectorAll('[class*="card"], [class*="border"], section > div > div').forEach(el => {
                if (!el.offsetWidth) return;
                const s = getComputedStyle(el);
                const bc = rgbToHex(s.borderColor);
                if (bc) borderSet.add(bc);
                if (s.boxShadow && s.boxShadow !== 'none') {
                    result.colors.box_shadow_sample = s.boxShadow.slice(0, 150);
                }
            });
            result.colors.border_colors = [...borderSet].slice(0, 5);

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
        except Exception as e:
            print(f"  [scrape] Theme extraction failed: {e}")
            theme = {"colors": {}, "fonts": {}}

        theme.setdefault("fonts", {})["google_font_urls"] = google_font_urls
        theme.setdefault("fonts", {})["custom_fonts"] = font_families

        # === EXTRACT CLICKABLES ===
        base_domain = urlparse(url).scheme + "://" + urlparse(url).netloc

        try:
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

                const looksLikeButton = (
                    s.display === 'inline-flex' || s.display === 'flex' ||
                    s.borderRadius !== '0px' ||
                    (a.className && a.className.match && a.className.match(/btn|button|cta/i)) ||
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
        except Exception as e:
            print(f"  [scrape] Clickables extraction failed: {e}")
            clickables = {"nav_links": [], "cta_buttons": [], "footer_links": [], "all_links": []}

        # === EXTRACT SVGs ===
        try:
            svgs = await page.evaluate('''() => {
            // Broader SVG search: look everywhere including inside containers
            const allSvgs = [...document.querySelectorAll('svg')];

            return allSvgs
                .filter(svg => {
                    const r = svg.getBoundingClientRect();
                    // Accept SVGs that are at least 5x5 (catches small icons too)
                    return r.width > 5 && r.height > 5;
                })
                .slice(0, 25)
                .map((svg, i) => {
                    const r = svg.getBoundingClientRect();
                    // Detect role: logo, icon, decorative
                    let role = 'decorative';
                    if (svg.closest('nav, header, [class*="nav"]')) role = 'logo';
                    else if (r.width <= 32 && r.height <= 32) role = 'icon';
                    else if (svg.closest('button, a, [role="button"]')) role = 'icon';

                    return {
                        id: svg.id || svg.getAttribute('aria-label') || svg.closest('[aria-label]')?.getAttribute('aria-label') || `svg-${i}`,
                        markup: svg.outerHTML.slice(0, 2000),
                        width: Math.round(r.width),
                        height: Math.round(r.height),
                        role: role,
                    };
                });
        }''')
        except Exception as e:
            print(f"  [scrape] SVGs extraction failed: {e}")
            svgs = []

        # === EXTRACT TEXT CONTENT ===
        try:
            text_content = await page.evaluate('''() => {
                return document.body.innerText.slice(0, 8000);
            }''')
        except Exception as e:
            print(f"  [scrape] Text content extraction failed: {e}")
            text_content = ""

        # === EXTRACT META ===
        try:
            meta = await page.evaluate('''() => {
                return {
                    title: document.title,
                    description: document.querySelector('meta[name="description"]')?.content || '',
                    og_image: document.querySelector('meta[property="og:image"]')?.content || '',
                    favicon: document.querySelector('link[rel="icon"]')?.href ||
                             document.querySelector('link[rel="shortcut icon"]')?.href || ''
                };
        }''')
        except Exception as e:
            print(f"  [scrape] Meta extraction failed: {e}")
            meta = {"title": "", "description": "", "og_image": "", "favicon": ""}

        # === EXTRACT SECTIONS (structured DOM) ===
        try:
            sections = await extract_sections(page)
        except Exception as e:
            print(f"  [scrape] Sections extraction failed: {e}")
            sections = []

        # === SCREENSHOTS (compressed to JPEG at capture time) ===
        page_height = await page.evaluate("document.body.scrollHeight")

        # Viewport screenshot (first fold)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
        viewport_bytes = await page.screenshot()
        viewport_b64, _ = screenshot_to_b64(viewport_bytes, compress=True)

        # Full page thumbnail (resized + compressed)
        # Cap page height to avoid OOM on very tall pages
        max_full_page_height = 12000
        if page_height > max_full_page_height:
            await page.evaluate(f"document.body.style.maxHeight = '{max_full_page_height}px'")
        full_bytes = await page.screenshot(full_page=True)
        if page_height > max_full_page_height:
            await page.evaluate("document.body.style.maxHeight = ''")
        full_b64, _ = screenshot_to_b64(full_bytes, compress=True, max_width=1024, quality=60)

        # Viewport-scroll screenshots for chunked generation (capped at 6)
        scroll_screenshots = []
        scroll_step = 880  # 1080 - 200 overlap
        max_scroll_screenshots = 6
        y = 0
        while y < page_height and len(scroll_screenshots) < max_scroll_screenshots:
            await page.evaluate(f"window.scrollTo(0, {y})")
            await page.wait_for_timeout(300)
            chunk_bytes = await page.screenshot()
            chunk_b64, _ = screenshot_to_b64(chunk_bytes, compress=True)
            scroll_screenshots.append({
                "y": y,
                "b64": chunk_b64,
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
        "sections": sections,
        "page_height": page_height,
        "screenshots": {
            "viewport": viewport_b64,
            "full_page": full_b64,
            "scroll_chunks": scroll_screenshots,
        },
        "meta": meta,
    }


async def extract_sections(page) -> list:
    """
    Extract structured, section-by-section DOM data.
    Identifies top-level sections, splits oversized blobs into sub-sections,
    extracts per-section content with layout properties, effective background
    colors (walking up DOM tree), image role detection, and improved SVG capture.
    """
    return await page.evaluate('''() => {
        // === UTILITIES ===
        function rgbToHex(rgb) {
            if (!rgb || rgb === 'transparent' || rgb === 'rgba(0, 0, 0, 0)') return null;
            const match = rgb.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
            if (!match) return rgb;
            return '#' + [match[1], match[2], match[3]]
                .map(x => parseInt(x).toString(16).padStart(2, '0'))
                .join('');
        }

        // Walk UP the DOM to find the first non-transparent background
        function getEffectiveBg(el) {
            let current = el;
            while (current && current !== document.documentElement) {
                const bg = getComputedStyle(current).backgroundColor;
                const hex = rgbToHex(bg);
                if (hex) return hex;
                current = current.parentElement;
            }
            return rgbToHex(getComputedStyle(document.body).backgroundColor) || '#ffffff';
        }

        // Extract gradient from backgroundImage
        function getGradient(el) {
            let current = el;
            while (current && current !== document.documentElement) {
                const bgImg = getComputedStyle(current).backgroundImage;
                if (bgImg && bgImg !== 'none' && bgImg.includes('gradient')) {
                    return bgImg.slice(0, 300);
                }
                current = current.parentElement;
            }
            return null;
        }

        function detectSectionType(el) {
            const tag = el.tagName.toLowerCase();
            const cls = (el.className && typeof el.className === 'string') ? el.className.toLowerCase() : '';
            const id = (el.id || '').toLowerCase();
            const text = cls + ' ' + id;

            if (tag === 'nav' || text.match(/\\bnav(bar|igation)?\\b/)) return 'navbar';
            if (tag === 'header' || text.match(/\\bheader\\b/)) return 'header';
            if (tag === 'footer' || text.match(/\\bfooter\\b/)) return 'footer';
            if (text.match(/\\bhero\\b/)) return 'hero';
            if (text.match(/\\bfeature/)) return 'features';
            if (text.match(/\\bpricing/)) return 'pricing';
            if (text.match(/\\btestimonial|\\breview/)) return 'testimonials';
            if (text.match(/\\bfaq/)) return 'faq';
            if (text.match(/\\bcta|\\bcall.to.action/)) return 'cta';
            if (text.match(/\\bcontact/)) return 'contact';
            if (text.match(/\\babout/)) return 'about';
            if (text.match(/\\bblog|\\barticle|\\bpost/)) return 'blog';
            if (text.match(/\\bteam/)) return 'team';
            if (text.match(/\\bpartner|\\bclient|\\blogo/)) return 'logos';
            if (text.match(/\\bstats|\\bnumber|\\bcounter/)) return 'stats';

            // Content-based heuristic: check first heading text
            const firstH = el.querySelector('h1, h2, h3');
            if (firstH) {
                const ht = firstH.innerText.toLowerCase();
                if (ht.match(/pric/)) return 'pricing';
                if (ht.match(/feature|what we/)) return 'features';
                if (ht.match(/testimoni|what.*say|review/)) return 'testimonials';
                if (ht.match(/faq|question/)) return 'faq';
                if (ht.match(/team|people|who we/)) return 'team';
                if (ht.match(/contact|get in touch|reach/)) return 'contact';
                if (ht.match(/partner|client|trusted|companies/)) return 'logos';
            }
            return 'section';
        }

        // Detect image role based on position, size, and context
        function getImageRole(img) {
            const displayW = img.offsetWidth;
            const displayH = img.offsetHeight;
            const natW = img.naturalWidth || displayW;
            const natH = img.naturalHeight || displayH;
            const rect = img.getBoundingClientRect();
            const pageY = rect.top + window.scrollY;
            const hint = (img.src + ' ' + (img.alt || '') + ' ' + (img.className || '')).toLowerCase();

            // Logo: in nav/header area, or has "logo" in URL/alt
            if (img.closest('nav, header')) return 'logo';
            if (hint.match(/logo/)) return 'logo';
            // Small image in top 150px is likely a logo
            if (pageY < 150 && displayH < 80) return 'logo';

            // Icon: small display size
            if (displayW <= 48 && displayH <= 48) return 'icon';
            if (displayW <= 80 && displayH <= 80 && hint.match(/icon|arrow|chevron|check/)) return 'icon';

            // Avatar: small, roughly square, near text that looks like a name/quote
            if (displayW <= 120 && displayH <= 120 && Math.abs(displayW - displayH) < 30) {
                // Check if near a blockquote or short text block (testimonial-like)
                const parent = img.closest('div, section, article');
                if (parent) {
                    const nearby = parent.innerText.toLowerCase();
                    if (nearby.match(/["\\u201c]|said|ceo|founder|cto|engineer|developer|manager/)) return 'avatar';
                }
                // Or if it has rounded corners (avatar styling)
                const style = getComputedStyle(img);
                if (style.borderRadius && parseInt(style.borderRadius) > 20) return 'avatar';
            }

            // Hero: large image near top of page
            if (pageY < 900 && (displayW > 500 || (displayW > 300 && displayH > 300))) return 'hero';

            // Company logo strip: small-medium images in a container with 3+ sibling images
            if (displayH <= 80 && displayW <= 250 && displayW > 40) {
                // Check parent and grandparent for sibling images
                const container = img.closest('div, section, ul');
                if (container) {
                    const siblingImgs = container.querySelectorAll('img');
                    if (siblingImgs.length >= 3) return 'company-logo';
                }
            }

            // Screenshot/product: medium-large with aspect ratio suggesting a screenshot
            if (displayW > 200 && displayH > 150) {
                const ratio = displayW / displayH;
                if (ratio > 1.2 && ratio < 2.5) return 'screenshot';
            }

            return 'content';
        }

        // === FIND SECTIONS ===
        const semantic = [...document.querySelectorAll('nav, header, main, footer, section, article, aside')];
        const root = document.querySelector('#__next, #root, #app, main') || document.body;
        const directChildren = [...root.children].filter(el => {
            const tag = el.tagName.toLowerCase();
            return tag !== 'script' && tag !== 'style' && tag !== 'link' &&
                   tag !== 'meta' && tag !== 'noscript' && el.offsetHeight > 30;
        });

        // Merge and deduplicate
        const seen = new Set();
        const candidates = [];
        for (const el of [...semantic, ...directChildren]) {
            if (seen.has(el)) continue;
            let dominated = false;
            for (const added of seen) {
                if (added.contains(el) && added !== el) { dominated = true; break; }
            }
            if (dominated) continue;
            seen.add(el);
            candidates.push(el);
        }

        candidates.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

        // === SPLIT OVERSIZED SECTIONS ===
        // Unwrap single-child wrappers and split blobs with >2 headings

        function getVisibleChildren(el) {
            return [...el.children].filter(c => {
                const tag = c.tagName.toLowerCase();
                return tag !== 'script' && tag !== 'style' && tag !== 'link' &&
                       tag !== 'meta' && tag !== 'noscript' && c.offsetHeight > 30;
            });
        }

        // Unwrap: if an element has only 1 visible child that's similarly sized, go inside
        function unwrap(el, depth) {
            if (depth > 5) return el;
            const kids = getVisibleChildren(el);
            if (kids.length === 1 && kids[0].offsetHeight > el.offsetHeight * 0.8) {
                return unwrap(kids[0], depth + 1);
            }
            return el;
        }

        // Split oversized sections into their children
        function splitIfNeeded(el) {
            // First unwrap single-child wrappers
            const real = unwrap(el, 0);
            const rect = real.getBoundingClientRect();
            const headingCount = real.querySelectorAll('h1, h2, h3, h4, h5, h6').length;

            if (rect.height > 800 && headingCount > 2) {
                const kids = getVisibleChildren(real);
                if (kids.length >= 2) {
                    // Recursively split children that are still too big
                    const result = [];
                    for (const kid of kids) {
                        const kidRect = kid.getBoundingClientRect();
                        const kidHeadings = kid.querySelectorAll('h1, h2, h3, h4, h5, h6').length;
                        if (kidRect.height > 800 && kidHeadings > 2) {
                            result.push(...splitIfNeeded(kid));
                        } else if (kidRect.height > 30) {
                            result.push(unwrap(kid, 0));
                        }
                    }
                    return result;
                }
            }
            return [real];
        }

        const expanded = [];
        for (const el of candidates) {
            expanded.push(...splitIfNeeded(el));
        }

        // Re-sort and cap
        expanded.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
        const sections = expanded.slice(0, 25);

        // === EXTRACT PER-SECTION DATA ===
        return sections.map((el, idx) => {
            const rect = el.getBoundingClientRect();
            const cs = getComputedStyle(el);

            // Layout properties
            const layout = {
                display: cs.display,
                flex_direction: cs.flexDirection !== 'row' ? cs.flexDirection : null,
                justify_content: cs.justifyContent !== 'normal' ? cs.justifyContent : null,
                align_items: cs.alignItems !== 'normal' ? cs.alignItems : null,
                gap: (cs.gap && cs.gap !== 'normal' && cs.gap !== '0px') ? cs.gap : null,
                padding: cs.padding !== '0px' ? cs.padding : null,
                max_width: cs.maxWidth !== 'none' ? cs.maxWidth : null,
                grid_template_columns: cs.display === 'grid' ? cs.gridTemplateColumns : null,
            };

            // Headings
            const headings = [...el.querySelectorAll('h1, h2, h3, h4, h5, h6')]
                .filter(h => h.offsetWidth > 0 && h.innerText.trim())
                .slice(0, 5).map(h => {
                const hs = getComputedStyle(h);
                return {
                    tag: h.tagName.toLowerCase(),
                    text: h.innerText.trim().slice(0, 200),
                    color: rgbToHex(hs.color),
                    font_size: hs.fontSize,
                    font_weight: hs.fontWeight,
                };
            });

            // Paragraphs
            const paragraphs = [...el.querySelectorAll('p')]
                .filter(p => p.offsetWidth > 0 && p.innerText.trim())
                .slice(0, 5).map(p => {
                const ps = getComputedStyle(p);
                return {
                    text: p.innerText.trim().slice(0, 300),
                    color: rgbToHex(ps.color),
                    font_size: ps.fontSize,
                };
            });

            // Helper: find nearest preceding heading within a section element
            function findNearestHeading(targetEl, sectionEl) {
                const allHeadings = [...sectionEl.querySelectorAll('h1, h2, h3, h4, h5, h6')];
                let closest = null;
                for (const h of allHeadings) {
                    // Heading must come before target in DOM order
                    if (targetEl.compareDocumentPosition(h) & Node.DOCUMENT_POSITION_PRECEDING) {
                        closest = h.innerText.trim().slice(0, 100);
                    }
                }
                return closest;
            }

            // Images with role detection + near_heading + position_index
            const images = [...el.querySelectorAll('img')]
                .filter(img => img.src && img.offsetWidth > 0)
                .slice(0, 10).map((img, posIdx) => ({
                    url: img.src,
                    alt: img.alt || '',
                    width: img.naturalWidth || img.offsetWidth,
                    height: img.naturalHeight || img.offsetHeight,
                    role: getImageRole(img),
                    near_heading: findNearestHeading(img, el),
                    position_index: posIdx,
                }));

            // Links
            const links = [...el.querySelectorAll('a[href]')]
                .filter(a => a.offsetWidth > 0 && a.innerText.trim())
                .slice(0, 10).map(a => ({
                    text: a.innerText.trim().slice(0, 100),
                    href: a.href,
                }));

            // Buttons — broader selector for framework sites
            const btnSelectors = 'button, [role="button"], a[class*="btn"], a[class*="button"], a[class*="cta"]';
            const buttons = [...el.querySelectorAll(btnSelectors)]
                .filter(b => b.offsetWidth > 0 && b.innerText.trim())
                .slice(0, 5).map(b => {
                const bs = getComputedStyle(b);
                return {
                    text: b.innerText.trim().slice(0, 100),
                    bg: rgbToHex(bs.backgroundColor),
                    color: rgbToHex(bs.color),
                    border_radius: bs.borderRadius,
                    padding: bs.padding,
                };
            });

            // Also detect link-styled buttons (non-transparent bg + rounded)
            if (buttons.length === 0) {
                const linkButtons = [...el.querySelectorAll('a')].filter(a => {
                    if (!a.offsetWidth || !a.innerText.trim()) return false;
                    const s = getComputedStyle(a);
                    const hasBg = rgbToHex(s.backgroundColor) !== null;
                    const isRounded = s.borderRadius !== '0px';
                    return hasBg && isRounded;
                }).slice(0, 5).map(a => {
                    const as = getComputedStyle(a);
                    return {
                        text: a.innerText.trim().slice(0, 100),
                        href: a.href,
                        bg: rgbToHex(as.backgroundColor),
                        color: rgbToHex(as.color),
                        border_radius: as.borderRadius,
                        padding: as.padding,
                    };
                });
                buttons.push(...linkButtons);
            }

            // SVGs — broader search including inside divs, spans, use-references
            const svgEls = [...el.querySelectorAll('svg')].filter(s => {
                const r = s.getBoundingClientRect();
                return r.width > 5 && r.height > 5;
            });
            const svgs = svgEls.slice(0, 5).map((s, i) => ({
                id: s.id || s.getAttribute('aria-label') || s.closest('[aria-label]')?.getAttribute('aria-label') || `s${idx}-svg-${i}`,
                markup: s.outerHTML.slice(0, 2000),
                width: Math.round(s.getBoundingClientRect().width),
                height: Math.round(s.getBoundingClientRect().height),
            }));

            // Effective background: walk up tree
            const effectiveBg = getEffectiveBg(el);
            const gradient = getGradient(el);

            // Direct background image (non-gradient)
            const bgImg = cs.backgroundImage;
            const bgImageUrl = (bgImg && bgImg !== 'none' && !bgImg.includes('gradient'))
                ? bgImg.slice(0, 300) : null;

            // Parse actual URL from background-image: url('...')
            let bgImageParsedUrl = null;
            if (bgImageUrl) {
                const urlMatch = bgImageUrl.match(/url\\(["']?([^"')]+)["']?\\)/);
                if (urlMatch && urlMatch[1]) {
                    let parsed = urlMatch[1];
                    // Resolve relative URLs to absolute
                    if (parsed.startsWith('//')) parsed = 'https:' + parsed;
                    else if (parsed.startsWith('/')) parsed = location.origin + parsed;
                    else if (!parsed.startsWith('http') && !parsed.startsWith('data:')) parsed = location.origin + '/' + parsed;
                    bgImageParsedUrl = parsed;
                }
            }

            // === ORDERED ELEMENT STREAM ===
            // Walk the DOM in order to show Claude exactly what comes first/second/third
            // and how elements relate to each other (grouping via depth)
            const elements = [];
            const seenInStream = new Set();
            let lastHeadingText = null;
            let groupCounter = 0;

            // Detect SVG role (same logic as top-level extraction)
            function getSvgRole(svgNode) {
                const r = svgNode.getBoundingClientRect();
                if (svgNode.closest('nav, header, [class*="nav"]')) return 'logo';
                if (r.width <= 32 && r.height <= 32) return 'icon';
                if (svgNode.closest('button, a, [role="button"]')) return 'icon';
                return 'decorative';
            }

            // Detect container semantic type from className
            function getContainerType(node) {
                const cls = (node.className && typeof node.className === 'string') ? node.className.toLowerCase() : '';
                if (cls.match(/card/)) return 'card';
                if (cls.match(/list-item|list_item/)) return 'list-item';
                if (cls.match(/grid-item|grid_item|col-/)) return 'grid-item';
                if (cls.match(/column|col(?:\s|$)/)) return 'column';
                return 'group';
            }

            function walkDOM(node, depth, groupIndex, containerType) {
                if (elements.length >= 30) return;
                if (!node || !node.offsetWidth) return;
                if (depth > 6) return;

                const tag = node.tagName?.toLowerCase();
                if (!tag || tag === 'script' || tag === 'style' || tag === 'noscript') return;

                // Check for meaningful content elements
                if (tag.match(/^h[1-6]$/) && node.innerText.trim()) {
                    const headingText = node.innerText.trim().slice(0, 200);
                    lastHeadingText = headingText;
                    const hs = getComputedStyle(node);
                    elements.push({
                        type: 'heading',
                        tag: tag,
                        text: headingText,
                        color: rgbToHex(hs.color),
                        font_size: hs.fontSize,
                        depth: depth,
                        near_heading: lastHeadingText,
                        group_index: groupIndex,
                        container_type: containerType,
                    });
                    seenInStream.add(node);
                    return;
                }

                if (tag === 'p' && node.innerText.trim() && node.children.length <= 5) {
                    elements.push({
                        type: 'text',
                        text: node.innerText.trim().slice(0, 300),
                        color: rgbToHex(getComputedStyle(node).color),
                        depth: depth,
                        near_heading: lastHeadingText,
                        group_index: groupIndex,
                        container_type: containerType,
                    });
                    seenInStream.add(node);
                    return;
                }

                if (tag === 'img' && node.src && node.offsetWidth > 0) {
                    elements.push({
                        type: 'image',
                        url: node.src,
                        alt: node.alt || '',
                        role: getImageRole(node),
                        width: node.naturalWidth || node.offsetWidth,
                        height: node.naturalHeight || node.offsetHeight,
                        depth: depth,
                        near_heading: lastHeadingText,
                        group_index: groupIndex,
                        container_type: containerType,
                    });
                    seenInStream.add(node);
                    return;
                }

                if (tag === 'svg' && node.getBoundingClientRect().width > 5) {
                    elements.push({
                        type: 'svg',
                        markup: node.outerHTML.slice(0, 1500),
                        width: Math.round(node.getBoundingClientRect().width),
                        height: Math.round(node.getBoundingClientRect().height),
                        role: getSvgRole(node),
                        depth: depth,
                        near_heading: lastHeadingText,
                        group_index: groupIndex,
                        container_type: containerType,
                    });
                    seenInStream.add(node);
                    return;
                }

                if ((tag === 'button' || node.getAttribute('role') === 'button') && node.innerText.trim()) {
                    const bs = getComputedStyle(node);
                    elements.push({
                        type: 'button',
                        text: node.innerText.trim().slice(0, 100),
                        bg: rgbToHex(bs.backgroundColor),
                        color: rgbToHex(bs.color),
                        depth: depth,
                        near_heading: lastHeadingText,
                        group_index: groupIndex,
                        container_type: containerType,
                    });
                    seenInStream.add(node);
                    return;
                }

                if (tag === 'a' && node.href && node.innerText.trim()) {
                    const as = getComputedStyle(node);
                    const hasBg = rgbToHex(as.backgroundColor);
                    const isButton = hasBg && as.borderRadius !== '0px';
                    elements.push({
                        type: isButton ? 'button-link' : 'link',
                        text: node.innerText.trim().slice(0, 100),
                        href: node.href,
                        bg: hasBg,
                        color: rgbToHex(as.color),
                        depth: depth,
                        near_heading: lastHeadingText,
                        group_index: groupIndex,
                        container_type: containerType,
                    });
                    seenInStream.add(node);
                    // Don't return — might have child img/svg
                }

                // For container elements, check if it's a meaningful group (card, row, etc.)
                if (tag === 'div' || tag === 'section' || tag === 'article' || tag === 'li' || tag === 'span') {
                    const nodeCs = getComputedStyle(node);
                    const isFlexOrGrid = nodeCs.display === 'flex' || nodeCs.display === 'grid';
                    const hasBg = rgbToHex(nodeCs.backgroundColor);
                    const hasBorder = nodeCs.borderWidth !== '0px' && rgbToHex(nodeCs.borderColor);
                    const isCard = hasBg || hasBorder || (nodeCs.boxShadow && nodeCs.boxShadow !== 'none');

                    // Mark layout groups so Claude understands structure
                    if ((isFlexOrGrid || isCard) && node.children.length > 0 && node.children.length <= 15) {
                        const groupLayout = {};
                        if (isFlexOrGrid) groupLayout.display = nodeCs.display;
                        if (nodeCs.flexDirection !== 'row' && isFlexOrGrid) groupLayout.direction = nodeCs.flexDirection;
                        if (nodeCs.gap && nodeCs.gap !== 'normal' && nodeCs.gap !== '0px') groupLayout.gap = nodeCs.gap;
                        if (hasBg) groupLayout.bg = hasBg;
                        if (hasBorder) groupLayout.border = rgbToHex(nodeCs.borderColor);
                        if (nodeCs.borderRadius && nodeCs.borderRadius !== '0px') groupLayout.radius = nodeCs.borderRadius;

                        const cType = getContainerType(node);

                        if (Object.keys(groupLayout).length > 0) {
                            elements.push({
                                type: 'group-start',
                                layout: groupLayout,
                                container_type: cType,
                                depth: depth,
                            });
                        }

                        let childIndex = 0;
                        for (const child of node.children) {
                            walkDOM(child, depth + 1, childIndex, cType);
                            childIndex++;
                        }

                        if (Object.keys(groupLayout).length > 0) {
                            elements.push({ type: 'group-end', depth: depth });
                        }
                        return;
                    }
                }

                // Recurse into children
                for (const child of node.children) {
                    walkDOM(child, depth + 1, groupIndex, containerType);
                }
            }

            walkDOM(el, 0, null, null);

            return {
                index: idx,
                type: detectSectionType(el),
                tag: el.tagName.toLowerCase(),
                bounding_rect: {
                    top: Math.round(rect.top + window.scrollY),
                    left: Math.round(rect.left),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                },
                background_color: effectiveBg,
                gradient: gradient,
                background_image: bgImageUrl,
                background_image_url: bgImageParsedUrl,
                layout,
                elements,
                headings,
                paragraphs,
                images,
                links,
                buttons,
                svgs,
            };
        });
    }''')


async def extract_animations(page) -> dict:
    """
    Extract CSS animations, transitions, and animation library usage from the page.
    Also detects scroll-triggered animations by finding elements in their pre-animation
    state (opacity:0, transformed off-screen).
    """
    return await page.evaluate('''() => {
        const result = {
            keyframes: [],
            animated_elements: [],
            libraries_detected: [],
            scroll_triggers: [],
            scroll_animations: [],
        };

        // Extract @keyframes from stylesheets (skip internal/noise like cm-blink)
        const skipKeyframes = new Set(['cm-blink', 'cm-blink2']);
        for (const sheet of document.styleSheets) {
            try {
                for (const rule of sheet.cssRules) {
                    if (rule instanceof CSSKeyframesRule && !skipKeyframes.has(rule.name) && !rule.name.startsWith('sp-k-')) {
                        result.keyframes.push({
                            name: rule.name,
                            css: rule.cssText.slice(0, 1000),
                        });
                        if (result.keyframes.length >= 15) break;
                    }
                }
            } catch(e) {
                // Cross-origin stylesheets will throw
            }
            if (result.keyframes.length >= 15) break;
        }

        // Extract per-element animation and transition styles
        const allElements = document.querySelectorAll('*');
        for (const el of allElements) {
            if (result.animated_elements.length >= 30) break;
            const cs = getComputedStyle(el);
            const anim = cs.animationName;
            const trans = cs.transition;

            const hasAnimation = anim && anim !== 'none';
            const hasTransition = trans && trans !== 'all 0s ease 0s' && trans !== 'none 0s ease 0s' && trans !== 'none';

            if (hasAnimation || hasTransition) {
                const tag = el.tagName.toLowerCase();
                const cls = (el.className && typeof el.className === 'string') ? el.className.slice(0, 150) : '';
                result.animated_elements.push({
                    selector: tag + (cls ? '.' + cls.split(/\\s+/).join('.') : ''),
                    animation: hasAnimation ? cs.animation : null,
                    transition: hasTransition ? trans : null,
                });
            }
        }

        // Detect animation libraries
        // AOS
        const aosElements = document.querySelectorAll('[data-aos]');
        if (aosElements.length > 0) {
            result.libraries_detected.push('aos');
            for (const el of [...aosElements].slice(0, 10)) {
                result.scroll_triggers.push({
                    library: 'aos',
                    animation: el.getAttribute('data-aos'),
                    duration: el.getAttribute('data-aos-duration') || null,
                    delay: el.getAttribute('data-aos-delay') || null,
                    easing: el.getAttribute('data-aos-easing') || null,
                    selector_hint: el.tagName.toLowerCase() + (el.className ? '.' + (typeof el.className === 'string' ? el.className.split(/\\s+/)[0] : '') : ''),
                });
            }
        }

        // Animate.css
        if (document.querySelector('[class*="animate__"]')) {
            result.libraries_detected.push('animate.css');
        }

        // WOW.js
        if (document.querySelector('.wow')) {
            result.libraries_detected.push('wow.js');
        }

        // GSAP
        if (window.gsap || document.querySelector('[data-gsap]') || document.querySelector('.gsap-marker-start')) {
            result.libraries_detected.push('gsap');
        }

        // Framer Motion / Framer sites
        const hasFramer = document.querySelector('[data-framer-component-type]')
            || document.querySelector('[data-framer-appear-id]')
            || document.querySelector('[data-framer-name]')
            || document.querySelector('[style*="will-change: transform"]')
            || document.querySelector('script[src*="framer"]');
        if (hasFramer) {
            result.libraries_detected.push('framer-motion');
        }

        // Lottie
        if (window.lottie || document.querySelector('lottie-player') || document.querySelector('[data-lottie]')) {
            result.libraries_detected.push('lottie');
        }

        // Detect scroll-triggered animations by finding elements in pre-animation state.
        // Elements below the fold with opacity:0 or transform offsets are waiting for scroll.
        const viewportH = window.innerHeight;
        const sections = document.querySelectorAll('section, [class*="section"], [data-framer-name], main > div > div');
        const seen = new Set();

        for (const section of sections) {
            if (result.scroll_animations.length >= 25) break;
            const rect = section.getBoundingClientRect();
            const sectionY = rect.top + window.scrollY;

            // Check direct children and grandchildren for animated elements
            const candidates = section.querySelectorAll(':scope > *, :scope > * > *');
            for (const el of candidates) {
                if (result.scroll_animations.length >= 25) break;
                const cs = getComputedStyle(el);
                const elRect = el.getBoundingClientRect();

                // Skip elements in the viewport (already animated)
                if (elRect.top < viewportH && elRect.top > 0) continue;
                // Skip tiny elements
                if (elRect.width < 50 || elRect.height < 20) continue;

                const opacity = parseFloat(cs.opacity);
                const transform = cs.transform || cs.webkitTransform || '';

                // Detect pre-animation states
                let animationType = null;

                if (opacity < 0.1 && transform && transform !== 'none') {
                    // Opacity + transform = fade + slide/scale
                    const m = transform.match(/matrix\\(([^)]+)\\)/);
                    if (m) {
                        const vals = m[1].split(',').map(Number);
                        const tx = vals[4] || 0;
                        const ty = vals[5] || 0;
                        const scaleX = vals[0] || 1;
                        if (Math.abs(ty) > 10) animationType = ty > 0 ? 'fade-up' : 'fade-down';
                        else if (Math.abs(tx) > 10) animationType = tx > 0 ? 'fade-left' : 'fade-right';
                        else if (scaleX < 0.9) animationType = 'fade-scale';
                        else animationType = 'fade-in';
                    } else {
                        animationType = 'fade-in';
                    }
                } else if (opacity < 0.1) {
                    animationType = 'fade-in';
                } else if (transform && transform !== 'none') {
                    const m = transform.match(/matrix\\(([^)]+)\\)/);
                    if (m) {
                        const vals = m[1].split(',').map(Number);
                        const ty = vals[5] || 0;
                        const tx = vals[4] || 0;
                        if (Math.abs(ty) > 20) animationType = 'slide-up';
                        else if (Math.abs(tx) > 20) animationType = 'slide-right';
                    }
                }

                if (animationType) {
                    // Find which section index this belongs to
                    const key = animationType + ':' + Math.round(sectionY / 200);
                    if (seen.has(key)) continue;
                    seen.add(key);

                    const tag = el.tagName.toLowerCase();
                    const heading = section.querySelector('h1, h2, h3');
                    const sectionHint = heading ? heading.textContent.trim().slice(0, 60) : '';

                    result.scroll_animations.push({
                        type: animationType,
                        section_y: Math.round(sectionY),
                        section_hint: sectionHint,
                        element: tag,
                        delay_group: cs.transitionDelay || '0s',
                    });
                }
            }
        }

        // If we found scroll animations but no library was detected, infer framer-motion for Framer sites
        if (result.scroll_animations.length > 0 && result.libraries_detected.length === 0) {
            result.libraries_detected.push('scroll-animations');
        }

        return result;
    }''')


async def extract_ui_patterns(page) -> list:
    """
    Detect interactive UI patterns: carousels, tabs, accordions, dropdowns, modals.
    Uses DOM patterns + ARIA attributes to identify interactive components.
    """
    return await page.evaluate('''() => {
        const patterns = [];

        // --- Carousels / Sliders ---
        const carouselSelectors = [
            { sel: '.swiper', lib: 'swiper' },
            { sel: '.slick-slider', lib: 'slick' },
            { sel: '.carousel', lib: 'bootstrap' },
            { sel: '.embla', lib: 'embla' },
            { sel: '.flickity-slider', lib: 'flickity' },
            { sel: '.owl-carousel', lib: 'owl' },
            { sel: '.splide', lib: 'splide' },
            { sel: '[role="group"][aria-roledescription="slide"]', lib: 'aria' },
            { sel: '[class*="carousel"]', lib: 'unknown' },
            { sel: '[class*="slider"]', lib: 'unknown' },
        ];
        const carouselSeen = new Set();
        for (const { sel, lib } of carouselSelectors) {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                if (carouselSeen.has(el)) continue;
                carouselSeen.add(el);
                const heading = el.closest('section, [class*="section"]')?.querySelector('h1, h2, h3');
                const slides = el.querySelectorAll('[role="group"][aria-roledescription="slide"], .swiper-slide, .slick-slide, .carousel-item, .embla__slide, .splide__slide, .owl-item');
                patterns.push({
                    type: 'carousel',
                    library: lib,
                    count: Math.max(slides.length, 1),
                    section_hint: heading ? heading.textContent.trim().slice(0, 60) : '',
                });
            }
        }

        // --- Tabs ---
        const tablists = document.querySelectorAll('[role="tablist"]');
        for (const tl of tablists) {
            const tabs = tl.querySelectorAll('[role="tab"]');
            const heading = tl.closest('section, [class*="section"]')?.querySelector('h1, h2, h3');
            if (tabs.length > 0) {
                patterns.push({
                    type: 'tabs',
                    library: 'aria',
                    count: tabs.length,
                    section_hint: heading ? heading.textContent.trim().slice(0, 60) : '',
                });
            }
        }
        // Bootstrap tabs
        const bsTabs = document.querySelectorAll('[data-bs-toggle="tab"]');
        if (bsTabs.length > 0 && tablists.length === 0) {
            patterns.push({
                type: 'tabs',
                library: 'bootstrap',
                count: bsTabs.length,
                section_hint: '',
            });
        }

        // --- Accordions ---
        const accordionEls = document.querySelectorAll('[class*="accordion"]');
        const detailsEls = document.querySelectorAll('details');
        const bsCollapse = document.querySelectorAll('[data-bs-toggle="collapse"]');
        // Containers with multiple aria-expanded children
        const expandContainers = document.querySelectorAll('[aria-expanded]');

        if (accordionEls.length > 0) {
            const heading = accordionEls[0].closest('section, [class*="section"]')?.querySelector('h1, h2, h3');
            patterns.push({
                type: 'accordion',
                library: 'css-class',
                count: accordionEls.length,
                section_hint: heading ? heading.textContent.trim().slice(0, 60) : '',
            });
        } else if (detailsEls.length > 1) {
            const heading = detailsEls[0].closest('section, [class*="section"]')?.querySelector('h1, h2, h3');
            patterns.push({
                type: 'accordion',
                library: 'html-details',
                count: detailsEls.length,
                section_hint: heading ? heading.textContent.trim().slice(0, 60) : '',
            });
        } else if (bsCollapse.length > 1) {
            patterns.push({
                type: 'accordion',
                library: 'bootstrap',
                count: bsCollapse.length,
                section_hint: '',
            });
        } else if (expandContainers.length > 2) {
            // Group by parent container
            const parents = new Map();
            for (const el of expandContainers) {
                const parent = el.parentElement;
                if (parent) parents.set(parent, (parents.get(parent) || 0) + 1);
            }
            for (const [parent, count] of parents) {
                if (count >= 2) {
                    const heading = parent.closest('section, [class*="section"]')?.querySelector('h1, h2, h3');
                    patterns.push({
                        type: 'accordion',
                        library: 'aria-expanded',
                        count: count,
                        section_hint: heading ? heading.textContent.trim().slice(0, 60) : '',
                    });
                    break;
                }
            }
        }

        // --- Dropdowns ---
        const menuEls = document.querySelectorAll('[role="menu"]');
        const haspopupEls = document.querySelectorAll('[aria-haspopup]');
        const bsDropdowns = document.querySelectorAll('[data-bs-toggle="dropdown"]');
        const dropdownCount = Math.max(menuEls.length, haspopupEls.length, bsDropdowns.length);
        if (dropdownCount > 0) {
            patterns.push({
                type: 'dropdown',
                library: bsDropdowns.length > 0 ? 'bootstrap' : 'aria',
                count: dropdownCount,
                section_hint: '',
            });
        }

        // --- Modals ---
        const dialogEls = document.querySelectorAll('[role="dialog"]');
        const ariaModals = document.querySelectorAll('[aria-modal="true"]');
        const bsModals = document.querySelectorAll('[data-bs-toggle="modal"]');
        const modalCount = Math.max(dialogEls.length, ariaModals.length, bsModals.length);
        if (modalCount > 0) {
            patterns.push({
                type: 'modal',
                library: bsModals.length > 0 ? 'bootstrap' : 'aria',
                count: modalCount,
                section_hint: '',
            });
        }

        return patterns.slice(0, 20);
    }''')


async def extract_button_behaviors(page) -> list:
    """
    Classify what each interactive button does based on ARIA attributes
    and data attributes: toggle, open-modal, switch-tab, etc.
    """
    return await page.evaluate('''() => {
        const results = [];
        const seen = new Set();

        const els = document.querySelectorAll(
            'button, [role="button"], [aria-expanded], [aria-haspopup], [data-bs-toggle]'
        );

        for (const el of els) {
            if (!el.offsetWidth) continue;
            const text = (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 80);
            if (!text || seen.has(text)) continue;
            seen.add(text);

            let behavior = null;
            let controls = el.getAttribute('aria-controls') || null;

            const haspopup = el.getAttribute('aria-haspopup');
            const bsToggle = el.getAttribute('data-bs-toggle');
            const role = el.getAttribute('role');
            const ariaExpanded = el.getAttribute('aria-expanded');

            // Classification hierarchy
            if (haspopup === 'dialog' || bsToggle === 'modal') {
                behavior = 'opens-modal';
            } else if (haspopup === 'menu' || haspopup === 'listbox' || bsToggle === 'dropdown') {
                behavior = 'opens-dropdown';
            } else if (bsToggle === 'collapse' || el.closest('[class*="accordion"]')) {
                behavior = 'toggle-accordion';
            } else if (role === 'tab' || bsToggle === 'tab') {
                behavior = 'switch-tab';
            } else if (ariaExpanded !== null) {
                behavior = 'toggle';
            } else if (el.type === 'submit' || el.getAttribute('type') === 'submit') {
                behavior = 'submit-form';
            } else {
                // Check if it's really a link in disguise
                const href = el.getAttribute('href') || el.closest('a')?.getAttribute('href');
                if (href && href !== '#' && !href.startsWith('javascript:')) {
                    behavior = 'navigation';
                }
            }

            if (behavior) {
                results.push({ text, behavior, controls });
            }
        }

        return results.slice(0, 30);
    }''')


async def scrape_interactive_elements(page) -> list:
    """
    Detect and scrape all interactive UI patterns in ONE pass.
    No redundant scroll (prepare_page already scrolled).
    Single JS evaluate for ALL detection, then fast clicking.
    """
    import time as _time
    start = _time.time()

    # ── PHASE 1: Detect everything in ONE page.evaluate call ──
    detected = await page.evaluate('''() => {
        function uid(el) {
            if (el.id) return "#" + el.id;
            const p = [];
            while (el && el !== document.body) {
                let s = el.tagName.toLowerCase();
                if (el.id) { p.unshift("#" + el.id); break; }
                if (el.className && typeof el.className === "string") {
                    const c = el.className.split(" ").filter(x => x && x.length < 30 && !/[!:\[\]@#$%^(){}]|^framer-/.test(x)).slice(0,2);
                    if (c.length) s += "." + c.join(".");
                }
                const par = el.parentElement;
                if (par) {
                    const sibs = Array.from(par.children).filter(x => x.tagName === el.tagName);
                    if (sibs.length > 1) s += ":nth-child(" + (sibs.indexOf(el)+1) + ")";
                }
                p.unshift(s);
                el = par;
            }
            return p.join(" > ");
        }
        function isActive(el) {
            const s = getComputedStyle(el), c = el.className || "";
            return el.getAttribute("aria-selected")==="true" || el.getAttribute("data-state")==="active" ||
                   c.includes("active") || c.includes("selected") || c.includes("current") ||
                   (s.backgroundColor !== "rgba(0, 0, 0, 0)" && s.backgroundColor !== "transparent");
        }

        // ── TABS ──
        const tabGroups = [];
        const seenTabs = new Set();
        // ARIA tabs
        for (const tl of document.querySelectorAll('[role="tablist"]')) {
            const tabs = tl.querySelectorAll('[role="tab"]');
            if (tabs.length < 2) continue;
            const key = Array.from(tabs).map(t=>t.textContent.trim()).join("|");
            if (seenTabs.has(key)) continue; seenTabs.add(key);
            tabGroups.push({ tabs: Array.from(tabs).map(t=>({label:t.textContent.trim(),sel:uid(t),active:isActive(t),controls:t.getAttribute("aria-controls")||""})) });
        }
        // Class-based tabs
        for (const g of document.querySelectorAll('.tabs,.tab-list,.tab-nav,[class*="tab-list"],[class*="tabList"],[class*="TabList"],[class*="tab-nav"],[class*="tabNav"]')) {
            const btns = g.querySelectorAll('button,a,[role="tab"]');
            if (btns.length < 2 || btns.length > 10) continue;
            const key = Array.from(btns).map(b=>b.textContent.trim()).join("|");
            if (seenTabs.has(key)) continue; seenTabs.add(key);
            tabGroups.push({ tabs: Array.from(btns).map(b=>({label:b.textContent.trim(),sel:uid(b),active:isActive(b),controls:b.getAttribute("aria-controls")||""})) });
        }

        // ── ACCORDIONS ──
        const accItems = [];
        const seenQ = new Set();
        // Native details/summary
        for (const d of document.querySelectorAll("details")) {
            const s = d.querySelector("summary");
            if (!s) continue;
            const q = s.textContent.trim();
            if (seenQ.has(q)) continue; seenQ.add(q);
            accItems.push({q, sel:uid(s), open:d.hasAttribute("open"), answer:d.open?d.textContent.replace(s.textContent,"").trim():""});
        }
        // aria-expanded buttons (skip nav/header — those are dropdowns not FAQs)
        for (const btn of document.querySelectorAll('[aria-expanded],button[class*="accordion"],button[class*="Accordion"],button[class*="faq"],button[class*="FAQ"],[class*="accordion-trigger"],[class*="AccordionTrigger"]')) {
            if (btn.closest("nav,header,[class*=navbar],[class*=Navbar],[class*=nav-]")) continue;
            const q = btn.textContent.trim();
            if (q.length<6||q.length>200||seenQ.has(q)) continue; seenQ.add(q);
            accItems.push({q, sel:uid(btn), open:btn.getAttribute("aria-expanded")==="true", answer:""});
        }
        // Heuristic: FAQ sections
        for (const sec of document.querySelectorAll('[class*="faq"],[class*="FAQ"],[class*="accordion"],[class*="Accordion"]')) {
            for (const btn of sec.querySelectorAll('button,[role="button"],summary,dt')) {
                const q = btn.textContent.trim();
                if (q.length<10||q.length>300||seenQ.has(q)) continue; seenQ.add(q);
                accItems.push({q, sel:uid(btn), open:false, answer:""});
            }
        }

        // ── TOGGLES ──
        const toggleItems = [];
        for (const sw of document.querySelectorAll('[role="switch"],input[type="checkbox"][class*="toggle"],button[class*="toggle"],button[class*="Toggle"],button[class*="switch"],button[class*="Switch"],[class*="pricing-toggle"],[class*="PricingToggle"]')) {
            const par = sw.closest('.flex,.inline-flex,[class*="toggle"],[class*="pricing"]') || sw.parentElement;
            if (!par) continue;
            const labels = Array.from(par.querySelectorAll("span,label,p")).map(e=>e.textContent.trim()).filter(t=>t.length>0&&t.length<30);
            if (labels.length >= 2) toggleItems.push({sel:uid(sw), labels:labels.slice(0,2)});
        }

        // ── DROPDOWNS ──
        const dropItems = [];
        const seenDrop = new Set();
        for (const item of document.querySelectorAll("nav a,nav button,header a,header button,[class*=nav] a,[class*=nav] button")) {
            const hasTrigger = item.querySelector("svg") || item.hasAttribute("aria-expanded") || item.getAttribute("aria-haspopup")==="true";
            if (!hasTrigger) continue;
            const lbl = item.textContent.trim();
            if (!lbl || seenDrop.has(lbl)) continue; seenDrop.add(lbl);
            dropItems.push({lbl, sel:uid(item)});
        }

        // Count visible menus baseline for dropdown diffing
        const MENU_SEL = '[role="menu"],[class*="dropdown-menu"],[class*="submenu"],[class*="Dropdown"],[class*="SubMenu"],[class*="popover"],[class*="Popover"]';
        let visMenuCount = 0;
        for (const m of document.querySelectorAll(MENU_SEL)) {
            const s = getComputedStyle(m);
            if (s.display!=="none"&&s.visibility!=="hidden"&&s.opacity!=="0"&&m.offsetHeight>0) visMenuCount++;
        }

        return { tabGroups, accItems: accItems.slice(0,15), toggleItems: toggleItems.slice(0,3), dropItems: dropItems.slice(0,8), visMenuCount };
    }''')

    interactives = []

    # ── PHASE 2: Click tabs ──
    for group in detected.get("tabGroups", []):
        if _time.time() - start > 12:
            break
        tab_data = {"type": "tabs", "tabs": []}
        for t in group["tabs"]:
            try:
                await page.click(t["sel"], timeout=2000)
                await page.wait_for_timeout(250)
                panel = await page.evaluate('''(sel) => {
                    const tab = document.querySelector(sel);
                    if (!tab) return null;
                    const pid = tab.getAttribute("aria-controls");
                    if (pid) { const p = document.getElementById(pid); if (p) return {t:p.innerText.trim(),c:!!p.querySelector("pre,code")}; }
                    for (const p of document.querySelectorAll('[role="tabpanel"]')) { const s=getComputedStyle(p); if(s.display!=="none"&&s.visibility!=="hidden"&&p.offsetHeight>0) return {t:p.innerText.trim(),c:!!p.querySelector("pre,code")}; }
                    const tp = tab.closest('[role="tablist"]')||tab.parentElement; if(tp&&tp.nextElementSibling){const n=tp.nextElementSibling; return {t:n.innerText.trim(),c:!!n.querySelector("pre,code")};}
                    return null;
                }''', t["sel"])
                tab_data["tabs"].append({"label": t["label"], "is_active": t["active"], "content_text": (panel["t"][:1500] if panel else ""), "has_code": (panel.get("c", False) if panel else False)})
            except Exception:
                tab_data["tabs"].append({"label": t["label"], "is_active": t["active"], "content_text": ""})
        if sum(1 for x in tab_data["tabs"] if x.get("content_text")) >= 2:
            interactives.append(tab_data)

    # ── PHASE 3: Click accordions (fast — no closing, batch read) ──
    acc_items_raw = detected.get("accItems", [])
    if acc_items_raw:
        result_items = []
        for item in acc_items_raw:
            if _time.time() - start > 12:
                break
            if item.get("answer"):
                result_items.append({"question": item["q"], "answer": item["answer"][:1000]})
                continue
            try:
                await page.click(item["sel"], timeout=2000)
                await page.wait_for_timeout(200)
                answer = await page.evaluate('''(sel) => {
                    const btn = document.querySelector(sel); if (!btn) return "";
                    const cid = btn.getAttribute("aria-controls");
                    if (cid) { const p = document.getElementById(cid); if (p) return p.innerText.trim(); }
                    const par = btn.closest('[class*="accordion"],[class*="disclosure"],details,[class*="faq"]')||btn.parentElement;
                    if (par) { const a = par.innerText.trim().replace(btn.innerText.trim(),"").trim(); if (a.length>10) return a; }
                    const nx = btn.nextElementSibling; if (nx&&nx.offsetHeight>0) return nx.innerText.trim();
                    return "";
                }''', item["sel"])
                if answer:
                    result_items.append({"question": item["q"], "answer": answer[:1000]})
            except Exception:
                pass
        if len(result_items) >= 2:
            interactives.append({"type": "accordion", "items": result_items})

    # ── PHASE 4: Click toggles ──
    for toggle in detected.get("toggleItems", []):
        if _time.time() - start > 12:
            break
        try:
            state_a = await page.evaluate('''() => { const p=document.querySelector('[class*="pricing"],[class*="Pricing"],[class*="plans"],[class*="Plans"]'); return p?p.innerText.trim().substring(0,1500):""; }''')
            await page.click(toggle["sel"], timeout=2000)
            await page.wait_for_timeout(300)
            state_b = await page.evaluate('''() => { const p=document.querySelector('[class*="pricing"],[class*="Pricing"],[class*="plans"],[class*="Plans"]'); return p?p.innerText.trim().substring(0,1500):""; }''')
            if state_a != state_b:
                interactives.append({"type": "toggle", "labels": toggle["labels"], "states": {toggle["labels"][0]: {"content_text": state_a}, toggle["labels"][1]: {"content_text": state_b}}})
            try:
                await page.click(toggle["sel"], timeout=1000)
                await page.wait_for_timeout(150)
            except Exception:
                pass
        except Exception:
            pass

    # ── PHASE 5: Hover dropdowns ──
    baseline_menus = detected.get("visMenuCount", 0)
    for drop in detected.get("dropItems", []):
        if _time.time() - start > 12:
            break
        try:
            await page.hover(drop["sel"], timeout=1500)
            await page.wait_for_timeout(300)
            sub_links = await page.evaluate('''(args) => {
                const MSEL='[role="menu"],[class*="dropdown-menu"],[class*="submenu"],[class*="Dropdown"],[class*="SubMenu"],[class*="popover"],[class*="Popover"]';
                const vis=[]; for(const m of document.querySelectorAll(MSEL)){const s=getComputedStyle(m);if(s.display!=="none"&&s.visibility!=="hidden"&&s.opacity!=="0"&&m.offsetHeight>0)vis.push(m);}
                if(vis.length<=args.base) return [];
                for(const menu of vis.slice(args.base)){const lnk=Array.from(menu.querySelectorAll("a,button")).map(l=>({text:l.textContent.trim(),href:l.getAttribute("href")||""})).filter(l=>l.text.length>0&&l.text.length<100);if(lnk.length>=2)return lnk;}
                return [];
            }''', {"base": baseline_menus})
            if sub_links and len(sub_links) >= 2:
                interactives.append({"type": "dropdown", "trigger_label": drop["lbl"], "sub_links": sub_links[:15]})
            await page.mouse.move(0, 0)
            await page.wait_for_timeout(150)
        except Exception:
            pass

    return interactives


async def extract_react_info(page) -> dict:
    """
    Detect frontend framework, meta-framework, UI component libraries,
    and extract React component tree from fiber (if React is detected).
    """
    return await page.evaluate('''() => {
        const result = {
            framework: null,
            meta_framework: null,
            ui_library: null,
            ui_libraries: [],
            components: [],
        };

        // --- Framework detection ---
        if (window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || document.querySelector('[data-reactroot]') || document.querySelector('#__next')) {
            result.framework = 'react';
        } else if (window.__nuxt || window.__NUXT__) {
            result.framework = 'vue';
        } else if (document.querySelector('[ng-version]') || document.querySelector('[_nghost]')) {
            result.framework = 'angular';
        } else if (document.querySelector('[data-svelte-h]') || document.querySelector('[class*="svelte-"]')) {
            result.framework = 'svelte';
        }

        // --- Meta-framework detection ---
        if (window.__NEXT_DATA__ || document.querySelector('#__next')) {
            result.meta_framework = 'nextjs';
        } else if (window.__nuxt || window.__NUXT__) {
            result.meta_framework = 'nuxt';
        } else if (document.querySelector('meta[name="generator"][content*="Gatsby"]')) {
            result.meta_framework = 'gatsby';
        } else if (document.querySelector('meta[name="generator"][content*="Astro"]')) {
            result.meta_framework = 'astro';
        }

        // --- UI library detection ---
        const uiLibChecks = [
            { sel: '[data-radix-collection-item], [data-radix-popper-content-wrapper], [class*="radix-"]', name: 'radix' },
            { sel: '[data-headlessui-state], [data-headlessui-focus-guard]', name: 'headlessui' },
            { sel: '[class*="chakra-"]', name: 'chakra' },
            { sel: '[class*="MuiBox"], [class*="MuiButton"], [class*="MuiTypography"]', name: 'mui' },
            { sel: '[class*="ant-btn"], [class*="ant-layout"], [class*="ant-"]', name: 'antd' },
            { sel: '[class*="mantine-"]', name: 'mantine' },
        ];

        for (const { sel, name } of uiLibChecks) {
            try {
                if (document.querySelector(sel)) {
                    result.ui_libraries.push(name);
                    if (!result.ui_library) result.ui_library = name;
                }
            } catch(e) {}
        }

        // Check for shadcn (Radix + Tailwind)
        if (result.ui_libraries.includes('radix')) {
            const hasTailwind = document.querySelector('[class*="rounded-"], [class*="px-"], [class*="bg-"]');
            if (hasTailwind) {
                result.ui_libraries.push('shadcn');
                result.ui_library = 'shadcn';
            }
        }

        // --- React fiber walk (if React detected) ---
        if (result.framework === 'react') {
            try {
                const rootEl = document.querySelector('#__next') || document.querySelector('#root') || document.querySelector('[data-reactroot]');
                if (rootEl) {
                    // Find the fiber key
                    let fiberKey = null;
                    for (const key of Object.keys(rootEl)) {
                        if (key.startsWith('__reactFiber$') || key.startsWith('__reactInternalInstance$')) {
                            fiberKey = key;
                            break;
                        }
                    }

                    if (fiberKey) {
                        const seenNames = new Set();
                        const components = [];

                        function walkFiber(fiber, depth) {
                            if (!fiber || depth > 5 || components.length >= 30) return;

                            // Extract component name
                            if (fiber.type && typeof fiber.type === 'function') {
                                const name = fiber.type.displayName || fiber.type.name;
                                if (name && !name.startsWith('_') && name.length > 1 && !seenNames.has(name)) {
                                    seenNames.add(name);

                                    // Try to get section hint from DOM position
                                    let sectionHint = '';
                                    try {
                                        if (fiber.stateNode && fiber.stateNode.getBoundingClientRect) {
                                            const rect = fiber.stateNode.getBoundingClientRect();
                                            const y = rect.top + window.scrollY;
                                            // Find nearest heading
                                            const headings = document.querySelectorAll('h1, h2, h3');
                                            let closestH = null;
                                            let closestDist = Infinity;
                                            for (const h of headings) {
                                                const hy = h.getBoundingClientRect().top + window.scrollY;
                                                const dist = Math.abs(hy - y);
                                                if (dist < closestDist && dist < 300) {
                                                    closestDist = dist;
                                                    closestH = h;
                                                }
                                            }
                                            if (closestH) sectionHint = closestH.textContent.trim().slice(0, 60);
                                        }
                                    } catch(e) {}

                                    components.push({ name, section_hint: sectionHint });
                                }
                            }

                            // Walk child + sibling
                            walkFiber(fiber.child, depth + 1);
                            walkFiber(fiber.sibling, depth);
                        }

                        walkFiber(rootEl[fiberKey], 0);
                        result.components = components;
                    }
                }
            } catch(e) {
                // Fiber walk failed — that's fine, we still have framework + UI lib info
            }
        }

        return result;
    }''')


async def extract_snapshot(url: str) -> str:
    """
    Extract a clean, self-contained HTML snapshot of the rendered DOM.

    Loads the page, waits for full render, then:
    1. Inlines all stylesheet rules into <style> blocks
    2. Resolves all asset URLs to absolute
    3. Strips scripts (no reactivity needed)
    4. Removes hidden/overlay elements
    5. Returns a single HTML string ready to serve

    This bypasses AI entirely — the browser already rendered it perfectly.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                await browser.close()
                raise Exception(f"Failed to load {url}: {e}")

        # Clean up page (dismiss banners, unlock scroll, trigger lazy load)
        await prepare_page(page)
        await page.wait_for_timeout(2000)

        # Build the snapshot inside the browser
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        snapshot_html = await page.evaluate('''(baseUrl) => {
            // === 1. Collect all CSS rules into a single string ===
            let allCSS = '';
            for (const sheet of document.styleSheets) {
                try {
                    for (const rule of sheet.cssRules) {
                        allCSS += rule.cssText + '\\n';
                    }
                } catch(e) {
                    // Cross-origin sheet — keep the <link> tag (handled below)
                    if (sheet.href) {
                        allCSS += `/* cross-origin: ${sheet.href} */\\n`;
                    }
                }
            }

            // === 2. Clone the document to manipulate without affecting the live page ===
            const doc = document.cloneNode(true);

            // === 3. Strip all <script> tags ===
            doc.querySelectorAll('script').forEach(s => s.remove());

            // === 4. Strip noscript tags ===
            doc.querySelectorAll('noscript').forEach(s => s.remove());

            // === 5. Remove cookie banners, overlays, modals ===
            doc.querySelectorAll(
                '[class*="cookie"], [id*="cookie"], [class*="consent"], ' +
                '[class*="gdpr"], [class*="modal"], [class*="popup"]'
            ).forEach(el => {
                if (el.innerText && el.innerText.toLowerCase().match(/cookie|consent|privacy|gdpr/)) {
                    el.remove();
                }
            });

            // === 6. Remove existing <link rel="stylesheet"> for sheets we already inlined ===
            const inlinedSheets = new Set();
            for (const sheet of document.styleSheets) {
                try {
                    // If we could read it, we inlined it — remove the <link>
                    sheet.cssRules;
                    if (sheet.ownerNode) {
                        const tag = sheet.ownerNode.tagName?.toLowerCase();
                        if (tag === 'link' && sheet.href) {
                            inlinedSheets.add(sheet.href);
                        }
                    }
                } catch(e) {
                    // Cross-origin — keep the <link>
                }
            }

            // Remove inlined stylesheet <link> tags from the clone
            doc.querySelectorAll('link[rel="stylesheet"]').forEach(link => {
                if (inlinedSheets.has(link.href)) {
                    link.remove();
                }
            });

            // Also remove inline <style> tags (we're replacing them with our collected CSS)
            doc.querySelectorAll('style').forEach(s => s.remove());

            // === 7. Resolve relative URLs to absolute ===
            function resolveUrl(u) {
                if (!u || u.startsWith('data:') || u.startsWith('blob:') || u.startsWith('#')) return u;
                if (u.startsWith('//')) return 'https:' + u;
                if (u.startsWith('http://') || u.startsWith('https://')) return u;
                if (u.startsWith('/')) return baseUrl + u;
                return baseUrl + '/' + u;
            }

            doc.querySelectorAll('img[src]').forEach(img => {
                img.setAttribute('src', resolveUrl(img.getAttribute('src')));
                if (img.getAttribute('srcset')) {
                    img.setAttribute('srcset',
                        img.getAttribute('srcset').split(',').map(s => {
                            const parts = s.trim().split(/\\s+/);
                            parts[0] = resolveUrl(parts[0]);
                            return parts.join(' ');
                        }).join(', ')
                    );
                }
                // Remove lazy loading
                img.removeAttribute('loading');
                img.removeAttribute('data-src');
                img.removeAttribute('data-srcset');
            });

            doc.querySelectorAll('a[href]').forEach(a => {
                const href = a.getAttribute('href');
                if (href && !href.startsWith('#') && !href.startsWith('javascript:') && !href.startsWith('mailto:')) {
                    a.setAttribute('href', resolveUrl(href));
                }
            });

            doc.querySelectorAll('source[src], source[srcset]').forEach(s => {
                if (s.getAttribute('src')) s.setAttribute('src', resolveUrl(s.getAttribute('src')));
                if (s.getAttribute('srcset')) {
                    s.setAttribute('srcset',
                        s.getAttribute('srcset').split(',').map(part => {
                            const pieces = part.trim().split(/\\s+/);
                            pieces[0] = resolveUrl(pieces[0]);
                            return pieces.join(' ');
                        }).join(', ')
                    );
                }
            });

            doc.querySelectorAll('video[src], video[poster]').forEach(v => {
                if (v.getAttribute('src')) v.setAttribute('src', resolveUrl(v.getAttribute('src')));
                if (v.getAttribute('poster')) v.setAttribute('poster', resolveUrl(v.getAttribute('poster')));
            });

            // Resolve URLs in CSS (background-image, @font-face, etc.)
            allCSS = allCSS.replace(/url\\(["']?(?!data:)(?!blob:)(.*?)["']?\\)/g, (match, u) => {
                return 'url("' + resolveUrl(u) + '")';
            });

            // === 8. Inject our collected CSS as a single <style> block at the top of <head> ===
            const head = doc.querySelector('head') || doc.documentElement;
            const styleEl = doc.createElement('style');
            styleEl.setAttribute('data-snapshot', 'true');
            styleEl.textContent = allCSS;
            head.prepend(styleEl);

            // === 9. Add <base> tag as fallback for anything we missed ===
            const existingBase = doc.querySelector('base');
            if (!existingBase) {
                const base = doc.createElement('base');
                base.setAttribute('href', baseUrl + '/');
                head.prepend(base);
            }

            // === 10. Add meta viewport if missing ===
            if (!doc.querySelector('meta[name="viewport"]')) {
                const meta = doc.createElement('meta');
                meta.setAttribute('name', 'viewport');
                meta.setAttribute('content', 'width=device-width, initial-scale=1.0');
                head.prepend(meta);
            }

            // === 11. Add charset if missing ===
            if (!doc.querySelector('meta[charset]')) {
                const meta = doc.createElement('meta');
                meta.setAttribute('charset', 'UTF-8');
                head.prepend(meta);
            }

            return '<!DOCTYPE html>\\n' + doc.documentElement.outerHTML;
        }''', base_url)

        await browser.close()

    return snapshot_html


def guess_mime(url: str, category: str) -> str:
    """Guess MIME type from URL extension."""
    url_lower = url.lower().split("?")[0]
    if category == "image":
        if url_lower.endswith(".png"):
            return "image/png"
        if url_lower.endswith(".jpg") or url_lower.endswith(".jpeg"):
            return "image/jpeg"
        if url_lower.endswith(".gif"):
            return "image/gif"
        if url_lower.endswith(".svg"):
            return "image/svg+xml"
        if url_lower.endswith(".webp"):
            return "image/webp"
        if url_lower.endswith(".ico"):
            return "image/x-icon"
        return "image/unknown"
    if category == "font":
        if url_lower.endswith(".woff2"):
            return "font/woff2"
        if url_lower.endswith(".woff"):
            return "font/woff"
        if url_lower.endswith(".ttf"):
            return "font/ttf"
        if url_lower.endswith(".otf"):
            return "font/otf"
        return "font/unknown"
    return "unknown"


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
            if (!el) return;
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
        if (document.body) {
            document.body.classList.remove('no-scroll', 'overflow-hidden', 'modal-open');
        }
    }''')

    # Scroll to trigger lazy loading (capped to avoid infinite scroll pages)
    await page.evaluate('''async () => {
        await new Promise(resolve => {
            let total = 0;
            const distance = 400;
            const maxScroll = 15000;
            let iterations = 0;
            const maxIterations = 50;
            const timer = setInterval(() => {
                window.scrollBy(0, distance);
                total += distance;
                iterations++;
                if (total >= document.body.scrollHeight || total >= maxScroll || iterations >= maxIterations) {
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


async def extract_dom_skeleton(page) -> str:
    """
    Extract a simplified DOM tree with layout annotations.
    Produces a text representation like:
        <nav> flex row between items-center bg:#0a2540
          <div> flex row gap-8
            <a> Logo
            <a> Products
          <div> flex row gap-4
            <a> Sign in
            <button> Get Started bg:#635bff
        <section> flex col items-center py-96px bg:#ffffff
          <h1> text-64px bold
          <p> text-20px
          <div> grid cols-3 gap-24px
            <div> card border rounded-12px
    """
    return await page.evaluate('''() => {
        const MAX_DEPTH = 6;
        const MAX_NODES = 300;
        let nodeCount = 0;

        function rgbToHex(rgb) {
            if (!rgb || rgb === 'transparent' || rgb === 'rgba(0, 0, 0, 0)') return null;
            const match = rgb.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
            if (!match) return rgb;
            return '#' + [match[1], match[2], match[3]]
                .map(x => parseInt(x).toString(16).padStart(2, '0'))
                .join('');
        }

        function layoutAnnotation(el) {
            const s = getComputedStyle(el);
            const parts = [];

            // Display / layout
            if (s.display === 'flex' || s.display === 'inline-flex') {
                parts.push('flex');
                if (s.flexDirection === 'row') parts.push('row');
                else if (s.flexDirection === 'column') parts.push('col');
                if (s.justifyContent === 'space-between') parts.push('between');
                else if (s.justifyContent === 'center') parts.push('justify-center');
                if (s.alignItems === 'center') parts.push('items-center');
                if (s.gap && s.gap !== 'normal' && s.gap !== '0px') parts.push('gap-' + s.gap);
            } else if (s.display === 'grid') {
                parts.push('grid');
                const cols = s.gridTemplateColumns;
                if (cols) {
                    const colCount = cols.split(/\\s+/).length;
                    parts.push('cols-' + colCount);
                }
                if (s.gap && s.gap !== 'normal' && s.gap !== '0px') parts.push('gap-' + s.gap);
            }

            // Background
            const bg = rgbToHex(s.backgroundColor);
            if (bg) parts.push('bg:' + bg);

            // Padding (simplified)
            const py = parseInt(s.paddingTop) || 0;
            const px = parseInt(s.paddingLeft) || 0;
            if (py > 16) parts.push('py-' + py + 'px');
            if (px > 16) parts.push('px-' + px + 'px');

            // Max width
            const maxW = parseInt(s.maxWidth);
            if (maxW && maxW < 2000) parts.push('max-w-' + maxW + 'px');

            // Position
            if (s.position === 'sticky') parts.push('sticky');
            if (s.position === 'fixed') parts.push('fixed');

            return parts.join(' ');
        }

        function textAnnotation(el) {
            const s = getComputedStyle(el);
            const parts = [];
            const fs = parseInt(s.fontSize);
            if (fs) parts.push('text-' + fs + 'px');
            const fw = parseInt(s.fontWeight);
            if (fw >= 700) parts.push('bold');
            else if (fw >= 600) parts.push('semibold');
            else if (fw >= 500) parts.push('medium');
            const color = rgbToHex(s.color);
            if (color) parts.push(color);
            return parts.join(' ');
        }

        function walkNode(el, depth, lines) {
            if (nodeCount >= MAX_NODES || depth > MAX_DEPTH) return;
            if (!el.offsetWidth && el.tagName !== 'HEAD') return;

            const tag = el.tagName.toLowerCase();
            // Skip invisible/utility elements
            if (['script', 'style', 'noscript', 'link', 'meta', 'head', 'svg', 'path'].includes(tag)) return;

            nodeCount++;
            const indent = '  '.repeat(depth);
            const layout = layoutAnnotation(el);
            const isTextNode = ['h1','h2','h3','h4','h5','h6','p','span','a','button','label','li'].includes(tag);

            let annotation = '';
            if (layout) annotation = ' ' + layout;
            if (isTextNode) {
                const ta = textAnnotation(el);
                if (ta) annotation += ' ' + ta;
                const text = (el.innerText || '').trim().slice(0, 50);
                if (text && el.children.length === 0) {
                    annotation += ' "' + text + '"';
                }
            }

            lines.push(indent + '<' + tag + '>' + annotation);

            // Recurse into children
            for (const child of el.children) {
                walkNode(child, depth + 1, lines);
            }
        }

        const lines = [];
        // Start from top-level semantic elements
        const roots = document.querySelectorAll('body > *');
        for (const root of roots) {
            walkNode(root, 0, lines);
        }

        return lines.join('\\n');
    }''')


async def extract_background_images(page) -> list:
    """
    Extract CSS background-image URLs and gradient definitions from all elements.
    Returns a list of {element, url, gradient, position} dicts.
    """
    return await page.evaluate('''() => {
        const results = [];
        const seen = new Set();

        document.querySelectorAll('*').forEach(el => {
            if (!el.offsetWidth || el.offsetWidth < 50) return;

            const s = getComputedStyle(el);
            const bgImage = s.backgroundImage;

            if (!bgImage || bgImage === 'none') return;

            // Identify the element
            const tag = el.tagName.toLowerCase();
            const cls = (el.className && typeof el.className === 'string')
                ? el.className.split(' ').slice(0, 3).join(' ') : '';
            const id = el.id || '';
            const elementDesc = tag + (id ? '#' + id : '') + (cls ? '.' + cls.replace(/ /g, '.') : '');

            // Check for URL-based backgrounds
            const urlMatch = bgImage.match(/url\\(["']?([^"')]+)["']?\\)/);
            if (urlMatch) {
                const url = urlMatch[1];
                if (seen.has(url)) return;
                seen.add(url);
                results.push({
                    element: elementDesc,
                    url: url,
                    gradient: null,
                    size: s.backgroundSize || 'auto',
                    position: s.backgroundPosition || '0% 0%',
                    repeat: s.backgroundRepeat || 'repeat',
                });
                return;
            }

            // Check for gradients
            if (bgImage.includes('gradient')) {
                const gradientKey = bgImage.slice(0, 100);
                if (seen.has(gradientKey)) return;
                seen.add(gradientKey);
                results.push({
                    element: elementDesc,
                    url: null,
                    gradient: bgImage.slice(0, 300),
                    size: s.backgroundSize || 'auto',
                    position: s.backgroundPosition || '0% 0%',
                    repeat: s.backgroundRepeat || 'repeat',
                });
            }
        });

        return results.slice(0, 30);
    }''')
