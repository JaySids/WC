"""
Project assembler — two-agent review pipeline:
1. Gemini assembly agent: reviews all components for CSS/design consistency
2. Claude code review agent: fixes JSX/React compilation errors
"""

import asyncio
import json
import os

import anthropic


# ---------------------------------------------------------------
# Gemini Assembly Agent
# ---------------------------------------------------------------

GEMINI_ASSEMBLY_PROMPT = """You are a senior React/Next.js developer reviewing a set of independently-generated components that need to work together as one cohesive website.

All components were generated in parallel from scraped website data. They share a design token system but may have inconsistencies.

## Your Job
Review ALL files and fix:
1. **CSS consistency**: All components must use the same design token hex values. Replace any random/wrong hex colors with the correct design token values.
2. **Font consistency**: All components should use the design token fonts. No hardcoded font-family that doesn't match.
3. **Spacing consistency**: Section padding should be consistent (use the design token section_padding_y).
4. **z-index layering**: Navbar z-50, dropdowns z-40, modals z-50+.
5. **Duplicate utility functions**: If multiple components define the same helper (e.g., `cn()`), remove duplicates and use the one from a shared import or inline it.
6. **Import correctness**: Ensure all imports resolve. No importing from files that don't exist.
7. **Swiper CSS**: If any component uses Swiper, ensure it imports the necessary CSS modules.
8. **globals.css**: Ensure font imports, Tailwind import, and base reset styles are present and correct.
9. **layout.jsx**: Ensure Google Font links, metadata, and body styles match design tokens.
10. **Responsive design**: Ensure all components use responsive prefixes (sm:, md:, lg:).

## Design Tokens Reference
{design_tokens}

## DO NOT:
- Rewrite component logic or structure
- Remove sections or content
- Add new sections
- Change text content, image URLs, or link hrefs
- Make the output shorter/simpler — preserve ALL content

## Output Format
Output ONLY a JSON object mapping filepath to the COMPLETE corrected file content.
Only include files that actually need changes. If no changes needed, output: {}
No markdown fences. No explanation. Just the JSON object."""


def _get_gemini_key():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        from app.config import get_settings
        key = get_settings().gemini_api_key
    return key


async def gemini_assembly_review(files: dict, design_tokens: dict) -> dict:
    """
    Gemini reviews all files for cross-component consistency.
    Returns the full corrected file set, or original files on failure.
    Uses the new google.genai SDK with native async support.
    """
    api_key = _get_gemini_key()
    if not api_key:
        print("  [gemini-assembly] No GEMINI_API_KEY — skipping assembly review")
        return files

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        # Build the file listing
        file_listing = []
        for fp in sorted(files.keys()):
            file_listing.append(f"=== {fp} ===\n{files[fp]}")
        all_files_text = "\n\n".join(file_listing)

        # Truncate design tokens to keep prompt reasonable
        tokens_json = json.dumps(design_tokens, indent=2)[:4000]

        prompt = GEMINI_ASSEMBLY_PROMPT.replace("{design_tokens}", tokens_json)
        prompt += f"\n\n## Files to Review\n\n{all_files_text}"

        print(f"  [gemini-assembly] Sending {len(files)} files to Gemini 2.5 Pro...")

        # Native async call with timeout (5 minutes max)
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model="gemini-2.5-pro",
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=65536,
                    temperature=0.1,
                ),
            ),
            timeout=300,
        )

        text = response.text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        result = json.loads(text)
        if isinstance(result, dict) and len(result) > 0:
            # Merge changes into original files
            merged = dict(files)
            merged.update(result)
            print(f"  [gemini-assembly] Changed {len(result)} files: {list(result.keys())}")
            return merged
        else:
            print("  [gemini-assembly] No changes needed")
            return files

    except asyncio.TimeoutError:
        print("  [gemini-assembly] Timed out after 5 minutes — keeping originals")
        return files
    except Exception as e:
        print(f"  [gemini-assembly] Failed: {e}")
        return files


# ---------------------------------------------------------------
# Claude Code Review Agent
# ---------------------------------------------------------------

