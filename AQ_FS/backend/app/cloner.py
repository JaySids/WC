"""
Core website cloning engine.

Extracted from cli/clone.py — Playwright screenshots + OpenRouter LLM → HTML.
"""

import asyncio
import base64
import json
import os
import re
import time
from io import BytesIO
from urllib.parse import urlparse

import httpx
from PIL import Image
from playwright.async_api import async_playwright

from app.config import get_settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
FALLBACK_MODEL = "openai/gpt-4o"
LLM_TIMEOUT = 120.0
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def resize_if_needed(img_bytes: bytes, max_dim: int = 7000) -> bytes:
    """Resize image if either dimension exceeds max_dim. Returns PNG bytes."""
    img = Image.open(BytesIO(img_bytes))
    w, h = img.size
    if w <= max_dim and h <= max_dim:
        return img_bytes
    scale = max_dim / max(w, h)
    new_w, new_h = int(w * scale), int(h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _encode_image(data: bytes) -> str:
    """Base64-encode image bytes for OpenRouter."""
    b64 = base64.b64encode(data).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _strip_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    text = re.sub(r"^```(?:html|json|jsx|tsx|typescript)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text.strip())
    return text.strip()


# ---------------------------------------------------------------------------
# OpenRouter API
# ---------------------------------------------------------------------------

def _get_api_key() -> str:
    settings = get_settings()
    key = settings.openrouter_api_key
    if not key:
        key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_KEY")
    if not key:
        raise RuntimeError("OpenRouter API key not found. Set OPENROUTER_API_KEY in .env")
    return key


async def call_openrouter(
    images: list[bytes],
    user_text: str,
    system_prompt: str,
    model: str,
    max_tokens: int = 16000,
    temperature: float = 0.2,
) -> str:
    """Call OpenRouter with images + text, return raw content string."""
    api_key = _get_api_key()

    content = [{"type": "text", "text": user_text}]
    for img in images:
        img = resize_if_needed(img)
        content.append({
            "type": "image_url",
            "image_url": {"url": _encode_image(img)},
        })

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/website-cloner",
        "X-Title": "Website Cloner v3",
    }

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
        resp = await client.post(OPENROUTER_URL, headers=headers, json=body)

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(
            f"OpenRouter non-JSON response (HTTP {resp.status_code}): {resp.text[:300]}"
        )

    if resp.status_code != 200:
        err = data.get("error", {}).get("message", "") if isinstance(data, dict) else ""
        raise RuntimeError(f"OpenRouter API error ({resp.status_code}): {err or resp.text[:300]}")

    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"No choices in OpenRouter response")

    return choices[0]["message"]["content"]


# ---------------------------------------------------------------------------
# Page preparation JS
# ---------------------------------------------------------------------------

PREPARE_PAGE_JS = """
() => {
    const log = [];

    // 1. Dismiss cookie consent banners
    const consentBtnSelectors = [
        '[class*="cookie"] button', '[id*="cookie"] button',
        '[class*="consent"] button', '[id*="consent"] button',
        '[class*="gdpr"] button',
        '#onetrust-accept-btn-handler', '.onetrust-close-btn-handler',
        '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
        '#CybotCookiebotDialogBodyButtonAccept',
        '[data-testid="cookie-accept"]', '[data-action="accept-cookies"]',
        'button[aria-label*="accept" i]', 'button[aria-label*="agree" i]',
        'button[aria-label*="cookie" i]', 'button[aria-label*="dismiss" i]',
        'button[aria-label*="close" i][class*="cookie" i]',
        '.cc-accept', '.cc-dismiss', '.cc-btn',
        '.cookie-accept', '.cookie-close', '.cookie-dismiss',
        '.accept-cookies', '.accept-cookie',
    ];

    let cookieDismissed = false;
    for (const sel of consentBtnSelectors) {
        if (cookieDismissed) break;
        try {
            const btns = document.querySelectorAll(sel);
            for (const btn of btns) {
                const text = (btn.innerText || '').toLowerCase();
                if (text.match(/accept|agree|allow|ok|got it|dismiss|close|understand/)) {
                    btn.click();
                    log.push('Clicked cookie consent: ' + sel);
                    cookieDismissed = true;
                    break;
                }
            }
        } catch (e) {}
    }

    // Force-hide remaining cookie banners
    const bannerSelectors = [
        '#onetrust-banner-sdk', '#onetrust-consent-sdk',
        '#CybotCookiebotDialog', '#CybotCookiebotDialogBodyUnderlay',
        '.cc-window', '.cc-banner', '.cc-revoke',
        '[class*="cookie-banner"]', '[class*="cookieBanner"]',
        '[class*="cookie-consent"]', '[class*="cookieConsent"]',
        '[class*="cookie-notice"]', '[class*="cookieNotice"]',
        '[id*="cookie-banner"]', '[id*="cookieBanner"]',
        '[id*="cookie-consent"]', '[id*="cookieConsent"]',
        '[class*="gdpr"]', '[id*="gdpr"]',
    ];
    for (const sel of bannerSelectors) {
        try {
            document.querySelectorAll(sel).forEach(el => {
                el.style.display = 'none';
                el.style.visibility = 'hidden';
                log.push('Hid banner: ' + sel);
            });
        } catch (e) {}
    }

    // 2. Close popup overlays / modals
    const viewW = window.innerWidth;
    const viewH = window.innerHeight;
    for (const el of document.querySelectorAll('*')) {
        try {
            const style = getComputedStyle(el);
            const position = style.position;
            if (position !== 'fixed' && position !== 'sticky') continue;

            const rect = el.getBoundingClientRect();
            const zIndex = parseInt(style.zIndex) || 0;
            const tag = el.tagName.toLowerCase();
            const cls = (el.className || '').toString().toLowerCase();
            const id = (el.id || '').toLowerCase();
            const combined = tag + ' ' + cls + ' ' + id;

            // Skip real nav bars
            const isNav = (tag === 'nav' || tag === 'header' ||
                           combined.includes('navbar') || combined.includes('nav-bar') ||
                           combined.includes('navigation') || combined.includes('top-bar') ||
                           combined.includes('topbar') || combined.includes('site-header') ||
                           combined.includes('main-header'));
            if (isNav && rect.height < 120) continue;

            const coverageX = rect.width / viewW;
            const coverageY = rect.height / viewH;
            const isLargeOverlay = (coverageX > 0.5 && coverageY > 0.5 && zIndex > 100);
            const isModal = combined.match(/modal|popup|overlay|lightbox|dialog|interstitial|newsletter|subscribe|signup|sign-up|promo/);
            const isNotifBar = (coverageX > 0.8 && rect.height < 200 && rect.height > 20 &&
                                (rect.top < 10 || rect.bottom > viewH - 10) && zIndex > 50 &&
                                combined.match(/banner|notice|alert|notification|promo|announcement|bar/));

            if (isLargeOverlay || isModal || isNotifBar) {
                el.style.display = 'none';
                el.style.visibility = 'hidden';
                log.push('Removed overlay: ' + tag);
            }
        } catch (e) {}
    }

    // Remove backdrop elements
    for (const sel of ['.modal-backdrop', '.overlay-backdrop', '.modal-overlay',
                        '[class*="backdrop"]', '[class*="Backdrop"]']) {
        try {
            document.querySelectorAll(sel).forEach(el => {
                el.style.display = 'none';
            });
        } catch (e) {}
    }

    // Restore body scroll
    document.body.style.overflow = '';
    document.body.style.overflowY = '';
    document.documentElement.style.overflow = '';
    document.documentElement.style.overflowY = '';
    document.body.classList.remove('modal-open', 'no-scroll', 'overflow-hidden', 'is-locked');

    // 3. Disable CSS animations and transitions
    const freezeStyle = document.createElement('style');
    freezeStyle.textContent = `
        *, *::before, *::after {
            animation-duration: 0s !important;
            animation-delay: 0s !important;
            transition-duration: 0s !important;
            transition-delay: 0s !important;
            scroll-behavior: auto !important;
        }
    `;
    document.head.appendChild(freezeStyle);
    log.push('Disabled animations');

    return log;
}
"""

