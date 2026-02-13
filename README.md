# Website Cloner

AI-powered website replication tool. Paste a URL, get a pixel-perfect React clone deployed to a live sandbox in minutes.

**[Live App](https://wc-three.vercel.app/)** | **[Demo Video](https://www.loom.com/share/44fa43542f084ca9a69d8f87b487fe83)**

Built for an [Afterquery](https://afterquery.com) (YC) internship assessment.

---

## How It Works

```
Paste URL  -->  AI scrapes the site  -->  Generates React clone  -->  Deploys to cloud sandbox
                                                                          |
                                                            Live preview + chat to refine
```

1. **Scrape** -- Playwright extracts the full DOM structure, every image/font/CSS asset via network interception, exact hex colors, font stacks, section layouts, and per-section screenshots
2. **Generate** -- Claude Sonnet 4 analyzes the screenshots + structured data and produces a complete Next.js app (layout, page, components, CSS) in a single pass
3. **Deploy** -- Files are uploaded to a Daytona cloud sandbox with a pre-configured Next.js environment. Hot-reloads instantly
4. **Verify & Fix** -- The agent checks compilation, fetches the page, and if there are errors, an AI diagnosis loop auto-fixes them (up to 4 iterations)
5. **Chat** -- After the initial clone, ask the agent to tweak anything ("make the header darker", "add a CTA button") and it updates the live sandbox in real-time

Everything streams to the frontend via Server-Sent Events so you see each step as it happens.

---

## Tech Stack

| Layer | Tech |
|-------|------|
| **Frontend** | React + Vite, Tailwind CSS, Monaco Editor, Material Symbols |
| **Backend** | FastAPI (Python), fully async |
| **AI** | Claude Sonnet 4 (Anthropic API) |
| **Scraping** | Playwright (headless Chromium) with network interception |
| **Sandboxes** | Daytona SDK -- cloud sandboxes for deploying clones |
| **Database** | Supabase (clone history, file storage, metadata) |
| **Streaming** | Server-Sent Events (FastAPI --> frontend) |
| **Deployment** | Vercel (frontend), Railway (backend) |

---

## Architecture

```
                          +------------------+
                          |   React Frontend |
                          |   (Vite + Tailwind)  |
                          +--------+---------+
                                   |
                              SSE stream
                                   |
                          +--------v---------+
                          |   FastAPI Backend |
                          +--------+---------+
                                   |
                     +-------------+-------------+
                     |                           |
              +------v------+           +--------v--------+
              |  Playwright  |           |  Claude Sonnet 4 |
              |  (scraper)   |           |  (generator)     |
              +------+------+           +--------+--------+
                     |                           |
                     |    structured data         |    generated files
                     +----------+----------------+
                                |
                       +--------v---------+
                       |  Daytona Sandbox  |
                       |  (Next.js + Bun)  |
                       +------------------+
                              |
                        live preview URL
```

---

## Project Structure

```
backend/
  app/
    main.py              # FastAPI endpoints (clone, chat, rebuild, export, cleanup)
    agent.py             # Core pipeline: scrape -> generate -> deploy -> verify -> fix
    scraper.py           # Playwright extraction (DOM, assets, themes, sections, screenshots)
    sandbox.py           # Daytona SDK wrapper (create, start, stop, delete sandboxes)
    sandbox_template.py  # File upload utilities, log retrieval
    database.py          # Supabase CRUD (clones, files, metadata)
    config.py            # Settings & env vars

frontend_new/
  App.tsx                # Main app: state management, SSE processing, all handlers
  components/
    Header.tsx           # URL input, history dropdown, view toggles, export
    LandingPage.tsx      # Landing page with clone history
    PreviewPane.tsx      # Live iframe preview of the sandbox
    TerminalPane.tsx     # Agent log / chat interface
  types.ts               # Shared TypeScript types
```

---

## Features

- **One-click cloning** -- paste any URL, get a working React app
- **Real-time streaming** -- watch the agent scrape, generate, deploy, and fix in real-time
- **AI-powered error fixing** -- automatic diagnosis and repair of compilation/runtime errors
- **Chat follow-up** -- refine the clone with natural language after initial generation
- **Clone history** -- all clones saved to Supabase with full file snapshots
- **Reactivation** -- rebuild any previous clone into a fresh sandbox from saved files
- **File editor** -- Monaco editor with syntax highlighting for all generated files
- **Live hot-fix** -- edit files in the editor and push changes to the sandbox instantly
- **Export** -- download all files as a .zip from any clone session
- **Split view** -- side-by-side editor + live preview, or fullscreen either pane

---

## Running Locally

### Prerequisites

- Python 3.11+
- Node.js 18+
- Playwright (`playwright install chromium`)
- Daytona API key
- Anthropic API key
- Supabase project

### Backend

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend_new
npm install
npm run dev
```

### Environment Variables

Create `backend/.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
DAYTONA_API_KEY=...
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_KEY=eyJ...
```

---

## Deployment

- **Frontend**: Vercel -- `cd frontend_new && npx vercel`
- **Backend**: Railway (Dockerfile) or any container host
- Set all env vars in your deployment platform
- CORS is configured in FastAPI for the frontend domain

---

## How the Scraper Works

Not just screenshots. We extract structured, machine-readable data:

- **Network interception** -- captures every image URL, font file, and CSS sheet the browser loads
- **DOM skeleton** -- simplified tree with layout annotations (`flex row between`, `grid cols-3 gap-24px`, `bg:#0a2540`)
- **Theme extraction** -- `getComputedStyle()` on all elements for exact hex colors, font families, sizes, weights
- **Section detection** -- semantic sections (navbar, hero, features, pricing, FAQ, footer) with per-section screenshots
- **Clickables** -- every `<a>` and `<button>` with real hrefs, categorized by location
- **SVGs** -- inline SVG markup extracted for icons

---

## Pre-installed Packages in Sandbox

Every sandbox comes with 33+ packages ready to use -- no install delay during generation:

**Icons**: lucide-react, react-icons |
**Animation**: framer-motion, react-countup, react-type-animation |
**Carousels**: swiper, embla-carousel-react |
**Radix UI**: accordion, dialog, tabs, dropdown-menu, navigation-menu, tooltip, popover, select, switch, checkbox, slider, scroll-area, avatar, progress, collapsible, separator, toast |
**Headless UI**: @headlessui/react |
**Utility**: clsx, tailwind-merge, class-variance-authority, react-intersection-observer, react-scroll, react-player
