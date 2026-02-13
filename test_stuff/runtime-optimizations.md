# Runtime Optimizations — Claude Code Prompt

We're switching from OpenRouter to the Anthropic SDK directly AND implementing runtime optimizations. These are two changes in one pass.

**Read the project's claude.md first for full context, then implement everything below.**

---

## Change 1: Switch to Anthropic SDK

Install the SDK:
```bash
cd backend
pip install anthropic --break-system-packages
```

Add to requirements.txt:
```
anthropic>=0.42.0
```

### Replace the OpenRouter call in `agent.py`

The Anthropic SDK uses a DIFFERENT format than OpenRouter's OpenAI-compatible API. Key differences:

- System prompt goes in `system` parameter, NOT in messages
- Tools use Anthropic's native format: `{"name": ..., "description": ..., "input_schema": ...}` (NO `{"type": "function", "function": {...}}` wrapper)
- Tool results use `{"type": "tool_result", "tool_use_id": ..., "content": ...}` content blocks
- Assistant tool calls come as `{"type": "tool_use", "id": ..., "name": ..., "input": {...}}` content blocks
- Images use `{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "..."}}` NOT `{"type": "image_url", ...}`

**Replace `call_claude_with_tools()` entirely:**

```python
import anthropic

# Initialize client globally
anthropic_client = anthropic.AsyncAnthropic()  # Uses ANTHROPIC_API_KEY env var

async def call_claude_with_tools(messages: list, system_prompt: str) -> dict:
    """
    Call Claude via Anthropic SDK with native tool use.
    
    Messages format for Anthropic:
    [
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "...", "name": "...", "input": {...}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]},
        ...
    ]
    """
    
    # Tools in Anthropic native format (same as mcp_tools.py already has)
    from app.mcp_tools import TOOLS
    
    try:
        response = await anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=16000,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"}  # Enable prompt caching
                }
            ],
            tools=TOOLS,  # Already in Anthropic format: {name, description, input_schema}
            messages=messages
        )
        return response
    except anthropic.APIError as e:
        print(f"Anthropic API error: {e}")
        return None
```

### Update the agent loop message handling

The message format is completely different. Replace the tool call handling in `run_clone_agent_streaming()`:

