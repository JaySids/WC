# Website Cloner — AI-Powered Website Replication Tool

## What This Is
A full-stack app where users paste a URL, and an AI agent (Claude via OpenRouter) scrapes the site, generates a pixel-perfect React/HTML clone, deploys it to a cloud sandbox (Daytona), and lets users chat with the agent to request fixes — all streamed in real-time via SSE.

Built for an Afterquery (YC company) internship assessment.

## Tech Stack
- **Frontend**: Next.js 14 (App Router), Tailwind CSS, Monaco Editor, shadcn/ui
- **Backend**: FastAPI (Python), async everywhere
- **AI**: Claude Sonnet 4 via OpenRouter API (OpenAI-compatible function calling format)
- **Scraping**: Playwright (headless Chromium) with network interception
- **Sandboxes**: Daytona SDK — cloud sandboxes for deploying clones
- **Database**: Supabase (clone history, metadata)
- **Streaming**: Server-Sent Events (SSE) from FastAPI → frontend

## Architecture

```
User → Next.js Frontend → FastAPI Backend → Claude (OpenRouter) → Tool Loop
                                                    ↓
                                            MCP-style Tools:
                                            ├── scrape_url (Playwright)
                                            ├── generate_and_deploy_html
                                            ├── generate_and_deploy_react
                                            ├── screenshot_preview
                                            ├── get_sandbox_logs
                                            └── update_sandbox_file
                                                    ↓
                                            Daytona Sandbox (live preview URL)
```

The backend does NOT directly generate code. Instead, it sends Claude a system prompt + tools, and Claude orchestrates the cloning by calling tools in a loop (scrape → generate → deploy → screenshot → fix → verify). Each tool call and result is streamed to the frontend as an SSE event.

## Project Structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app. Endpoints: POST /clone/stream, POST /clone/{id}/chat, GET /clone/{id}/files, PUT /clone/{id}/files/{path}
│   ├── agent.py             # Core agent loop. run_clone_agent_streaming() yields SSE events. Contains REACT_SYSTEM_PROMPT and HTML_SYSTEM_PROMPT. Calls Claude via OpenRouter with tool definitions.
│   ├── scraper.py           # Playwright extraction. scrape_website() does: network interception (captures all images/fonts/CSS), DOM skeleton extraction, theme extraction (exact hex colors, fonts), clickables extraction (all hrefs), background image/gradient extraction, section detection, per-section screenshots.
│   ├── mcp_tools.py         # Tool definitions array (name, description, input_schema) passed to Claude's tool_use.
│   ├── tool_handlers.py     # handle_tool_call() routes tool names to handler functions. Manages sandbox state, scrape cache.
│   ├── sandbox.py           # Daytona SDK wrapper. deploy_html_to_sandbox(), deploy_react_to_sandbox().
│   ├── sandbox_template.py  # Pre-configured Next.js 14 project template. TEMPLATE_FILES dict, provision_react_sandbox(). All npm packages pre-installed so Claude only writes component files.
│   ├── database.py          # Supabase client. save_clone(), update_clone(), get_clone().
│   └── config.py            # Settings, env vars
├── requirements.txt
├── Dockerfile
└── .env                     # OPENROUTER_API_KEY, DAYTONA_API_KEY, SUPABASE_URL, SUPABASE_KEY

frontend/
├── app/
│   ├── page.tsx             # Renders <CloneAgent />
│   └── layout.tsx
├── components/
│   └── CloneAgent.tsx       # Main UI. Split pane: left = agent chat log + file editor (Monaco), right = preview iframe. Handles SSE parsing, file state, chat input.
└── ...
```

## Key Concepts

### Scraping (scraper.py)
We don't just screenshot and guess. We extract structured data:
- **Network interception**: `page.route("**/*", handler)` captures every request the browser makes — every image URL, font file, CSS sheet. More reliable than DOM parsing.
- **DOM skeleton**: Simplified DOM tree with layout annotations (`flex row between`, `grid cols-3 gap-24px`, `bg:#0a2540`). Gives Claude the REAL page structure instead of guessing from pixels.
- **Theme extraction**: `getComputedStyle()` on all elements → exact hex colors, font families, font sizes, font weights. RGB converted to hex.
- **Clickables**: Every `<a>` and `<button>` with real hrefs, categorized (nav, CTA, footer).
- **Background images**: CSS `background-image` URLs and gradient definitions with colors + direction.
- **Sections**: Semantic section detection (navbar, hero, features, pricing, FAQ, footer) with per-section bounds, colors, content, and individual screenshots.
- **SVGs**: Inline SVG markup extracted for icons.

