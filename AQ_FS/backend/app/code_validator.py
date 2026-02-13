"""
Static JSX/TSX code validator. Pure Python, no AI.
Catches common React/Next.js issues before deployment.
"""

import re

# Packages pre-installed in the sandbox (and their subpaths)
VALID_PACKAGES = {
    "react", "react-dom", "next",
    "framer-motion",
    "lucide-react",
    "@radix-ui/react-accordion", "@radix-ui/react-dialog",
    "@radix-ui/react-tabs",
    "react-intersection-observer",
    "clsx", "tailwind-merge",
    # Subpaths
    "next/font", "next/font/google", "next/font/local",
    "next/image", "next/link", "next/navigation", "next/head",
}


def validate_files(files: dict) -> dict:
    """
    Validate all generated JSX/TSX/CSS files.

    Args:
        files: dict mapping filepath to content

    Returns:
        {
            "valid": bool,
            "errors": [{"file": str, "line": int, "type": str, "message": str, "fix_hint": str}],
            "warnings": [{"file": str, "line": int, "type": str, "message": str}],
            "stats": {"total_files": int, "components": int, "lines": int}
        }
    """
    errors = []
    warnings = []
    component_files = set()
    total_lines = 0

    for filepath, content in files.items():
        if not content or not content.strip():
            continue

        lines = content.split("\n")
        total_lines += len(lines)

        is_jsx = filepath.endswith((".jsx", ".tsx", ".js", ".ts"))
        is_css = filepath.endswith(".css")
        is_component = filepath.startswith("components/") and is_jsx

        if is_component:
            component_files.add(filepath)

        if is_jsx:
            errors.extend(_check_jsx(filepath, content, lines))
            warnings.extend(_check_jsx_warnings(filepath, content, lines))

        if is_css:
            warnings.extend(_check_css(filepath, content, lines))

    # Cross-file checks
    page_file = None
    for fp in ["app/page.jsx", "app/page.tsx"]:
        if fp in files:
            page_file = fp
            break

    if page_file:
        errors.extend(_check_imports(page_file, files[page_file], files))
        warnings.extend(_check_orphans(files, page_file))
    else:
        warnings.append({
            "file": "app/page.jsx",
            "line": 0,
            "type": "missing_page",
            "message": "app/page.jsx not found — the app won't render",
        })

    # Check for layout
    has_layout = any(fp in files for fp in ["app/layout.jsx", "app/layout.tsx"])
    if not has_layout:
        warnings.append({
            "file": "app/layout.jsx",
            "line": 0,
            "type": "missing_layout",
            "message": "app/layout.jsx not generated (may use sandbox default)",
        })

    # Check for globals.css
    if "app/globals.css" not in files:
        warnings.append({
            "file": "app/globals.css",
            "line": 0,
            "type": "missing_globals",
            "message": "app/globals.css not generated (may use sandbox default)",
        })

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "total_files": len(files),
            "components": len(component_files),
            "lines": total_lines,
        },
    }


def format_error_report(validation: dict) -> str:
    """Format validation results into a human-readable string."""
    parts = []
    if validation["errors"]:
        parts.append(f"ERRORS ({len(validation['errors'])}):")
        for e in validation["errors"]:
            line_str = f":{e['line']}" if e.get("line") else ""
            parts.append(f"  [{e['type']}] {e['file']}{line_str} — {e['message']}")
            if e.get("fix_hint"):
                parts.append(f"    Fix: {e['fix_hint']}")

    if validation["warnings"]:
        parts.append(f"\nWARNINGS ({len(validation['warnings'])}):")
        for w in validation["warnings"]:
            line_str = f":{w['line']}" if w.get("line") else ""
            parts.append(f"  [{w['type']}] {w['file']}{line_str} — {w['message']}")

    return "\n".join(parts) if parts else "All checks passed."