```python
async def run_clone_agent_streaming(url: str, output_format: str = "html") -> AsyncGenerator[str, None]:
    
    system_prompt = REACT_SYSTEM_PROMPT if output_format == "react" else HTML_SYSTEM_PROMPT
    
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
        yield sse_event("warning", {"message": f"DB unavailable: {e}"})
    
    format_instruction = (
        "Use HTML mode (single HTML file with Tailwind)." 
        if output_format == "html" 
        else "Use React mode (Vite + React + Tailwind with separate component files)."
    )
    
    # Anthropic message format
    messages = [
        {"role": "user", "content": f"Clone this website: {url}\n\n{format_instruction}\n\nStart by scraping the URL."}
    ]
    
    for iteration in range(MAX_ITERATIONS):
        state["iterations"] = iteration + 1
        yield sse_event("iteration", {"current": iteration + 1, "max": MAX_ITERATIONS})
        yield sse_event("thinking", {"message": "Agent is thinking..."})
        
        # Call Claude
        response = await call_claude_with_tools(messages, system_prompt)
        
        if not response:
            yield sse_event("error", {"message": "Failed to get response from Claude"})
            break
        
        # --- Process Anthropic response ---
        # response.content is a list of content blocks:
        # [{"type": "text", "text": "..."}, {"type": "tool_use", "id": "...", "name": "...", "input": {...}}]
        
        # Extract text blocks
        text_blocks = [b.text for b in response.content if b.type == "text"]
        if text_blocks:
            yield sse_event("agent_message", {"text": "\n".join(text_blocks)})
        
        # Extract tool use blocks
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        
        # Add assistant response to messages (include ALL content blocks)
        messages.append({"role": "assistant", "content": response.content})
        
        # If no tool calls, agent is done
        if not tool_use_blocks:
            yield sse_event("agent_done", {"message": "Agent finished"})
            break
        
        # If stop_reason is "end_turn" with no tool calls, we're done
        if response.stop_reason == "end_turn" and not tool_use_blocks:
            yield sse_event("agent_done", {"message": "Agent finished"})
            break
        
        # Execute each tool call
        tool_results = []
        
        for tool_block in tool_use_blocks:
            tool_name = tool_block.name
            tool_input = tool_block.input
            tool_use_id = tool_block.id
            
            # --- Stream what's happening (same SSE logic as before) ---
            if tool_name == "scrape_url":
                yield sse_event("step", {"step": "scraping", "message": f"Scraping {tool_input.get('url', '')}...", "icon": "🌐"})
            elif tool_name == "generate_and_deploy_html":
                yield sse_event("step", {"step": "deploying_html", "message": "Deploying HTML to sandbox...", "icon": "🚀"})
                yield sse_event("file", {"path": "index.html", "content": tool_input.get("html_content", "")[:50000], "language": "html"})
                state["files"]["index.html"] = tool_input.get("html_content", "")
            elif tool_name == "generate_and_deploy_react":
                yield sse_event("step", {"step": "deploying_react", "message": "Deploying React app...", "icon": "🚀"})
                files = tool_input.get("files", {})
                for filepath, content in files.items():
                    yield sse_event("file", {"path": filepath, "content": content, "language": "jsx" if filepath.endswith(".jsx") else "javascript"})
                    state["files"][filepath] = content
            elif tool_name == "screenshot_preview":
                yield sse_event("step", {"step": "checking", "message": "Taking screenshot of clone...", "icon": "🔍"})
            elif tool_name == "get_sandbox_logs":
                yield sse_event("step", {"step": "checking_logs", "message": "Checking for errors...", "icon": "📋"})
            elif tool_name == "update_sandbox_file":
                fp = tool_input.get("filepath", "unknown")
                yield sse_event("step", {"step": "fixing", "message": f"Fixing {fp}...", "icon": "🔧"})
                yield sse_event("file_updated", {"path": fp, "content": tool_input.get("content", ""), "language": "jsx" if fp.endswith(".jsx") else "html"})
                state["files"][fp] = tool_input.get("content", "")
            
            # --- Execute the tool ---
            tool_result_str = await handle_tool_call(tool_name, tool_input)
            
            # --- Stream results ---
            try:
                parsed = json.loads(tool_result_str)
                if tool_name == "scrape_url":
                    yield sse_event("scrape_done", {
                        "title": parsed.get("title", ""),
                        "images": len(parsed.get("assets", {}).get("images", [])),
                        "fonts": len(parsed.get("assets", {}).get("fonts", [])),
                        "links": len(parsed.get("clickables", {}).get("nav_links", [])),
                        "page_height": parsed.get("page_height", 0)
                    })
                elif "preview_url" in parsed:
                    state["preview_url"] = parsed["preview_url"]
                    state["sandbox_id"] = parsed.get("sandbox_id")
                    yield sse_event("deployed", {"preview_url": parsed["preview_url"], "sandbox_id": parsed.get("sandbox_id")})
                elif "screenshot_b64" in parsed:
                    yield sse_event("screenshot", {"image_b64": parsed["screenshot_b64"]})
                elif "logs" in parsed:
                    has_errors = any(p in parsed["logs"] for p in ["Error", "error", "SyntaxError", "Cannot find"])
                    yield sse_event("logs", {"content": parsed["logs"][:1000], "has_errors": has_errors})
                elif "error" in parsed:
                    yield sse_event("tool_error", {"tool": tool_name, "error": parsed["error"]})
            except json.JSONDecodeError:
                pass
            
            # Build tool result content for Anthropic format
            # If screenshot, include the image so Claude can see it
            if tool_name == "screenshot_preview":
                try:
                    parsed = json.loads(tool_result_str)
                    if "screenshot_b64" in parsed:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/jpeg",  # We compress to JPEG now
                                        "data": parsed["screenshot_b64"]
                                    }
                                },
                                {
                                    "type": "text",
                                    "text": "Screenshot of the clone. Compare to the original and fix any differences."
                                }
                            ]
                        })
                        continue
                except:
                    pass
            
            # Default: return tool result as text
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": tool_result_str
            })
        
        # Add ALL tool results as a single user message
        messages.append({"role": "user", "content": tool_results})
    
    # Final event
    yield sse_event("done", {
        "preview_url": state.get("preview_url"),
        "sandbox_id": state.get("sandbox_id"),
        "clone_id": state.get("clone_id"),
        "files": list(state.get("files", {}).keys()),
        "iterations": state["iterations"]
    })
    
    # Update DB
    try:
        from app.database import update_clone
        if state.get("clone_id"):
            await update_clone(state["clone_id"], {
                "status": "success" if state.get("preview_url") else "failed",
                "preview_url": state.get("preview_url"),
                "sandbox_id": state.get("sandbox_id"),
                "metadata": {"files": state.get("files"), "iterations": state["iterations"], "output_format": output_format}
            })
    except:
        pass
    
    _chat_sessions[state.get("clone_id", "")] = {"messages": messages, "state": state}
```

