# Streaming Agent UI + Chat — Claude Code Prompt

We want the cloning agent to stream its progress to the frontend in real-time, show files as they're generated, and let the user chat with the agent to request fixes.

**Read all existing code first. This builds ON TOP of the agent architecture.**

---

## What the User Sees

```
┌──────────────────────────────────────────────────────────────────┐
│  Website Cloner                                                   │
├──────────────────────────┬───────────────────────────────────────┤
│                          │                                       │
│  💬 Agent Chat           │        Live Preview                   │
│                          │        (iframe)                       │
│  ┌────────────────────┐  │                                       │
│  │ 🌐 Scraping        │  │                                       │
│  │ stripe.com...      │  │                                       │
│  │ Found 23 images,   │  │                                       │
│  │ 4 fonts, 31 links  │  │                                       │
│  └────────────────────┘  │                                       │
│                          │                                       │
│  ┌────────────────────┐  │                                       │
│  │ 🤖 Generating      │  │                                       │
│  │ React components   │  │                                       │
│  │                    │  │                                       │
│  │ 📄 App.jsx        │  │                                       │
│  │ 📄 Navbar.jsx     │  │                                       │
│  │ 📄 Hero.jsx       │  │                                       │
│  │ 📄 Features.jsx   │  │                                       │
│  │ 📄 Footer.jsx     │  │                                       │
│  └────────────────────┘  │                                       │
│                          │                                       │
│  ┌────────────────────┐  │                                       │
│  │ 🚀 Deployed!       │  │                                       │
│  │ Checking output... │  │                                       │
│  └────────────────────┘  │                                       │
│                          │                                       │
│  ┌────────────────────┐  │                                       │
│  │ 🔍 Comparing...    │  │                                       │
│  │ Found 3 issues:    │  │                                       │
│  │ - Header color off │  │                                       │
│  │ - Missing CTA btn  │  │                                       │
│  │ - Footer misaligned│  │                                       │
│  └────────────────────┘  │                                       │
│                          │                                       │
│  ┌────────────────────┐  │                                       │
│  │ 🔧 Fixing Hero.jsx │  │                                       │
│  │ Updated 3 files... │  │                                       │
│  │ ✅ All issues fixed │  │                                       │
│  └────────────────────┘  │                                       │
│                          │                                       │
│  [Make the header blue ] │                                       │
│  [Send]                  │                                       │
│                          │                                       │
├──────────────────────────┤                                       │
│  📁 Files                │                                       │
│  App.jsx | Navbar | Hero │                                       │
│  ┌────────────────────┐  │                                       │
│  │ import Navbar...   │  │                                       │
│  │ import Hero...     │  │                                       │
│  │                    │  │                                       │
│  │ export default...  │  │                                       │
│  └────────────────────┘  │                                       │
└──────────────────────────┴───────────────────────────────────────┘
```

Left side: agent chat log (streaming) + file viewer/editor at bottom
Right side: live preview iframe
Bottom of chat: user input for follow-up requests

---

## Architecture: Server-Sent Events (SSE)

```
Frontend                         Backend
   │                                │
   │  POST /clone/stream            │
   │  { url, output_format }        │
   │ ─────────────────────────────► │
   │                                │  Agent starts...
   │  SSE: {"type":"step",          │
   │        "step":"scraping"}      │
   │ ◄───────────────────────────── │
   │                                │  scrape_url completes
   │  SSE: {"type":"scrape_done",   │
   │        "images":23,"fonts":4}  │
   │ ◄───────────────────────────── │
   │                                │  Claude generates code
   │  SSE: {"type":"generating"}    │
   │ ◄───────────────────────────── │
   │                                │  
   │  SSE: {"type":"file",          │
   │        "path":"src/App.jsx",   │
   │        "content":"import..."}  │
   │ ◄───────────────────────────── │
   │                                │
   │  SSE: {"type":"file",          │
   │        "path":"src/Navbar.jsx",│
   │        "content":"export..."}  │
   │ ◄───────────────────────────── │
   │                                │  Deploy to sandbox
   │  SSE: {"type":"deploying"}     │
   │ ◄───────────────────────────── │
   │                                │
   │  SSE: {"type":"deployed",      │
   │        "preview_url":"https:.. │
   │ ◄───────────────────────────── │
   │                                │  Screenshot + compare
   │  SSE: {"type":"checking"}      │
   │ ◄───────────────────────────── │
   │                                │
   │  SSE: {"type":"fixing",        │
   │        "file":"Hero.jsx",      │
   │        "issues":["color..."]}  │
   │ ◄───────────────────────────── │
   │                                │
   │  SSE: {"type":"done",          │
   │        "preview_url":"...",    │
   │        "clone_id":"..."}       │
   │ ◄───────────────────────────── │
   │                                │
   │                                │
   │  POST /clone/{id}/chat         │
   │  { "message": "make header     │
   │     blue" }                    │
   │ ─────────────────────────────► │  User follow-up
   │                                │
   │  SSE: {"type":"fixing",...}    │
   │ ◄───────────────────────────── │  Agent fixes
   │                                │
   │  SSE: {"type":"done",...}      │
   │ ◄───────────────────────────── │
```