SCROLL_FOR_LAZY_LOAD_JS = """
async () => {
    const totalHeight = document.documentElement.scrollHeight;
    const viewportHeight = window.innerHeight;
    const step = Math.floor(viewportHeight * 0.8);
    let position = 0;

    while (position < totalHeight) {
        window.scrollTo(0, position);
        position += step;
        await new Promise(r => setTimeout(r, 300));
    }
    window.scrollTo(0, totalHeight);
    await new Promise(r => setTimeout(r, 500));
    window.scrollTo(0, 0);
    await new Promise(r => setTimeout(r, 300));

    return { scrolledTo: totalHeight, steps: Math.ceil(totalHeight / step) };
}
"""

WAIT_FOR_IMAGES_JS = """
() => {
    const imgs = [...document.images];
    const total = imgs.length;
    const loaded = imgs.filter(img => img.complete && img.naturalWidth > 0).length;
    return { total, loaded, pending: total - loaded };
}
"""


# ---------------------------------------------------------------------------
# Page data extraction JS
# ---------------------------------------------------------------------------

PAGE_DATA_EXTRACTION_JS = """
() => {
    const data = {};

    data.title = document.title || '';

    // Images with src, alt, dimensions (skip tiny ones)
    data.images = [...document.querySelectorAll('img[src]')].map(img => {
        const rect = img.getBoundingClientRect();
        if (rect.width < 10 || rect.height < 10) return null;
        let src = img.getAttribute('src') || '';
        try { src = new URL(src, document.location.href).href; } catch {}
        return {
            src: src,
            alt: img.getAttribute('alt') || '',
            width: Math.round(rect.width),
            height: Math.round(rect.height)
        };
    }).filter(Boolean);

    // Background images
    data.bgImages = [];
    const allEls = document.querySelectorAll('*');
    for (let i = 0; i < allEls.length && data.bgImages.length < 50; i++) {
        const bg = getComputedStyle(allEls[i]).backgroundImage;
        if (bg && bg !== 'none' && bg.includes('url(')) {
            const match = bg.match(/url\\(["']?([^"')]+)["']?\\)/);
            if (match) {
                let url = match[1];
                try { url = new URL(url, document.location.href).href; } catch {}
                if (!data.bgImages.includes(url)) {
                    data.bgImages.push(url);
                }
            }
        }
    }

    // Links
    data.links = [...document.querySelectorAll('a[href]')].map(a => {
        const text = (a.innerText || '').trim().substring(0, 100);
        let href = a.getAttribute('href') || '';
        try { href = new URL(href, document.location.href).href; } catch {}
        return { text, href };
    }).filter(l => l.text || l.href);

    // Font links (Google Fonts / Typekit)
    data.fontLinks = [];
    document.querySelectorAll('link[rel="stylesheet"]').forEach(link => {
        const href = link.getAttribute('href') || '';
        if (href.includes('fonts.googleapis.com') || href.includes('use.typekit.net')) {
            data.fontLinks.push(href);
        }
    });
    document.querySelectorAll('style').forEach(style => {
        const text = style.textContent || '';
        const imports = text.match(/@import\\s+url\\(["']?([^"')]+)["']?\\)/g);
        if (imports) {
            imports.forEach(imp => {
                const m = imp.match(/url\\(["']?([^"')]+)["']?\\)/);
                if (m && (m[1].includes('fonts.googleapis.com') || m[1].includes('use.typekit.net'))) {
                    data.fontLinks.push(m[1]);
                }
            });
        }
    });

    // Full body text
    data.bodyText = (document.body.innerText || '').substring(0, 10000);

    // Small SVG icons (10-200px)
    data.svgs = [];
    document.querySelectorAll('svg').forEach(svg => {
        if (data.svgs.length >= 20) return;
        const rect = svg.getBoundingClientRect();
        if (rect.width < 10 || rect.height < 10) return;
        if (rect.width > 200 || rect.height > 200) return;
        const markup = svg.outerHTML;
        if (markup.length > 2000) return;
        data.svgs.push({
            width: Math.round(rect.width),
            height: Math.round(rect.height),
            markup: markup
        });
    });

    data.pageHeight = document.documentElement.scrollHeight;
    data.pageWidth = document.documentElement.scrollWidth;

    return data;
}
"""


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

