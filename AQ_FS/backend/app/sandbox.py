"""
Daytona sandbox management — deploy cloned HTML or React apps to a live preview URL.
Uses the `daytona` package (v0.141+) with process.exec() API.
"""

import asyncio
import queue
import time

from daytona import Daytona, DaytonaConfig, CreateSandboxFromSnapshotParams

from app.config import get_settings


SERVER_SCRIPT = '''\
import http.server
import socketserver
import os

os.chdir("/home/daytona")

class CORSHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

with socketserver.TCPServer(("0.0.0.0", 3000), CORSHandler) as httpd:
    print("Serving on port 3000")
    httpd.serve_forever()
'''


def _get_api_key():
    settings = get_settings()
    api_key = settings.daytona_api_key
    if not api_key:
        raise RuntimeError("DAYTONA_API_KEY not set")
    return api_key


def get_daytona_client() -> Daytona:
    """Get a configured Daytona client. Shared by sandbox.py and tool_handlers.py."""
    return Daytona(DaytonaConfig(api_key=_get_api_key()))


async def deploy_to_sandbox(html_content: str) -> dict:
    """
    Deploy HTML to a Daytona sandbox (legacy interface).
    Returns { "preview_url": "...", "sandbox_id": "...", "project_root": "..." }
    """
    return await deploy_html_to_sandbox(html_content)


async def deploy_html_to_sandbox(html_content: str) -> dict:
    """
    Deploy a single HTML file to a Daytona sandbox with a CORS Python server.
    Returns { "preview_url": "...", "sandbox_id": "...", "project_root": "..." }
    """

    def _deploy():
        daytona = get_daytona_client()

        # public=True at creation time so the preview URL works from any device
        # auto_stop_interval=0 disables Daytona's default auto-stop timer
        # auto_archive_interval set high to prevent archiving (0 = "max default", not "never")
        params = CreateSandboxFromSnapshotParams(
            public=True,
            auto_stop_interval=0,
            auto_archive_interval=7 * 24 * 60,  # 7 days in minutes
        )
        sandbox = daytona.create(params, timeout=60)

        # Belt-and-suspenders: explicitly set intervals on the live sandbox
        try:
            sandbox.set_autostop_interval(0)
            sandbox.set_auto_archive_interval(7 * 24 * 60)
        except Exception:
            pass

        # Upload cloned HTML
        sandbox.fs.upload_file(
            html_content.encode("utf-8"),
            "/home/daytona/index.html",
        )

        # Upload CORS-enabled HTTP server
        sandbox.fs.upload_file(
            SERVER_SCRIPT.encode("utf-8"),
            "/home/daytona/server.py",
        )

        # Start the web server
        sandbox.process.exec("nohup python3 /home/daytona/server.py > /home/daytona/server.log 2>&1 &")

        # Wait for server to start
        time.sleep(3)

        preview = sandbox.get_preview_link(3000)

        return {
            "preview_url": preview.url,
            "sandbox_id": sandbox.id,
            "project_root": "/home/daytona",
        }

    return await asyncio.to_thread(_deploy)


# ── Next.js Project Setup (bun create next-app@latest) ─────────────────────

PROJECT_PATH = "/home/daytona/my-app"
BUN_BIN = "/home/daytona/.bun/bin/bun"

# ErrorBoundary component — uploaded after scaffolding
ERROR_BOUNDARY_TSX = '''\
"use client";
import { Component, ReactNode } from "react";

interface Props {
  name?: string;
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }
  static getDerivedStateFromError(error: Error) {
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
}'''

# Extra packages installed on top of what create-next-app provides
EXTRA_PACKAGES = [
    "framer-motion", "gsap", "swiper", "embla-carousel-react",
    "@headlessui/react",
    "@radix-ui/react-accordion", "@radix-ui/react-dialog",
    "@radix-ui/react-dropdown-menu", "@radix-ui/react-tabs",
    "@radix-ui/react-tooltip", "@radix-ui/react-popover",
    "lucide-react", "react-icons", "@heroicons/react",
    "react-intersection-observer", "react-scroll",
    "react-countup", "react-type-animation",
    "clsx", "tailwind-merge", "class-variance-authority",
]