### Update `run_chat_followup()` with the same Anthropic message format

Same pattern — tool_use blocks in assistant messages, tool_result blocks in user messages. Copy the same structure from above.

### Update `mcp_tools.py`

The tools are ALREADY in Anthropic format (`name`, `description`, `input_schema`). Remove any OpenAI wrappers if they exist. The TOOLS array should look like:

```python
TOOLS = [
    {
        "name": "scrape_url",
        "description": "...",
        "input_schema": {
            "type": "object",
            "properties": {...},
            "required": [...]
        }
    },
    # ... etc
]
```

No `{"type": "function", "function": {...}}` wrapper. Anthropic SDK takes tools directly.

### Environment Variable

Change from `OPENROUTER_API_KEY` to `ANTHROPIC_API_KEY`:
```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
```

The `anthropic.AsyncAnthropic()` client reads `ANTHROPIC_API_KEY` from env automatically.

---

## Change 2: Sandbox Pool (pre-warm sandboxes)

Create `backend/app/sandbox_pool.py`:

```python
"""
Pre-provisions Daytona sandboxes so users never wait for npm install.
On server startup, creates a pool of ready-to-use sandboxes.
When one is consumed, a new one is provisioned in the background.
"""
import asyncio
from collections import deque

class SandboxPool:
    def __init__(self, pool_size=2):
        self.pool_size = pool_size
        self.available: deque = deque()
        self.lock = asyncio.Lock()
        self._provisioning = False
    
    async def initialize(self):
        """Call on server startup. Pre-provisions sandboxes in parallel."""
        print(f"Pre-warming {self.pool_size} sandboxes...")
        tasks = [self._provision_one() for _ in range(self.pool_size)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, dict) and "sandbox_id" in r:
                self.available.append(r)
            else:
                print(f"Failed to pre-warm sandbox: {r}")
        print(f"Sandbox pool ready: {len(self.available)} available")
    
    async def acquire(self) -> dict:
        """
        Get a pre-provisioned sandbox. Returns immediately if pool has one.
        Falls back to on-demand provisioning if pool is empty.
        Kicks off background replenishment after acquiring.
        """
        async with self.lock:
            if self.available:
                sandbox = self.available.popleft()
                print(f"Pool: acquired sandbox {sandbox['sandbox_id']} ({len(self.available)} remaining)")
                # Replenish in background
                asyncio.create_task(self._replenish())
                return sandbox
        
        # Pool empty — provision on demand
        print("Pool empty — provisioning on demand (slow path)")
        return await self._provision_one()
    
    async def _replenish(self):
        """Add one sandbox back to the pool in the background."""
        async with self.lock:
            if len(self.available) >= self.pool_size:
                return  # Pool already full
            if self._provisioning:
                return  # Already provisioning
            self._provisioning = True
        
        try:
            sandbox = await self._provision_one()
            async with self.lock:
                if len(self.available) < self.pool_size:
                    self.available.append(sandbox)
                    print(f"Pool: replenished to {len(self.available)} sandboxes")
        except Exception as e:
            print(f"Pool: replenish failed: {e}")
        finally:
            async with self.lock:
                self._provisioning = False
    
    async def _provision_one(self) -> dict:
        from app.sandbox_template import provision_react_sandbox
        return await provision_react_sandbox()
    
    @property
    def size(self) -> int:
        return len(self.available)


# Global singleton
sandbox_pool = SandboxPool(pool_size=2)
```

### Wire into FastAPI