def _check_jsx(filepath: str, content: str, lines: list) -> list:
    """Check a JSX/TSX file for errors."""
    errors = []

    # --- missing "use client" ---
    needs_use_client = bool(re.search(
        r"\b(useState|useEffect|useRef|useCallback|useMemo|useReducer|useContext"
        r"|onClick|onChange|onSubmit|onMouseEnter|onMouseLeave|onKeyDown"
        r"|window\.|document\.)\b",
        content,
    ))
    has_use_client = '"use client"' in content or "'use client'" in content

    if needs_use_client and not has_use_client:
        errors.append({
            "file": filepath,
            "line": 1,
            "type": "missing_use_client",
            "message": "Component uses hooks/events but missing \"use client\" directive",
            "fix_hint": "Add '\"use client\";' as the very first line of the file",
        })

    # --- class= instead of className= ---
    for i, line in enumerate(lines, 1):
        # Skip comments and strings
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        # Match class= but not className= and not CSS class selectors
        matches = re.finditer(r'\bclass\s*=\s*["{]', line)
        for m in matches:
            # Make sure it's not className
            before = line[:m.start()]
            if not before.endswith("className") and not before.endswith("class"):
                # Actually check properly
                if not re.search(r'className\s*$', before):
                    errors.append({
                        "file": filepath,
                        "line": i,
                        "type": "class_not_classname",
                        "message": f"Use className= instead of class= in JSX",
                        "fix_hint": "Replace class= with className=",
                    })

    # --- for= instead of htmlFor= ---
    for i, line in enumerate(lines, 1):
        if re.search(r'<label[^>]*\bfor\s*=', line):
            errors.append({
                "file": filepath,
                "line": i,
                "type": "for_not_htmlfor",
                "message": "Use htmlFor= instead of for= on labels",
                "fix_hint": "Replace for= with htmlFor=",
            })

    # --- style="..." instead of style={{}} ---
    for i, line in enumerate(lines, 1):
        if re.search(r'\bstyle\s*=\s*"[^"]*"', line):
            # Make sure it's in JSX context (not a comment)
            stripped = line.strip()
            if not stripped.startswith("//") and not stripped.startswith("*"):
                errors.append({
                    "file": filepath,
                    "line": i,
                    "type": "style_string",
                    "message": "style=\"...\" should be style={{...}} in JSX",
                    "fix_hint": "Convert style string to style object: style={{ property: 'value' }}",
                })

    # --- HTML comments <!-- --> ---
    for i, line in enumerate(lines, 1):
        if "<!--" in line:
            stripped = line.strip()
            if not stripped.startswith("//") and not stripped.startswith("*"):
                errors.append({
                    "file": filepath,
                    "line": i,
                    "type": "html_comment",
                    "message": "HTML comment <!-- --> found — use {/* */} in JSX",
                    "fix_hint": "Replace <!-- comment --> with {/* comment */}",
                })

    # --- Truncation comments ---
    truncation_patterns = [
        r"//\s*\.\.\.",
        r"//\s*rest of",
        r"//\s*more items",
        r"//\s*etc\.?$",
        r"//\s*add more",
        r"//\s*remaining",
        r"//\s*continue",
        r"\{/\*\s*\.\.\.\s*\*/\}",
    ]
    for i, line in enumerate(lines, 1):
        for pat in truncation_patterns:
            if re.search(pat, line, re.IGNORECASE):
                errors.append({
                    "file": filepath,
                    "line": i,
                    "type": "truncation_comment",
                    "message": f"Truncation placeholder found: {line.strip()[:80]}",
                    "fix_hint": "Replace with actual content — never abbreviate",
                })
                break

    # --- Duplicate consecutive blocks (4+ identical lines) ---
    if len(lines) > 8:
        for i in range(len(lines) - 3):
            block = lines[i:i + 4]
            if all(l.strip() for l in block):
                if i + 8 <= len(lines) and lines[i + 4:i + 8] == block:
                    errors.append({
                        "file": filepath,
                        "line": i + 5,
                        "type": "duplicate_block",
                        "message": "4+ consecutive identical lines repeated — likely copy-paste error",
                        "fix_hint": "Remove the duplicate block",
                    })

    # --- Missing default export ---
    if filepath.startswith("components/") or filepath == "app/page.jsx" or filepath == "app/page.tsx":
        has_default = bool(re.search(r"export\s+default\s+", content))
        if not has_default:
            errors.append({
                "file": filepath,
                "line": 0,
                "type": "missing_default_export",
                "message": "No default export found — component won't be importable",
                "fix_hint": "Add 'export default function ComponentName() {...}' or 'export default ComponentName'",
            })

    # --- Empty component ---
    if re.search(r"return\s*\(\s*null\s*\)|return\s+null\s*;|return\s*\(\s*<>\s*</>\s*\)", content):
        errors.append({
            "file": filepath,
            "line": 0,
            "type": "empty_component",
            "message": "Component returns null or empty fragment",
            "fix_hint": "Implement the component with actual content",
        })

    # --- Bad imports ---
    for i, line in enumerate(lines, 1):
        m = re.match(r"import\s+.*\s+from\s+['\"]([^.'\"@/][^'\"]*)['\"]", line)
        if m:
            pkg = m.group(1)
            # Extract base package name (e.g., "@radix-ui/react-accordion" from "@radix-ui/react-accordion")
            if pkg.startswith("@"):
                parts = pkg.split("/")
                base = "/".join(parts[:2]) if len(parts) >= 2 else pkg
            else:
                base = pkg.split("/")[0]

            if base not in VALID_PACKAGES and pkg not in VALID_PACKAGES:
                errors.append({
                    "file": filepath,
                    "line": i,
                    "type": "bad_import",
                    "message": f"Import from '{pkg}' — package not installed in sandbox",
                    "fix_hint": f"Use an installed alternative or remove this import",
                })

    return errors