def _chunk_system_prompt(chunk_index: int, total_chunks: int) -> str:
    return f"""\
You have perfect vision and pay great attention to detail which makes you an expert at building single page apps using Tailwind, HTML and JS.

You will receive a screenshot of ONE SCROLL-VIEW of a webpage, plus extracted data (text, image URLs, link URLs, SVGs) from the full page. This is chunk {chunk_index + 1} of {total_chunks}.

Your task: produce the HTML + Tailwind CSS for EXACTLY what is visible in this screenshot.

CRITICAL RULES:

VISUAL ACCURACY:
- Make sure the output looks EXACTLY like the screenshot.
- Pay close attention to background color, text color, font size, font family, padding, margin, border, etc. Match the colors and sizes exactly.
- Match all spacing, layout, columns, alignment, centering, and element ordering precisely.
- Match decorative details: border radius, shadows, borders, opacity, gradients.

TEXT AND DATA:
- Use the EXACT text from the extracted data provided. Do not rephrase, shorten, or approximate any text.
- Use the EXACT image URLs from the extracted data. Never use placeholder images if real URLs are provided.
- Use the EXACT link hrefs from the extracted data.
- If SVG markup is provided for icons, use it directly inline.
- If an image URL is unavailable or broken, use https://placehold.co with appropriate dimensions and colors, and include a detailed description in the alt text.

COMPLETENESS:
- Do not add comments in the code such as "<!-- Add other navigation links as needed -->" and "<!-- ... other news items ... -->" in place of writing the full code. WRITE THE FULL CODE.
- Repeat elements as needed to match the screenshot. For example, if there are 15 items, the code should have 15 items. DO NOT LEAVE comments like "<!-- Repeat for each news item -->" or bad things will happen.
- Write EVERY element visible in the screenshot. Do not abbreviate or skip anything.

COLORS AND LINKS:
- A CSS color palette is provided in the data. Prefer those exact hex values over guessing from the screenshot.
- Link hrefs are provided. Use the real href for each link/button — do not use href="#" when a real URL exists.

OVERLAP HANDLING:
- There is a 200px overlap between adjacent chunks. Content at the very top of this screenshot may have already been generated by the previous chunk.
- If this is chunk 1: include everything, including the navigation/header.
- If this is chunk 2+: You will be given the last 30 lines of the previous chunk's HTML output. Do NOT regenerate any of that content. Start from the FIRST element that appears AFTER that content.
- Content at the bottom edge may be partially cut off — include it, the next chunk will complete it.

IMAGES:
- All images must use the CSS class "block" to prevent baseline whitespace gaps.
- All images must include max-w-full and h-auto for responsive sizing.
- When images are inside fixed-size containers, use object-cover with w-full h-full.

TAILWIND CSS:
- Use Tailwind utility classes for ALL styling.
- Use arbitrary values when needed: bg-[#0a2540], text-[18px], p-[22px], gap-[30px]
- For gradients: bg-gradient-to-r from-[#color1] to-[#color2]
- For complex shadows: shadow-[0_4px_12px_rgba(0,0,0,0.1)]

LIBRARIES AVAILABLE:
- Tailwind: <script src="https://cdn.tailwindcss.com"></script>
- Font Awesome: <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.3/css/all.min.css">
- Google Fonts (any)
- Use Font Awesome icons (<i class="fas fa-icon-name">) whenever you see icons in the screenshot.

OUTPUT:
- Output ONLY the raw HTML for this section.
- No <!DOCTYPE>, <html>, <head>, <body> wrapper tags.
- No markdown code fences or backticks.
- No explanations or comments.
- Start directly with the section HTML."""