In `main.py`:
```python
from contextlib import asynccontextmanager
from app.sandbox_pool import sandbox_pool

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await sandbox_pool.initialize()
    yield
    # Shutdown (nothing needed)

app = FastAPI(lifespan=lifespan)
```

### Use pool in tool handler

In `tool_handlers.py`, update `handle_deploy_react`:
```python
from app.sandbox_pool import sandbox_pool

async def handle_deploy_react(input: dict) -> str:
    files = input["files"]
    
    # Get pre-provisioned sandbox from pool (instant if pool has one)
    sandbox_info = await sandbox_pool.acquire()
    sandbox_id = sandbox_info["sandbox_id"]
    preview_url = sandbox_info["preview_url"]
    
    # Upload Claude's generated files into the ready sandbox
    def _upload():
        from daytona import Daytona, DaytonaConfig
        import os
        daytona = Daytona(DaytonaConfig(api_key=os.getenv("DAYTONA_API_KEY"), target="us"))
        sandbox = daytona.get(sandbox_id)
        for filepath, content in files.items():
            full_path = f"/home/daytona/clone-app/{filepath}"
            dir_path = '/'.join(full_path.split('/')[:-1])
            sandbox.process.exec(f"mkdir -p {dir_path}")
            sandbox.fs.upload_file(content.encode(), full_path)
    
    await asyncio.to_thread(_upload)
    await asyncio.sleep(2)  # Let Next.js hot-reload pick up files
    
    active_sandboxes[sandbox_id] = sandbox_info
    
    return json.dumps({
        "preview_url": preview_url,
        "sandbox_id": sandbox_id,
        "status": "deployed"
    })
```

---

## Change 3: Screenshot Compression

Create `backend/app/image_utils.py`:

```python
"""Screenshot optimization — compress before sending to Claude API."""
from PIL import Image
import io
import base64


def optimize_screenshot(screenshot_bytes: bytes, max_width: int = 1280, quality: int = 75) -> bytes:
    """
    Resize and compress a screenshot for API consumption.
    1920x1080 PNG (~2-4MB) → 1280x720 JPEG (~150-300KB)
    Claude sees it just as well. Saves tokens and latency.
    """
    img = Image.open(io.BytesIO(screenshot_bytes))
    
    # Resize if wider than max_width
    w, h = img.size
    if w > max_width:
        ratio = max_width / w
        img = img.resize((max_width, int(h * ratio)), Image.LANCZOS)
    
    # Convert RGBA to RGB (JPEG doesn't support alpha)
    if img.mode == 'RGBA':
        bg = Image.new('RGB', img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    
    # Save as JPEG
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality, optimize=True)
    return buf.getvalue()


def screenshot_to_b64(screenshot_bytes: bytes, compress: bool = True) -> tuple[str, str]:
    """
    Convert screenshot bytes to base64 string.
    Returns (base64_string, media_type).
    """
    if compress:
        optimized = optimize_screenshot(screenshot_bytes)
        return base64.b64encode(optimized).decode(), "image/jpeg"
    else:
        return base64.b64encode(screenshot_bytes).decode(), "image/png"
```

### Use it everywhere screenshots are taken

In `tool_handlers.py`, update `handle_screenshot_preview`:
```python
from app.image_utils import screenshot_to_b64

async def handle_screenshot_preview(input: dict) -> str:
    from playwright.async_api import async_playwright
    preview_url = input["preview_url"]
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.goto(preview_url, wait_until="networkidle", timeout=20000)
        except:
            await page.goto(preview_url, wait_until="domcontentloaded", timeout=10000)
            await page.wait_for_timeout(2000)
        screenshot_bytes = await page.screenshot()
        await browser.close()
    
    # Compress: ~2MB PNG → ~200KB JPEG
    b64, media_type = screenshot_to_b64(screenshot_bytes, compress=True)
    
    return json.dumps({
        "screenshot_b64": b64,
        "media_type": media_type,  # "image/jpeg"
        "message": "Screenshot taken. Compare to original and fix differences."
    })
```