---

## Step 1: Backend — SSE Streaming Endpoint

Update `backend/app/agent.py` to yield events instead of returning a final result:

```python
"""
Streaming agent orchestrator.
Yields SSE events as the agent works.
"""
import httpx
import json
import os
import base64
import asyncio
from typing import AsyncGenerator
from app.mcp_tools import TOOLS
from app.tool_handlers import handle_tool_call, _scrape_cache

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
Call scrape_url first. You'll get exact hex colors, fonts, all image/font URLs from network traffic, link hrefs, SVGs, and text content.

### Step 2: Generate Code
Using ALL the scraped data, generate the website.

For HTML mode: a single self-contained HTML file with Tailwind CDN, Font Awesome, Google Fonts, and CSS reset.

For React mode: a Vite + React + Tailwind project with component files:
- src/App.jsx (root, imports all sections)
- src/components/Navbar.jsx, Hero.jsx, Features.jsx, Footer.jsx, etc.
- CRITICAL JSX RULES: className not class, self-close void elements (<img />, <br />), style={{}} objects, {/* comments */}

### Step 3: Deploy
Call generate_and_deploy_html or generate_and_deploy_react.

### Step 4: Verify
Call screenshot_preview to check your work. Compare to original.

### Step 5: Fix if needed
For React: call update_sandbox_file for specific components.
For HTML: regenerate and redeploy.

## RULES
- Use EXACT hex colors, image URLs, link hrefs, fonts from scraped data
- Write EVERY element. Never abbreviate with comments like "<!-- more items -->"
- No duplicate sections
- Responsive with Tailwind sm:, md:, lg:
- For React: ALWAYS use className, never class
- Aim for 2-3 iterations max"""


async def run_clone_agent_streaming(
    url: str,
    output_format: str = "html"
) -> AsyncGenerator[str, None]:
    """
    Run the agent loop, yielding SSE events as it progresses.
    Each yield is a JSON string representing one event.
    """
    
    # Track state
    state = {
        "preview_url": None,
        "sandbox_id": None,
        "files": {},
        "iterations": 0,
        "clone_id": None
    }
    
    # Save to Supabase
    try:
        from app.database import save_clone
        record = await save_clone({"url": url, "status": "processing"})
        state["clone_id"] = record["id"]
        yield sse_event("clone_created", {"clone_id": record["id"]})
    except Exception as e:
        yield sse_event("warning", {"message": f"Supabase unavailable: {e}"})
    
    # Build initial messages
    format_instruction = (
        "Use HTML mode (single HTML file with Tailwind)." 
        if output_format == "html" 
        else "Use React mode (Vite + React + Tailwind with separate component files)."
    )
    
    messages = [
        {"role": "user", "content": f"Clone this website: {url}\n\n{format_instruction}\n\nStart by scraping the URL."}
    ]
    
    for iteration in range(MAX_ITERATIONS):
        state["iterations"] = iteration + 1
        yield sse_event("iteration", {"current": iteration + 1, "max": MAX_ITERATIONS})
        
        # Call Claude
        yield sse_event("thinking", {"message": "Agent is thinking..."})
        
        response = await call_claude_with_tools(messages)
        
        if not response:
            yield sse_event("error", {"message": "Failed to get response from Claude"})
            break
        
        assistant_message = response["choices"][0]["message"]
        
        # Stream any text response from Claude
        text_content = assistant_message.get("content", "")
        if text_content:
            yield sse_event("agent_message", {"text": text_content})
        
        # Add assistant message to history
        messages.append({
            "role": "assistant",
            "content": text_content,
            "tool_calls": assistant_message.get("tool_calls")
        })
        
        # Check for tool calls
        tool_calls = assistant_message.get("tool_calls", [])
        
        if not tool_calls:
            yield sse_event("agent_done", {"message": "Agent finished"})
            break
        
        # Execute tool calls and stream each one
        tool_results = []
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            tool_input = json.loads(tc["function"]["arguments"])
            
            # --- Stream what's happening ---
            
            if tool_name == "scrape_url":
                yield sse_event("step", {
                    "step": "scraping",
                    "message": f"Scraping {tool_input['url']}...",
                    "icon": "🌐"
                })
            
            elif tool_name == "generate_and_deploy_html":
                yield sse_event("step", {
                    "step": "deploying_html",
                    "message": "Deploying HTML to sandbox...",
                    "icon": "🚀"
                })
                # Stream the HTML as a file
                yield sse_event("file", {
                    "path": "index.html",
                    "content": tool_input.get("html_content", "")[:50000],
                    "language": "html"
                })
                state["files"]["index.html"] = tool_input.get("html_content", "")
            
            elif tool_name == "generate_and_deploy_react":
                yield sse_event("step", {
                    "step": "deploying_react",
                    "message": "Deploying React app to sandbox...",
                    "icon": "🚀"
                })
                # Stream each file
                files = tool_input.get("files", {})
                for filepath, content in files.items():
                    yield sse_event("file", {
                        "path": filepath,
                        "content": content,
                        "language": "jsx" if filepath.endswith(".jsx") else "javascript"
                    })
                    state["files"][filepath] = content
            
            elif tool_name == "screenshot_preview":
                yield sse_event("step", {
                    "step": "checking",
                    "message": "Taking screenshot of clone...",
                    "icon": "🔍"
                })
            
            elif tool_name == "get_sandbox_logs":
                yield sse_event("step", {
                    "step": "checking_logs",
                    "message": "Checking for errors...",
                    "icon": "📋"
                })
            
            elif tool_name == "update_sandbox_file":
                filepath = tool_input.get("filepath", "unknown")
                yield sse_event("step", {
                    "step": "fixing",
                    "message": f"Fixing {filepath}...",
                    "icon": "🔧"
                })
                # Stream the updated file
                yield sse_event("file_updated", {
                    "path": filepath,
                    "content": tool_input.get("content", ""),
                    "language": "jsx" if filepath.endswith(".jsx") else "html"
                })
                state["files"][filepath] = tool_input.get("content", "")
            
            # --- Execute the tool ---
            tool_result = await handle_tool_call(tool_name, tool_input)
            
            # --- Stream results ---
            try:
                parsed = json.loads(tool_result)
                
                if tool_name == "scrape_url":
                    yield sse_event("scrape_done", {
                        "title": parsed.get("title", ""),
                        "images": len(parsed.get("assets", {}).get("images", [])),
                        "fonts": len(parsed.get("assets", {}).get("fonts", [])),
                        "links": len(parsed.get("clickables", {}).get("all_links", parsed.get("clickables", {}).get("nav_links", []))),
                        "page_height": parsed.get("page_height", 0)
                    })
                
                elif "preview_url" in parsed:
                    state["preview_url"] = parsed["preview_url"]
                    state["sandbox_id"] = parsed.get("sandbox_id")
                    yield sse_event("deployed", {
                        "preview_url": parsed["preview_url"],
                        "sandbox_id": parsed.get("sandbox_id")
                    })
                
                elif "screenshot_b64" in parsed:
                    yield sse_event("screenshot", {
                        "image_b64": parsed["screenshot_b64"]
                    })
                
                elif "logs" in parsed:
                    logs = parsed["logs"]
                    has_errors = any(p in logs for p in ["Error", "error", "SyntaxError", "Cannot find"])
                    yield sse_event("logs", {
                        "content": logs[:1000],
                        "has_errors": has_errors
                    })
                
                elif "error" in parsed:
                    yield sse_event("tool_error", {
                        "tool": tool_name,
                        "error": parsed["error"]
                    })
            
            except json.JSONDecodeError:
                pass
            
            tool_results.append({
                "tool_call_id": tc["id"],
                "role": "tool",
                "content": tool_result
            })
        
        # Add tool results to conversation
        messages.extend(tool_results)
        
        # Send screenshots as images back to Claude
        for tc, tr in zip(tool_calls, tool_results):
            if tc["function"]["name"] == "screenshot_preview":
                try:
                    parsed = json.loads(tr["content"])
                    if "screenshot_b64" in parsed:
                        messages.append({
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{parsed['screenshot_b64']}"}},
                                {"type": "text", "text": "Here's the screenshot of the clone. Compare to the original and decide if fixes are needed."}
                            ]
                        })
                except:
                    pass
    
    # Final event
    yield sse_event("done", {
        "preview_url": state.get("preview_url"),
        "sandbox_id": state.get("sandbox_id"),
        "clone_id": state.get("clone_id"),
        "files": list(state.get("files", {}).keys()),
        "iterations": state["iterations"]
    })
    
    # Update Supabase
    try:
        from app.database import update_clone
        if state.get("clone_id"):
            await update_clone(state["clone_id"], {
                "status": "success" if state.get("preview_url") else "failed",
                "preview_url": state.get("preview_url"),
                "sandbox_id": state.get("sandbox_id"),
                "metadata": {
                    "files": state.get("files"),
                    "iterations": state["iterations"],
                    "output_format": output_format
                }
            })
    except:
        pass
    
    # Store messages for chat continuation
    _chat_sessions[state.get("clone_id", "")] = {
        "messages": messages,
        "state": state
    }


# Chat session storage
_chat_sessions = {}


async def run_chat_followup(
    clone_id: str,
    user_message: str
) -> AsyncGenerator[str, None]:
    """
    Handle a user follow-up message in the chat.
    Continues the existing agent conversation.
    """
    session = _chat_sessions.get(clone_id)
    if not session:
        yield sse_event("error", {"message": "No active session for this clone. Try cloning again."})
        return
    
    messages = session["messages"]
    state = session["state"]
    
    # Add user's message
    messages.append({
        "role": "user",
        "content": user_message
    })
    
    yield sse_event("user_message", {"text": user_message})
    
    # Run agent for up to 4 iterations on follow-ups
    for iteration in range(4):
        yield sse_event("thinking", {"message": "Agent is thinking..."})
        
        response = await call_claude_with_tools(messages)
        if not response:
            yield sse_event("error", {"message": "Failed to get response"})
            break
        
        assistant_message = response["choices"][0]["message"]
        text_content = assistant_message.get("content", "")
        
        if text_content:
            yield sse_event("agent_message", {"text": text_content})
        
        messages.append({
            "role": "assistant",
            "content": text_content,
            "tool_calls": assistant_message.get("tool_calls")
        })
        
        tool_calls = assistant_message.get("tool_calls", [])
        if not tool_calls:
            break
        
        # Execute tools (same logic as above)
        tool_results = []
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            tool_input = json.loads(tc["function"]["arguments"])
            
            if tool_name == "update_sandbox_file":
                filepath = tool_input.get("filepath", "")
                yield sse_event("step", {
                    "step": "fixing",
                    "message": f"Updating {filepath}...",
                    "icon": "🔧"
                })
                yield sse_event("file_updated", {
                    "path": filepath,
                    "content": tool_input.get("content", ""),
                    "language": "jsx" if filepath.endswith(".jsx") else "html"
                })
                state["files"][filepath] = tool_input.get("content", "")
            
            elif tool_name == "screenshot_preview":
                yield sse_event("step", {
                    "step": "checking",
                    "message": "Verifying changes...",
                    "icon": "🔍"
                })
            
            elif tool_name in ("generate_and_deploy_html", "generate_and_deploy_react"):
                yield sse_event("step", {
                    "step": "redeploying",
                    "message": "Redeploying with changes...",
                    "icon": "🚀"
                })
                if tool_name == "generate_and_deploy_react":
                    files = tool_input.get("files", {})
                    for fp, content in files.items():
                        yield sse_event("file_updated", {"path": fp, "content": content, "language": "jsx"})
                        state["files"][fp] = content
            
            tool_result = await handle_tool_call(tool_name, tool_input)
            
            try:
                parsed = json.loads(tool_result)
                if "preview_url" in parsed:
                    state["preview_url"] = parsed["preview_url"]
                    yield sse_event("deployed", {"preview_url": parsed["preview_url"]})
                if "screenshot_b64" in parsed:
                    yield sse_event("screenshot", {"image_b64": parsed["screenshot_b64"]})
            except:
                pass
            
            tool_results.append({
                "tool_call_id": tc["id"],
                "role": "tool",
                "content": tool_result
            })
        
        messages.extend(tool_results)
        
        # Send screenshots back to Claude
        for tc, tr in zip(tool_calls, tool_results):
            if tc["function"]["name"] == "screenshot_preview":
                try:
                    parsed = json.loads(tr["content"])
                    if "screenshot_b64" in parsed:
                        messages.append({
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{parsed['screenshot_b64']}"}},
                                {"type": "text", "text": "Updated screenshot. Does it look correct now?"}
                            ]
                        })
                except:
                    pass
    
    yield sse_event("done", {
        "preview_url": state.get("preview_url"),
        "files": list(state.get("files", {}).keys())
    })
    
    # Update stored session
    _chat_sessions[clone_id] = {"messages": messages, "state": state}


def sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event string."""
    payload = {"type": event_type, **data}
    return f"data: {json.dumps(payload)}\n\n"


async def call_claude_with_tools(messages: list) -> dict:
    """Call Claude via OpenRouter with tool definitions."""
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
    
    # Clean messages for API
    formatted = []
    for msg in messages:
        if isinstance(msg.get("content"), list):
            formatted.append(msg)
        elif msg.get("tool_calls"):
            formatted.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": msg["tool_calls"]
            })
        elif msg.get("role") == "tool":
            formatted.append(msg)
        else:
            formatted.append({"role": msg["role"], "content": msg.get("content", "")})
    
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
                "messages": formatted,
                "tools": functions,
                "tool_choice": "auto"
            }
        )
        
        if response.status_code != 200:
            print(f"Claude API error: {response.status_code}")
            return None
        
        return response.json()
```