SINGLE_SHOT_SYSTEM_PROMPT = """\
You have perfect vision and pay great attention to detail which makes you an expert at building single page apps using Tailwind, HTML and JS.

You will receive a screenshot of a complete webpage, plus extracted data (text, image URLs, link URLs, SVGs) from the page.

Your task: produce a complete HTML file that looks EXACTLY like the screenshot.

CRITICAL RULES:

VISUAL ACCURACY:
- Make sure the output looks EXACTLY like the screenshot.
- Pay close attention to background color, text color, font size, font family, padding, margin, border, etc. Match the colors and sizes exactly.
- Match all spacing, layout, columns, alignment, centering, and element ordering precisely.
- Match decorative details: border radius, shadows, borders, opacity, gradients.

COLORS AND LINKS:
- A CSS color palette is provided in the data. Prefer those exact hex values over guessing from the screenshot.
- Link hrefs are provided. Use the real href for each link/button — do not use href="#" when a real URL exists.

TEXT AND DATA:
- Use the EXACT text from the extracted data provided. Do not rephrase, shorten, or approximate any text.
- Use the EXACT image URLs from the extracted data. Never use placeholder images if real URLs are provided.
- Use the EXACT link hrefs from the extracted data.
- If SVG markup is provided for icons, use it directly inline.
- If an image URL is unavailable or broken, use https://placehold.co with appropriate dimensions and colors, and include a detailed description in the alt text.

COMPLETENESS:
- Do not add comments in the code such as "<!-- Add other navigation links as needed -->" and "<!-- ... other news items ... -->" in place of writing the full code. WRITE THE FULL CODE.
- Repeat elements as needed to match the screenshot. For example, if there are 15 items, the code should have 15 items. DO NOT LEAVE comments like "<!-- Repeat for each news item -->" or bad things will happen.
- Write EVERY element visible in the screenshot. Do not abbreviate or skip anything.

IMAGES:
- All images must use the CSS class "block" to prevent baseline whitespace gaps.
- All images must include max-w-full and h-auto for responsive sizing.
- When images are inside fixed-size containers, use object-cover with w-full h-full.

TAILWIND CSS:
- Use Tailwind utility classes for ALL styling.
- Use arbitrary values when needed: bg-[#0a2540], text-[18px], p-[22px], gap-[30px]
- For gradients: bg-gradient-to-r from-[#color1] to-[#color2]

LIBRARIES AVAILABLE:
- Tailwind: <script src="https://cdn.tailwindcss.com"></script>
- Font Awesome: <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.3/css/all.min.css">
- Google Fonts (any)

OUTPUT:
- Output a COMPLETE HTML file starting with <!DOCTYPE html>.
- Include Tailwind CDN script, Font Awesome link, and a CSS reset style block in <head>.
- No markdown code fences or backticks.
- No explanations or comments.
- Start directly with <!DOCTYPE html>."""


# ---------------------------------------------------------------------------
# Page preparation helpers
# ---------------------------------------------------------------------------

async def extract_theme(page) -> dict:
    """
    Extract exact CSS theme values from the live page using computed styles.
    Returns colors, fonts, spacing, and other design tokens.
    """
    theme = await page.evaluate(r'''() => {
        const result = {
            colors: {},
            fonts: {},
            backgrounds: [],
            buttons: [],
            links: []
        };

        // --- RGB to HEX conversion ---
        function rgbToHex(rgb) {
            if (!rgb || rgb === 'transparent') return null;
            const match = rgb.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
            if (!match) return rgb;
            return '#' + [match[1], match[2], match[3]]
                .map(x => parseInt(x).toString(16).padStart(2, '0'))
                .join('');
        }

        // --- COLORS ---
        const body = document.body;
        const bodyStyle = getComputedStyle(body);
        result.colors.body_bg = rgbToHex(bodyStyle.backgroundColor);
        result.colors.body_text = rgbToHex(bodyStyle.color);

        // Get all unique background colors from major sections
        const sections = document.querySelectorAll(
            'section, header, footer, nav, main, [class*="hero"], [class*="section"], ' +
            '[class*="banner"], [class*="container"], body > div > div'
        );
        const bgColors = new Set();
        const textColors = new Set();
        sections.forEach(el => {
            const s = getComputedStyle(el);
            if (s.backgroundColor && s.backgroundColor !== 'rgba(0, 0, 0, 0)' && s.backgroundColor !== 'transparent') {
                const hex = rgbToHex(s.backgroundColor);
                if (hex) bgColors.add(hex);
            }
            if (s.color) {
                const hex = rgbToHex(s.color);
                if (hex) textColors.add(hex);
            }
        });
        result.colors.backgrounds = [...bgColors].slice(0, 10);
        result.colors.text_colors = [...textColors].slice(0, 10);

        // Get heading colors specifically
        const headings = document.querySelectorAll('h1, h2, h3');
        const headingColors = new Set();
        headings.forEach(h => {
            const hex = rgbToHex(getComputedStyle(h).color);
            if (hex) headingColors.add(hex);
        });
        result.colors.heading_colors = [...headingColors].slice(0, 5);

        // --- FONTS ---
        result.fonts.body = bodyStyle.fontFamily;
        result.fonts.body_size = bodyStyle.fontSize;
        result.fonts.body_weight = bodyStyle.fontWeight;
        result.fonts.body_line_height = bodyStyle.lineHeight;

        // Heading fonts
        const h1 = document.querySelector('h1');
        if (h1) {
            const h1s = getComputedStyle(h1);
            result.fonts.heading = h1s.fontFamily;
            result.fonts.heading_size = h1s.fontSize;
            result.fonts.heading_weight = h1s.fontWeight;
            result.fonts.heading_line_height = h1s.lineHeight;
            result.fonts.heading_letter_spacing = h1s.letterSpacing;
        }

        // Detect Google Fonts links
        const fontLinks = [...document.querySelectorAll('link[href*="fonts.googleapis.com"]')];
        result.fonts.google_font_urls = fontLinks.map(l => l.href);

        // Detect @font-face declarations
        const sheets = [...document.styleSheets];
        const customFonts = new Set();
        sheets.forEach(sheet => {
            try {
                [...sheet.cssRules].forEach(rule => {
                    if (rule instanceof CSSFontFaceRule) {
                        customFonts.add(rule.style.fontFamily.replace(/['"]/g, ''));
                    }
                });
            } catch(e) {} // cross-origin stylesheets will throw
        });
        result.fonts.custom_fonts = [...customFonts];

        // --- BUTTONS ---
        const buttons = document.querySelectorAll('button, a[class*="btn"], a[class*="button"], [role="button"]');
        buttons.forEach(btn => {
            const s = getComputedStyle(btn);
            if (btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                result.buttons.push({
                    text: btn.innerText.trim().slice(0, 50),
                    bg: rgbToHex(s.backgroundColor),
                    color: rgbToHex(s.color),
                    border: s.border,
                    border_radius: s.borderRadius,
                    padding: s.padding,
                    font_size: s.fontSize,
                    font_weight: s.fontWeight,
                    href: btn.href || (btn.closest('a') ? btn.closest('a').href : null)
                });
            }
        });
        result.buttons = result.buttons.slice(0, 20);

        // --- LINKS ---
        const links = document.querySelectorAll('a[href]');
        links.forEach(a => {
            const s = getComputedStyle(a);
            if (a.offsetWidth > 0 && a.innerText.trim()) {
                result.links.push({
                    text: a.innerText.trim().slice(0, 80),
                    href: a.href,
                    color: rgbToHex(s.color),
                    is_nav: !!a.closest('nav, header, [class*="nav"]')
                });
            }
        });
        result.links = result.links.slice(0, 50);

        return result;
    }''')

    return theme


