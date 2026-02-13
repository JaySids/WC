# Code Change Verifier - Agent Memory

## Project Architecture (confirmed 2026-02-13)
- Backend uses **Anthropic native SDK** (not OpenRouter). `_get_client()` returns `anthropic.AsyncAnthropic`.
- CLAUDE.md still references OpenRouter but code has migrated to direct Anthropic API.
- CLAUDE.md says paths are `/home/daytona/clone-app/` but actual code uses `/home/daytona/my-app/`.
- Frontend is at `frontend_new/` (not `frontend/`), uses Vite + React (not Next.js).
- No `tool_handlers.py` or `mcp_tools.py` exist anymore -- the project uses a one-shot generation pipeline, not an MCP tool loop.
- Agent pipeline: scrape -> one-shot Claude generation -> upload to sandbox -> check/fix loop.
- Fix loop uses: Gemini (per-file) -> Claude `fix_targeted()` fallback.

## Key File Relationships
- `agent.py` imports from: `sse_utils`, `scraper`, `nextjs_error_parser`, `sandbox`, `sandbox_template`, `config`, `database`
- `config.py` fields: `anthropic_api_key`, `daytona_api_key`, `supabase_url`, `supabase_key`, `gemini_api_key`
- `sandbox.py` exports: `PROJECT_PATH=/home/daytona/my-app`, `BUN_BIN`, `get_daytona_client`, `create_react_boilerplate_sandbox`
- `sandbox_template.py` exports: `upload_files_to_sandbox`, `get_sandbox_logs` (active), plus dead code: `TEMPLATE_FILES`, `provision_react_sandbox_from_template`
- SSE events emitted by agent: `clone_created`, `step`, `scrape_done`, `file`, `file_updated`, `deployed`, `generation_complete`, `compiled`, `compile_errors`, `runtime_errors`, `warning`, `agent_message`, `user_message`, `error`, `done`
- Frontend handles all above except `user_message` (intentionally dropped).

## Common Patterns
- All Daytona SDK calls are synchronous; wrapped in `asyncio.to_thread()` for async compatibility.
- Database functions in `database.py` are `async def` but use sync Supabase calls (blocks event loop).
- Retry pattern: `for attempt in range(N): try/except with progressive backoff; raise last_err on exhaustion`.
- SSE error boundary in main.py: `try/except/finally` with `got_done` flag ensures `done` event always sent.
- Smart polling pattern: `for _poll in range(N): await asyncio.sleep(3); ... else: log("timeout")`.

## Known Issues / Fragile Areas
- **Stale closure in App.tsx**: `processStream` (deps=[]) and `handleEvent` (deps=[addMsg]) capture `url` and `previewUrl` at creation time. `retryUrl` metadata is always '' and `compiled` iframe reload never fires. Fix: use refs like `urlRef`/`previewUrlRef`.
- `_check_sandbox_http` error_messages regex parsing is sensitive to HTML structure changes in Next.js error overlay.
- The fix loop merges `missing_fixed` into `fixed` dict at line ~1297 AFTER already emitting `file_updated` events for `missing_fixed` at line ~1225 -- could cause duplicate events.
- `sandbox_template.py` has dead code (TEMPLATE_FILES, PACKAGE_JSON etc.) using Tailwind v3 syntax while active pipeline uses v4.

## Dependency: google-genai
- `requirements.txt` has `google-genai`, import is `from google import genai` -- correct for official Google GenAI Python SDK.