---

## Step 2: FastAPI SSE Endpoints

Add to `backend/app/main.py`:

```python
from fastapi.responses import StreamingResponse
from app.agent import run_clone_agent_streaming, run_chat_followup

@app.post("/clone/stream")
async def clone_stream(request: CloneRequest):
    """
    Clone a website with streaming progress via SSE.
    Returns a stream of server-sent events.
    """
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
            "X-Accel-Buffering": "no",  # Disable nginx buffering
            "Access-Control-Allow-Origin": "*"
        }
    )


@app.post("/clone/{clone_id}/chat")
async def clone_chat(clone_id: str, request: dict):
    """
    Send a follow-up message to the agent about an existing clone.
    Streams SSE events as the agent makes changes.
    """
    user_message = request.get("message", "")
    if not user_message:
        raise HTTPException(status_code=400, detail="message is required")
    
    async def event_stream():
        async for event in run_chat_followup(clone_id, user_message):
            yield event
    
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*"
        }
    )


@app.get("/clone/{clone_id}/files")
async def get_clone_files(clone_id: str):
    """Get all generated files for a clone."""
    from app.database import get_clone
    clone = await get_clone(clone_id)
    if not clone:
        raise HTTPException(status_code=404, detail="Clone not found")
    
    metadata = clone.get("metadata", {})
    files = metadata.get("files", {})
    
    if not files and clone.get("html"):
        files = {"index.html": clone["html"]}
    
    return {
        "files": files,
        "format": metadata.get("output_format", "html")
    }


@app.put("/clone/{clone_id}/files/{filepath:path}")
async def update_clone_file(clone_id: str, filepath: str, request: dict):
    """
    User manually edits a file. Updates sandbox + Supabase.
    """
    content = request.get("content", "")
    
    from app.database import get_clone, update_clone
    clone = await get_clone(clone_id)
    if not clone:
        raise HTTPException(status_code=404, detail="Clone not found")
    
    # Update file in sandbox
    sandbox_id = clone.get("sandbox_id")
    if sandbox_id:
        from app.tool_handlers import handle_update_file
        await handle_update_file({
            "sandbox_id": sandbox_id,
            "filepath": filepath,
            "content": content
        })
    
    # Update in Supabase
    metadata = clone.get("metadata", {})
    if "files" not in metadata:
        metadata["files"] = {}
    metadata["files"][filepath] = content
    await update_clone(clone_id, {"metadata": metadata})
    
    return {"status": "updated", "filepath": filepath}
```