async def extract_clickables(page, base_url: str) -> dict:
    """Extract ALL clickable elements with their destinations."""
    clickables = await page.evaluate('''(baseUrl) => {
        const result = {
            nav_links: [],
            cta_buttons: [],
            footer_links: [],
            all_links: []
        };

        document.querySelectorAll('a[href]').forEach(a => {
            if (!a.offsetWidth || !a.innerText.trim()) return;

            let href = a.href;
            try { href = new URL(href, baseUrl).href; } catch(e) {}

            const entry = {
                text: a.innerText.trim().slice(0, 100),
                href: href,
                is_external: !href.startsWith(baseUrl),
                opens_new_tab: a.target === '_blank'
            };

            // Categorize
            if (a.closest('nav, header, [class*="nav"]')) {
                result.nav_links.push(entry);
            } else if (a.closest('footer, [class*="footer"]')) {
                result.footer_links.push(entry);
            }

            // Check if it looks like a CTA button
            const s = getComputedStyle(a);
            const looksLikeButton = (
                s.display === 'inline-flex' || s.display === 'flex' ||
                s.borderRadius !== '0px' ||
                (a.classList.toString().match && a.classList.toString().match(/btn|button|cta/i)) ||
                (s.backgroundColor !== 'rgba(0, 0, 0, 0)' && s.backgroundColor !== 'transparent')
            );
            if (looksLikeButton) {
                result.cta_buttons.push(entry);
            }

            result.all_links.push(entry);
        });

        // Also get <button> elements that might have onclick navigation
        document.querySelectorAll('button').forEach(btn => {
            if (!btn.offsetWidth || !btn.innerText.trim()) return;
            const parentLink = btn.closest('a');
            result.cta_buttons.push({
                text: btn.innerText.trim().slice(0, 100),
                href: parentLink ? parentLink.href : '#',
                is_external: false,
                opens_new_tab: false
            });
        });

        // Deduplicate
        const seen = new Set();
        result.all_links = result.all_links.filter(l => {
            const key = l.text + '|' + l.href;
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });

        return result;
    }''', base_url)

    return clickables


async def prepare_page(page) -> dict:
    """Clean up page: dismiss cookies, close popups, trigger lazy loading, wait for images."""
    summary = {"actions": [], "scroll": {}, "images": {}}

    # Dismiss cookies, close popups, disable animations
    try:
        actions = await page.evaluate(PREPARE_PAGE_JS)
        summary["actions"] = actions
    except Exception:
        pass

    await page.wait_for_timeout(500)

    # Scroll to trigger lazy-loaded content
    try:
        scroll_info = await page.evaluate(SCROLL_FOR_LAZY_LOAD_JS)
        summary["scroll"] = scroll_info
    except Exception:
        pass

    # Wait for images to load (max 5s)
    max_wait = 5
    waited = 0
    while waited < max_wait:
        try:
            img_status = await page.evaluate(WAIT_FOR_IMAGES_JS)
            summary["images"] = img_status
            if img_status.get("pending", 0) == 0:
                break
        except Exception:
            break
        await page.wait_for_timeout(500)
        waited += 0.5

    return summary


# ---------------------------------------------------------------------------
# Per-chunk generation
# ---------------------------------------------------------------------------

def _build_theme_context(theme: dict) -> str:
    """Build a compact theme hint — just colors and fonts, not prescriptive."""
    if not theme:
        return ""

    colors = theme.get("colors", {})
    fonts = theme.get("fonts", {})

    # Collect all unique hex colors into one short line
    all_colors = set()
    for c in colors.get("backgrounds", []):
        if c: all_colors.add(c)
    for c in colors.get("text_colors", []):
        if c: all_colors.add(c)
    for c in colors.get("heading_colors", []):
        if c: all_colors.add(c)
    if colors.get("body_bg"):
        all_colors.add(colors["body_bg"])
    if colors.get("body_text"):
        all_colors.add(colors["body_text"])

    parts = ["## CSS Color Palette (use these exact hex values instead of guessing from the screenshot)"]
    parts.append(f"Colors: {', '.join(sorted(all_colors))}")
    parts.append(f"Body: bg={colors.get('body_bg')} text={colors.get('body_text')}")

    if fonts.get("body"):
        parts.append(f"Body font: {fonts['body']}")
    if fonts.get("heading"):
        parts.append(f"Heading font: {fonts['heading']}")

    return "\n".join(parts)


