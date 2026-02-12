from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: keep-alive pinger for active sandboxes
    try:
        from app.sandbox import start_keep_alive
        start_keep_alive()
    except Exception as e:
        print(f"[keep-alive] Failed to start: {e}")
    yield
    # Shutdown (nothing needed)


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
    output_format: str = "html"  # "html", "react", or "snapshot"


class CloneResponse(BaseModel):
    clone_id: str
    preview_url: str | None = None
    html: str | None = None
    status: str
    delivery: str  # "sandbox" or "inline" or "failed"
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
    """
    Clone a website. Uses MCP agent architecture:
    Claude orchestrates scraping, code generation, deployment, and self-correction.
    """

    # Validate URL
    url = request.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Create pending record in Supabase (graceful if Supabase fails)
    clone_id = None
    try:
        from app.database import save_clone, update_clone
        record = await save_clone({"url": url, "status": "processing"})
        clone_id = record.get("id")
    except Exception as e:
        print(f"Supabase save failed (continuing without history): {e}")

    # === SNAPSHOT MODE: grab rendered DOM directly, no AI ===
    if request.output_format == "snapshot":
        try:
            from app.scraper import extract_snapshot
            from app.sandbox import deploy_html_to_sandbox

            print(f"Snapshot mode: extracting DOM from {url}")
            snapshot_html = await asyncio.wait_for(
                extract_snapshot(url),
                timeout=60,
            )
            print(f"Snapshot extracted ({len(snapshot_html)} bytes), deploying...")

            deploy_result = await asyncio.wait_for(
                deploy_html_to_sandbox(snapshot_html),
                timeout=30,
            )

            if clone_id:
                try:
                    from app.database import update_clone
                    await update_clone(clone_id, {
                        "status": "success",
                        "preview_url": deploy_result["preview_url"],
                        "sandbox_id": deploy_result["sandbox_id"],
                        "html": snapshot_html if len(snapshot_html) < 500_000 else None,
                    })
                except Exception:
                    pass

            return CloneResponse(
                clone_id=clone_id or "no-db",
                preview_url=deploy_result["preview_url"],
                html=None,
                status="success",
                delivery="sandbox",
                iterations=0,
            )
        except Exception as e:
            print(f"Snapshot mode failed: {e}, falling back to inline")
            # If sandbox deploy fails, return HTML inline
            try:
                return CloneResponse(
                    clone_id=clone_id or "no-db",
                    preview_url=None,
                    html=snapshot_html if 'snapshot_html' in dir() else None,
                    status="success" if 'snapshot_html' in dir() else "failed",
                    delivery="inline" if 'snapshot_html' in dir() else "failed",
                    iterations=0,
                )
            except Exception:
                raise HTTPException(status_code=500, detail=f"Snapshot failed: {str(e)}")

    try:
        # Run the agent loop
        from app.agent import run_clone_agent
        agent_result = await asyncio.wait_for(
            run_clone_agent(url, output_format=request.output_format),
            timeout=600,  # 10 minutes max for agent loop
        )

        # Update Supabase record
        if clone_id:
            try:
                from app.database import update_clone
                update_data = {
                    "status": agent_result["status"],
                    "html": agent_result.get("html"),
                    "metadata": {
                        "iterations": agent_result.get("iterations"),
                        "output_format": request.output_format,
                    },
                }
                if agent_result.get("preview_url"):
                    update_data["preview_url"] = agent_result["preview_url"]
                if agent_result.get("sandbox_id"):
                    update_data["sandbox_id"] = agent_result["sandbox_id"]
                await update_clone(clone_id, update_data)
            except Exception as e:
                print(f"Supabase update failed: {e}")

        # Determine delivery mode
        if agent_result.get("preview_url"):
            delivery = "sandbox"
        elif agent_result.get("html"):
            delivery = "inline"
        else:
            delivery = "failed"

        return CloneResponse(
            clone_id=clone_id or "no-db",
            preview_url=agent_result.get("preview_url"),
            html=agent_result.get("html") if delivery == "inline" else None,
            status=agent_result["status"],
            delivery=delivery,
            iterations=agent_result.get("iterations"),
        )

    except asyncio.TimeoutError:
        if clone_id:
            try:
                from app.database import update_clone
                await update_clone(clone_id, {
                    "status": "failed",
                    "error_message": "Clone timed out after 300 seconds",
                })
            except Exception:
                pass
        raise HTTPException(status_code=504, detail="Clone timed out. Try a simpler page.")

    except Exception as e:
        if clone_id:
            try:
                from app.database import update_clone
                await update_clone(clone_id, {
                    "status": "failed",
                    "error_message": str(e),
                })
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"Clone failed: {str(e)}")


@app.get("/clones")
async def list_clones(limit: int = 20):
    """List recent clones."""
    try:
        from app.database import get_clones
        clones = await get_clones(limit=min(limit, 50))
        return {"clones": clones}
    except Exception as e:
        return {"clones": [], "error": str(e)}


