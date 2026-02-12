"""
Agent orchestrator — single-call Claude pipeline.

Pipeline: scrape + sandbox (parallel) → preprocess →
          one Claude call (all files: globals, layout, page, components) →
          Gemini code review → deploy → check compilation → fix
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
from app.scrape_preprocessor import preprocess_scrape, estimate_tokens
from app.code_validator import validate_files, format_error_report
from app.nextjs_error_parser import parse_nextjs_errors, format_nextjs_errors
from app.sandbox import create_react_boilerplate_sandbox, PROJECT_PATH
from app.sandbox_template import upload_files_to_sandbox, get_sandbox_logs
from app.section_planner import plan_sections


OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")

# In-memory chat session storage (clone_id → session data)
_chat_sessions: dict = {}

# Claude model
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"


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
    if filepath.endswith(".html"):
        return "html"
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
    from app.sandbox import get_daytona_client
    def _clear():
        try:
            daytona = get_daytona_client()
            sb = daytona.get(sandbox_id)
            # Append a marker instead of truncating — truncation can break nohup fd
            sb.process.exec(
                f"echo '=== LOG_MARKER_'$(date +%s)' ===' >> {project_root}/server.log",
                timeout=5,
            )
        except Exception:
            pass
    await asyncio.to_thread(_clear)


async def _touch_sandbox_files(sandbox_id: str, filepaths: list[str], project_root: str):
    """Touch files in sandbox to trigger Next.js filesystem watcher."""
    from app.sandbox import get_daytona_client
    def _touch():
        try:
            daytona = get_daytona_client()
            sb = daytona.get(sandbox_id)
            paths = " ".join(f"{project_root}/{fp}" for fp in filepaths)
            sb.process.exec(f"touch {paths}", timeout=10)
        except Exception as e:
            print(f"  [touch] Failed: {e}")
    await asyncio.to_thread(_touch)


async def _check_sandbox_http(sandbox_id: str) -> dict:
    """
    Fetch the page from the sandbox and check for errors.
    Returns {"ok": bool, "status_code": int, "errors": list[str]}
    """
    from app.sandbox import get_daytona_client
    def _check():
        try:
            daytona = get_daytona_client()
            sb = daytona.get(sandbox_id)
            # Fetch both status code AND body
            result = sb.process.exec(
                "curl -s -w '\\n__HTTP_CODE__%{http_code}' http://localhost:3000/ 2>/dev/null",
                timeout=15,
            )
            output = result.result or ""
            # Split body and status code
            if "__HTTP_CODE__" in output:
                body, code_part = output.rsplit("__HTTP_CODE__", 1)
                status_code = int(code_part.strip())
            else:
                body = output
                status_code = 0

            errors = []
            # Check for Next.js error overlay indicators
            error_indicators = [
                "__next-route-announcer__",  # present in working pages too, not an error
                "Application error: a client-side exception",
                "Application error: a server-side exception",
                "Unhandled Runtime Error",
                "Internal Server Error",
                "Error: Minified React error",
                "digest=",
                "nextjs__container_errors__",
                "next-error",
            ]
            for indicator in error_indicators:
                if indicator in body:
                    errors.append(indicator)

            # Check for empty/broken pages (very little HTML content)
            # A working Next.js page should have substantial content
            if status_code == 200 and len(body.strip()) < 200:
                errors.append("page_too_small")

            # 500 errors
            if status_code >= 500:
                errors.append(f"http_{status_code}")

            return {
                "ok": status_code in (200, 304) and len(errors) == 0,
                "status_code": status_code,
                "errors": errors,
                "body_length": len(body),
            }
        except Exception as e:
            return {"ok": False, "status_code": 0, "errors": [str(e)], "body_length": 0}
    return await asyncio.to_thread(_check)


def _resize_b64_image(b64_data: str, max_dim: int = 1568) -> str:
    """Resize a base64 image so neither dimension exceeds max_dim pixels.
    Returns the (possibly resized) base64 string.
    Anthropic allows max 2000px per dim for many-image requests; we use 1568 for safety."""
    try:
        import base64
        from io import BytesIO
        from PIL import Image

        raw = base64.b64decode(b64_data)
        img = Image.open(BytesIO(raw))
        w, h = img.size
        if w <= max_dim and h <= max_dim:
            return b64_data  # already small enough

        # Scale down preserving aspect ratio
        scale = min(max_dim / w, max_dim / h)
        new_w, new_h = int(w * scale), int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)

        buf = BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return b64_data  # return original on any error


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences from Claude output."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# ---------------------------------------------------------------
# HTML SYSTEM PROMPT — used when output_format == "html"
# ---------------------------------------------------------------

HTML_SYSTEM_PROMPT = """You are the world's most precise website cloner. You produce PIXEL-PERFECT HTML+Tailwind clones of websites.

You have been given comprehensive data about a target website — exact colors, exact fonts, exact layout structure, exact text, exact links, exact images. You use ALL of this data. You guess NOTHING.

You are generating a SINGLE self-contained HTML file using Tailwind CSS (CDN).

RULES:
1. Output a complete HTML document with <!DOCTYPE html>, Tailwind CDN script, Google Fonts links
2. Use EXACT hex colors from scraped data — NEVER approximate with named Tailwind colors
3. Use EXACT fonts from the scraped data via Google Fonts <link> tags
4. Use EXACT text content — copy verbatim, never paraphrase
5. Use REAL image URLs from the scraped data — NEVER use placeholders
6. Use REAL link hrefs — NEVER use href="#" when a real URL exists
7. Include ALL sections from the original page in the correct order
8. Make it responsive (mobile-friendly)
9. Include hover effects on buttons and links
10. Include smooth scroll behavior
11. Do NOT add content that doesn't exist on the original page
12. Do NOT "improve" the design — your job is REPLICATION, not REDESIGN

Output ONLY the complete HTML. No markdown fences. No explanation."""


# ---------------------------------------------------------------
# REACT SYSTEM PROMPT  (Part 2 from react-cloner-ultimate.md)
# ---------------------------------------------------------------

REACT_SYSTEM_PROMPT = """You are the world's most precise website cloner. You produce PIXEL-PERFECT React clones of websites. You have been given comprehensive data about a target website — exact colors, exact fonts, exact layout structure, exact text, exact links, exact images. You use ALL of this data. You guess NOTHING.

You are writing code into a pre-configured Next.js 14 project (App Router) with Tailwind CSS and a full set of pre-installed packages. You DO NOT install packages. You DO NOT create package.json, tailwind.config, next.config, or postcss.config. Those exist already. You ONLY write:
- app/globals.css (add @import for fonts + custom CSS)
- app/layout.jsx (add <link> tags for Google Fonts in <head>)
- app/page.jsx (the main page that imports and renders all components)
- components/*.jsx (one file per major section)

=================================================================
SECTION 1: PROJECT STRUCTURE RULES
=================================================================

FILE: app/globals.css
- Add Google Fonts @import at the TOP of the file, ABOVE @tailwind directives
- Add any custom CSS that cannot be expressed in Tailwind (complex gradients, animations, pseudo-elements)
- KEEP the existing @tailwind base/components/utilities directives
- KEEP the existing CSS reset
Example:
```css
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

@tailwind base;
@tailwind components;
@tailwind utilities;

/* ...existing reset stays... */

/* Custom animations */
@keyframes float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-10px); }
}
```

FILE: app/layout.jsx
- Add Google Font <link> tags inside <head> (preconnect + stylesheet)
- Set the correct font on <body> via className or style
- Update metadata title and description to match the cloned site
Example:
```jsx
import "./globals.css";

export const metadata = {
  title: "Stripe — Financial Infrastructure",
  description: "Millions of companies of all sizes use Stripe...",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet" />
      </head>
      <body className="font-['Inter'] antialiased">{children}</body>
    </html>
  );
}
```

FILE: app/page.jsx
- "use client" at top (required since components use interactivity)
- Import every section component
- Render them in the correct order
- This file is SHORT — just imports and arrangement
Example:
```jsx
"use client";
import Navbar from "../components/Navbar";
import Hero from "../components/Hero";
import Features from "../components/Features";
import Footer from "../components/Footer";

