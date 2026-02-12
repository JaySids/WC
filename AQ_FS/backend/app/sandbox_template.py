"""
Provisions a Daytona sandbox with a fully configured Next.js 14 project.
All packages pre-installed. Claude only writes component files.

Also provides utility functions for uploading files and reading logs.
"""

import asyncio
import os
import time as _time

from app.sandbox import (
    create_react_boilerplate_sandbox,
    get_daytona_client,
    PROJECT_PATH,
)


# ── Template File Constants ──────────────────────────────────────────────────

PACKAGE_JSON = '''{
  "name": "website-clone",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "next dev --port 3000 --hostname 0.0.0.0",
    "build": "next build",
    "start": "next start"
  },
  "dependencies": {
    "next": "14.2.21",
    "react": "^18.3.1",
    "react-dom": "^18.3.1",

    "framer-motion": "^11.15.0",
    "gsap": "^3.12.7",

    "swiper": "^11.1.15",
    "embla-carousel-react": "^8.5.1",

    "@headlessui/react": "^2.2.0",
    "@radix-ui/react-accordion": "^1.2.2",
    "@radix-ui/react-dialog": "^1.1.4",
    "@radix-ui/react-dropdown-menu": "^2.1.4",
    "@radix-ui/react-tabs": "^1.1.2",
    "@radix-ui/react-tooltip": "^1.1.6",
    "@radix-ui/react-popover": "^1.1.4",

    "lucide-react": "^0.469.0",
    "react-icons": "^5.4.0",
    "@heroicons/react": "^2.2.0",

    "react-intersection-observer": "^9.14.1",
    "react-scroll": "^1.9.0",
    "react-countup": "^6.5.3",
    "react-type-animation": "^3.2.0",

    "clsx": "^2.1.1",
    "tailwind-merge": "^2.6.0",
    "class-variance-authority": "^0.7.1"
  },
  "devDependencies": {
    "tailwindcss": "^3.4.17",
    "postcss": "^8.4.49",
    "autoprefixer": "^10.4.20",
    "@types/react": "^18",
    "@types/node": "^20"
  }
}'''

TAILWIND_CONFIG = '''/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/**/*.{js,jsx,ts,tsx}',
    './app/**/*.{js,jsx,ts,tsx}',
    './components/**/*.{js,jsx,ts,tsx}',
  ],
  theme: {
    extend: {
      // Claude will inject site-specific theme values here
    },
  },
  plugins: [],
}'''

POSTCSS_CONFIG = '''module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}'''

NEXT_CONFIG = '''/** @type {import('next').NextConfig} */
const nextConfig = {
  images: {
    remotePatterns: [
      {
        protocol: 'https',
        hostname: '**',
      },
    ],
  },
  // Allow all external image domains
  typescript: {
    ignoreBuildErrors: true,
  },
  eslint: {
    ignoreDuringBuilds: true,
  },
}

module.exports = nextConfig'''

GLOBALS_CSS = '''@tailwind base;
@tailwind components;
@tailwind utilities;

/* Claude injects Google Fonts @import and custom CSS here */

/* Reset */
*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

html {
  scroll-behavior: smooth;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

img, video {
  max-width: 100%;
  height: auto;
  display: block;
}

/* Utility for hiding scrollbar */
.scrollbar-hide::-webkit-scrollbar {
  display: none;
}
.scrollbar-hide {
  -ms-overflow-style: none;
  scrollbar-width: none;
}
'''

ROOT_LAYOUT = '''import "./globals.css";

export const metadata = {
  title: "Website Clone",
  description: "Cloned website",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        {/* Claude injects <link> tags for Google Fonts here */}
      </head>
      <body>{children}</body>
    </html>
  );
}
'''

PAGE_TEMPLATE = '''// Claude replaces this entire file with the cloned page
export default function Home() {
  return <div>Loading...</div>;
}
'''

# File structure to upload to the sandbox
TEMPLATE_FILES = {
    "package.json": PACKAGE_JSON,
    "tailwind.config.js": TAILWIND_CONFIG,
    "postcss.config.js": POSTCSS_CONFIG,
    "next.config.js": NEXT_CONFIG,
    "app/globals.css": GLOBALS_CSS,
    "app/layout.jsx": ROOT_LAYOUT,
    "app/page.jsx": PAGE_TEMPLATE,
}


# ── Provisioning ─────────────────────────────────────────────────────────────