### Agent Loop (agent.py)
1. User sends URL → backend creates SSE stream
2. Claude receives system prompt + tool definitions
3. Claude calls `scrape_url` → gets structured data
4. Claude generates code → calls `generate_and_deploy_react` (or html)
5. Claude calls `screenshot_preview` → sees its own clone
6. Claude compares to original, calls `update_sandbox_file` to fix issues
7. Loop repeats up to MAX_ITERATIONS (8)
8. Every step emits SSE events to frontend

### React Mode (sandbox_template.py)
For React output, the sandbox is pre-provisioned with a full Next.js 14 project:
- package.json with 20+ packages already installed (framer-motion, swiper, headlessui, radix, lucide-react, react-countup, react-type-animation, clsx, tailwind-merge, cva)
- tailwind.config.js, postcss.config.js, next.config.js ready
- `npm install` runs during provisioning, NOT during generation
- Claude only writes: `app/globals.css`, `app/layout.jsx`, `app/page.jsx`, `components/*.jsx`
- Next.js dev server hot-reloads when files are uploaded

### SSE Events (frontend ↔ backend)
Events streamed from `/clone/stream`:
- `step` — tool being called (scraping, deploying, checking, fixing)
- `scrape_done` — extraction complete with counts (images, fonts, links)
- `file` / `file_updated` — code file created/modified (shown in editor)
- `deployed` — sandbox live with preview_url
- `screenshot` — base64 PNG of the clone
- `agent_message` — Claude's text reasoning
- `logs` — sandbox console output
- `done` — agent finished

### Chat Follow-up
After initial clone, user can type "make the header darker" → `POST /clone/{id}/chat` → agent continues the conversation with same sandbox, updates files, re-verifies.

## Critical Conventions

### OpenRouter API Format
We use OpenAI-compatible format (NOT Anthropic native):
```python
tools = [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
```
Messages use `tool_calls` and `tool` role, not `tool_use` content blocks.

### JSX Rules (enforced in system prompt)
- `className` not `class`
- Self-close void elements: `<img />`, `<br />`, `<input />`
- `style={{ color: '#fff' }}` not `style="color: #fff"`
- `{/* comment */}` not `<!-- comment -->`
- `htmlFor` not `for`
- Every `.map()` needs `key` prop
- Every `<img>` needs `alt` prop
- `"use client"` at top of every component file

### Color Rules
NEVER use named Tailwind colors (bg-blue-500). ALWAYS use arbitrary hex values from scraped data:
```
bg-[#0a2540] text-[#425466] border-[#e5e7eb]
```

### Image Rules
NEVER use placeholder images. Always use real URLs from scrape data. Use `<img>` tags (not Next.js `<Image>`) to avoid domain config issues.

### Sandbox File Paths
All files in Daytona sandbox are under `/home/daytona/clone-app/`. When uploading:
```python
full_path = f"/home/daytona/clone-app/{filepath}"  # e.g. /home/daytona/clone-app/components/Hero.jsx
```

## Environment Variables
```
OPENROUTER_API_KEY=sk-or-...
DAYTONA_API_KEY=...
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJ...
```

## Running Locally
```bash
# Backend
cd backend
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --reload --port 8000

# Frontend
cd frontend
npm install
npm run dev  # port 3000
```

## Deployment
- Frontend: Vercel (`cd frontend && npx vercel`)
- Backend: Railway (Dockerfile) or any container host
- Set all env vars in deployment platform
- CORS configured in FastAPI for frontend domain

## Common Tasks

### Adding a new tool
1. Add definition to `mcp_tools.py` TOOLS array
2. Add handler in `tool_handlers.py`
3. Add SSE event emission in `agent.py` streaming loop
4. Add event rendering in frontend `CloneAgent.tsx`

### Modifying the system prompt
Edit `REACT_SYSTEM_PROMPT` or `HTML_SYSTEM_PROMPT` in `agent.py`. The prompt controls clone quality — changes here have the biggest impact on output.

### Adding a new package to React sandbox
1. Add to `PACKAGE_JSON` in `sandbox_template.py`
2. Document usage in the system prompt's package reference
3. Add import examples to the interactivity section if relevant

### Debugging a bad clone
1. Check scrape data: is the DOM skeleton correct? Are colors extracted?
2. Check agent iterations: did Claude use the scraped data or guess?
3. Check sandbox logs: are there JSX compilation errors?
4. Compare screenshots: what specifically differs?

## Testing
```bash
# Test scraper standalone
python -c "import asyncio; from app.scraper import scrape_website; import json; print(json.dumps(asyncio.run(scrape_website('https://example.com')), indent=2))"

# Test SSE endpoint
curl -N -X POST http://localhost:8000/clone/stream \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com","output_format":"react"}'

# Test sandbox provisioning
python -c "import asyncio; from app.sandbox_template import provision_react_sandbox; print(asyncio.run(provision_react_sandbox()))"
```