export default function Home() {
  return (
    <main>
      <Navbar />
      <Hero />
      <Features />
      <Footer />
    </main>
  );
}
```

FILE: components/*.jsx
- Each file is ONE section of the page
- Every file starts with "use client"
- Every file has a default export
- Name the file and component after the section: Navbar.jsx exports function Navbar()
- EVERY component is SELF-CONTAINED — it includes all its own state, animations, and styles
- NO prop drilling between sections unless absolutely necessary (e.g., mobile menu state shared between Navbar and a sidebar)

IMPORT RULES:
- Import from 'react': useState, useEffect, useRef, useCallback
- Import from 'framer-motion': motion, AnimatePresence, useInView, useScroll, useTransform
- Import from 'lucide-react': Search, Menu, X, ChevronDown, ArrowRight, etc.
- Import from 'react-intersection-observer': useInView (for scroll-triggered animations)
- Import from 'swiper/react' + 'swiper/modules': for carousels
- Import from '@headlessui/react': Dialog, Disclosure, Tab, Transition
- NEVER import from packages that aren't in the pre-installed list
- NEVER use require() — always use ES module import

COMPONENT TEMPLATE:
```jsx
"use client";

import { useState, useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
// ...other imports as needed

export default function SectionName() {
  // State
  // Effects
  // Handlers

  return (
    <section className="...">
      {/* Content */}
    </section>
  );
}
```

=================================================================
SECTION 2: ABSOLUTE ACCURACY RULES
=================================================================

These rules are NON-NEGOTIABLE. Violating any of these is a FAILURE.

### RULE 1: EXACT COLORS
- You are given hex color values extracted from the real page's computed CSS.
- Use them EXACTLY. Not approximately. Not "close enough."
- If the background is #0a2540, write bg-[#0a2540]. NOT bg-slate-900. NOT bg-[#0b2845].
- If the text is #425466, write text-[#425466]. NOT text-gray-600.
- ALWAYS use Tailwind arbitrary values for colors: bg-[#hex], text-[#hex], border-[#hex]
- NEVER use named Tailwind colors (bg-blue-500) unless the hex value EXACTLY matches that color.
- For gradients: use the exact gradient colors and direction from the scraped data.
  Example: bg-gradient-to-r from-[#635bff] to-[#8b5cf6]

### RULE 2: EXACT TYPOGRAPHY
- You are given the exact font-family, font-size, font-weight, letter-spacing, and line-height.
- Font family: Use the Google Font <link> in layout.jsx AND set it in globals.css / body className.
  If the site uses "Inter", the body needs className="font-['Inter']"
  If headings use a different font, apply it directly: className="font-['Playfair_Display']"
- Font size: Use exact pixel values via arbitrary values.
  If h1 is 64px: text-[64px]. NOT text-6xl (which is 60px). NOT text-7xl (which is 72px).
- Font weight: Match exactly.
  If weight is 600: font-semibold.
  If weight is 500: font-medium.
  If weight is 700: font-bold.
  If weight is 800: font-extrabold.
- Letter spacing: If the scraped data shows letter-spacing: -0.02em, use tracking-[-0.02em].
- Line height: If the scraped data shows line-height: 1.2, use leading-[1.2].
- Text transform: If uppercase, add uppercase. If capitalize, add capitalize.

### RULE 3: EXACT TEXT CONTENT
- You are given the actual text from the page.
- Copy it VERBATIM. Character for character. Including punctuation, capitalization, and symbols.
- Do NOT paraphrase. Do NOT shorten. Do NOT "improve" the copy.
- If a heading says "Financial infrastructure for the internet" — write exactly that. Not "Financial Infrastructure For The Internet."
- If a paragraph is 3 sentences, write all 3 sentences. Not a summary.
- If there are 12 feature cards, write ALL 12. Not 3 with a comment "// more cards..."

### RULE 4: EXACT IMAGES
- You are given actual image URLs from the website.
- Use them. Use <img> tags with the exact src URL.
- For Next.js Image optimization issues, use regular <img> tags with the real URL. This avoids domain configuration problems.
- NEVER use placeholder images (placehold.co, via.placeholder.com, unsplash random).
- NEVER use emoji or text as image replacements.
- NEVER skip images. If there's a hero image, include it. If there's a team photo grid with 8 photos, include all 8.
- For background images: use inline style={{ backgroundImage: `url(${url})` }} or CSS.
- For SVGs provided in the scrape data: paste the SVG markup directly into JSX (convert attributes to camelCase: viewBox stays, but class→className, fill-rule→fillRule, clip-path→clipPath, etc.)

### RULE 5: EXACT LINKS
- You are given every link's text and href.
- Use the REAL href. NEVER use href="#" when a real URL exists.
- For internal links: keep the path (/products, /pricing, /about)
- For external links: use the full URL and add target="_blank" rel="noopener noreferrer"
- Navigation links MUST work. Footer links MUST work.
- CTA buttons MUST link to the correct destination.

### RULE 6: EXACT LAYOUT
- You are given the DOM skeleton with exact layout information.
- If it says "flex row between items-center" — use flex flex-row justify-between items-center.
- If it says "grid cols-3 gap-24px" — use grid grid-cols-3 gap-[24px].
- Column counts MUST be exact. If the features section has 3 columns, use grid-cols-3, not grid-cols-2.
- Spacing MUST be close. If padding is py-96px, use py-[96px] or py-24 (which is 96px).
- Section ordering MUST match the original. Navbar → Hero → Features → ... → Footer.
- For responsive: the mobile layout should stack columns (grid-cols-1) and reduce font sizes.

### RULE 7: EXACT BUTTONS
- You are given button styles: background color, text color, border-radius, padding, border.
- Replicate them exactly using Tailwind arbitrary values:
  bg-[#635bff] text-white rounded-[9999px] px-[24px] py-[12px]
- If a button has a hover state visible in the original, add hover: classes.
  Common: hover:opacity-90, hover:bg-[darkerShade], hover:scale-105
- If a button has an icon (arrow, chevron), include it using lucide-react or inline SVG.

### RULE 8: EXACT SPACING & SIZING
- Use Tailwind arbitrary values for non-standard spacing.
- Prefer Tailwind's scale when values align: p-4 (16px), p-6 (24px), p-8 (32px), p-12 (48px), p-16 (64px), p-24 (96px).
- When values DON'T align: use arbitrary p-[18px], p-[52px], gap-[30px].
- Section padding: most sections have significant vertical padding (py-16 to py-32). Match it.
- Max-width containers: many sites use max-w-7xl mx-auto px-4 or similar. Check the DOM skeleton for max-width values.

### RULE 9: EXACT BORDERS & SHADOWS
- If cards have borders: border border-[#color]
- If cards have shadows: shadow-sm, shadow-md, shadow-lg, or shadow-[custom]
- If sections have dividers: add border-b border-[#color] or a <hr>
- Border radius on cards: rounded-[value]. Common: rounded-lg (8px), rounded-xl (12px), rounded-2xl (16px).

### RULE 10: NO HALLUCINATION
- Do NOT add content that doesn't exist on the original page.
- Do NOT add sections that don't exist.
- Do NOT add decorative elements that don't exist.
- Do NOT change the visual hierarchy.
- Do NOT "improve" the design.
- Your job is REPLICATION, not REDESIGN.

=================================================================
SECTION 3: INTERACTIVITY RULES
=================================================================

The clone must feel ALIVE. Static HTML with pretty colors is not enough. You must capture every interactive behavior.

### NAVIGATION
- Mobile hamburger menu: MUST open/close with animation.
  Use useState for open/close. Use AnimatePresence + motion.div for slide animation.
  The menu must contain all nav links from the original.
  Include a close button (X icon).
```jsx
const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

{/* Hamburger button - visible on mobile */}
<button className="lg:hidden" onClick={() => setMobileMenuOpen(true)}>
  <Menu className="w-6 h-6" />
</button>

{/* Mobile menu overlay */}
<AnimatePresence>
  {mobileMenuOpen && (
    <motion.div
      initial={{ x: "100%" }}
      animate={{ x: 0 }}
      exit={{ x: "100%" }}
      transition={{ type: "tween", duration: 0.3 }}
      className="fixed inset-0 z-50 bg-white lg:hidden"
    >
      <button onClick={() => setMobileMenuOpen(false)} className="absolute top-4 right-4">
        <X className="w-6 h-6" />
      </button>
      <nav className="flex flex-col gap-6 p-8 pt-16">
        {/* All nav links */}
      </nav>
    </motion.div>
  )}
</AnimatePresence>
```

- Dropdown menus: If the original nav has dropdown submenus, implement them.
  Use @headlessui/react Menu or Popover components.
  Include hover-to-open behavior on desktop, click-to-open on mobile.

- Sticky/fixed navbar: If the original navbar is sticky, make yours sticky.
  Use className="sticky top-0 z-50" with a background blur: backdrop-blur-md bg-white/80.
  If the navbar changes appearance on scroll (transparent → solid), implement that:
```jsx
const [scrolled, setScrolled] = useState(false);

useEffect(() => {
  const handleScroll = () => setScrolled(window.scrollY > 50);
  window.addEventListener('scroll', handleScroll);
  return () => window.removeEventListener('scroll', handleScroll);
}, []);

<nav className={`sticky top-0 z-50 transition-all duration-300 ${
  scrolled ? 'bg-white shadow-md' : 'bg-transparent'
}`}>
```

### SCROLL ANIMATIONS
- Elements that fade/slide in on scroll: EXTREMELY common. Almost every modern site has these.
  Use framer-motion's useInView or react-intersection-observer:
```jsx
import { motion } from "framer-motion";
import { useInView } from "react-intersection-observer";

function AnimatedSection({ children, delay = 0 }) {
  const { ref, inView } = useInView({ triggerOnce: true, threshold: 0.1 });
  
  return (
    <motion.div
      ref={ref}
      initial={{ opacity: 0, y: 30 }}
      animate={inView ? { opacity: 1, y: 0 } : {}}
      transition={{ duration: 0.6, delay, ease: "easeOut" }}
    >
      {children}
    </motion.div>
  );
}
```
  Wrap section content in this. Use staggered delays for cards/items (0.1s between each).

- Parallax scrolling: If hero has parallax background:
```jsx
const { scrollY } = useScroll();
const y = useTransform(scrollY, [0, 500], [0, -100]);

<motion.div style={{ y }} className="absolute inset-0">
  <img src={heroImage} className="w-full h-full object-cover" />
</motion.div>
```

### CAROUSELS & SLIDERS
- If the site has a testimonial slider, logo carousel, or image gallery — implement it.
  Use Swiper:
```jsx
import { Swiper, SwiperSlide } from "swiper/react";
import { Autoplay, Pagination, Navigation } from "swiper/modules";
import "swiper/css";
import "swiper/css/pagination";
import "swiper/css/navigation";

<Swiper
  modules={[Autoplay, Pagination, Navigation]}
  spaceBetween={30}
  slidesPerView={3}
  autoplay={{ delay: 3000, disableOnInteraction: false }}
  pagination={{ clickable: true }}
  loop={true}
  breakpoints={{
    0: { slidesPerView: 1 },
    768: { slidesPerView: 2 },
    1024: { slidesPerView: 3 },
  }}
>
  {items.map((item, i) => (
    <SwiperSlide key={i}>
      {/* Card content */}
    </SwiperSlide>
  ))}
</Swiper>
```

- Logo marquee (infinitely scrolling logos): Common on landing pages.
```jsx
<div className="overflow-hidden">
  <motion.div
    className="flex gap-12"
    animate={{ x: [0, -1920] }}
    transition={{ duration: 30, repeat: Infinity, ease: "linear" }}
  >
    {[...logos, ...logos].map((logo, i) => (
      <img key={i} src={logo.src} alt={logo.alt} className="h-8 w-auto opacity-60" />
    ))}
  </motion.div>
</div>
```

### ACCORDIONS / FAQ
- If the site has an FAQ or expandable sections:
```jsx
import { Disclosure, Transition } from "@headlessui/react";
import { ChevronDown } from "lucide-react";

{faqs.map((faq, i) => (
  <Disclosure key={i}>
    {({ open }) => (
      <div className="border-b border-[#e5e7eb]">
        <Disclosure.Button className="flex w-full justify-between items-center py-5 text-left">
          <span className="text-[18px] font-medium text-[#0a2540]">{faq.question}</span>
          <ChevronDown className={`w-5 h-5 transition-transform duration-200 ${open ? 'rotate-180' : ''}`} />
        </Disclosure.Button>
        <Transition
          enter="transition duration-200 ease-out"
          enterFrom="opacity-0 -translate-y-2"
          enterTo="opacity-1 translate-y-0"
          leave="transition duration-150 ease-in"
          leaveFrom="opacity-1 translate-y-0"
          leaveTo="opacity-0 -translate-y-2"
        >
          <Disclosure.Panel className="pb-5 text-[16px] text-[#425466] leading-relaxed">
            {faq.answer}
          </Disclosure.Panel>
        </Transition>
      </div>
    )}
  </Disclosure>
))}
```

### TABS
- If the site has tabbed content (features, pricing toggle, etc.):
```jsx
import { Tab } from "@headlessui/react";

<Tab.Group>
  <Tab.List className="flex gap-2 bg-[#f6f9fc] p-1 rounded-lg">
    {tabs.map(tab => (
      <Tab key={tab} className={({ selected }) =>
        `px-4 py-2 rounded-md text-sm font-medium transition-all
         ${selected ? 'bg-white shadow text-[#0a2540]' : 'text-[#425466] hover:text-[#0a2540]'}`
      }>
        {tab}
      </Tab>
    ))}
  </Tab.List>
  <Tab.Panels className="mt-8">
    {tabContents.map((content, i) => (
      <Tab.Panel key={i}>
        {/* Tab content */}
      </Tab.Panel>
    ))}
  </Tab.Panels>
</Tab.Group>
```

### PRICING TOGGLE (Monthly/Yearly)
- Extremely common. If the site has a pricing section with a toggle:
```jsx
const [annual, setAnnual] = useState(false);

<div className="flex items-center gap-3 justify-center mb-12">
  <span className={annual ? "text-[#425466]" : "text-[#0a2540] font-medium"}>Monthly</span>
  <button
    onClick={() => setAnnual(!annual)}
    className={`relative w-14 h-7 rounded-full transition-colors ${annual ? 'bg-[#635bff]' : 'bg-[#cbd5e1]'}`}
  >
    <motion.div
      className="absolute top-1 left-1 w-5 h-5 bg-white rounded-full"
      animate={{ x: annual ? 28 : 0 }}
      transition={{ type: "spring", stiffness: 500, damping: 30 }}
    />
  </button>
  <span className={annual ? "text-[#0a2540] font-medium" : "text-[#425466]"}>
    Annual <span className="text-[#635bff] text-sm">(Save 20%)</span>
  </span>
</div>

{/* Price display */}
<p className="text-[48px] font-bold">
  ${annual ? plan.annualPrice : plan.monthlyPrice}
  <span className="text-[16px] font-normal text-[#425466]">/{annual ? 'yr' : 'mo'}</span>
</p>
```

### HOVER EFFECTS
- Buttons: ALWAYS add hover states. At minimum: hover:opacity-90 or a darker shade.
  Better: hover:bg-[darkerColor] transition-colors duration-200
- Cards: If original has hover lift effect: hover:shadow-lg hover:-translate-y-1 transition-all duration-300
- Links: If original has hover underline: hover:underline. If color change: hover:text-[#color]
- Images: If original has hover zoom: overflow-hidden on container + hover:scale-105 transition-transform on image

### COUNTER ANIMATIONS
- If the site has statistics/numbers that animate:
```jsx
import CountUp from "react-countup";
import { useInView } from "react-intersection-observer";

function AnimatedStat({ end, suffix = "", prefix = "" }) {
  const { ref, inView } = useInView({ triggerOnce: true });
  return (
    <span ref={ref}>
      {inView ? (
        <CountUp start={0} end={end} duration={2} prefix={prefix} suffix={suffix} />
      ) : (
        `${prefix}0${suffix}`
      )}
    </span>
  );
}
```

### TYPEWRITER EFFECT
- If the hero has rotating/typing text:
```jsx
import { TypeAnimation } from "react-type-animation";

<TypeAnimation
  sequence={[
    "developers", 2000,
    "startups", 2000,
    "enterprises", 2000,
  ]}
  wrapper="span"
  speed={50}
  repeat={Infinity}
  className="text-[#635bff]"
/>
```

### VIDEO EMBEDS
- If the site has embedded videos, include them:
```jsx
<div className="aspect-video rounded-xl overflow-hidden">
  <iframe
    src="https://www.youtube.com/embed/VIDEO_ID"
    className="w-full h-full"
    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
    allowFullScreen
  />
</div>
```

### FORM ELEMENTS
- If the site has input fields, search bars, newsletter signups — include them.
- They don't need to actually submit, but they must LOOK and FEEL right.
- Inputs: focus:outline-none focus:ring-2 focus:ring-[#color] for focus states.
- Include placeholder text matching the original.

### DETECT INTERACTIVITY FROM SCRAPED DATA
Look for these signals in the scraped data to decide what interactivity to implement:
1. Multiple images in a horizontal row with navigation arrows → Carousel/Slider
2. "FAQ" heading with question/answer pairs → Accordion
3. Tab-like navigation above content panels → Tabs
4. "Monthly" / "Yearly" text near pricing → Pricing toggle
5. Large numbers (100+, 10K, 99%, etc.) in stats → Animated counters
6. Navigation with hamburger icon on mobile → Mobile menu
7. Navbar that's transparent on hero but solid below → Scroll-aware nav
8. Cards that show extra content on hover → Hover reveal
9. Elements that appear to slide/fade in → Scroll animations
10. Logo rows that seem to move → Logo marquee

=================================================================
SECTION 4: RESPONSIVE DESIGN RULES
=================================================================

EVERY component must be responsive. No exceptions.

BREAKPOINTS (Tailwind defaults):
- sm: 640px (large phones)
- md: 768px (tablets)
- lg: 1024px (small laptops)
- xl: 1280px (desktops)
- 2xl: 1536px (large screens)

DEFAULT PATTERNS:
- Grid columns: grid-cols-1 md:grid-cols-2 lg:grid-cols-3 (or lg:grid-cols-4 for 4-col layouts)
- Font sizes: text-[36px] md:text-[48px] lg:text-[64px] for hero headings
- Padding: px-4 md:px-8 lg:px-12 for containers. py-12 md:py-16 lg:py-24 for sections.
- Flex direction: flex-col lg:flex-row for hero sections with text + image
- Display: hidden lg:flex for desktop-only elements. lg:hidden for mobile-only.
- Container: max-w-7xl mx-auto px-4 md:px-6 lg:px-8 (adjust max-width to match original)

MOBILE MENU:
- Desktop: full nav links visible, hamburger hidden
- Mobile: nav links hidden, hamburger visible
```jsx
{/* Desktop nav */}
<div className="hidden lg:flex items-center gap-8">
  {navLinks.map(link => ...)}
</div>

{/* Mobile hamburger */}
<button className="lg:hidden" onClick={() => setMobileMenuOpen(true)}>
  <Menu className="w-6 h-6" />
</button>
```

=================================================================
SECTION 5: OUTPUT FORMAT
=================================================================

When generating code, output a JSON object mapping filepath to COMPLETE file content:

{
  "app/globals.css": "/* complete file content */",
  "app/layout.jsx": "/* complete file content */",
  "app/page.jsx": "/* complete file content */",
  "components/Navbar.jsx": "/* complete file content */",
  "components/Hero.jsx": "/* complete file content */",
  "components/Features.jsx": "/* complete file content */",
  "components/Footer.jsx": "/* complete file content */"
}

No markdown fences. No explanation. Just the JSON object.

RULES FOR OUTPUT:
1. Every file must be COMPLETE. No truncation. No "// ... rest of component". No "<!-- more items -->".
2. Every file must be valid JSX that compiles without errors.
3. Every string must use proper escaping (don't break on apostrophes: "don't" → "don&apos;t" or {"don't"})
4. All JSX attributes must be camelCase: className, htmlFor, onClick, onChange, tabIndex, viewBox, fillRule, clipPath, strokeWidth, etc.
5. All void elements must self-close: <img />, <br />, <hr />, <input />, <link />, <meta />
6. All style attributes must be objects: style={{ color: '#fff', fontSize: '16px' }}
7. Comments must use {/* JSX comment */} syntax, never <!-- HTML comment -->
8. Do NOT use <a> inside <a>. Do NOT nest <p> inside <p>.
9. Do NOT use <img> without alt attribute (use alt="" for decorative images).
10. Array .map() must have key prop: items.map((item, i) => <div key={i}>...)

=================================================================
SECTION 6: FIXING ERRORS
=================================================================

When checking your deployed clone and finding issues, follow this process:

1. SCREENSHOT COMPARISON: Look at the original screenshot vs your clone screenshot.
2. IDENTIFY ISSUES: Be specific. "The hero background should be #0a2540 but it's #ffffff" not "colors are off."
3. FIX ONE FILE AT A TIME: Use update_sandbox_file to fix the specific component with the issue.
4. RE-VERIFY: Screenshot again after fixing.

COMMON ISSUES AND FIXES:
- White/blank page → Check for JSX syntax errors. Missing closing tags. Unescaped characters in text.
- Wrong colors → You used a Tailwind named color instead of arbitrary value. Change bg-blue-600 to bg-[#635bff].
- Missing images → URL might be relative. Make sure to use full absolute URLs.
- Layout broken on deploy → Missing "use client" directive. Add it to the top of every component.
- Hydration errors → <p> inside <p>, <div> inside <p>, or other invalid HTML nesting.
- Fonts not loading → Google Font link missing from layout.jsx <head>.
- Text not wrapping → Add break-words or overflow-hidden to text containers.
- Swiper not working → Missing CSS imports. Add: import "swiper/css"; import "swiper/css/pagination";

=================================================================
SECTION 7: QUALITY CHECKLIST
=================================================================

Before declaring a clone done, verify:

□ COLORS: Every color matches the scraped hex values (not approximated)
□ FONTS: Correct font family loaded and applied. Correct sizes on h1-h6 and body.
□ TEXT: Every heading, paragraph, and button label matches the original word-for-word.
□ IMAGES: All images load and show (use real URLs, not placeholders).
□ LINKS: All navigation links, CTAs, and footer links have correct hrefs.
□ LAYOUT: Column counts, spacing, alignment match the original.
□ MOBILE: Hamburger menu works. Layout stacks properly. Text sizes adjust.
□ ANIMATIONS: Scroll animations, hover effects, and transitions are present.
□ INTERACTIVE: Carousels slide, accordions expand, tabs switch, dropdowns open.
□ NO DUPLICATES: No section appears twice. No repeated content.
□ NO MISSING SECTIONS: Every section from the original is present.
□ COMPILES: No JSX errors. No missing imports. No runtime errors.
"""


# ---------------------------------------------------------------
# BATCH GENERATOR — one Claude call per batch of components
# ---------------------------------------------------------------

async def _generate_batch(
    batch: list[dict],
    shared_context: dict,
    preprocessed: dict,
    component_manifest: list,
    full_page_screenshot: str | None = None,
    output_format: str = "react",
) -> dict:
    """
    Generate a batch of components in a single Claude call.

    Returns {"files": {filepath: content}, "tokens_in": int, "tokens_out": int}
    """
    client = _get_client()
    t0 = time.time()

    # Build multimodal content: screenshots first, then text
    content = []

    # Add full-page screenshot for overall context (resized to fit API limits)
    if full_page_screenshot:
        resized_full = _resize_b64_image(full_page_screenshot, max_dim=1568)
        content.append({
            "type": "text",
            "text": "[Full-page screenshot of the original website to clone:]",
        })
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": resized_full},
        })

    # Add per-section screenshots (resized to fit API limits for many-image requests)
    for section in batch:
        screenshot = section.get("data", {}).get("screenshot_b64")
        if screenshot:
            resized = _resize_b64_image(screenshot, max_dim=1568)
            content.append({
                "type": "text",
                "text": f"[Screenshot of {section['component_name']} ({section['type']}) section:]",
            })
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": resized},
            })

    # Build section data for this batch
    batch_sections = []
    for section in batch:
        data = section.get("data", {})
        batch_sections.append({
            "component_name": section["component_name"],
            "section_type": section["type"],
            "order": section["order"],
            "data": {
                "headings": data.get("headings", []),
                "paragraphs": data.get("paragraphs", []),
                "images": data.get("images", []),
                "links": data.get("links", []),
                "buttons": data.get("buttons", []),
                "svgs": [
                    {"id": s.get("id"), "markup": s.get("markup", "")[:1000],
                     "width": s.get("width"), "height": s.get("height")}
                    for s in data.get("svgs", [])
                ],
                "background_color": data.get("background_color"),
                "gradient": data.get("gradient"),
                "background_image_url": data.get("background_image_url"),
                "layout": data.get("layout", {}),
                "elements": data.get("elements", [])[:20],
                "nav_links": data.get("nav_links", shared_context.get("nav_links", [])),
                "footer_links": data.get("footer_links", shared_context.get("footer_links", [])),
            },
        })

    # Text prompt
    components_to_generate = [s["component_name"] for s in batch]
    theme_json = json.dumps(shared_context.get("theme", {}), indent=2)[:5000]
    user_text = (
        f"Clone this website. Generate ALL files: app/globals.css, app/layout.jsx, "
        f"app/page.jsx, and all component files.\n\n"
        f"COMPONENTS TO GENERATE (in order):\n{json.dumps(component_manifest, indent=2)}\n\n"
        f"THEME (colors, fonts, Google Font URLs):\n```json\n{theme_json}\n```\n\n"
        f"SECTION DATA:\n```json\n{json.dumps(batch_sections, indent=2)[:40000]}\n```\n\n"
        f"FULL SITE CONTEXT (preprocessed):\n```json\n{json.dumps(preprocessed, indent=2)[:20000]}\n```\n\n"
        "IMPORTANT:\n"
        "- Generate app/globals.css, app/layout.jsx, app/page.jsx, AND every components/*.jsx file\n"
        "- app/page.jsx must import and render ALL components in order, each wrapped in ErrorBoundary\n"
        "- Use EXACT hex colors, fonts, text content from the scraped data\n"
        "- Use REAL image URLs from the scraped data — NEVER use placeholders\n"
        "- Output a flat JSON object: {\"app/globals.css\": \"...\", \"components/Hero.jsx\": \"...\", ...}\n"
        "- No markdown fences. No explanation. Just the JSON.\n"
    )

    content.append({"type": "text", "text": user_text})

    batch_names = ", ".join(components_to_generate)
    print(f"  [generate] Generating all files ({len(batch)} sections: {batch_names})")

    raw = ""
    try:
        async with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=64000,
            system=[{
                "type": "text",
                "text": REACT_SYSTEM_PROMPT if output_format == "react" else HTML_SYSTEM_PROMPT,
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

        elapsed = time.time() - t0
        print(f"  [generate] Done in {elapsed:.1f}s — "
              f"{tokens_in} in / {tokens_out} out, {len(raw)} chars")

        # Parse response
        parsed = _extract_json_object(raw)
        if not parsed:
            print(f"  [generate] JSON parse failed, attempting repair...")
            parsed = await _repair_json_output(raw, "Could not parse batch output")

        if not parsed:
            print(f"  [generate] FAILED to parse any files")
            return {"files": {}, "tokens_in": tokens_in, "tokens_out": tokens_out}

        # Handle nested "files" wrapper if Claude outputs Part 2 format
        all_files = {}
        for fp, file_content in parsed.items():
            if fp == "files" and isinstance(file_content, dict):
                # Nested format: {"files": {...}, "tailwind_extend": {...}}
                for inner_fp, inner_content in file_content.items():
                    all_files[inner_fp] = inner_content
                continue
            if fp == "tailwind_extend":
                continue  # Skip tailwind_extend metadata
            all_files[fp] = file_content

        # Normalize bare filenames → components/
        normalized = {}
        for fp, content in all_files.items():
            if "/" not in fp and (fp.endswith(".jsx") or fp.endswith(".tsx")):
                normalized[f"components/{fp}"] = content
            else:
                normalized[fp] = content

        print(f"  [generate] Parsed {len(normalized)} files")
        return {"files": normalized, "tokens_in": tokens_in, "tokens_out": tokens_out}

    except Exception as e:
        elapsed = time.time() - t0
        print(f"  [generate] FAILED in {elapsed:.1f}s: {e}")
        return {"files": {}, "tokens_in": 0, "tokens_out": 0}


# ---------------------------------------------------------------
# MAIN PIPELINE — run_clone_streaming
# ---------------------------------------------------------------

async def run_clone_streaming(
    url: str, output_format: str = "react"
) -> AsyncGenerator[str, None]:
    """
    Main clone pipeline — single Claude call architecture.

    Steps:
    [A+B] Scrape + sandbox acquire (parallel)
    [C]   Preprocess scrape data (smart data diet)
    [D]   Deploy sandbox (await early for preview URL)
    [E]   Generate: plan sections → one Claude call → all files (globals, layout, page, components)
    [F]   Gemini code review (fix JSX/React errors)
    [G]   Upload to sandbox → check compilation → fix loop
    [H]   Done
    """
    state = {
        "preview_url": None,
        "sandbox_id": None,
        "project_root": None,
        "files": {},
        "clone_id": None,
        "output_format": output_format,
    }
    start = time.time()

    def _elapsed():
        return f"{time.time() - start:.1f}s"

    def _log(msg):
        print(f"  [{_elapsed()}] {msg}")

    _log(f"=== CLONE START: {url} | format={output_format} ===")

    # Save to DB
    try:
        from app.database import save_clone
        record = await save_clone({"url": url, "status": "processing"})
        state["clone_id"] = record.get("id")
        _log(f"DB: clone_id={state['clone_id']}")
        yield sse_event("clone_created", {"clone_id": state["clone_id"]})
    except Exception as e:
        _log(f"DB skip: {e}")
        yield sse_event("warning", {"message": f"DB skip: {e}"})

    # ============================================================
    # [A + B] SCRAPE + SANDBOX in parallel
    # ============================================================
    yield sse_event("step", {"step": "scraping", "message": f"Scraping {url}..."})

    scrape_task = asyncio.create_task(scrape_website(url))
    sandbox_task = asyncio.create_task(create_react_boilerplate_sandbox())

    try:
        scrape_data = await scrape_task
    except Exception as e:
        _log(f"Scrape failed: {e}")
        yield sse_event("error", {"message": f"Scrape failed: {e}"})
        yield sse_event("done", {"preview_url": None, "error": str(e)})
        return

    sections_raw = scrape_data.get("sections", [])
    images_raw = scrape_data.get("assets", {}).get("images", [])
    interactive_raw = scrape_data.get("interactives", [])
    _log(f"Scrape done — {len(sections_raw)} sections, {len(images_raw)} images, "
         f"{len(interactive_raw)} interactives, {scrape_data.get('page_height', 0)}px tall")

    yield sse_event("scrape_done", {
        "title": scrape_data.get("title", ""),
        "sections": len(sections_raw),
        "images": len(images_raw),
        "interactives": len(interactive_raw),
        "page_height": scrape_data.get("page_height", 0),
    })

    # Check for empty pages
    if not sections_raw:
        _log("No sections detected — aborting")
        yield sse_event("error", {
            "message": "No content sections detected on the page. "
                       "The site may be behind a login, use heavy JavaScript rendering, "
                       "or block automated access."
        })
        yield sse_event("done", {"preview_url": None, "error": "No sections detected"})
        return

    # ============================================================
    # [C] PREPROCESS (smart data diet)
    # ============================================================
    yield sse_event("step", {"step": "preprocessing", "message": "Preprocessing scrape data..."})

    preprocessed = preprocess_scrape(scrape_data)
    preprocessed_json = json.dumps(preprocessed, indent=2)
    meta = preprocessed.get("_meta", {})

    _log(f"Preprocessed — {meta.get('estimated_tokens', 0)} tokens, "
         f"dedup {meta.get('dedup_ratio', 1.0)}x, "
         f"framework={meta.get('framework', '?')}, "
         f"{meta.get('sections_out', 0)} sections")

    yield sse_event("step", {
        "step": "preprocessed",
        "message": (
            f"Preprocessed: {meta.get('estimated_tokens', 0):,} tokens, "
            f"dedup {meta.get('dedup_ratio', 1.0)}x, "
            f"{meta.get('sections_out', 0)} sections"
        ),
    })

    # Save diagnostics
    _save_file_locally(state.get("clone_id"), "_diagnostics/preprocessed_data.json", preprocessed_json)
    _save_file_locally(state.get("clone_id"), "_diagnostics/site_profile.json",
                       json.dumps(preprocessed.get("site_profile", {}), indent=2))

    # ============================================================
    # [D] DEPLOY SANDBOX (await early so preview URL shows before generation)
    # ============================================================
    yield sse_event("step", {"step": "deploying", "message": "Preparing sandbox..."})

    try:
        sandbox_info = await sandbox_task
    except Exception as e:
        _log(f"Sandbox acquisition failed: {e}")
        yield sse_event("error", {"message": f"Sandbox failed: {e}"})
        yield sse_event("done", {"preview_url": None, "error": str(e)})
        return

    state["sandbox_id"] = sandbox_info["sandbox_id"]
    state["preview_url"] = sandbox_info["preview_url"]
    state["project_root"] = sandbox_info.get("project_root", PROJECT_PATH)

    from app.tool_handlers import active_sandboxes
    active_sandboxes[sandbox_info["sandbox_id"]] = sandbox_info

    _log(f"Sandbox ready: {sandbox_info['sandbox_id'][:12]} — {sandbox_info['preview_url']}")

    yield sse_event("deployed", {
        "preview_url": sandbox_info["preview_url"],
        "sandbox_id": sandbox_info["sandbox_id"],
    })

    # Capture boilerplate files from sandbox (package.json, tsconfig, etc.)
    # so they appear in the output folder and frontend file list
    boilerplate_files = sandbox_info.get("initial_files", {})
    for fp, content in boilerplate_files.items():
        _save_file_locally(state.get("clone_id"), fp, content)

    # ============================================================
    # [E] GENERATE
    # ============================================================
    gen_start = time.time()

    # E.1: Plan sections (deterministic, no Claude)
    plan = plan_sections(scrape_data)
    shared_context = plan["shared_context"]
    planned_sections = plan["sections"]
    _log(f"Planned {len(planned_sections)} components")

    # Build component manifest — tells each generator what siblings exist
    component_manifest = [
        {"name": s["component_name"], "type": s["type"], "order": s["order"]}
        for s in planned_sections
    ]

    # E.2: Claude generates ALL files (globals.css, layout.jsx, page.jsx, components)
    # per the Part 2 REACT_SYSTEM_PROMPT
    files = {}

    yield sse_event("step", {
        "step": "generating",
        "message": f"Generating all files ({len(planned_sections)} sections)...",
    })

    # E.3: Generate ALL files in one Claude call (globals.css, layout.jsx, page.jsx + components)
    full_page_b64 = scrape_data.get("screenshots", {}).get("full_page")
    gen_result = await _generate_batch(
        batch=planned_sections,
        shared_context=shared_context,
        preprocessed=preprocessed,
        component_manifest=component_manifest,
        full_page_screenshot=full_page_b64,
        output_format=output_format,
    )

    tokens_in = gen_result.get("tokens_in", 0)
    tokens_out = gen_result.get("tokens_out", 0)
    failed_components = []

    gen_files = gen_result.get("files", {})
    if not gen_files:
        _log("Generation returned no files!")
        failed_components = [s["component_name"] for s in planned_sections]
    else:
        for fp, content in gen_files.items():
            files[fp] = content
            lang = "css" if fp.endswith(".css") else "tsx"
            yield sse_event("file", {
                "path": fp,
                "content": content,
                "language": lang,
            })
        _log(f"Generated {len(gen_files)} files")

    # Safety override: always assemble page.jsx deterministically
    # This ensures all generated components are correctly imported + ErrorBoundary wrapped
    imports = ['import ErrorBoundary from "../components/ErrorBoundary";']
    elements = []
    for sec in planned_sections:
        name = sec["component_name"]
        # Only include components that were successfully generated
        if f"components/{name}.jsx" in files:
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
    yield sse_event("file", {"path": "app/page.jsx", "content": page_jsx, "language": "tsx"})

    gen_time = round(time.time() - gen_start, 1)

    _log(f"Generated {len(files)} files in {gen_time}s — "
         f"{tokens_in} tokens in, {tokens_out} tokens out"
         + (f" ({len(failed_components)} fallbacks)" if failed_components else ""))

    _save_file_locally(state.get("clone_id"), "_diagnostics/generation_stats.json",
                       json.dumps({
                           "tokens_in": tokens_in,
                           "tokens_out": tokens_out,
                           "generation_time": gen_time,
                           "model": CLAUDE_MODEL,
                           "file_count": len(files),
                           "failed_components": failed_components,
                           "component_count": len(gen_files),
                       }, indent=2))

    yield sse_event("step", {
        "step": "generated",
        "message": (
            f"Generated {len(files)} files in {gen_time}s "
            f"({tokens_in:,} in / {tokens_out:,} out)"
            + (f" — {len(failed_components)} used fallbacks" if failed_components else "")
        ),
    })

    # ============================================================
    # [F] CODE REVIEW (Gemini) — fix JSX/React errors
    # ============================================================
    from app.project_assembler import gemini_code_review
    yield sse_event("step", {"step": "code_review", "message": "Gemini reviewing code..."})
    review_start = time.time()

    code_fixes = await gemini_code_review(files)
    if code_fixes:
        _log(f"Code review fixed {len(code_fixes)} files in {time.time() - review_start:.1f}s")
        for fp, content in code_fixes.items():
            files[fp] = content
            yield sse_event("file_updated", {"path": fp, "content": content})
    else:
        _log(f"Code review: no errors ({time.time() - review_start:.1f}s)")

    yield sse_event("step", {"step": "code_review_done", "message": "Code review complete"})

    # Also run static validation
    validation = validate_files(files)
    _log(f"Static validation: {len(validation['errors'])} errors, {len(validation['warnings'])} warnings")
    if validation["valid"]:
        yield sse_event("validation_passed", {"message": "All checks passed"})
    else:
        yield sse_event("validation_failed", {
            "error_count": len(validation["errors"]),
            "report": format_error_report(validation)[:500],
        })

    # Merge boilerplate + generated files for the full project
    all_files = {}
    all_files.update(boilerplate_files)  # package.json, tsconfig, etc.
    all_files.update(files)              # generated files take precedence
    state["files"] = all_files

    # Save all files locally
    for fp, content in all_files.items():
        _save_file_locally(state.get("clone_id"), fp, content)

    # ============================================================
    # [G] UPLOAD FILES TO SANDBOX
    # ============================================================
    yield sse_event("step", {"step": "uploading", "message": "Uploading files to sandbox..."})

    upload_start = time.time()
    try:
        await upload_files_to_sandbox(
            sandbox_info["sandbox_id"],
            files,
            project_root=state["project_root"],
        )
        # Touch files to ensure Next.js filesystem watcher detects changes
        await _touch_sandbox_files(
            sandbox_info["sandbox_id"],
            list(files.keys()),
            state["project_root"],
        )
        _log(f"Uploaded {len(files)} files in {time.time() - upload_start:.1f}s")
    except Exception as upload_err:
        _log(f"Upload failed: {upload_err}")
        _save_file_locally(state.get("clone_id"), "_diagnostics/upload_error.txt",
                           f"Upload failed after {time.time() - upload_start:.1f}s\nError: {upload_err}\nSandbox: {sandbox_info['sandbox_id']}")
        yield sse_event("error", {"message": f"Failed to upload files to sandbox: {upload_err}"})
        yield sse_event("done", {"preview_url": state.get("preview_url"), "error": str(upload_err)})
        return

    # ============================================================
    # [H] CHECK COMPILATION + FIX
    # ============================================================
    _log("Waiting 15s for Next.js to compile...")
    yield sse_event("step", {"step": "checking", "message": "Waiting for compilation..."})
    await asyncio.sleep(15)

    for attempt in range(3):
        _log(f"Checking compilation (attempt {attempt + 1}/3)...")

        # Step 1: Check server logs for build errors
        logs = ""
        try:
            logs = await get_sandbox_logs(sandbox_info["sandbox_id"], state["project_root"])
        except Exception as log_err:
            _log(f"Failed to fetch sandbox logs: {log_err}")
            _save_file_locally(state.get("clone_id"), f"_diagnostics/sandbox_error_attempt{attempt+1}.txt",
                               f"Error fetching logs: {log_err}\n\nSandbox ID: {sandbox_info['sandbox_id']}\nProject root: {state['project_root']}")
            yield sse_event("warning", {"message": f"Sandbox connection error (attempt {attempt+1}/3): {log_err}"})
            if attempt < 2:
                _log("Retrying in 10s...")
                await asyncio.sleep(10)
                continue
            else:
                _log("Could not reach sandbox after 3 attempts — skipping compilation check")
                yield sse_event("warning", {"message": "Could not verify compilation — sandbox unreachable"})
                break

        # Save raw logs for diagnostics
        _save_file_locally(state.get("clone_id"), f"_diagnostics/server_logs_attempt{attempt+1}.txt", logs)
        _log(f"Raw logs (last 300 chars): {logs[-300:]}")
        parsed = parse_nextjs_errors(logs)

        # Step 2: If logs say "compiled", also verify via HTTP page fetch
        if parsed["compiled"] and not parsed["has_errors"]:
            _log("Server logs say compiled — verifying page renders...")
            http_result = await _check_sandbox_http(sandbox_info["sandbox_id"])
            _save_file_locally(state.get("clone_id"), f"_diagnostics/http_check_attempt{attempt+1}.json",
                               json.dumps(http_result, indent=2))

            if http_result["ok"]:
                _log(f"Page verified OK (HTTP {http_result['status_code']}, {http_result['body_length']} bytes)")
                yield sse_event("compiled", {"message": "Compiled and verified successfully"})
                break
            else:
                _log(f"Page has runtime errors: {http_result['errors']} "
                     f"(HTTP {http_result['status_code']}, {http_result['body_length']} bytes)")
                # Server compiled but page has runtime errors — treat as errors to fix
                for err_indicator in http_result["errors"]:
                    parsed["errors"].append({
                        "type": "runtime_error",
                        "file": None,
                        "line": None,
                        "message": f"Runtime error detected on page: {err_indicator}",
                    })
                parsed["has_errors"] = True
                # Fall through to the error handling below

        if not parsed["has_errors"]:
            if attempt < 2:
                _log("Not compiled yet, waiting 10s...")
                await asyncio.sleep(10)
                continue
            else:
                # Logs inconclusive — fall back to HTTP check
                _log("Logs inconclusive after 3 attempts, trying HTTP check...")
                http_result = await _check_sandbox_http(sandbox_info["sandbox_id"])
                if http_result["ok"]:
                    _log("HTTP check passed — page renders correctly")
                    yield sse_event("compiled", {"message": "Server responding correctly"})
                else:
                    _log(f"HTTP check failed: {http_result['errors']}")
                    yield sse_event("warning", {"message": f"Page has errors: {http_result['errors']}"})
                break

        error_report = format_nextjs_errors(parsed)
        _log(f"Errors found ({len(parsed['errors'])}): {error_report[:200]}")
        _save_file_locally(state.get("clone_id"), f"_diagnostics/compile_errors_attempt{attempt+1}.txt", error_report)

        yield sse_event("compile_errors", {
            "attempt": attempt + 1,
            "error_count": len(parsed["errors"]),
            "report": error_report[:500],
        })

        if attempt < 2:
            # Handle missing modules first — remove bad imports from page.jsx
            import re as _re
            missing_fixed = {}
            remaining_errors = []
            for err in parsed["errors"]:
                if err.get("type") == "module_not_found" and err.get("module"):
                    mod = err["module"]
                    # Check if this is a component import that simply doesn't exist
                    comp_path = mod.lstrip("./").lstrip("../")
                    if comp_path.startswith("components/") and f"{comp_path}.jsx" not in files:
                        comp_name = comp_path.split("/")[-1]
                        _log(f"Missing component '{comp_name}' — removing import from page.jsx")
                        page = files.get("app/page.jsx", "")
                        # Remove import line
                        page = _re.sub(
                            rf'import\s+{_re.escape(comp_name)}\s+from\s+"[^"]*";\n?', "", page
                        )
                        # Remove usage in JSX
                        page = _re.sub(
                            rf'^\s*<ErrorBoundary[^>]*><{_re.escape(comp_name)}\s*/><\/ErrorBoundary>\n?',
                            "", page, flags=_re.MULTILINE
                        )
                        missing_fixed["app/page.jsx"] = page
                        continue
                remaining_errors.append(err)

            if missing_fixed:
                files.update(missing_fixed)
                state["files"].update(missing_fixed)
                for fp, content in missing_fixed.items():
                    yield sse_event("file_updated", {"path": fp, "content": content})
                _log(f"Removed missing component imports from page.jsx")

            # Fix remaining errors with Claude
            fixed = {}
            if remaining_errors:
                try:
                    fixed = await fix_targeted(files, remaining_errors, "compilation")
                except Exception as fix_err:
                    _log(f"Fix agent failed: {fix_err}")
                    _save_file_locally(state.get("clone_id"), f"_diagnostics/fix_error_attempt{attempt+1}.txt", str(fix_err))
                    yield sse_event("warning", {"message": f"Fix agent error: {fix_err}"})
                    break

            # Merge all fixes
            fixed.update(missing_fixed)

            if fixed:
                for fp, content in fixed.items():
                    files[fp] = content
                    state["files"][fp] = content
                    yield sse_event("file_updated", {"path": fp, "content": content})

                # Add a log marker so error parser can find fresh output
                await _clear_sandbox_logs(sandbox_info["sandbox_id"], state["project_root"])

                await upload_files_to_sandbox(
                    sandbox_info["sandbox_id"],
                    fixed,
                    project_root=state["project_root"],
                )

                # Touch files to trigger Next.js filesystem watcher
                await _touch_sandbox_files(
                    sandbox_info["sandbox_id"],
                    list(fixed.keys()),
                    state["project_root"],
                )
                _log(f"Fixed and re-uploaded {len(fixed)} files, waiting 15s...")
                await asyncio.sleep(15)
            else:
                _log("Fix agent returned no changes")
                break

    # ============================================================
    # [I] DONE
    # ============================================================
    total_time = round(time.time() - start, 1)
    final_status = "success" if state.get("preview_url") else "failed"
    file_list = list(state.get("files", {}).keys())

    _log(f"=== CLONE COMPLETE ===")
    _log(f"  Status: {final_status}")
    _log(f"  Total: {total_time}s")
    _log(f"  Preview: {state.get('preview_url', 'NONE')}")
    _log(f"  Files: {len(file_list)}")
    _log(f"  Claude: {tokens_in} in / {tokens_out} out in {gen_time}s")

    yield sse_event("done", {
        "preview_url": state.get("preview_url"),
        "sandbox_id": state.get("sandbox_id"),
        "clone_id": state.get("clone_id"),
        "files": file_list,
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
                "output_format": output_format,
                "metadata": {
                    "files": files,
                    "iterations": 1,
                    "output_format": output_format,
                    "gen_time": gen_time,
                    "total_time": total_time,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                },
            })
            _log("DB updated")
    except Exception as e:
        _log(f"DB update failed: {e}")


# ---------------------------------------------------------------
# JSON EXTRACTION HELPER
# ---------------------------------------------------------------

def _extract_json_object(text: str) -> dict | None:
    """Try to extract a JSON object from text that may have extra content."""
    # Strategy 1: Direct parse after stripping fences
    cleaned = _strip_code_fences(text)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Strategy 2: Find outermost { ... } using string-aware brace matching
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    break

    # Strategy 3: Try to fix common JSON issues and re-parse
    # Truncated output — find last complete key-value pair
    last_good = text.rfind('"\n}')
    if last_good == -1:
        last_good = text.rfind('"}')
    if last_good > start:
        candidate = text[start:last_good + 2]
        # Close any open string
        if candidate.count('"') % 2 != 0:
            candidate += '"'
        if not candidate.rstrip().endswith("}"):
            candidate += "}"
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Strategy 4: Parse file by file using regex
    # Pattern: "filepath": "content" pairs
    file_pattern = r'"([^"]+\.(?:jsx?|tsx?|css|json))":\s*"'
    files = {}
    for m in __import__("re").finditer(file_pattern, text):
        filepath = m.group(1)
        content_start = m.end()
        # Find the end of this string value
        pos = content_start
        while pos < len(text):
            if text[pos] == "\\" and pos + 1 < len(text):
                pos += 2
                continue
            if text[pos] == '"':
                break
            pos += 1
        if pos < len(text):
            try:
                content = json.loads('"' + text[content_start:pos] + '"')
                files[filepath] = content
            except json.JSONDecodeError:
                # Raw unescape fallback
                raw = text[content_start:pos]
                raw = raw.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
                files[filepath] = raw
    if files:
        print(f"  [json-recovery] Recovered {len(files)} files via regex extraction")
        return files

    return None


# ---------------------------------------------------------------
# REPAIR AGENT — sends broken JSON to Claude for fixing
# ---------------------------------------------------------------

async def _repair_json_output(raw_text: str, error_msg: str) -> dict | None:
    """
    Send malformed Claude output to a second Claude call that extracts
    and fixes the JSON. Returns {filepath: content} dict or None.
    """
    client = _get_client()

    # Truncate to avoid sending 200K+ of broken output
    # Keep first 120K chars which should contain all the files
    truncated = raw_text[:120_000]

    prompt = (
        "The following text is supposed to be a JSON object mapping file paths to file contents, "
        f"like {{\"app/page.jsx\": \"...\", \"components/Hero.jsx\": \"...\"}}\n\n"
        f"But it failed to parse with this error: {error_msg}\n\n"
        "Please extract ALL file paths and their complete contents from the text below "
        "and return a valid JSON object. Output ONLY the JSON — no markdown fences, no explanation.\n\n"
        "IMPORTANT: Preserve the COMPLETE content of each file. Do not truncate or summarize.\n\n"
        f"--- BROKEN OUTPUT ---\n{truncated}"
    )

    try:
        text = ""
        async with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=64000,
            system="You are a JSON repair tool. Extract file path → content mappings from broken JSON output. Return ONLY valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for chunk in stream.text_stream:
                text += chunk

        text = _strip_code_fences(text)
        result = json.loads(text)
        if isinstance(result, dict) and len(result) > 0:
            print(f"  [repair-agent] Recovered {len(result)} files")
            return result
        return None
    except Exception as e:
        print(f"  [repair-agent] Failed: {e}")
        return None


# ---------------------------------------------------------------
# TARGETED FIX — shared by validation + compilation fix paths
# ---------------------------------------------------------------

async def fix_targeted(files: dict, errors: list, error_source: str) -> dict:
    """
    Single Claude call to fix specific errors in specific files.
    Returns only the fixed files (filepath → content), or {} on failure.
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

    for fp, errs in by_file.items():
        parts.append(f"\n--- {fp} ---")
        for e in errs:
            line = e.get("line", 0)
            msg = e.get("message", "")
            hint = e.get("fix_hint", "")
            parts.append(f"  Line {line}: {msg}" + (f" → {hint}" if hint else ""))

        match = None
        for key in files:
            if key == fp or key.endswith(fp) or fp.endswith(key):
                match = key
                break
        if match:
            parts.append(f"CODE:\n```\n{files[match]}\n```")

    try:
        # Use streaming to avoid 10-minute timeout
        text = ""
        async with client.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=12000,
            system=(
                "Fix React/JSX errors in a Next.js App Router project. "
                "This project uses the app/ directory (NOT pages/). "
                "NEVER create pages/_app.tsx, pages/_document.tsx, or any file under pages/. "
                "NEVER create styles/globals.css — the CSS file is at app/globals.css. "
                "Only fix files under app/ and components/. "
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
        # Filter out any files outside allowed paths (prevent Pages Router contamination)
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
# CHAT FOLLOW-UP — continues an existing clone session
# ---------------------------------------------------------------

CHAT_SYSTEM_PROMPT = """You are a website clone assistant. The user has an existing cloned website and wants to make changes.

You have access to:
1. `update_sandbox_file` — modify a file in the running sandbox (Next.js hot-reloads)
2. `get_sandbox_logs` — check for compilation errors

Rules:
- Use className, not class
- Use "use client" for components with hooks/events
- Use Tailwind arbitrary values for colors: bg-[#hex], text-[#hex]
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


async def run_chat_followup(
    clone_id: str, user_message: str
) -> AsyncGenerator[str, None]:
    """
    Handle a user follow-up message about an existing clone.
    Uses tool-use loop with update_sandbox_file + get_sandbox_logs.
    """
    session = _chat_sessions.get(clone_id)
    if not session:
        yield sse_event("error", {"message": "No active session. Try cloning again."})
        return

    state = session["state"]
    files = session["files"]
    sandbox_id = state.get("sandbox_id")

    if not sandbox_id:
        yield sse_event("error", {"message": "No sandbox for this clone."})
        return

    client = _get_client()

    file_listing = "\n".join(
        f"--- {fp} ---\n{content[:3000]}"
        for fp, content in files.items()
        if fp.endswith((".jsx", ".tsx", ".css"))
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

            from app.tool_handlers import handle_tool_call
            result = await handle_tool_call(tool_name, tool_input)

            try:
                parsed = json.loads(result)
                if "preview_url" in parsed:
                    yield sse_event("deployed", {"preview_url": parsed["preview_url"]})
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


# ---------------------------------------------------------------
# LEGACY COMPATIBILITY — old endpoints reference these
# ---------------------------------------------------------------

async def run_clone_agent(url: str, output_format: str = "html") -> dict:
    """
    Synchronous-style wrapper for the streaming pipeline.
    Collects SSE events and returns a result dict.
    """
    result = {
        "preview_url": None,
        "sandbox_id": None,
        "html": None,
        "files": {},
        "iterations": 1,
        "status": "processing",
    }

    async for event_str in run_clone_streaming(url, output_format):
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
    url: str, output_format: str = "html"
) -> AsyncGenerator[str, None]:
    """Alias for run_clone_streaming (backwards compatibility)."""
    async for event in run_clone_streaming(url, output_format):
        yield event