async def provision_react_sandbox(progress=None) -> dict:
    """
    Provision a Daytona sandbox with a full Next.js project.

    Uses the existing create_react_boilerplate_sandbox which:
    1. Creates a Daytona sandbox
    2. Installs bun + scaffolds Next.js via bun create next-app@latest
    3. Installs extra packages (framer-motion, swiper, Radix, etc.)
    4. Uploads ErrorBoundary component
    5. Starts the dev server

    Returns: { "sandbox_id", "preview_url", "project_root", "initial_files" }
    """
    return await create_react_boilerplate_sandbox(progress=progress)


async def provision_react_sandbox_from_template() -> dict:
    """
    Create a Daytona sandbox with the full Next.js template.
    Uses pre-defined TEMPLATE_FILES — uploads them, runs npm install,
    and starts the dev server.

    Returns { "sandbox_id", "preview_url", "project_root" }
    """
    from daytona import CreateSandboxFromSnapshotParams

    project_root = "/home/daytona/clone-app"

    def _create():
        daytona = get_daytona_client()

        params = CreateSandboxFromSnapshotParams(
            language="javascript",
            public=True,
            auto_stop_interval=0,
            auto_archive_interval=7 * 24 * 60,
        )
        sandbox = daytona.create(params, timeout=120)

        # Belt-and-suspenders: explicitly set intervals
        try:
            sandbox.set_autostop_interval(0)
            sandbox.set_auto_archive_interval(7 * 24 * 60)
        except Exception:
            pass

        # Upload all template files
        for filepath, content in TEMPLATE_FILES.items():
            full_path = f"{project_root}/{filepath}"
            dir_path = "/".join(full_path.split("/")[:-1])
            sandbox.process.exec(f"mkdir -p {dir_path}", timeout=5)
            sandbox.fs.upload_file(content.encode("utf-8"), full_path)

        # Install dependencies
        print("  [template] Running npm install...")
        install = sandbox.process.exec(
            f"cd {project_root} && npm install --legacy-peer-deps 2>&1",
            timeout=120,
        )
        print(f"  [template] npm install exit code: {install.exit_code}")

        # Start dev server in background
        log_file = f"{project_root}/server.log"
        sandbox.process.exec(
            f"cd {project_root} && nohup npx next dev --port 3000 --hostname 0.0.0.0 "
            f"> {log_file} 2>&1 &"
        )

        # Wait for dev server to be ready
        ready = False
        for _ in range(30):
            _time.sleep(2)
            try:
                logs = sandbox.process.exec(
                    f"tail -20 {log_file} 2>/dev/null", timeout=5
                )
                log_text = (logs.result or "").lower()
                if "ready" in log_text or "compiled" in log_text:
                    ready = True
                    print("  [template] Next.js compiled successfully")
                    break
            except Exception:
                pass
        if not ready:
            print("  [template] Timeout waiting for Next.js — proceeding anyway")

        # Get preview URL
        preview = sandbox.get_preview_link(3000)

        return {
            "sandbox_id": sandbox.id,
            "preview_url": preview.url,
            "project_root": project_root,
        }

    return await asyncio.to_thread(_create)


# ── Utility Functions ────────────────────────────────────────────────────────

async def upload_files_to_sandbox(sandbox_id: str, files: dict, project_root: str = None):
    """
    Upload multiple files to a Daytona sandbox.
    Creates directories as needed. Retries on transient errors.
    """
    if not project_root:
        project_root = PROJECT_PATH

    def _upload():
        last_err = None
        for attempt in range(3):
            try:
                daytona = get_daytona_client()
                sb = daytona.get(sandbox_id)
                for fp, content in files.items():
                    full_path = f"{project_root}/{fp}"
                    dir_path = "/".join(full_path.split("/")[:-1])
                    sb.process.exec(f"mkdir -p {dir_path}", timeout=5)
                    sb.fs.upload_file(content.encode("utf-8"), full_path)
                return  # success
            except Exception as e:
                last_err = e
                print(f"  [upload] Attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    _time.sleep(3)
        raise last_err

    await asyncio.to_thread(_upload)


async def get_sandbox_logs(sandbox_id: str, project_root: str = None) -> str:
    """Get Next.js dev server output from the sandbox. Retries on transient errors."""
    if not project_root:
        project_root = PROJECT_PATH

    def _get():
        log_file = f"{project_root}/server.log"
        last_err = None
        for attempt in range(3):
            try:
                daytona = get_daytona_client()
                sb = daytona.get(sandbox_id)
                result = sb.process.exec(
                    f"tail -100 {log_file} 2>/dev/null || echo 'No logs yet'",
                    timeout=15,
                )
                return result.result or ""
            except Exception as e:
                last_err = e
                print(f"  [get_sandbox_logs] Attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    _time.sleep(2)
        raise last_err

    return await asyncio.to_thread(_get)