In `scraper.py`, update ALL screenshot captures:
```python
from app.image_utils import optimize_screenshot, screenshot_to_b64

# In scrape_website(), where viewport screenshots are taken:
viewport_bytes = await page.screenshot()
viewport_b64, _ = screenshot_to_b64(viewport_bytes, compress=True)

# In scroll chunk screenshots:
chunk_bytes = await page.screenshot()
chunk_b64, _ = screenshot_to_b64(chunk_bytes, compress=True)

# In full page thumbnail:
full_bytes = await page.screenshot(full_page=True)
full_b64, _ = screenshot_to_b64(full_bytes, compress=True)
```

---

## Change 4: Scrape Data Truncation

In `tool_handlers.py`, update `handle_scrape_url` to send less data without losing quality:

```python
async def handle_scrape_url(input: dict) -> str:
    url = input["url"]
    data = await scrape_website(url)
    _scrape_cache[url] = data
    
    # Build a RIGHT-SIZED summary — enough for Claude, not wasteful
    summary = {
        "url": data["url"],
        "title": data["title"],
        "page_height": data["page_height"],
    }
    
    # Theme: always include in full — it's small and critical
    summary["theme"] = data["theme"]
    
    # Images: URL + alt only, cap at 25
    summary["images"] = [
        {"url": img["url"], "alt": img.get("alt", "")}
        for img in data.get("assets", {}).get("images", [])[:25]
    ]
    
    # Fonts: include all (usually <10)
    summary["fonts"] = data.get("assets", {}).get("fonts", [])[:10]
    
    # Clickables: cap each category
    clickables = data.get("clickables", {})
    summary["clickables"] = {
        "nav_links": clickables.get("nav_links", [])[:15],
        "cta_buttons": clickables.get("cta_buttons", [])[:10],
        "footer_links": clickables.get("footer_links", [])[:15],
    }
    
    # DOM skeleton: most important data. Keep but cap at 8000 chars
    skeleton = data.get("dom_skeleton", "")
    if len(skeleton) > 8000:
        summary["dom_skeleton"] = skeleton[:4000] + "\n\n... [middle sections — refer to section screenshots] ...\n\n" + skeleton[-4000:]
    else:
        summary["dom_skeleton"] = skeleton
    
    # Text content: cap at 3000 chars (Claude has screenshots for the rest)
    summary["text_content"] = data.get("text_content", "")[:3000]
    
    # SVGs: top 5, truncate markup
    summary["svgs"] = [
        {"id": s["id"], "markup": s["markup"][:400]}
        for s in data.get("svgs", [])[:5]
    ]
    
    # Backgrounds: include but trim huge gradient strings
    summary["backgrounds"] = []
    for bg in data.get("backgrounds", [])[:10]:
        clean_bg = {k: v for k, v in bg.items()}
        if "value" in clean_bg and len(str(clean_bg["value"])) > 200:
            clean_bg["value"] = str(clean_bg["value"])[:200] + "..."
        summary["backgrounds"].append(clean_bg)
    
    # Sections: include type + bounds + content summary
    summary["sections"] = []
    for s in data.get("sections", [])[:15]:
        section_summary = {
            "index": s.get("index"),
            "probable_type": s.get("probable_type"),
            "bounds": s.get("bounds"),
            "style": s.get("style"),
            "headings": s.get("content", {}).get("headings", [])[:3],
            "images_count": len(s.get("content", {}).get("images", [])),
            "buttons": s.get("content", {}).get("buttons", [])[:3],
        }
        summary["sections"].append(section_summary)
    
    # Meta
    summary["meta"] = data.get("meta", {})
    
    return json.dumps(summary, indent=2)
```

---

## Change 5: Scrape Cache

In `tool_handlers.py`, add a TTL cache so re-cloning the same URL is instant:

```python
import time
import hashlib

_scrape_cache = {}
_SCRAPE_CACHE_TTL = 300  # 5 minutes

async def handle_scrape_url(input: dict) -> str:
    url = input["url"]
    
    # Check cache
    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cache_key in _scrape_cache:
        entry = _scrape_cache[cache_key]
        if time.time() - entry["ts"] < _SCRAPE_CACHE_TTL:
            print(f"Scrape cache hit: {url}")
            return entry["result"]
    
    # Cache miss — scrape
    data = await scrape_website(url)
    
    # Build summary (same truncation logic as above)
    summary = build_scrape_summary(data)  # Extract the summary logic into a function
    result_str = json.dumps(summary, indent=2)
    
    # Cache it
    _scrape_cache[cache_key] = {"ts": time.time(), "result": result_str, "full_data": data}
    
    return result_str
```