def _build_clickables_context(clickables: dict) -> str:
    """Build a compact link-href mapping so the LLM uses real URLs."""
    if not clickables:
        return ""

    # Merge all links into one deduplicated list
    seen = set()
    entries = []
    for l in (clickables.get("nav_links", []) +
              clickables.get("cta_buttons", []) +
              clickables.get("footer_links", [])):
        key = l.get("text", "").strip()
        href = l.get("href", "")
        if not key or not href or href == "#" or key in seen:
            continue
        seen.add(key)
        entries.append(f"- \"{key}\" → {href}")

    if not entries:
        return ""

    return ("## Link URLs (use these real hrefs, never use href=\"#\" if a real URL is listed)\n"
            + "\n".join(entries[:25]))


async def generate_chunk(
    chunk_index: int,
    total_chunks: int,
    screenshot: bytes,
    page_data: dict,
    model: str,
    y_start: int,
    y_end: int,
    overlap_context: str = "",
    theme: dict = None,
    clickables: dict = None,
) -> str | None:
    """Generate HTML for a single viewport-scroll chunk. Returns HTML or None on failure."""

    theme_context = _build_theme_context(theme) if theme else ""
    clickables_context = _build_clickables_context(clickables) if clickables else ""

    parts = [
        f"Recreate this section of the page from the screenshot "
        f"(chunk {chunk_index + 1} of {total_chunks}, viewport at y={y_start}-{y_end}).\n",
        "Only recreate what is VISIBLE in the screenshot. "
        "Use the data below to find exact text, URLs, and links.\n",
    ]

    # Theme hint (compact — just colors and fonts)
    if theme_context:
        parts.append(theme_context)
        parts.append("")

    # Full page text so LLM gets exact wording
    if page_data.get("bodyText"):
        parts.append(f"## Full Page Text\n{page_data['bodyText']}\n")

    if page_data.get("images"):
        parts.append("## Images (use these exact URLs for any images you see)\n")
        for img in page_data["images"][:30]:
            parts.append(f"- src: {img['src']}  alt: {img.get('alt', '')}  "
                         f"({img.get('width', '?')}x{img.get('height', '?')})")
        parts.append("")

    if page_data.get("bgImages"):
        parts.append("## Background Images\n")
        for url in page_data["bgImages"][:10]:
            parts.append(f"- {url}")
        parts.append("")

    if page_data.get("links"):
        parts.append("## Links (use these exact hrefs)\n")
        for link in page_data["links"][:30]:
            parts.append(f"- text: \"{link['text']}\"  href: {link['href']}")
        parts.append("")

    # Clickable hrefs supplement (compact)
    if clickables_context:
        parts.append(clickables_context)
        parts.append("")

    if page_data.get("svgs"):
        parts.append("## SVG Icons (use these inline if you see matching icons)\n")
        for svg in page_data["svgs"][:10]:
            parts.append(f"- {svg['width']}x{svg['height']}: {svg['markup'][:200]}")
        parts.append("")

    # Overlap context from previous chunk
    if overlap_context:
        parts.append(overlap_context)

    user_text = "\n".join(parts)
    system_prompt = _chunk_system_prompt(chunk_index, total_chunks)

    try:
        raw = await call_openrouter(
            images=[screenshot],
            user_text=user_text,
            system_prompt=system_prompt,
            model=model,
            max_tokens=16000,
            temperature=0.2,
        )
    except Exception as e:
        print(f"Chunk {chunk_index} LLM call failed: {e}")
        return None

    content = _strip_fences(raw)

    # Strip text before first HTML tag
    first_tag = re.search(r"<[a-zA-Z]", content)
    if first_tag and first_tag.start() > 0:
        content = content[first_tag.start():]

    # Validate: must contain at least one HTML tag
    if not re.search(r"<[a-zA-Z][^>]*>", content):
        # Retry once
        try:
            raw = await call_openrouter(
                images=[screenshot],
                user_text=user_text + "\n\nIMPORTANT: Output ONLY raw HTML. No explanations.",
                system_prompt=system_prompt,
                model=model,
                max_tokens=16000,
                temperature=0.3,
            )
            content = _strip_fences(raw)
            first_tag = re.search(r"<[a-zA-Z]", content)
            if first_tag and first_tag.start() > 0:
                content = content[first_tag.start():]
        except Exception:
            return None

        if not re.search(r"<[a-zA-Z][^>]*>", content):
            return None

    return content


