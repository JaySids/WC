"""
Daytona sandbox management — deploy React apps to a live preview URL.
Uses the `daytona` package (v0.141+) with process.exec() API.

Sandboxes auto-stop after SANDBOX_TTL_MINUTES of inactivity.
A background monitor updates Supabase when sandboxes go down.
"""

import asyncio
import queue
import time

from daytona import Daytona, DaytonaConfig, CreateSandboxFromSnapshotParams

from app.config import get_settings


# Sandbox auto-stop after N minutes of inactivity (Daytona-managed)
SANDBOX_TTL_MINUTES = 30


def _get_api_key():
    settings = get_settings()
    api_key = settings.daytona_api_key
    if not api_key:
        raise RuntimeError("DAYTONA_API_KEY not set")
    return api_key


def get_daytona_client() -> Daytona:
    """Get a configured Daytona client."""
    return Daytona(DaytonaConfig(api_key=_get_api_key()))


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
# Keep this minimal — fewer packages = faster sandbox creation + simpler clones
EXTRA_PACKAGES = [
    "framer-motion",                  # Animations (only if original site has them)
    "lucide-react",                   # Icons
    "@radix-ui/react-accordion",      # Accordion/FAQ sections
    "@radix-ui/react-dialog",         # Modals
    "@radix-ui/react-tabs",           # Tab components
    "react-intersection-observer",    # Scroll-triggered visibility
    "clsx", "tailwind-merge",         # className merging
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

    Sandboxes auto-stop after SANDBOX_TTL_MINUTES of inactivity.

    Returns { "preview_url": "...", "sandbox_id": "...", "project_root": "...", "initial_files": {...} }
    """

    def _notify(msg: str):
        print(f"  {msg}")
        if progress:
            progress.put(msg)

    def _exec(sandbox, cmd, timeout=60, retries=2):
        """Run a command with retry on timeout."""
        for attempt in range(retries):
            try:
                return sandbox.process.exec(cmd, timeout=timeout)
            except Exception as e:
                if attempt < retries - 1 and "timeout" in str(e).lower():
                    _notify(f"  Command timed out, retrying ({attempt + 1}/{retries})...")
                    time.sleep(2)
                else:
                    raise

    def _create():
        daytona = get_daytona_client()

        _notify("Provisioning cloud sandbox...")
        params = CreateSandboxFromSnapshotParams(
            language="typescript",
            public=True,
            auto_stop_interval=SANDBOX_TTL_MINUTES,
            auto_archive_interval=7 * 24 * 60,  # 7 days in minutes
        )
        sandbox = daytona.create(params, timeout=120)

        try:
            sandbox.set_autostop_interval(SANDBOX_TTL_MINUTES)
            sandbox.set_auto_archive_interval(7 * 24 * 60)
        except Exception:
            pass

        # Install bun & kill stale processes
        _notify("Installing bun runtime...")
        _exec(sandbox, "curl -fsSL https://bun.sh/install | bash", timeout=60)
        sandbox.process.exec("pkill -f next || true; pkill -f bun || true")

        # Scaffold Next.js project (latest — TypeScript + Tailwind + App Router)
        _notify("Scaffolding Next.js project...")
        _exec(
            sandbox,
            f"{BUN_BIN} create next-app@latest {PROJECT_PATH} "
            f"--typescript --tailwind --eslint --app --use-bun --yes",
            timeout=90,
        )

        # Install extra packages in one shot (skip separate bun install — scaffold already did it)
        _notify("Installing extra packages...")
        _exec(
            sandbox,
            f"{BUN_BIN} add --cwd {PROJECT_PATH} {' '.join(EXTRA_PACKAGES)}",
            timeout=90,
        )

        # Upload ErrorBoundary component
        sandbox.process.exec(f"mkdir -p {PROJECT_PATH}/components", timeout=5)
        sandbox.fs.upload_file(
            ERROR_BOUNDARY_TSX.strip().encode("utf-8"),
            f"{PROJECT_PATH}/components/ErrorBoundary.tsx",
        )

        # Start dev server
        _notify("Starting Next.js dev server...")
        log_file = f"{PROJECT_PATH}/server.log"
        start_cmd = (
            f"nohup {BUN_BIN} --cwd {PROJECT_PATH} --bun next dev -p 3000 -H 0.0.0.0 "
            f"> {log_file} 2>&1 &"
        )
        sandbox.process.exec(start_cmd)

        # Wait for dev server — shorter poll (15 attempts x 2s = 30s max)
        _notify("Waiting for compilation...")
        ready = False
        for _ in range(15):
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

        # Use signed preview URL for iframe embedding (no auth header needed)
        try:
            signed = sandbox.create_signed_preview_url(3000, expires_in_seconds=7200)
            preview_url = signed.url if hasattr(signed, 'url') else str(signed)
        except Exception:
            # Fallback to regular preview link
            preview = sandbox.get_preview_link(3000)
            preview_url = preview.url

        # Read key project files
        initial_files = _read_sandbox_files(sandbox, PROJECT_PATH)
        _notify(f"Sandbox ready — {len(initial_files)} files loaded")

        return {
            "preview_url": preview_url,
            "sandbox_id": sandbox.id,
            "project_root": PROJECT_PATH,
            "initial_files": initial_files,
        }

    # Retry entire sandbox creation once on failure
    try:
        return await asyncio.to_thread(_create)
    except Exception as first_err:
        _notify(f"Sandbox creation failed ({first_err}), retrying...")
        try:
            return await asyncio.to_thread(_create)
        except Exception:
            raise first_err  # Raise original error


async def stop_sandbox(sandbox_id: str, delete: bool = False):
    """Stop or delete a Daytona sandbox. Updates Supabase is_active."""
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

    # Remove from active tracking
    from app.agent import active_sandboxes
    active_sandboxes.pop(sandbox_id, None)

    # Mark inactive in Supabase
    try:
        from app.database import _get_client as get_db
        db = get_db()
        db.table("clones").update({"is_active": False}).eq("sandbox_id", sandbox_id).execute()
    except Exception as e:
        print(f"[stop_sandbox] DB update failed: {e}")


async def start_sandbox(sandbox_id: str) -> dict:
    """Start a stopped Daytona sandbox and restart the dev server."""
    def _start():
        daytona = get_daytona_client()
        sandbox = daytona.get(sandbox_id)
        daytona.start(sandbox)

        # Set auto-stop timer
        try:
            sandbox.set_autostop_interval(SANDBOX_TTL_MINUTES)
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

        # Use signed preview URL for iframe embedding
        try:
            signed = sandbox.create_signed_preview_url(3000, expires_in_seconds=7200)
            preview_url = signed.url if hasattr(signed, 'url') else str(signed)
        except Exception:
            preview = sandbox.get_preview_link(3000)
            preview_url = preview.url
        print(f"Sandbox {sandbox_id} started — preview: {preview_url}")
        return {
            "preview_url": preview_url,
            "sandbox_id": sandbox.id,
            "project_root": PROJECT_PATH,
        }

    return await asyncio.to_thread(_start)


# ---------------------------------------------------------------------------
# Background Sandbox Monitor — checks for expired sandboxes, updates Supabase
# ---------------------------------------------------------------------------

async def _sandbox_monitor_loop():
    """
    Periodically check all tracked sandboxes.
    - Sandboxes that have been stopped by Daytona's auto-stop → mark inactive in Supabase
    - Sandboxes running beyond TTL with no user activity → clean up
    """
    while True:
        await asyncio.sleep(5 * 60)  # every 5 minutes
        try:
            from app.agent import active_sandboxes

            sandbox_ids = list(active_sandboxes.keys())
            if not sandbox_ids:
                continue

            print(f"[sandbox-monitor] Checking {len(sandbox_ids)} sandbox(es)...")

            for sid in sandbox_ids:
                try:
                    info = active_sandboxes.get(sid, {})
                    created_at = info.get("created_at", 0)
                    age_minutes = (time.time() - created_at) / 60 if created_at else 0

                    # Check if sandbox is still reachable
                    def _check(sandbox_id=sid):
                        try:
                            daytona = get_daytona_client()
                            sandbox = daytona.get(sandbox_id)
                            # If we can get it, it's still alive
                            return True
                        except Exception:
                            return False

                    is_alive = await asyncio.to_thread(_check)

                    if not is_alive:
                        print(f"[sandbox-monitor] {sid[:12]} is stopped/gone — marking inactive")
                        active_sandboxes.pop(sid, None)

                        # Update Supabase
                        try:
                            from app.database import _get_client as get_db
                            db = get_db()
                            db.table("clones").update(
                                {"is_active": False}
                            ).eq("sandbox_id", sid).execute()
                        except Exception as e:
                            print(f"[sandbox-monitor] DB update failed for {sid[:12]}: {e}")

                    elif age_minutes > 0:
                        print(f"[sandbox-monitor] {sid[:12]} alive ({age_minutes:.0f}m old)")

                except Exception as e:
                    print(f"[sandbox-monitor] Error checking {sid[:12]}: {e}")

        except Exception as e:
            print(f"[sandbox-monitor] Loop error: {e}")


def start_sandbox_monitor():
    """Start the sandbox monitor background task. Call from server lifespan."""
    asyncio.create_task(_sandbox_monitor_loop())