# Key project files to read back from the sandbox for the frontend file explorer
_FILES_TO_READ = [
    "app/page.tsx",
    "app/layout.tsx",
    "app/globals.css",
    "components/ErrorBoundary.tsx",
    "package.json",
    "next.config.ts",
    "next.config.mjs",
    "tsconfig.json",
]


def _read_sandbox_files(sandbox, project_root: str) -> dict:
    """Read key project files from the sandbox and return as {path: content}."""
    files = {}
    for fpath in _FILES_TO_READ:
        try:
            result = sandbox.process.exec(f"cat {project_root}/{fpath} 2>/dev/null", timeout=5)
            content = result.result.strip() if result.result else ""
            if content:
                files[fpath] = content
        except Exception:
            pass
    return files


async def create_react_boilerplate_sandbox(progress: queue.Queue | None = None) -> dict:
    """
    Create a Daytona sandbox with a Next.js project scaffolded via
    `bun create next-app@latest` (latest Next.js + TypeScript + Tailwind v4).
    Installs extra interactive packages, uploads ErrorBoundary, starts dev server.

    Args:
        progress: Optional thread-safe queue for reporting progress messages
                  to the SSE stream. Each item is a string message.

    Returns { "preview_url": "...", "sandbox_id": "...", "project_root": "...", "initial_files": {...} }
    """

    def _notify(msg: str):
        print(f"  {msg}")
        if progress:
            progress.put(msg)

    def _create():
        daytona = get_daytona_client()

        # Create a public sandbox (public=True at creation time for universal access)
        # auto_stop_interval=0 disables Daytona's default auto-stop timer
        # auto_archive_interval set high to prevent archiving (0 = "max default", not "never")
        _notify("Provisioning cloud sandbox...")
        params = CreateSandboxFromSnapshotParams(
            language="typescript",
            public=True,
            auto_stop_interval=0,
            auto_archive_interval=7 * 24 * 60,  # 7 days in minutes
        )
        sandbox = daytona.create(params, timeout=120)

        # Belt-and-suspenders: explicitly set intervals on the live sandbox
        try:
            sandbox.set_autostop_interval(0)
            sandbox.set_auto_archive_interval(7 * 24 * 60)
        except Exception:
            pass

        # Install bun & kill stale processes
        _notify("Installing bun runtime...")
        sandbox.process.exec("curl -fsSL https://bun.sh/install | bash", timeout=60)
        sandbox.process.exec("pkill -f next || true; pkill -f bun || true")

        # Scaffold Next.js project (latest — TypeScript + Tailwind + App Router)
        _notify("Scaffolding Next.js project (bun create next-app@latest)...")
        sandbox.process.exec(
            f"{BUN_BIN} create next-app@latest {PROJECT_PATH} "
            f"--typescript --tailwind --eslint --app --use-bun --yes",
            timeout=120,
        )

        # Ensure dependencies are properly installed
        _notify("Running bun install...")
        sandbox.process.exec(f"{BUN_BIN} install --cwd {PROJECT_PATH}", timeout=60)

        # Install extra interactive packages
        _notify("Installing interactive packages (framer-motion, swiper, Radix, etc.)...")
        sandbox.process.exec(
            f"{BUN_BIN} add --cwd {PROJECT_PATH} {' '.join(EXTRA_PACKAGES)}",
            timeout=120,
        )

        # Upload ErrorBoundary component
        _notify("Uploading ErrorBoundary component...")
        sandbox.process.exec(f"mkdir -p {PROJECT_PATH}/components", timeout=5)
        sandbox.fs.upload_file(
            ERROR_BOUNDARY_TSX.strip().encode("utf-8"),
            f"{PROJECT_PATH}/components/ErrorBoundary.tsx",
        )

        # Start dev server (using --bun flag for bun runtime)
        _notify("Starting Next.js dev server...")
        log_file = f"{PROJECT_PATH}/server.log"
        start_cmd = (
            f"nohup {BUN_BIN} --cwd {PROJECT_PATH} --bun next dev -p 3000 -H 0.0.0.0 "
            f"> {log_file} 2>&1 &"
        )
        sandbox.process.exec(start_cmd)

        # Wait for dev server to actually be ready (poll logs)
        _notify("Waiting for Next.js to compile...")
        ready = False
        for _ in range(30):  # up to 60s (30 x 2s)
            time.sleep(2)
            try:
                logs = sandbox.process.exec(f"tail -20 {log_file} 2>/dev/null", timeout=5)
                log_text = (logs.result or "").lower()
                if "ready" in log_text or "compiled" in log_text or "✓ ready" in log_text:
                    ready = True
                    _notify("Next.js compiled successfully")
                    break
                if "error" in log_text and "failed to compile" in log_text:
                    _notify("Next.js has errors but server is running")
                    ready = True
                    break
            except Exception:
                pass
        if not ready:
            _notify("Timeout waiting for Next.js — proceeding anyway")

        preview = sandbox.get_preview_link(3000)

        # Read all key project files so the frontend can show them
        _notify("Reading project files...")
        initial_files = _read_sandbox_files(sandbox, PROJECT_PATH)

        _notify(f"Sandbox ready — {len(initial_files)} files loaded")

        return {
            "preview_url": preview.url,
            "sandbox_id": sandbox.id,
            "project_root": PROJECT_PATH,
            "initial_files": initial_files,
        }

    return await asyncio.to_thread(_create)