async def generate_single_shot(
    page_data: dict,
    full_screenshot: bytes,
    viewport_screenshot: bytes,
    model: str,
    theme: dict = None,
    clickables: dict = None,
) -> str:
    """Single-shot generation for short pages."""
    theme_context = _build_theme_context(theme) if theme else ""
    clickables_context = _build_clickables_context(clickables) if clickables else ""

    parts = [
        "Recreate this entire webpage from the screenshot.\n",
        f"## Page Title\n{page_data.get('title', '')}\n",
    ]

    if theme_context:
        parts.append(theme_context)
        parts.append("")

    if page_data.get("bodyText"):
        parts.append(f"## Full Page Text\n{page_data['bodyText']}\n")

    if page_data.get("images"):
        parts.append("## Images (use these exact URLs)\n")
        for img in page_data["images"][:30]:
            parts.append(f"- src: {img['src']}  alt: {img.get('alt', '')}  "
                         f"({img.get('width', '?')}x{img.get('height', '?')})")
        parts.append("")

    if page_data.get("bgImages"):
        parts.append("## Background Images\n")
        for url in page_data["bgImages"][:10]:
            parts.append(f"- {url}")
        parts.append("")

    if page_data.get("links"):
        parts.append("## Links (use these exact hrefs)\n")
        for link in page_data["links"][:30]:
            parts.append(f"- text: \"{link['text']}\"  href: {link['href']}")
        parts.append("")

    if clickables_context:
        parts.append(clickables_context)
        parts.append("")

    if page_data.get("fontLinks"):
        parts.append("## Font Links (include these in <head>)\n")
        for fl in page_data["fontLinks"]:
            parts.append(f"- {fl}")
        parts.append("")

    if page_data.get("svgs"):
        parts.append("## SVG Icons (use these inline if you see matching icons)\n")
        for svg in page_data["svgs"][:10]:
            parts.append(f"- {svg['width']}x{svg['height']}: {svg['markup'][:200]}")
        parts.append("")

    user_text = "\n".join(parts)

    raw = await call_openrouter(
        images=[full_screenshot, viewport_screenshot],
        user_text=user_text,
        system_prompt=SINGLE_SHOT_SYSTEM_PROMPT,
        model=model,
        max_tokens=16000,
        temperature=0.2,
    )

    content = _strip_fences(raw)

    # Validate HTML
    html_lower = content.lower().lstrip()
    if not (html_lower.startswith("<!doctype") or html_lower.startswith("<html")):
        # Retry with fallback model
        if model != FALLBACK_MODEL:
            raw = await call_openrouter(
                images=[full_screenshot, viewport_screenshot],
                user_text=user_text,
                system_prompt=SINGLE_SHOT_SYSTEM_PROMPT,
                model=FALLBACK_MODEL,
                max_tokens=16000,
                temperature=0.2,
            )
            content = _strip_fences(raw)

    return content


# ---------------------------------------------------------------------------
# HTML Assembly
# ---------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    """Remove all HTML tags, returning only visible text."""
    text = re.sub(r'<[^>]+>', ' ', html)
    return ' '.join(text.split()).strip().lower()


def _text_similarity(a: str, b: str) -> float:
    """Quick word-overlap similarity between two text strings (0-1)."""
    if not a or not b:
        return 0.0
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    return len(intersection) / min(len(words_a), len(words_b))


def _split_into_sections(html: str) -> list[str]:
    """
    Split HTML into top-level section blocks.
    Splits on <section>, <div class="..."> that are at root indent, <nav>, <header>, <footer>.
    Falls back to splitting on blank-line-separated blocks.
    """
    # Try splitting by <section> tags first
    parts = re.split(r'(?=<section\b)', html)
    if len(parts) > 2:
        return [p for p in parts if p.strip()]

    # Fallback: split on top-level semantic tags
    parts = re.split(r'(?=<(?:section|div|nav|header|footer|main)\b)', html)
    if len(parts) > 2:
        return [p for p in parts if p.strip()]

    # Last fallback: split on double newlines
    parts = re.split(r'\n\n+', html)
    return [p for p in parts if p.strip()]


def validate_and_deduplicate(chunk_htmls: list[str]) -> list[str]:
    """
    Post-process parallel-generated chunks to remove duplicate content
    caused by the 200px overlap between adjacent viewport screenshots.

    Strategy:
    1. Join all chunks into one HTML body string.
    2. Split into section-level blocks.
    3. Fingerprint each section by its TEXT CONTENT (ignoring tags/classes).
    4. If a section's text is >70% similar to an already-seen section, drop it.
    5. Return the cleaned HTML as a single-entry list.
    """
    if not chunk_htmls:
        return chunk_htmls

    combined = "\n\n".join(chunk_htmls)
    sections = _split_into_sections(combined)

    print(f"  Dedup: {len(sections)} sections found across {len(chunk_htmls)} chunks")

    kept = []
    seen_fingerprints = []  # list of text fingerprints we've already kept

    for section in sections:
        text = _strip_tags(section)

        # Skip very short sections (whitespace, single tags)
        if len(text) < 20:
            kept.append(section)
            continue

        # Check against all previously kept sections
        is_duplicate = False
        for seen_text in seen_fingerprints:
            sim = _text_similarity(text, seen_text)
            if sim > 0.7:
                is_duplicate = True
                print(f"  Dedup: dropped duplicate section ({sim:.0%} similar, {len(text)} chars text)")
                break

        if not is_duplicate:
            kept.append(section)
            seen_fingerprints.append(text)

    print(f"  Dedup: kept {len(kept)}/{len(sections)} sections")
    return ["\n\n".join(kept)]


def assemble_html(chunk_htmls: list[str], page_data: dict, theme: dict = None) -> str:
    """Combine chunk HTMLs into a final complete HTML document."""
    title = page_data.get("title", "Cloned Page")

    # Collect font links from page_data AND theme
    all_font_urls = set(page_data.get("fontLinks", []))
    if theme:
        for gu in theme.get("fonts", {}).get("google_font_urls", []):
            all_font_urls.add(gu)

    font_link_tags = ""
    for fl in all_font_urls:
        font_link_tags += f'    <link rel="stylesheet" href="{fl}">\n'

    body_content = "\n\n".join(chunk_htmls)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - Clone</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.15.3/css/all.min.css">
{font_link_tags}    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        img {{ display: block; max-width: 100%; height: auto; }}
        body {{ overflow-x: hidden; overflow-y: auto !important; }}
        html, body {{ height: auto !important; min-height: 100vh; }}
    </style>
</head>
<body>
{body_content}
</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Main clone function
# ---------------------------------------------------------------------------

