"""
Parse Next.js dev server log output for compilation errors.
Pure Python, no AI — regex-based extraction.
"""

import re


def parse_nextjs_errors(log_output: str) -> dict:
    """
    Parse Next.js dev server logs and extract structured error information.

    Returns:
        {
            "has_errors": bool,
            "compiled": bool,
            "errors": [
                {
                    "type": "build_error" | "module_not_found" | "syntax_error" | ...,
                    "file": str or None,
                    "line": int or None,
                    "message": str,
                }
            ]
        }
    """
    if not log_output:
        return {"has_errors": False, "compiled": False, "errors": []}

    errors = []
    compiled = False

    # If we have a LOG_MARKER, only look at content after the LAST marker
    marker_parts = re.split(r"=== LOG_MARKER_\d+ ===", log_output)
    if len(marker_parts) > 1:
        log_output = marker_parts[-1]

    # Only look at the LAST compilation result (not old ones from scaffold)
    # Split on "Compiling" to get the most recent compilation block
    compile_blocks = re.split(r"(?:Compiling|▲ Next\.js)", log_output)
    last_block = compile_blocks[-1] if compile_blocks else log_output

    # Check if the LATEST compilation succeeded
    if re.search(r"Compiled\s+(successfully|in\s+\d)", last_block, re.IGNORECASE):
        compiled = True

    # "Failed to compile" in latest block
    if "Failed to compile" in last_block:
        compiled = False

    # Use last_block for error extraction to avoid false positives from old compiles
    log_output = last_block

    # --- Module not found ---
    # Pattern: Module not found: Can't resolve 'xxx' in '/path/to/file'
    for m in re.finditer(
        r"Module not found:\s*(?:Can't resolve|Error:\s*Can't resolve)\s*['\"]([^'\"]+)['\"]\s*(?:in\s*['\"]([^'\"]+)['\"])?",
        log_output,
    ):
        module = m.group(1)
        file = m.group(2) or None
        errors.append({
            "type": "module_not_found",
            "file": _extract_project_path(file) if file else None,
            "line": None,
            "message": f"Module not found: '{module}'",
            "module": module,
        })

    # --- Build errors with file paths ---
    # Pattern: ./components/Foo.tsx:10:5
    # or: ./app/page.tsx
    # Followed by error text
    for m in re.finditer(
        r"(\./[\w/.-]+\.(?:tsx?|jsx?|css))(?::(\d+)(?::(\d+))?)?[^\n]*\n([^\n]+)",
        log_output,
    ):
        file = m.group(1)
        line = int(m.group(2)) if m.group(2) else None
        msg = m.group(4).strip()
        # Skip if it's just a file listing, not an error
        if msg and not msg.startswith("./") and len(msg) > 5:
            errors.append({
                "type": "build_error",
                "file": _extract_project_path(file),
                "line": line,
                "message": msg[:300],
            })

    # --- SyntaxError ---
    for m in re.finditer(
        r"SyntaxError:\s*([^\n]+?)(?:\s*\((\d+):(\d+)\))?",
        log_output,
    ):
        msg = m.group(1).strip()
        line = int(m.group(2)) if m.group(2) else None
        errors.append({
            "type": "syntax_error",
            "file": None,
            "line": line,
            "message": f"SyntaxError: {msg}",
        })

    # --- TypeError ---
    for m in re.finditer(r"TypeError:\s*([^\n]+)", log_output):
        msg = m.group(1).strip()
        # Try to find a file reference nearby
        file = _find_nearby_file(log_output, m.start())
        errors.append({
            "type": "type_error",
            "file": file,
            "line": None,
            "message": f"TypeError: {msg[:300]}",
        })

    # --- Hydration mismatch ---
    for m in re.finditer(
        r"(?:Hydration|hydration)\s+(?:failed|mismatch|error)[^\n]*",
        log_output, re.IGNORECASE,
    ):
        errors.append({
            "type": "hydration_error",
            "file": None,
            "line": None,
            "message": m.group(0).strip()[:300],
        })

    # --- Bad default export ---
    for m in re.finditer(
        r"(?:The default export is not a React Component|does not have a default export)",
        log_output,
    ):
        file = _find_nearby_file(log_output, m.start())
        errors.append({
            "type": "bad_export",
            "file": file,
            "line": None,
            "message": m.group(0).strip(),
        })

    # --- Generic "Error:" lines not caught above ---
    for m in re.finditer(r"^\s*Error:\s*([^\n]+)", log_output, re.MULTILINE):
        msg = m.group(1).strip()
        # Skip if already captured
        if any(msg in e["message"] for e in errors):
            continue
        # Skip non-actionable errors
        if any(skip in msg for skip in ["ENOENT", "EACCES", "watch", "EMFILE"]):
            continue
        file = _find_nearby_file(log_output, m.start())
        errors.append({
            "type": "generic_error",
            "file": file,
            "line": None,
            "message": msg[:300],
        })

    # Deduplicate
    seen = set()
    unique_errors = []
    for e in errors:
        key = (e["type"], e.get("file"), e["message"][:100])
        if key not in seen:
            seen.add(key)
            unique_errors.append(e)

    has_errors = len(unique_errors) > 0 or "Failed to compile" in log_output

    return {
        "has_errors": has_errors,
        "compiled": compiled and not has_errors,
        "errors": unique_errors,
    }


def format_nextjs_errors(parsed: dict) -> str:
    """Format parsed errors into a string for Claude to fix."""
    if not parsed["errors"]:
        if parsed["has_errors"]:
            return "Compilation failed but no specific errors were extracted from logs."
        return "No errors found."

    lines = [f"Found {len(parsed['errors'])} compilation error(s):\n"]
    for i, err in enumerate(parsed["errors"], 1):
        file_str = err.get("file") or "unknown file"
        line_str = f":{err['line']}" if err.get("line") else ""
        lines.append(f"{i}. [{err['type']}] {file_str}{line_str}")
        lines.append(f"   {err['message']}")
        if err.get("module"):
            lines.append(f"   Missing module: {err['module']}")
        lines.append("")

    return "\n".join(lines)


def _extract_project_path(path: str | None) -> str | None:
    """Extract project-relative path from absolute or ./relative path."""
    if not path:
        return None
    # Remove leading ./
    if path.startswith("./"):
        return path[2:]
    # Extract from absolute path
    for marker in ["/my-app/", "/clone-app/"]:
        idx = path.find(marker)
        if idx >= 0:
            return path[idx + len(marker):]
    return path


def _find_nearby_file(log_output: str, position: int) -> str | None:
    """Try to find a file path near the given position in the log output."""
    # Look in a 500-char window before the position
    start = max(0, position - 500)
    window = log_output[start:position]
    files = re.findall(r"(\./[\w/.-]+\.(?:tsx?|jsx?|css))", window)
    if files:
        return _extract_project_path(files[-1])
    return None
