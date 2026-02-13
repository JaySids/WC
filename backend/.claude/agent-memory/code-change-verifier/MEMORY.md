# Code Change Verifier - Agent Memory

## Project Architecture (confirmed 2026-02-12)
- Backend uses **Anthropic native SDK** (not OpenRouter). `_get_client()` returns `anthropic.AsyncAnthropic`.
- CLAUDE.md still references OpenRouter but code has migrated to direct Anthropic API.
- Frontend is at `frontend_new/` (not `frontend/`), uses Vite + React (not Next.js).
- No `tool_handlers.py` or `mcp_tools.py` exist anymore -- the project uses a one-shot generation pipeline, not an MCP tool loop.
- Agent pipeline: scrape -> one-shot Claude generation -> upload to sandbox -> check/fix loop.
- Fix loop uses: Gemini Flash (per-file) -> Claude `fix_targeted()` fallback.

## Key File Relationships
- `agent.py` imports from: `sse_utils`, `scraper`, `nextjs_error_parser`, `sandbox`, `sandbox_template`, `config`, `database`
- `config.py` fields: `anthropic_api_key`, `daytona_api_key`, `supabase_url`, `supabase_key`, `gemini_api_key`
- `sandbox.py` exports: `PROJECT_PATH=/home/daytona/my-app`, `BUN_BIN`, `get_daytona_client`, `create_react_boilerplate_sandbox`
- `sandbox_template.py` exports: `upload_files_to_sandbox`, `get_sandbox_logs`
- SSE events emitted by agent: `clone_created`, `step`, `scrape_done`, `file`, `file_updated`, `deployed`, `generation_complete`, `compiled`, `compile_errors`, `runtime_errors`, `warning`, `agent_message`, `error`, `done`
- Frontend handles all above event types in `App.tsx` handleEvent switch.

## Common Patterns
- All Daytona SDK calls are synchronous; wrapped in `asyncio.to_thread()` for async compatibility.
- `_check_sandbox_http()` returns dict with `ok`, `status_code`, `errors`, `error_messages`, `body_length`, `body`.
- Smart polling pattern: `for _poll in range(N): await asyncio.sleep(3); ... else: log("timeout")`.
- `re` imported at top of agent.py (line 15) AND locally as `_re` inside `_check_sandbox_http` (line 179) -- redundant but not harmful.

## Fragile Areas
- `_check_sandbox_http` error_messages regex parsing is sensitive to HTML structure changes in Next.js error overlay.
- The fix loop merges `missing_fixed` into `fixed` dict at line 1293 AFTER already emitting `file_updated` events for `missing_fixed` at line 1225 -- could cause duplicate events if a file appears in both dicts.
- `sandbox_template.py` GLOBALS_CSS uses Tailwind v3 `@tailwind` directives but `agent.py` generates v4 `@import "tailwindcss"` -- the template CSS is overwritten during generation so this is not a real issue, but could confuse if template is ever used standalone.

## Dependency: google-genai
- `requirements.txt` has `google-genai`, import is `from google import genai` -- this is correct for the official Google GenAI Python SDK.