async def clone_website(url: str, model: str = None) -> dict:
    """
    Main clone function. Returns:
    {
        "html": "<html>...</html>",
        "metadata": {
            "title": "...",
            "images": [...],
            "fonts": [...],
            "colors": [],
            "page_height": 1234,
            "num_chunks": 5,
            "model_used": "..."
        }
    }
    """
    settings = get_settings()
    gen_model = model or settings.default_model
    viewport_w = settings.viewport_width
    viewport_h = settings.viewport_height
    scroll_overlap = settings.scroll_overlap
    scroll_step = viewport_h - scroll_overlap

    theme = {}
    clickables = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": viewport_w, "height": viewport_h},
            user_agent=USER_AGENT,
        )
        page = await context.new_page()

        # Navigate
        try:
            await page.goto(url, wait_until="networkidle", timeout=settings.page_load_timeout)
        except Exception as e:
            # Try to continue even if networkidle times out
            print(f"Navigation warning (continuing): {e}")

        await page.wait_for_timeout(2000)

        # Prepare page (cookies, popups, lazy-load)
        await prepare_page(page)

        # Extract theme (exact CSS values) — BEFORE screenshots
        try:
            theme = await extract_theme(page)
            print(f"Theme extracted: {len(theme.get('colors', {}).get('backgrounds', []))} bg colors, "
                  f"{len(theme.get('buttons', []))} buttons, "
                  f"{len(theme.get('fonts', {}).get('google_font_urls', []))} google fonts")
        except Exception as e:
            print(f"Theme extraction failed (continuing): {e}")
            theme = {}

        # Extract clickable elements
        try:
            clickables = await extract_clickables(page, url)
            print(f"Clickables extracted: {len(clickables.get('nav_links', []))} nav links, "
                  f"{len(clickables.get('cta_buttons', []))} CTA buttons, "
                  f"{len(clickables.get('footer_links', []))} footer links")
        except Exception as e:
            print(f"Clickables extraction failed (continuing): {e}")
            clickables = {}

        # Extract page data
        try:
            page_data = await page.evaluate(PAGE_DATA_EXTRACTION_JS)
        except Exception:
            page_data = {
                "title": "", "images": [], "bgImages": [], "links": [],
                "fontLinks": [], "bodyText": "", "svgs": [],
                "pageHeight": viewport_h, "pageWidth": viewport_w,
            }

        # Full-page screenshot (for single-shot fallback)
        full_screenshot = await page.screenshot(type="png", full_page=True)
        viewport_screenshot = await page.screenshot(type="png", full_page=False)

        # Determine single-shot vs chunked
        page_height = await page.evaluate("document.body.scrollHeight")
        use_single_shot = page_height <= viewport_h

        chunk_screenshots = []
        max_chunks = 8  # Cap to avoid excessively long generation times

        if not use_single_shot:
            # Capture viewport-scroll chunks
            y = 0
            while y < page_height and len(chunk_screenshots) < max_chunks:
                y_end = min(y + viewport_h, page_height)
                await page.evaluate(f"window.scrollTo(0, {y})")
                await page.wait_for_timeout(300)
                try:
                    ss = await page.screenshot(type="png")
                    chunk_screenshots.append((ss, y, y_end))
                except Exception:
                    chunk_screenshots.append((None, y, y_end))
                y += scroll_step

        await browser.close()

    # LLM Generation
    if use_single_shot:
        generated = await generate_single_shot(
            page_data=page_data,
            full_screenshot=full_screenshot,
            viewport_screenshot=viewport_screenshot,
            model=gen_model,
            theme=theme,
            clickables=clickables,
        )
        html = generated
        num_chunks = 1
    else:
        total_chunks = len(chunk_screenshots)
        print(f"Generating {total_chunks} chunks in parallel...")

        # Generate ALL chunks in parallel for speed
        tasks = []
        for i, (ss, y_start, y_end) in enumerate(chunk_screenshots):
            if ss is None:
                tasks.append(asyncio.ensure_future(asyncio.coroutine(lambda: None)()))
                continue
            tasks.append(generate_chunk(
                chunk_index=i,
                total_chunks=total_chunks,
                screenshot=ss,
                page_data=page_data,
                model=gen_model,
                y_start=y_start,
                y_end=y_end,
                overlap_context="",  # no overlap context in parallel mode
                theme=theme,
                clickables=clickables,
            ))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        chunk_htmls = []
        for i, result in enumerate(results):
            if isinstance(result, str) and result:
                chunk_htmls.append(result)
                print(f"Chunk {i + 1}/{total_chunks}: generated ({len(result)} chars)")
            else:
                err = result if isinstance(result, Exception) else "empty"
                print(f"Chunk {i + 1}/{total_chunks}: failed ({err})")

        if not chunk_htmls:
            # Fallback to single-shot
            generated = await generate_single_shot(
                page_data=page_data,
                full_screenshot=full_screenshot,
                viewport_screenshot=viewport_screenshot,
                model=gen_model,
                theme=theme,
                clickables=clickables,
            )
            html = generated
            num_chunks = 1
        else:
            # Validation pass: deduplicate overlap between adjacent chunks
            print("Running validation / deduplication pass...")
            cleaned_chunks = validate_and_deduplicate(chunk_htmls)
            html = assemble_html(cleaned_chunks, page_data, theme=theme)
            num_chunks = len(chunk_htmls)

    # Extract theme colors for metadata
    theme_colors = []
    if theme:
        theme_colors = (
            theme.get("colors", {}).get("backgrounds", []) +
            theme.get("colors", {}).get("text_colors", [])
        )

    metadata = {
        "title": page_data.get("title", ""),
        "images": [img.get("src", "") for img in page_data.get("images", [])[:10]],
        "fonts": page_data.get("fontLinks", []),
        "colors": theme_colors[:10],
        "page_height": page_data.get("pageHeight", 0),
        "num_chunks": num_chunks,
        "model_used": gen_model,
    }

    return {"html": html, "metadata": metadata}