CODE_REVIEW_PROMPT = """You are a strict React/Next.js code reviewer. Review the following component files and fix ANY code errors that would cause compilation failures or runtime crashes.

## Fix These Issues:
1. **"use client" directive**: Add it to every component file that uses hooks (useState, useEffect, useRef), event handlers (onClick, onChange), or browser APIs (window, document).
2. **className not class**: Replace every `class=` with `className=`.
3. **Self-closing void elements**: `<img />`, `<br />`, `<input />`, `<hr />` — never `<img>`.
4. **style objects**: `style={{ color: '#fff' }}` not `style="color: #fff"`.
5. **htmlFor not for**: In `<label>` elements.
6. **key props**: Every `.map()` call must have a `key` prop on the returned element.
7. **alt props**: Every `<img>` must have an `alt` prop.
8. **JSX comments**: `{/* comment */}` not `<!-- comment -->`.
9. **export default**: Every component file must have `export default function ComponentName()`.
10. **Unclosed tags**: Fix any unclosed JSX tags or mismatched braces.
11. **Invalid JSX**: Fix `{...}` expressions that would cause syntax errors.
12. **Import paths**: Ensure all imports use valid paths. Remove imports for non-existent modules.

## DO NOT:
- Change colors, text content, images, or layout
- Refactor or simplify code
- Remove sections or content
- Add new functionality

## Output Format
Output ONLY a JSON object mapping filepath to COMPLETE corrected file content.
Only include files that actually need changes. If no changes needed, output: {}
No markdown fences. No explanation."""


def _get_anthropic_client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        from app.config import get_settings
        api_key = get_settings().anthropic_api_key
    return anthropic.AsyncAnthropic(api_key=api_key)


async def claude_code_review(files: dict) -> dict:
    """
    Claude reviews all files for JSX/React code errors.
    Returns only the changed files (or empty dict if clean).
    """
    try:
        client = _get_anthropic_client()

        # Build file listing
        file_listing = []
        for fp in sorted(files.keys()):
            file_listing.append(f"=== {fp} ===\n{files[fp]}")
        all_files_text = "\n\n".join(file_listing)

        # Truncate if massive
        if len(all_files_text) > 80000:
            all_files_text = all_files_text[:80000] + "\n\n... (truncated)"

        text = ""
        async with client.messages.stream(
            model="claude-sonnet-4-5-20250929",
            max_tokens=32000,
            system=CODE_REVIEW_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Review these files for code errors:\n\n{all_files_text}",
            }],
        ) as stream:
            async for chunk in stream.text_stream:
                text += chunk

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]

        changes = json.loads(text.strip())

        if changes:
            print(f"  [code-review] Fixed {len(changes)} files: {list(changes.keys())}")
        else:
            print("  [code-review] Clean — no code errors found")

        return changes

    except Exception as e:
        print(f"  [code-review] Failed: {e}")
        return {}


# ---------------------------------------------------------------
# Gemini Code Review Agent
# ---------------------------------------------------------------

async def gemini_code_review(files: dict) -> dict:
    """
    Gemini reviews all files for JSX/React code errors.
    Returns only the changed files (or empty dict if clean).
    Uses the google.genai SDK with native async.
    """
    api_key = _get_gemini_key()
    if not api_key:
        print("  [gemini-code-review] No GEMINI_API_KEY — skipping code review")
        return {}

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        # Build file listing
        file_listing = []
        for fp in sorted(files.keys()):
            file_listing.append(f"=== {fp} ===\n{files[fp]}")
        all_files_text = "\n\n".join(file_listing)

        prompt = CODE_REVIEW_PROMPT + f"\n\nReview these files for code errors:\n\n{all_files_text}"

        print(f"  [gemini-code-review] Sending {len(files)} files to Gemini 2.5 Pro...")

        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model="gemini-2.5-pro",
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=65536,
                    temperature=0.1,
                ),
            ),
            timeout=300,
        )

        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        changes = json.loads(text)

        if isinstance(changes, dict) and changes:
            print(f"  [gemini-code-review] Fixed {len(changes)} files: {list(changes.keys())}")
            return changes
        else:
            print("  [gemini-code-review] Clean — no code errors found")
            return {}

    except asyncio.TimeoutError:
        print("  [gemini-code-review] Timed out after 5 minutes — skipping")
        return {}
    except Exception as e:
        print(f"  [gemini-code-review] Failed: {e}")
        return {}