---

## Change 6: Parallel Scrape + Sandbox Provision

In `agent.py`, kick off sandbox provisioning the moment a clone starts, while scraping runs in parallel:

```python
async def run_clone_agent_streaming(url: str, output_format: str = "html") -> AsyncGenerator[str, None]:
    
    # Kick off sandbox pre-acquisition immediately for React mode
    # This runs in parallel with whatever Claude does first (scraping)
    sandbox_future = None
    if output_format == "react":
        from app.sandbox_pool import sandbox_pool
        sandbox_future = asyncio.create_task(sandbox_pool.acquire())
        yield sse_event("step", {"step": "preparing", "message": "Preparing sandbox...", "icon": "⚡"})
    
    # ... rest of agent loop ...
    
    # When handle_deploy_react is called, pass the pre-acquired sandbox:
    # Store it so the tool handler can use it
    if sandbox_future and not sandbox_future.done():
        _pre_acquired_sandbox = await sandbox_future
    elif sandbox_future:
        _pre_acquired_sandbox = sandbox_future.result()
```

Then in `tool_handlers.py`, check for a pre-acquired sandbox before provisioning a new one:

```python
# Module-level storage for pre-acquired sandbox
_pre_acquired_sandbox = None

async def set_pre_acquired_sandbox(sandbox_info: dict):
    global _pre_acquired_sandbox
    _pre_acquired_sandbox = sandbox_info

async def handle_deploy_react(input: dict) -> str:
    global _pre_acquired_sandbox
    files = input["files"]
    
    if _pre_acquired_sandbox:
        sandbox_info = _pre_acquired_sandbox
        _pre_acquired_sandbox = None  # Consume it
        print("Using pre-acquired sandbox")
    else:
        from app.sandbox_pool import sandbox_pool
        sandbox_info = await sandbox_pool.acquire()
    
    # ... upload files as before ...
```

---

## Summary of ALL Changes

### New files to create:
- `backend/app/sandbox_pool.py` — sandbox pre-warming pool
- `backend/app/image_utils.py` — screenshot compression

### Files to modify:
- `backend/app/agent.py` — switch to Anthropic SDK, Anthropic message format, parallel sandbox prep, pass system_prompt to call function
- `backend/app/tool_handlers.py` — use sandbox pool, compressed screenshots, scrape caching, data truncation
- `backend/app/mcp_tools.py` — remove OpenAI function wrapper if present (keep Anthropic native format)
- `backend/app/main.py` — add lifespan for sandbox pool initialization
- `backend/app/scraper.py` — use screenshot compression everywhere
- `backend/requirements.txt` — add `anthropic>=0.42.0`, keep `Pillow`

### Environment variables:
- REMOVE: `OPENROUTER_API_KEY`
- ADD: `ANTHROPIC_API_KEY=sk-ant-...`
- KEEP: `DAYTONA_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`

### Implementation order:
1. Install anthropic SDK, update requirements.txt
2. Create `image_utils.py` (standalone, no dependencies on other changes)
3. Create `sandbox_pool.py` (standalone)
4. Update `mcp_tools.py` to ensure Anthropic native format
5. Rewrite `agent.py` — new `call_claude_with_tools()`, updated message handling, parallel sandbox prep
6. Update `tool_handlers.py` — sandbox pool, screenshot compression, scrape cache, data truncation
7. Update `main.py` — lifespan for pool init
8. Update `scraper.py` — use image_utils for all screenshots
9. Update `.env` with ANTHROPIC_API_KEY
10. Test: `curl -N -X POST http://localhost:8000/clone/stream -H "Content-Type: application/json" -d '{"url":"https://example.com","output_format":"react"}'`

### Expected impact:
| Before | After |
|--------|-------|
| 30-60s sandbox provisioning per clone | 0-2s (pre-warmed) |
| 2-4MB PNG screenshots | 150-300KB JPEG |
| Full scrape data every call (~8K tokens) | Truncated (~4K tokens) |
| Sequential scrape then provision | Parallel |
| Re-scrape same URL | Cached 5 min |
| OpenRouter markup on API costs | Direct Anthropic pricing |