async def stop_sandbox(sandbox_id: str, delete: bool = False):
    """Stop or delete a Daytona sandbox."""
    settings = get_settings()
    if not settings.daytona_api_key:
        return

    def _stop():
        try:
            daytona = get_daytona_client()
            sandbox = daytona.get(sandbox_id)
            if delete:
                daytona.delete(sandbox)
                print(f"Sandbox {sandbox_id} deleted")
            else:
                daytona.stop(sandbox)
                print(f"Sandbox {sandbox_id} stopped")
        except Exception as e:
            print(f"Error with sandbox {sandbox_id}: {e}")

    await asyncio.to_thread(_stop)


async def start_sandbox(sandbox_id: str) -> dict:
    """Start a stopped Daytona sandbox and restart the dev server."""
    def _start():
        daytona = get_daytona_client()
        sandbox = daytona.get(sandbox_id)
        daytona.start(sandbox)

        # Ensure sandbox stays alive after restart
        try:
            sandbox.set_autostop_interval(0)
            sandbox.set_auto_archive_interval(7 * 24 * 60)
        except Exception:
            pass

        # Restart dev server
        sandbox.process.exec("pkill -f next || true; pkill -f bun || true")
        log_file = f"{PROJECT_PATH}/server.log"
        start_cmd = (
            f"nohup {BUN_BIN} --cwd {PROJECT_PATH} --bun next dev -p 3000 -H 0.0.0.0 "
            f"> {log_file} 2>&1 &"
        )
        sandbox.process.exec(start_cmd)
        time.sleep(5)

        preview = sandbox.get_preview_link(3000)
        print(f"Sandbox {sandbox_id} started — preview: {preview.url}")
        return {
            "preview_url": preview.url,
            "sandbox_id": sandbox.id,
            "project_root": PROJECT_PATH,
        }

    return await asyncio.to_thread(_start)


# ---------------------------------------------------------------------------
# Background Keep-Alive — ping all tracked sandboxes every 5 minutes
# ---------------------------------------------------------------------------

async def _keep_alive_loop():
    """Periodically refresh activity on all active sandboxes to prevent auto-stop/archive."""
    while True:
        await asyncio.sleep(5 * 60)  # every 5 minutes
        try:
            from app.tool_handlers import active_sandboxes
            sandbox_ids = list(active_sandboxes.keys())
            if not sandbox_ids:
                continue
            print(f"[keep-alive] Pinging {len(sandbox_ids)} sandbox(es)...")
            for sid in sandbox_ids:
                try:
                    def _ping(sandbox_id=sid):
                        daytona = get_daytona_client()
                        sandbox = daytona.get(sandbox_id)
                        sandbox.refresh_activity()
                    await asyncio.to_thread(_ping)
                except Exception as e:
                    print(f"[keep-alive] Failed to ping {sid}: {e}")
        except Exception as e:
            print(f"[keep-alive] Error: {e}")


def start_keep_alive():
    """Start the keep-alive background task. Call from server lifespan."""
    asyncio.create_task(_keep_alive_loop())