def _check_jsx_warnings(filepath: str, content: str, lines: list) -> list:
    """Check for non-critical issues."""
    warnings = []

    # --- .map() without key ---
    for i, line in enumerate(lines, 1):
        if ".map(" in line or ".map (" in line:
            # Look ahead a few lines for key=
            block = "\n".join(lines[i - 1:min(i + 10, len(lines))])
            if "key=" not in block and "key =" not in block:
                warnings.append({
                    "file": filepath,
                    "line": i,
                    "type": "missing_key_prop",
                    "message": ".map() call may be missing key prop on returned elements",
                })

    return warnings


def _check_css(filepath: str, content: str, lines: list) -> list:
    """Check CSS files for issues."""
    warnings = []
    # Could add CSS-specific checks here if needed
    return warnings


def _check_imports(page_file: str, page_content: str, files: dict) -> list:
    """Check that page.jsx imports match existing component files."""
    errors = []

    # Find all imports from relative paths
    for m in re.finditer(r"import\s+(\w+)\s+from\s+['\"]\.\.?/([^'\"]+)['\"]", page_content):
        component_name = m.group(1)
        import_path = m.group(2)

        # Normalize path
        if not import_path.endswith((".jsx", ".tsx", ".js", ".ts")):
            candidates = [
                import_path + ".jsx",
                import_path + ".tsx",
                import_path + "/index.jsx",
                import_path + "/index.tsx",
            ]
        else:
            candidates = [import_path]

        found = False
        for candidate in candidates:
            if candidate in files:
                found = True
                break

        if not found:
            errors.append({
                "file": page_file,
                "line": 0,
                "type": "missing_component_file",
                "message": f"Import '{component_name}' from '{import_path}' — file not found in generated files",
                "fix_hint": f"Either generate the missing component or remove the import",
            })

    return errors


def _check_orphans(files: dict, page_file: str) -> list:
    """Check for component files not imported by page.jsx."""
    warnings = []
    page_content = files.get(page_file, "")

    for filepath in files:
        if not filepath.startswith("components/"):
            continue
        if filepath.endswith("ErrorBoundary.tsx") or filepath.endswith("ErrorBoundary.jsx"):
            continue

        # Extract component name from filename
        basename = filepath.rsplit("/", 1)[-1]
        name = basename.replace(".jsx", "").replace(".tsx", "")

        if name not in page_content:
            warnings.append({
                "file": filepath,
                "line": 0,
                "type": "orphan_component",
                "message": f"Component '{name}' exists but is not imported in {page_file}",
            })

    return warnings
