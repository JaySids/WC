"""
MCP-style tool definitions for the website cloner agent.
These aren't a real MCP server — they're tool definitions we pass
to the Anthropic API's tool_use feature. Same effect, simpler setup.
"""

TOOLS = [
    {
        "name": "scrape_url",
        "description": (
            "Load a URL in a headless browser, intercept all network requests, "
            "and extract comprehensive page data including: STRUCTURED SECTIONS "
            "(section-by-section breakdown with per-section images, headings, "
            "text, buttons, SVGs, and background styles — use this as primary "
            "source for layout), an ASSET MAP (descriptive keys like "
            "'features.card_0.icon_svg' mapped to exact URLs/markup for "
            "deterministic asset placement), CSS ANIMATIONS (keyframes, "
            "transitions, library detection for AOS/GSAP/Framer Motion), "
            "INTERACTIVE UI PATTERNS (detected carousels, tabs, accordions, "
            "dropdowns, modals with library hints), BUTTON BEHAVIORS (what "
            "each button does — toggle, open-modal, switch-tab, etc.), "
            "FRAMEWORK DETECTION (React/Vue/Angular/Svelte, Next.js/Nuxt, "
            "UI libraries like shadcn/MUI/Chakra, and React component tree), "
            "all asset URLs (images, fonts, stylesheets) captured from network "
            "traffic, exact CSS theme values (colors, fonts, spacing), all "
            "clickable elements with their real hrefs, SVG markup, text "
            "content, and viewport screenshots. This is the FIRST tool you "
            "should call — it gives you everything you need to clone the site."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to scrape",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "generate_and_deploy_html",
        "description": (
            "Generate a single self-contained HTML+Tailwind file from scraped "
            "data and deploy it to a Daytona sandbox. Use this for simple HTML "
            "output mode. Returns a live preview URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "html_content": {
                    "type": "string",
                    "description": (
                        "The complete HTML content to deploy. Must be a full "
                        "HTML document with <!DOCTYPE html>, Tailwind CDN, etc."
                    ),
                }
            },
            "required": ["html_content"],
        },
    },
    {
        "name": "generate_and_deploy_react",
        "description": (
            "Deploy a React (Next.js 14) project to a Daytona sandbox. "
            "Provisions a sandbox with all packages pre-installed (framer-motion, "
            "swiper, Radix, HeadlessUI, lucide-react, etc.), then uploads the "
            "provided files. The dev server hot-reloads automatically. "
            "Pass a JSON object mapping filepath to file content, e.g. "
            "{\"app/globals.css\": \"...\", \"app/page.jsx\": \"...\", "
            "\"components/Hero.jsx\": \"...\"}. Returns a live preview URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "object",
                    "description": (
                        "A JSON object mapping file paths to their content. "
                        "Paths are relative to the project root, e.g. "
                        "'app/page.jsx', 'components/Hero.jsx', 'app/globals.css'"
                    ),
                },
                "sandbox_id": {
                    "type": "string",
                    "description": (
                        "Optional: reuse an existing sandbox instead of creating a new one"
                    ),
                },
            },
            "required": ["files"],
        },
    },
    {
        "name": "screenshot_preview",
        "description": (
            "Take a screenshot of the deployed clone's preview URL. "
            "Returns a base64-encoded JPEG image. Use this to visually compare "
            "your clone against the original website and identify differences."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "preview_url": {
                    "type": "string",
                    "description": "The preview URL to screenshot",
                }
            },
            "required": ["preview_url"],
        },
    },
    {
        "name": "get_sandbox_logs",
        "description": (
            "Get the console output and error logs from the Daytona sandbox's "
            "Next.js dev server. Use this to check for compilation errors, "
            "runtime errors, missing imports, or server startup issues."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sandbox_id": {
                    "type": "string",
                    "description": "The Daytona sandbox ID",
                }
            },
            "required": ["sandbox_id"],
        },
    },
    {
        "name": "update_sandbox_file",
        "description": (
            "Write or update a single file in an existing Daytona sandbox. The "
            "Next.js dev server hot-reloads automatically after each file save. "
            "Use this to write components, update pages, or fix errors."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sandbox_id": {
                    "type": "string",
                    "description": "The Daytona sandbox ID",
                },
                "filepath": {
                    "type": "string",
                    "description": (
                        "Path relative to project root (e.g. 'app/page.tsx', "
                        "'components/Hero.tsx', 'components/Navbar.tsx')"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The new file content",
                },
            },
            "required": ["sandbox_id", "filepath", "content"],
        },
    },
    {
        "name": "create_react_sandbox",
        "description": (
            "Create a live Next.js App Router + TypeScript + Tailwind CSS v4 "
            "cloud sandbox (bun). Scaffolded via `bun create next-app` with "
            "everything pre-installed. Extra deps: framer-motion, lucide-react, "
            "react-icons. Returns a preview_url and sandbox_id. After calling "
            "this, use `update_sandbox_file` to write components (components/*.tsx) "
            "and the main page (app/page.tsx). Use .tsx file extensions. "
            "Next.js hot-reloads after each file save."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "install_package",
        "description": (
            "Install an npm package in an existing Daytona sandbox using bun. "
            "Use this if you need a package that isn't pre-installed. "
            "Pre-installed packages: framer-motion, lucide-react, react-icons — "
            "you do NOT need to install these."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sandbox_id": {
                    "type": "string",
                    "description": "The Daytona sandbox ID",
                },
                "package_name": {
                    "type": "string",
                    "description": "The npm package name to install (e.g. 'swiper', '@headlessui/react')",
                },
            },
            "required": ["sandbox_id", "package_name"],
        },
    },
]