---

## Step 3: Frontend — Streaming Chat UI

Install dependencies:
```bash
cd frontend
npm install @monaco-editor/react
npx shadcn@latest add tabs scroll-area badge separator avatar
```

Create the main clone page. This is the CORE component:

**frontend/components/CloneAgent.tsx** (or wherever your component goes):

```tsx
"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import Editor from "@monaco-editor/react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// Event types from the SSE stream
interface AgentEvent {
  type: string;
  [key: string]: any;
}

interface FileMap {
  [path: string]: string;
}

export default function CloneAgent() {
  // State
  const [url, setUrl] = useState("");
  const [isCloning, setIsCloning] = useState(false);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [files, setFiles] = useState<FileMap>({});
  const [activeFile, setActiveFile] = useState<string>("");
  const [previewUrl, setPreviewUrl] = useState<string>("");
  const [cloneId, setCloneId] = useState<string>("");
  const [sandboxId, setSandboxId] = useState<string>("");
  const [chatInput, setChatInput] = useState("");
  const [outputFormat, setOutputFormat] = useState<"html" | "react">("react");
  const [elapsedTime, setElapsedTime] = useState(0);
  
  const eventsEndRef = useRef<HTMLDivElement>(null);
  const timerRef = useRef<NodeJS.Timeout>();

  // Auto-scroll chat to bottom
  useEffect(() => {
    eventsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  // Elapsed time counter
  useEffect(() => {
    if (isCloning) {
      setElapsedTime(0);
      timerRef.current = setInterval(() => setElapsedTime(t => t + 1), 1000);
    } else {
      clearInterval(timerRef.current);
    }
    return () => clearInterval(timerRef.current);
  }, [isCloning]);

  // Process SSE stream
  const processStream = useCallback(async (response: Response) => {
    const reader = response.body?.getReader();
    const decoder = new TextDecoder();
    
    if (!reader) return;
    
    let buffer = "";
    
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      buffer += decoder.decode(value, { stream: true });
      
      // Parse SSE events (format: "data: {...}\n\n")
      const lines = buffer.split("\n\n");
      buffer = lines.pop() || ""; // Keep incomplete last chunk
      
      for (const line of lines) {
        if (line.startsWith("data: ")) {
          try {
            const event: AgentEvent = JSON.parse(line.slice(6));
            
            setEvents(prev => [...prev, event]);
            
            // Handle specific events
            switch (event.type) {
              case "clone_created":
                setCloneId(event.clone_id);
                break;
              
              case "file":
              case "file_updated":
                setFiles(prev => ({
                  ...prev,
                  [event.path]: event.content
                }));
                if (!activeFile || event.type === "file") {
                  setActiveFile(event.path);
                }
                break;
              
              case "deployed":
                setPreviewUrl(event.preview_url);
                setSandboxId(event.sandbox_id || "");
                break;
              
              case "done":
                setIsCloning(false);
                if (event.preview_url) setPreviewUrl(event.preview_url);
                break;
              
              case "error":
                setIsCloning(false);
                break;
            }
          } catch (e) {
            console.error("Failed to parse SSE event:", e);
          }
        }
      }
    }
    
    setIsCloning(false);
  }, [activeFile]);

  // Start cloning
  const handleClone = async () => {
    if (!url.trim()) return;
    
    setIsCloning(true);
    setEvents([]);
    setFiles({});
    setActiveFile("");
    setPreviewUrl("");
    setCloneId("");
    
    try {
      const response = await fetch(`${API_URL}/clone/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: url.trim(),
          output_format: outputFormat
        })
      });
      
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      
      await processStream(response);
    } catch (error: any) {
      setEvents(prev => [...prev, {
        type: "error",
        message: error.message || "Failed to connect to backend"
      }]);
      setIsCloning(false);
    }
  };

  // Send chat follow-up
  const handleChat = async () => {
    if (!chatInput.trim() || !cloneId) return;
    
    const message = chatInput.trim();
    setChatInput("");
    setIsCloning(true);
    
    setEvents(prev => [...prev, { type: "user_chat", text: message }]);
    
    try {
      const response = await fetch(`${API_URL}/clone/${cloneId}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message })
      });
      
      await processStream(response);
    } catch (error: any) {
      setEvents(prev => [...prev, {
        type: "error",
        message: error.message
      }]);
      setIsCloning(false);
    }
  };

  // User edits a file manually
  const handleFileEdit = async (filepath: string, content: string) => {
    setFiles(prev => ({ ...prev, [filepath]: content }));
    
    if (!cloneId) return;
    
    // Debounce: save after 1 second of no typing
    try {
      await fetch(`${API_URL}/clone/${cloneId}/files/${encodeURIComponent(filepath)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content })
      });
    } catch (e) {
      console.error("Failed to save file:", e);
    }
  };

  // Render an event in the chat log
  const renderEvent = (event: AgentEvent, index: number) => {
    switch (event.type) {
      case "step":
        return (
          <div key={index} className="flex items-center gap-2 py-2 px-3 bg-slate-800/50 rounded-lg">
            <span>{event.icon}</span>
            <span className="text-sm text-slate-300">{event.message}</span>
            {isCloning && index === events.length - 1 && (
              <span className="ml-auto text-xs text-slate-500 animate-pulse">working...</span>
            )}
          </div>
        );
      
      case "scrape_done":
        return (
          <div key={index} className="py-2 px-3 bg-slate-800/50 rounded-lg">
            <p className="text-sm text-slate-300 font-medium">✅ Scraped: {event.title}</p>
            <div className="flex gap-3 mt-1">
              <Badge variant="secondary">{event.images} images</Badge>
              <Badge variant="secondary">{event.fonts} fonts</Badge>
              <Badge variant="secondary">{event.links} links</Badge>
              <Badge variant="secondary">{event.page_height}px tall</Badge>
            </div>
          </div>
        );
      
      case "file":
        return (
          <div key={index} className="flex items-center gap-2 py-1 px-3">
            <span className="text-blue-400">📄</span>
            <button
              onClick={() => setActiveFile(event.path)}
              className="text-sm text-blue-400 hover:text-blue-300 underline"
            >
              {event.path}
            </button>
            <span className="text-xs text-slate-500">created</span>
          </div>
        );
      
      case "file_updated":
        return (
          <div key={index} className="flex items-center gap-2 py-1 px-3">
            <span className="text-yellow-400">✏️</span>
            <button
              onClick={() => setActiveFile(event.path)}
              className="text-sm text-yellow-400 hover:text-yellow-300 underline"
            >
              {event.path}
            </button>
            <span className="text-xs text-slate-500">updated</span>
          </div>
        );
      
      case "deployed":
        return (
          <div key={index} className="py-2 px-3 bg-green-900/30 border border-green-800/50 rounded-lg">
            <p className="text-sm text-green-400 font-medium">🚀 Deployed!</p>
            <a
              href={event.preview_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-green-300 hover:underline break-all"
            >
              {event.preview_url}
            </a>
          </div>
        );
      
      case "screenshot":
        return (
          <div key={index} className="py-2 px-3">
            <p className="text-sm text-slate-400 mb-2">📸 Clone screenshot:</p>
            <img
              src={`data:image/png;base64,${event.image_b64}`}
              alt="Clone screenshot"
              className="rounded border border-slate-700 max-h-48 w-auto"
            />
          </div>
        );
      
      case "agent_message":
        return (
          <div key={index} className="py-2 px-3 bg-slate-800/30 rounded-lg">
            <p className="text-sm text-slate-300 whitespace-pre-wrap">{event.text}</p>
          </div>
        );
      
      case "user_chat":
        return (
          <div key={index} className="py-2 px-3 bg-blue-900/30 border border-blue-800/50 rounded-lg ml-8">
            <p className="text-sm text-blue-300">{event.text}</p>
          </div>
        );
      
      case "logs":
        return (
          <div key={index} className="py-2 px-3">
            <p className="text-sm text-slate-400">
              {event.has_errors ? "❌ Errors found:" : "📋 Logs:"}
            </p>
            <pre className="text-xs text-slate-500 mt-1 max-h-24 overflow-auto bg-slate-900 p-2 rounded">
              {event.content}
            </pre>
          </div>
        );
      
      case "error":
        return (
          <div key={index} className="py-2 px-3 bg-red-900/30 border border-red-800/50 rounded-lg">
            <p className="text-sm text-red-400">❌ {event.message}</p>
          </div>
        );
      
      case "done":
        return (
          <div key={index} className="py-2 px-3 bg-green-900/30 border border-green-800/50 rounded-lg">
            <p className="text-sm text-green-400 font-medium">
              ✅ Done! {event.iterations} iteration{event.iterations !== 1 ? 's' : ''}
            </p>
          </div>
        );
      
      case "iteration":
        return (
          <div key={index} className="flex items-center gap-2 py-1 px-3">
            <span className="text-xs text-slate-500">
              Iteration {event.current}/{event.max}
            </span>
          </div>
        );
      
      default:
        return null;
    }
  };

  return (
    <div className="h-screen flex flex-col bg-slate-950">
      {/* Header */}
      <div className="border-b border-slate-800 px-6 py-3 flex items-center gap-4">
        <h1 className="text-lg font-semibold text-white">Website Cloner</h1>
        
        <div className="flex-1 flex items-center gap-2 max-w-2xl">
          <Input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="Enter any website URL..."
            className="bg-slate-900 border-slate-700 text-white"
            onKeyDown={(e) => e.key === "Enter" && !isCloning && handleClone()}
            disabled={isCloning}
          />
          
          <div className="flex items-center border border-slate-700 rounded-md overflow-hidden">
            <button
              onClick={() => setOutputFormat("html")}
              className={`px-3 py-2 text-xs font-medium transition-colors ${
                outputFormat === "html"
                  ? "bg-slate-700 text-white"
                  : "text-slate-400 hover:text-white"
              }`}
            >
              HTML
            </button>
            <button
              onClick={() => setOutputFormat("react")}
              className={`px-3 py-2 text-xs font-medium transition-colors ${
                outputFormat === "react"
                  ? "bg-slate-700 text-white"
                  : "text-slate-400 hover:text-white"
              }`}
            >
              React
            </button>
          </div>
          
          <Button
            onClick={handleClone}
            disabled={isCloning || !url.trim()}
            className="bg-blue-600 hover:bg-blue-700 whitespace-nowrap"
          >
            {isCloning ? `Cloning... ${elapsedTime}s` : "Clone"}
          </Button>
        </div>
      </div>

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left panel: Chat + Files */}
        <div className="w-[480px] border-r border-slate-800 flex flex-col">
          {/* Agent chat log */}
          <ScrollArea className="flex-1 p-4">
            <div className="space-y-2">
              {events.length === 0 && (
                <p className="text-sm text-slate-500 text-center py-8">
                  Enter a URL and click Clone to start
                </p>
              )}
              {events.map((event, i) => renderEvent(event, i))}
              <div ref={eventsEndRef} />
            </div>
          </ScrollArea>

          {/* Chat input */}
          {cloneId && (
            <div className="border-t border-slate-800 p-3 flex gap-2">
              <Input
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                placeholder="Ask for changes... (e.g., 'make the header darker')"
                className="bg-slate-900 border-slate-700 text-white text-sm"
                onKeyDown={(e) => e.key === "Enter" && !isCloning && handleChat()}
                disabled={isCloning}
              />
              <Button
                onClick={handleChat}
                disabled={isCloning || !chatInput.trim()}
                size="sm"
                className="bg-slate-700 hover:bg-slate-600"
              >
                Send
              </Button>
            </div>
          )}

          {/* File tabs + editor */}
          {Object.keys(files).length > 0 && (
            <div className="border-t border-slate-800 h-[300px] flex flex-col">
              <Tabs value={activeFile} onValueChange={setActiveFile}>
                <div className="px-2 border-b border-slate-800 overflow-x-auto">
                  <TabsList className="bg-transparent h-auto p-0 gap-0">
                    {Object.keys(files).map(fp => (
                      <TabsTrigger
                        key={fp}
                        value={fp}
                        className="rounded-none border-b-2 border-transparent data-[state=active]:border-blue-500 data-[state=active]:bg-transparent text-xs px-3 py-2"
                      >
                        {fp.split("/").pop()}
                      </TabsTrigger>
                    ))}
                  </TabsList>
                </div>
                
                {Object.entries(files).map(([fp, content]) => (
                  <TabsContent key={fp} value={fp} className="flex-1 m-0">
                    <Editor
                      height="100%"
                      defaultLanguage={fp.endsWith(".jsx") ? "javascript" : fp.endsWith(".html") ? "html" : "javascript"}
                      value={content}
                      onChange={(value) => handleFileEdit(fp, value || "")}
                      theme="vs-dark"
                      options={{
                        minimap: { enabled: false },
                        fontSize: 12,
                        lineNumbers: "on",
                        scrollBeyondLastLine: false,
                        wordWrap: "on",
                        tabSize: 2
                      }}
                    />
                  </TabsContent>
                ))}
              </Tabs>
            </div>
          )}
        </div>

        {/* Right panel: Preview */}
        <div className="flex-1 flex flex-col bg-slate-900">
          {previewUrl ? (
            <>
              <div className="border-b border-slate-800 px-4 py-2 flex items-center gap-3">
                <div className="flex-1 bg-slate-800 rounded px-3 py-1 text-xs text-slate-400 truncate">
                  {previewUrl}
                </div>
                <a
                  href={previewUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs text-blue-400 hover:text-blue-300"
                >
                  Open ↗
                </a>
                <Button
                  size="sm"
                  variant="ghost"
                  className="text-xs"
                  onClick={() => {
                    // Force iframe reload
                    setPreviewUrl(prev => prev + (prev.includes('?') ? '&' : '?') + 'r=' + Date.now());
                  }}
                >
                  Reload
                </Button>
              </div>
              <iframe
                src={previewUrl}
                className="flex-1 w-full bg-white"
                title="Clone preview"
              />
            </>
          ) : (
            <div className="flex-1 flex items-center justify-center">
              <p className="text-slate-600 text-sm">
                {isCloning ? "Preview will appear once deployed..." : "Clone a website to see the preview"}
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
```

---

## Step 4: Wire Into Next.js Page

Update your main page to use the CloneAgent component:

```tsx
// app/page.tsx (or pages/index.tsx depending on your router)
import CloneAgent from "@/components/CloneAgent";

export default function Home() {
  return <CloneAgent />;
}
```

---

## Event Types Reference

| Event Type | Data | When |
|-----------|------|------|
| `clone_created` | `{clone_id}` | Record saved to Supabase |
| `iteration` | `{current, max}` | Each agent loop iteration |
| `thinking` | `{message}` | Waiting for Claude response |
| `step` | `{step, message, icon}` | Before each tool call |
| `scrape_done` | `{title, images, fonts, links, page_height}` | After scraping completes |
| `file` | `{path, content, language}` | New file created |
| `file_updated` | `{path, content, language}` | Existing file modified |
| `deploying` | `{message}` | Starting sandbox deploy |
| `deployed` | `{preview_url, sandbox_id}` | Sandbox is live |
| `screenshot` | `{image_b64}` | Clone screenshot taken |
| `logs` | `{content, has_errors}` | Sandbox console output |
| `agent_message` | `{text}` | Claude's text response |
| `user_chat` | `{text}` | User's follow-up message |
| `tool_error` | `{tool, error}` | Tool execution failed |
| `error` | `{message}` | Fatal error |
| `done` | `{preview_url, clone_id, files, iterations}` | Agent finished |

---

## Implementation Order

1. Backend SSE endpoint (`/clone/stream`) — test with `curl` first
2. Frontend EventSource parsing — verify events show up in console
3. Chat log rendering — display events as cards
4. Preview iframe — show when `deployed` event arrives
5. File viewer — show files when `file` events arrive
6. Monaco editor — editable code with live save
7. Chat input — follow-up messages via `/clone/{id}/chat`
8. Polish — loading states, error handling, responsive layout

Test with `curl` first:
```bash
curl -N -X POST http://localhost:8000/clone/stream \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","output_format":"html"}'
```

You should see SSE events streaming in the terminal. Only then build the frontend.

---

## Gotchas

1. **CORS for SSE** — Make sure CORS middleware allows streaming responses. The headers on the StreamingResponse should handle it, but test cross-origin.

2. **EventSource vs fetch** — We use `fetch` with `response.body.getReader()` instead of native `EventSource` because `EventSource` only supports GET requests. We need POST for the clone request.

3. **Monaco Editor size** — It needs an explicit height. The parent container must have a fixed height, not flex-grow alone. The `h-[300px]` on the file panel handles this.

4. **File save debouncing** — The current handleFileEdit saves immediately on every keystroke. Add a debounce (500ms-1000ms) to avoid hammering the API. Use a useRef + setTimeout pattern.

5. **Chat session persistence** — `_chat_sessions` is in-memory on the backend. If the server restarts, chat sessions are lost. For demo purposes this is fine. For production you'd persist messages in Supabase.

6. **Image events are huge** — The `screenshot` event contains a full base64 PNG (~1-3MB). This is fine over SSE but make sure you're not storing hundreds of them in React state. Keep only the latest screenshot.

7. **Vite hot reload** — When `update_sandbox_file` updates a .jsx file in the sandbox, Vite's HMR should auto-refresh. The iframe might not reflect this immediately — add a small delay then reload the iframe when a `file_updated` event arrives after deployment.
