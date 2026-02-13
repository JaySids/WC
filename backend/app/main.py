from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: sandbox status checker
    try:
        from app.sandbox import start_sandbox_monitor
        start_sandbox_monitor()
    except Exception as e:
        print(f"[sandbox-monitor] Failed to start: {e}")
    yield


app = FastAPI(title="Backend API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CloneRequest(BaseModel):
    url: str


class CloneResponse(BaseModel):
    clone_id: str
    preview_url: str | None = None
    status: str
    iterations: int | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"message": "Backend is running"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/clone", response_model=CloneResponse)
async def clone_website_endpoint(request: CloneRequest):
    """Clone a website as a React app."""
    url = request.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        from app.agent import run_clone_agent
        agent_result = await asyncio.wait_for(
            run_clone_agent(url),
            timeout=600,
        )

        return CloneResponse(
            clone_id=agent_result.get("clone_id", "no-db"),
            preview_url=agent_result.get("preview_url"),
            status=agent_result["status"],
            iterations=agent_result.get("iterations"),
        )

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Clone timed out. Try a simpler page.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Clone failed: {str(e)}")


@app.get("/clones")
async def list_clones(limit: int = 20):
    """List recent clones from Supabase."""
    try:
        from app.database import get_clones
        clones = await get_clones(limit=min(limit, 50))
        return {"clones": clones}
    except Exception as e:
        return {"clones": [], "error": str(e)}


@app.get("/clone/{clone_id}")
async def get_clone_detail(clone_id: str):
    """Get full details of a clone from Supabase."""
    try:
        from app.database import get_clone
        clone = await get_clone(clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Clone not found")
        return clone
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/clone/{clone_id}")
async def delete_clone_endpoint(clone_id: str):
    """Delete a clone and its sandbox permanently."""
    try:
        from app.database import get_clone, delete_clone
        clone = await get_clone(clone_id)
        if clone and clone.get("sandbox_id"):
            from app.sandbox import stop_sandbox
            await stop_sandbox(clone["sandbox_id"], delete=True)
        await delete_clone(clone_id)
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ToggleActiveRequest(BaseModel):
    is_active: bool


@app.patch("/clone/{clone_id}/active")
async def toggle_clone_active_endpoint(clone_id: str, request: ToggleActiveRequest):
    """Toggle a clone's active status. Deletes the sandbox if deactivating."""
    try:
        from app.database import get_clone, toggle_clone_active, update_clone, sync_files_to_supabase
        from app.agent import _chat_sessions
        clone = await get_clone(clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Clone not found")

        if not request.is_active and clone.get("sandbox_id"):
            # GUARD: don't destroy sandbox for a clone that's still processing
            if clone.get("status") == "processing":
                raise HTTPException(status_code=409, detail="Clone is still processing — cannot deactivate")

            # Sync latest files to Supabase before destroying
            session = _chat_sessions.get(clone_id)
            if session and session.get("files"):
                try:
                    await sync_files_to_supabase(clone_id, session["files"])
                except Exception as e:
                    print(f"[toggle_active] File sync failed: {e}")

            # DELETE the sandbox (not just stop) to free resources
            try:
                from app.sandbox import stop_sandbox
                await stop_sandbox(clone["sandbox_id"], delete=True)
            except Exception as e:
                print(f"Failed to delete sandbox: {e}")

            # Clear sandbox_id since it no longer exists
            await update_clone(clone_id, {"sandbox_id": None})

            # Clean up in-memory session
            _chat_sessions.pop(clone_id, None)

        updated = await toggle_clone_active(clone_id, request.is_active)
        return {"status": "updated", "is_active": request.is_active, "clone": updated}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/clone/{clone_id}/deactivate")
async def deactivate_clone_endpoint(clone_id: str):
    """
    Deactivate a clone: sync files to Supabase, delete the sandbox, mark inactive.
    Designed to be fire-and-forget from the frontend — never raises HTTPException.

    IMPORTANT: Refuses to deactivate clones that are still processing (status="processing")
    to prevent destroying sandboxes mid-clone.
    """
    try:
        from app.database import get_clone, update_clone, sync_files_to_supabase
        from app.agent import _chat_sessions

        clone = await get_clone(clone_id)
        if not clone:
            return {"status": "not_found"}

        # GUARD: never destroy a sandbox for a clone that's still processing
        if clone.get("status") == "processing":
            print(f"[deactivate] BLOCKED — clone {clone_id[:12]} is still processing, refusing to delete sandbox")
            return {"status": "blocked", "reason": "clone is still processing"}

        # Sync latest in-memory files before destroying
        session = _chat_sessions.get(clone_id)
        if session and session.get("files"):
            try:
                await sync_files_to_supabase(clone_id, session["files"])
            except Exception as e:
                print(f"[deactivate] File sync failed: {e}")

        # Delete the Daytona sandbox
        sandbox_id = clone.get("sandbox_id")
        if sandbox_id:
            try:
                from app.sandbox import stop_sandbox
                await stop_sandbox(sandbox_id, delete=True)
            except Exception as e:
                print(f"[deactivate] Sandbox delete failed: {e}")

        # Update DB: mark inactive, clear sandbox_id
        await update_clone(clone_id, {
            "is_active": False,
            "sandbox_id": None,
        })

        # Clean up in-memory session
        _chat_sessions.pop(clone_id, None)

        return {"status": "deactivated"}
    except Exception as e:
        print(f"[deactivate] Error: {e}")
        return {"status": "error", "message": str(e)}


@app.post("/clone/{clone_id}/rebuild")
async def rebuild_clone_endpoint(clone_id: str):
    """
    Rebuild a previous clone by creating a brand-new sandbox and uploading saved files.
    Old sandbox is always deleted on navigate-away, so we always create fresh.
    """
    try:
        from app.database import get_clone, update_clone
        clone = await get_clone(clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Clone not found")

        metadata = clone.get("metadata") or {}
        saved_files = metadata.get("files") or {}
        if not saved_files:
            raise HTTPException(status_code=400, detail="No saved files to rebuild from")

        # Always create a fresh sandbox
        from app.sandbox import create_react_boilerplate_sandbox
        sandbox_result = await create_react_boilerplate_sandbox()
        project_root = sandbox_result.get("project_root", "/home/daytona/my-app")

        from app.sandbox_template import upload_files_to_sandbox
        from app.sandbox import get_daytona_client, BUN_BIN
        from app.agent import _restart_dev_server, active_sandboxes, _chat_sessions
        import time

        # Remove conflicting .tsx files (scaffolded defaults) if we have .jsx versions
        tsx_to_remove = [fp.replace(".jsx", ".tsx") for fp in saved_files if fp.endswith(".jsx")]
        if tsx_to_remove:
            def _remove_tsx():
                try:
                    daytona = get_daytona_client()
                    sb = daytona.get(sandbox_result["sandbox_id"])
                    paths = " ".join(f"{project_root}/{p}" for p in tsx_to_remove)
                    sb.process.exec(f"rm -f {paths}", timeout=10)
                except Exception:
                    pass
            await asyncio.to_thread(_remove_tsx)

        await upload_files_to_sandbox(
            sandbox_result["sandbox_id"],
            saved_files,
            project_root=project_root,
        )

        # Restart the dev server after the tsx→jsx swap — without this the
        # running Next.js process crashes on the file type change and never
        # picks up the new files.  _restart_dev_server now includes an HTTP
        # health check, so no extra sleep is needed.
        await _restart_dev_server(sandbox_result["sandbox_id"], project_root)

        await update_clone(clone_id, {
            "sandbox_id": sandbox_result["sandbox_id"],
            "preview_url": sandbox_result["preview_url"],
            "is_active": True,
        })

        active_sandboxes[sandbox_result["sandbox_id"]] = {
            **sandbox_result,
            "created_at": time.time(),
        }

        # Restore chat session
        _chat_sessions[clone_id] = {
            "files": saved_files,
            "state": {
                "sandbox_id": sandbox_result["sandbox_id"],
                "preview_url": sandbox_result["preview_url"],
                "project_root": project_root,
                "files": saved_files,
                "clone_id": clone_id,
                "output_format": "react",
            },
            "scrape_data": {},
        }

        return {
            "status": "rebuilt",
            "preview_url": sandbox_result["preview_url"],
            "sandbox_id": sandbox_result["sandbox_id"],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# SSE Streaming Endpoints
# ---------------------------------------------------------------------------

@app.post("/clone/stream")
async def clone_stream(request: CloneRequest):
    """Clone a website with real-time streaming progress via SSE."""
    from app.agent import run_clone_agent_streaming
    from app.sse_utils import sse_event

    url = request.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async def event_stream():
        got_done = False
        try:
            async for event in run_clone_agent_streaming(url):
                if '"type": "done"' in event or '"type":"done"' in event:
                    got_done = True
                yield event
        except Exception as e:
            print(f"[clone/stream] Generator crashed: {e}")
            yield sse_event("error", {"message": f"Server error: {e}"})
        finally:
            if not got_done:
                yield sse_event("done", {"preview_url": None, "error": "Stream ended unexpectedly"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


class ChatRequest(BaseModel):
    message: str


@app.post("/clone/{clone_id}/chat")
async def clone_chat(clone_id: str, request: ChatRequest):
    """Send a follow-up message to the agent about an existing clone."""
    from app.agent import run_chat_followup
    from app.sse_utils import sse_event

    if not request.message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    async def event_stream():
        got_done = False
        try:
            async for event in run_chat_followup(clone_id, request.message.strip()):
                if '"type": "done"' in event or '"type":"done"' in event:
                    got_done = True
                yield event
        except Exception as e:
            print(f"[clone/{clone_id}/chat] Generator crashed: {e}")
            yield sse_event("error", {"message": f"Server error: {e}"})
        finally:
            if not got_done:
                yield sse_event("done", {"preview_url": None, "error": "Chat stream ended unexpectedly"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/clone/{clone_id}/files")
async def get_clone_files(clone_id: str):
    """Get all generated files for a clone (session first, then Supabase fallback)."""
    from app.agent import _chat_sessions
    session = _chat_sessions.get(clone_id)
    if session:
        return {"files": session["state"].get("files", {})}

    try:
        from app.database import get_clone
        clone = await get_clone(clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Clone not found")
        metadata = clone.get("metadata", {}) or {}
        files = metadata.get("files", {})
        return {"files": files}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/clone/logs")
async def get_clone_logs(
    clone_id: str | None = None,
    sandbox_id: str | None = None,
    lines: int = 200,
):
    """
    Poll Next.js dev server logs from a Daytona sandbox.
    Accepts clone_id or sandbox_id.
    """
    if not sandbox_id and not clone_id:
        raise HTTPException(status_code=400, detail="clone_id or sandbox_id required")

    project_root = None
    if clone_id:
        from app.agent import _chat_sessions
        session = _chat_sessions.get(clone_id)
        if session:
            sandbox_id = sandbox_id or session["state"].get("sandbox_id")
            project_root = session["state"].get("project_root")

    if not sandbox_id and clone_id:
        try:
            from app.database import get_clone
            clone = await get_clone(clone_id)
            if clone:
                sandbox_id = clone.get("sandbox_id")
        except Exception:
            pass

    if not sandbox_id:
        raise HTTPException(status_code=404, detail="Sandbox not found for clone_id")

    from app.sandbox_template import get_sandbox_logs
    from app.sandbox import PROJECT_PATH
    logs = await get_sandbox_logs(sandbox_id, project_root or PROJECT_PATH, lines=lines)
    return {"sandbox_id": sandbox_id, "logs": logs}


@app.put("/clone/{clone_id}/files/{filepath:path}")
async def update_clone_file(clone_id: str, filepath: str, request: dict):
    """
    User manually edits a file via the frontend editor.
    Uploads to sandbox and triggers Next.js hot-reload.
    """
    content = request.get("content", "")

    from app.agent import hot_fix_file
    result = await hot_fix_file(clone_id, filepath, content)

    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message", "Update failed"))

    return result


# ---------------------------------------------------------------------------
# Stop & Prune
# ---------------------------------------------------------------------------

class StopCloneRequest(BaseModel):
    sandbox_id: str | None = None
    clone_id: str | None = None


@app.post("/clone/stop")
async def stop_clone_endpoint(request: StopCloneRequest):
    """Stop an in-progress clone and cleanup its Daytona sandbox."""
    sandbox_id = request.sandbox_id

    if not sandbox_id and request.clone_id:
        from app.agent import _chat_sessions
        session = _chat_sessions.get(request.clone_id)
        if session:
            sandbox_id = session["state"].get("sandbox_id")
        if not sandbox_id:
            try:
                from app.database import get_clone
                clone = await get_clone(request.clone_id)
                if clone:
                    sandbox_id = clone.get("sandbox_id")
            except Exception:
                pass

    if sandbox_id:
        try:
            from app.sandbox import stop_sandbox
            await stop_sandbox(sandbox_id, delete=True)
        except Exception as e:
            print(f"Failed to delete sandbox {sandbox_id}: {e}")

    if request.clone_id:
        try:
            from app.database import update_clone
            await update_clone(request.clone_id, {"status": "stopped", "is_active": False})
        except Exception:
            pass

    return {"status": "stopped", "sandbox_id": sandbox_id}


@app.post("/sandboxes/cleanup")
async def cleanup_all_sandboxes():
    """
    Delete ALL active Daytona sandboxes on startup.
    Preserves all Supabase data (files, metadata, history) — only removes
    the live sandbox references so clones can still be reactivated later.

    Runs actual Daytona deletions in a background task so the endpoint
    returns immediately and does NOT hold _daytona_lock while the user
    starts a new clone.
    """
    from app.agent import active_sandboxes, _chat_sessions
    from app.database import _get_client as get_db

    # Collect sandbox IDs to delete BEFORE clearing in-memory state
    sandbox_ids_to_delete: set[str] = set(active_sandboxes.keys())

    # Immediately clear in-memory state so new clones don't collide
    active_sandboxes.clear()
    _chat_sessions.clear()

    # Also grab sandbox IDs from DB
    try:
        db = get_db()
        result = (
            db.table("clones")
            .select("id, sandbox_id")
            .not_.is_("sandbox_id", "null")
            .execute()
        )
        for row in (result.data or []):
            sid = row.get("sandbox_id")
            if sid:
                sandbox_ids_to_delete.add(sid)

        # Immediately clear sandbox references in DB — files/metadata stay intact
        db.table("clones").update({
            "is_active": False,
            "sandbox_id": None,
        }).not_.is_("sandbox_id", "null").execute()
    except Exception as e:
        print(f"[cleanup] DB error: {e}")

    # Fire-and-forget: delete Daytona sandboxes in background so we don't
    # hold _daytona_lock and block new sandbox creation.
    async def _bg_delete():
        deleted = 0
        for sid in sandbox_ids_to_delete:
            try:
                from app.sandbox import stop_sandbox
                await stop_sandbox(sid, delete=True)
                deleted += 1
            except Exception as e:
                print(f"[cleanup-bg] Failed to delete {sid[:12]}: {e}")
        print(f"[cleanup-bg] Deleted {deleted}/{len(sandbox_ids_to_delete)} sandbox(es)")

    asyncio.create_task(_bg_delete())

    print(f"[cleanup] Queued {len(sandbox_ids_to_delete)} sandbox(es) for background deletion")
    return {"status": "cleaned", "queued": len(sandbox_ids_to_delete)}


@app.get("/clone/{clone_id}/export")
async def export_clone_files(clone_id: str):
    """
    Export all files for a clone as a downloadable .zip.
    Pulls from in-memory session first, falls back to Supabase metadata.
    """
    import io
    import zipfile
    from fastapi.responses import Response
    from app.agent import _chat_sessions

    # Get files from session or DB
    files = {}
    session = _chat_sessions.get(clone_id)
    if session:
        files = session.get("files") or session.get("state", {}).get("files", {})

    if not files:
        try:
            from app.database import get_clone
            clone = await get_clone(clone_id)
            if not clone:
                raise HTTPException(status_code=404, detail="Clone not found")
            metadata = clone.get("metadata") or {}
            files = metadata.get("files") or {}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    if not files:
        raise HTTPException(status_code=404, detail="No files found for this clone")

    # Build zip in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for filepath, content in files.items():
            zf.writestr(filepath, content)
    buf.seek(0)

    # Use clone_id prefix for the filename
    filename = f"clone-{clone_id[:8]}.zip"

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