@app.get("/clone/{clone_id}")
async def get_clone_detail(clone_id: str):
    """Get full details of a clone including HTML."""
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
    """Toggle a clone's active status. Stops the sandbox if deactivating."""
    try:
        from app.database import get_clone, toggle_clone_active
        clone = await get_clone(clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Clone not found")

        # If deactivating, stop the sandbox
        if not request.is_active and clone.get("sandbox_id"):
            try:
                from app.sandbox import stop_sandbox
                await stop_sandbox(clone["sandbox_id"])
            except Exception as e:
                print(f"Failed to stop sandbox: {e}")

        updated = await toggle_clone_active(clone_id, request.is_active)
        return {"status": "updated", "is_active": request.is_active, "clone": updated}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/clone/{clone_id}/reactivate")
async def reactivate_clone_endpoint(clone_id: str):
    """
    Reactivate an inactive clone by starting its existing sandbox.
    Falls back to creating a new sandbox if the old one is gone.
    """
    try:
        from app.database import get_clone, update_clone
        clone = await get_clone(clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Clone not found")

        sandbox_id = clone.get("sandbox_id")

        # Try to start the existing sandbox first
        if sandbox_id:
            try:
                from app.sandbox import start_sandbox
                sandbox_result = await start_sandbox(sandbox_id)

                await update_clone(clone_id, {
                    "preview_url": sandbox_result["preview_url"],
                    "is_active": True,
                })

                from app.tool_handlers import active_sandboxes
                active_sandboxes[sandbox_result["sandbox_id"]] = sandbox_result

                return {
                    "status": "reactivated",
                    "preview_url": sandbox_result["preview_url"],
                    "sandbox_id": sandbox_result["sandbox_id"],
                }
            except Exception as e:
                print(f"Failed to restart existing sandbox {sandbox_id}: {e}, creating new one...")

        # Fallback: create a new sandbox
        metadata = clone.get("metadata") or {}
        saved_files = metadata.get("files") or {}
        output_format = clone.get("output_format") or metadata.get("output_format", "html")

        if output_format == "react":
            from app.sandbox import create_react_boilerplate_sandbox
            sandbox_result = await create_react_boilerplate_sandbox()
            project_root = sandbox_result["project_root"]

            if saved_files:
                from app.sandbox import get_daytona_client
                def _upload_files():
                    daytona = get_daytona_client()
                    sandbox = daytona.get(sandbox_result["sandbox_id"])
                    for filepath, content in saved_files.items():
                        full_path = f"{project_root}/{filepath}"
                        sandbox.fs.upload_file(content.encode(), full_path)
                await asyncio.to_thread(_upload_files)
        else:
            html_content = saved_files.get("index.html") or clone.get("html", "")
            if not html_content:
                raise HTTPException(status_code=400, detail="No saved HTML content to reactivate")
            from app.sandbox import deploy_html_to_sandbox
            sandbox_result = await deploy_html_to_sandbox(html_content)

        await update_clone(clone_id, {
            "sandbox_id": sandbox_result["sandbox_id"],
            "preview_url": sandbox_result["preview_url"],
            "is_active": True,
        })

        from app.tool_handlers import active_sandboxes
        active_sandboxes[sandbox_result["sandbox_id"]] = sandbox_result

        return {
            "status": "reactivated",
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

    url = request.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    async def event_stream():
        async for event in run_clone_agent_streaming(url, request.output_format):
            yield event

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

    if not request.message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    async def event_stream():
        async for event in run_chat_followup(clone_id, request.message.strip()):
            yield event

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
    """Get all generated files for a clone."""
    from app.agent import _chat_sessions
    session = _chat_sessions.get(clone_id)
    if session:
        return {"files": session["state"].get("files", {})}

    # Fallback to Supabase
    try:
        from app.database import get_clone
        clone = await get_clone(clone_id)
        if not clone:
            raise HTTPException(status_code=404, detail="Clone not found")
        metadata = clone.get("metadata", {}) or {}
        files = metadata.get("files", {})
        if not files and clone.get("html"):
            files = {"index.html": clone["html"]}
        return {"files": files}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/clone/{clone_id}/files/{filepath:path}")
async def update_clone_file(clone_id: str, filepath: str, request: dict):
    """User manually edits a file. Updates the sandbox in real-time."""
    content = request.get("content", "")

    # Update in active session
    from app.agent import _chat_sessions
    session = _chat_sessions.get(clone_id)
    sandbox_id = None
    if session:
        sandbox_id = session["state"].get("sandbox_id")
        session["state"]["files"][filepath] = content

    if sandbox_id:
        from app.tool_handlers import handle_update_file
        await handle_update_file({
            "sandbox_id": sandbox_id,
            "filepath": filepath,
            "content": content,
        })

    # Return output_format so frontend knows whether to reload iframe
    fmt = None
    if session:
        fmt = session["state"].get("output_format")
    return {"status": "updated", "filepath": filepath, "output_format": fmt}


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

    # If we only have clone_id, look up sandbox_id from session or DB
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
            await stop_sandbox(sandbox_id)
        except Exception as e:
            print(f"Failed to stop sandbox {sandbox_id}: {e}")

    if request.clone_id:
        try:
            from app.database import update_clone
            await update_clone(request.clone_id, {"status": "stopped"})
        except Exception:
            pass

    return {"status": "stopped", "sandbox_id": sandbox_id}


