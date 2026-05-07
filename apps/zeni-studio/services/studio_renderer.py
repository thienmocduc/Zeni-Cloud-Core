"""
Zeni Studio renderer — turn a project tree (components + pages + data sources +
actions + theme) into a deployable codebase.

Two output frameworks today:

    * Next.js 14 (app router) — preferred
    * React + Vite — for SPA / embed scenarios

Both renderers produce an in-memory zip the API can stream back. We deliberately
keep this module pure: no DB, no network. The API layer hydrates the project
into a plain ``dict`` (matches the canvas tree shape) and feeds it in here.

Public API
----------
- ``render_next_project(project, components_by_id, pages, data_sources, actions, theme)`` -> ``bytes``
- ``render_react_project(project, components_by_id, pages, data_sources, actions, theme)`` -> ``bytes``
- ``tree_to_jsx(component, depth=0)`` -> ``str``  (recursive component → JSX)
- ``tokens_to_css_vars(theme)`` -> ``str``  (design tokens → CSS variables)
- ``generate_data_fetcher(data_source)`` -> ``str``  (data source → fetcher code)
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from typing import Any

log = logging.getLogger("zeni.studio.renderer")

# Tailwind utility classes that ship out-of-the-box. We don't tree-shake — let
# the consumer's own Tailwind config purge unused rules at build time.
_DEFAULT_PROPS_FOR_TAG: dict[str, dict[str, str]] = {
    "input":    {"className": "border rounded px-3 py-2"},
    "textarea": {"className": "border rounded px-3 py-2"},
    "button":   {"className": "px-4 py-2 rounded"},
}


# ════════════════════════════════════════════════════════════════════════════
# Helpers — string sanitation
# ════════════════════════════════════════════════════════════════════════════
def _safe_jsx_text(text: str) -> str:
    """Escape `{`, `}`, and `<` so user-supplied text never breaks JSX."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
        .replace("<", "&lt;")
    )


def _safe_attr(value: Any) -> str:
    """Render a JSX attribute value. Numbers/bools become {expr}, strings JSON-encoded."""
    if isinstance(value, bool):
        return "{true}" if value else "{false}"
    if isinstance(value, (int, float)):
        return f"{{{value}}}"
    if isinstance(value, (list, dict)):
        return "{" + json.dumps(value, ensure_ascii=False) + "}"
    return json.dumps(str(value), ensure_ascii=False)


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name or "untitled").strip("-").lower()
    return s or "untitled"


def _component_function_name(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", " ", name or "Page").title().replace(" ", "")
    if not s:
        s = "Page"
    if not s[0].isalpha():
        s = "P" + s
    return s


# ════════════════════════════════════════════════════════════════════════════
# Component → JSX (recursive)
# ════════════════════════════════════════════════════════════════════════════
# Map Studio component types to native HTML tags. Anything not in this map
# becomes a `<div data-zeni-type=...>` so the renderer never explodes on
# unknown types.
_TAG_MAP: dict[str, str] = {
    "container": "div",
    "section":   "section",
    "main":      "main",
    "layout":    "div",
    "navbar":    "nav",
    "sidebar":   "aside",
    "footer":    "footer",
    "heading":   "h2",
    "text":      "p",
    "paragraph": "p",
    "button":    "button",
    "link":      "a",
    "image":     "img",
    "input":     "input",
    "textarea":  "textarea",
    "form":      "form",
    "grid":      "div",
    "card":      "div",
    "table":     "div",
    "chart":     "div",
    "stat_card": "div",
    "post_list": "div",
    "post_detail": "article",
    "product_grid": "div",
}


def _heading_tag(props: dict[str, Any]) -> str:
    level = props.get("level")
    if isinstance(level, int) and 1 <= level <= 6:
        return f"h{level}"
    return "h2"


def _attrs_for(component: dict[str, Any]) -> str:
    """Render JSX attributes for a component dict."""
    props: dict[str, Any] = dict(component.get("props") or {})
    style: dict[str, Any] = dict(component.get("style") or {})
    events: dict[str, Any] = dict(component.get("events") or {})
    ctype = component.get("type", "")
    cname = component.get("name")

    # Inject default classes for known input-likes
    defaults = _DEFAULT_PROPS_FOR_TAG.get(ctype, {})
    for k, v in defaults.items():
        if k == "className":
            existing = style.get("className", "")
            style["className"] = (v + " " + existing).strip() if existing else v

    parts: list[str] = []
    # className
    if "className" in style:
        parts.append(f"className={_safe_attr(style['className'])}")
    # data-zeni-name for the editor / inspector
    if cname:
        parts.append(f"data-zeni-name={_safe_attr(cname)}")
    # type marker (helps debug)
    parts.append(f"data-zeni-type={_safe_attr(ctype)}")

    # Map common props to HTML attrs
    pass_through = (
        "id", "name", "type", "value", "placeholder", "required",
        "href", "src", "alt", "rows", "cols", "min", "max", "step",
        "checked", "disabled", "readOnly", "target", "rel",
    )
    for key in pass_through:
        if key in props and props[key] is not None:
            parts.append(f"{key}={_safe_attr(props[key])}")

    # Inline style object
    inline_style = {k: v for k, v in style.items() if k != "className"}
    if inline_style:
        parts.append("style={" + json.dumps(inline_style, ensure_ascii=False) + "}")

    # Events: { onClick: "action_name" } -> onClick={() => actions.action_name(event)}
    for evt, action_name in events.items():
        if not action_name:
            continue
        slug = _slugify(str(action_name)).replace("-", "_")
        parts.append(f"{evt}={{(event) => actions.{slug}?.(event)}}")

    return (" " + " ".join(parts)) if parts else ""


def tree_to_jsx(component: dict[str, Any], depth: int = 0) -> str:
    """Recursive renderer. Returns a JSX string snippet."""
    indent = "  " * depth
    if not isinstance(component, dict):
        return f"{indent}{_safe_jsx_text(component)}"

    ctype = component.get("type", "div")
    props = dict(component.get("props") or {})
    children = component.get("children") or []

    # Resolve the tag
    tag = _TAG_MAP.get(ctype, "div")
    if ctype == "heading":
        tag = _heading_tag(props)

    # Self-closing tags (no children, no text)
    void_tags = {"img", "input", "br", "hr"}
    attrs = _attrs_for(component)

    # Text-bearing leaf: text/heading/button/link/paragraph render their `text`/`label`
    text_value: str | None = None
    if ctype in ("text", "paragraph", "heading"):
        text_value = props.get("text") or props.get("body") or ""
    elif ctype == "button":
        text_value = props.get("label") or "Button"
    elif ctype == "link":
        text_value = props.get("label") or props.get("text") or ""

    # Card stat aggregate
    if ctype == "card":
        title = _safe_jsx_text(props.get("title", ""))
        body = _safe_jsx_text(props.get("body", ""))
        return (
            f"{indent}<div{attrs}>\n"
            f"{indent}  <h3 className=\"text-lg font-semibold mb-2\">{title}</h3>\n"
            f"{indent}  <p className=\"text-gray-600\">{body}</p>\n"
            f"{indent}</div>"
        )
    if ctype == "stat_card":
        label = _safe_jsx_text(props.get("label", ""))
        value = _safe_jsx_text(props.get("value", ""))
        return (
            f"{indent}<div{attrs}>\n"
            f"{indent}  <div className=\"text-sm text-gray-500\">{label}</div>\n"
            f"{indent}  <div className=\"text-2xl font-bold mt-1\">{value}</div>\n"
            f"{indent}</div>"
        )

    if tag in void_tags or (not children and text_value is None):
        if tag in void_tags:
            return f"{indent}<{tag}{attrs} />"
        # Empty container
        return f"{indent}<{tag}{attrs}></{tag}>"

    # Render children (or text)
    inner_lines: list[str] = []
    if text_value is not None:
        inner_lines.append(f"{indent}  {_safe_jsx_text(text_value)}")
    for child in children:
        inner_lines.append(tree_to_jsx(child, depth + 1))

    inner = "\n".join(inner_lines)
    return f"{indent}<{tag}{attrs}>\n{inner}\n{indent}</{tag}>"


# ════════════════════════════════════════════════════════════════════════════
# Theme tokens → CSS variables
# ════════════════════════════════════════════════════════════════════════════
def tokens_to_css_vars(theme: dict[str, Any] | None) -> str:
    """Flatten ``{colors:{primary:'#x'}, fonts:{}, spacing:{}}`` into CSS vars."""
    theme = theme or {}
    lines: list[str] = [":root {"]

    def emit(prefix: str, items: dict[str, Any]) -> None:
        for k, v in items.items():
            if isinstance(v, dict):
                emit(f"{prefix}-{k}", v)
            else:
                key = re.sub(r"[^a-zA-Z0-9]+", "-", str(k)).strip("-").lower()
                lines.append(f"  --zeni-{prefix}-{key}: {v};")

    for group, key in (("colors", "color"), ("fonts", "font"),
                       ("spacing", "space"), ("radius", "radius"),
                       ("shadows", "shadow")):
        items = theme.get(group)
        if isinstance(items, dict):
            emit(key, items)

    lines.append("}")
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
# Data source → fetcher code
# ════════════════════════════════════════════════════════════════════════════
def generate_data_fetcher(data_source: dict[str, Any]) -> str:
    """Generate a JS fetcher function source for a data source row."""
    name = _slugify(data_source.get("name", "data")).replace("-", "_")
    dtype = data_source.get("type", "static")
    config = data_source.get("config") or {}

    if dtype == "static":
        body = json.dumps(config.get("data", []), ensure_ascii=False, indent=2)
        return (
            f"export async function fetch_{name}() {{\n"
            f"  return {body};\n"
            f"}}\n"
        )

    if dtype == "api":
        url = json.dumps(config.get("url") or "/", ensure_ascii=False)
        method = json.dumps((config.get("method") or "GET").upper(), ensure_ascii=False)
        headers = json.dumps(config.get("headers") or {}, ensure_ascii=False)
        return (
            f"export async function fetch_{name}(params) {{\n"
            f"  const url = new URL({url}, typeof window === 'undefined' ? 'http://localhost' : window.location.origin);\n"
            f"  if (params) Object.entries(params).forEach(([k,v]) => url.searchParams.set(k, String(v)));\n"
            f"  const res = await fetch(url.toString(), {{ method: {method}, headers: {headers} }});\n"
            f"  if (!res.ok) throw new Error('Fetch failed: ' + res.status);\n"
            f"  return res.json();\n"
            f"}}\n"
        )

    if dtype == "sql":
        # Routed through a Zeni Cloud /data endpoint; never expose raw SQL client-side.
        query = json.dumps(config.get("query") or "SELECT 1", ensure_ascii=False)
        return (
            f"export async function fetch_{name}(params) {{\n"
            f"  const res = await fetch('/api/zeni/data/sql', {{\n"
            f"    method: 'POST',\n"
            f"    headers: {{ 'Content-Type': 'application/json' }},\n"
            f"    body: JSON.stringify({{ query: {query}, params }}),\n"
            f"  }});\n"
            f"  if (!res.ok) throw new Error('SQL failed: ' + res.status);\n"
            f"  return res.json();\n"
            f"}}\n"
        )

    if dtype == "zeni-router":
        task = json.dumps(config.get("task") or "qa_simple", ensure_ascii=False)
        return (
            f"export async function fetch_{name}(payload) {{\n"
            f"  const res = await fetch('/api/v1/router/complete', {{\n"
            f"    method: 'POST',\n"
            f"    headers: {{ 'Content-Type': 'application/json' }},\n"
            f"    body: JSON.stringify({{ task_type: {task}, ...(payload || {{}}) }}),\n"
            f"  }});\n"
            f"  if (!res.ok) throw new Error('Router failed: ' + res.status);\n"
            f"  return res.json();\n"
            f"}}\n"
        )

    # Unknown / future types — fall back to a stub
    return (
        f"export async function fetch_{name}() {{\n"
        f"  return {{ todo: 'unsupported data source type: {dtype}' }};\n"
        f"}}\n"
    )


def _render_data_module(data_sources: list[dict[str, Any]]) -> str:
    parts: list[str] = ["// Auto-generated data sources — do not edit by hand.", ""]
    for ds in data_sources or []:
        parts.append(generate_data_fetcher(ds))
        parts.append("")
    parts.append("export const dataSources = {")
    for ds in data_sources or []:
        slug = _slugify(ds.get("name", "data")).replace("-", "_")
        parts.append(f"  {slug}: fetch_{slug},")
    parts.append("};")
    parts.append("")
    return "\n".join(parts)


def _render_actions_module(actions: list[dict[str, Any]]) -> str:
    parts: list[str] = [
        "// Auto-generated event handlers.",
        "// Each handler receives the original DOM event.",
        "",
        "import { dataSources } from './data';",
        "",
        "export const actions = {",
    ]
    for act in actions or []:
        slug = _slugify(act.get("name", "action")).replace("-", "_")
        atype = act.get("type", "js")
        if atype == "js":
            code = act.get("code") or "// no-op"
            parts.append(f"  {slug}: async (event) => {{")
            parts.append("    " + code.replace("\n", "\n    "))
            parts.append("  },")
        elif atype == "api":
            params = act.get("params") or {}
            url = json.dumps(params.get("url") or "/", ensure_ascii=False)
            method = json.dumps((params.get("method") or "POST").upper(), ensure_ascii=False)
            parts.append(f"  {slug}: async (event) => {{")
            parts.append(f"    const form = event?.target ? new FormData(event.target) : null;")
            parts.append(f"    const body = form ? Object.fromEntries(form.entries()) : {{}};")
            parts.append(f"    const res = await fetch({url}, {{ method: {method}, headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(body) }});")
            parts.append(f"    return res.json();")
            parts.append("  },")
        elif atype == "workflow":
            wid = json.dumps(act.get("params", {}).get("workflow_id") or "", ensure_ascii=False)
            parts.append(f"  {slug}: async (event) => {{")
            parts.append(f"    const res = await fetch('/api/v1/automation/workflows/' + {wid} + '/run', {{ method: 'POST' }});")
            parts.append(f"    return res.json();")
            parts.append("  },")
        else:
            parts.append(f"  {slug}: async () => {{ /* unknown action type: {atype} */ }},")
    parts.append("};")
    parts.append("")
    return "\n".join(parts)


# ════════════════════════════════════════════════════════════════════════════
# Page assembly — produce a single page module
# ════════════════════════════════════════════════════════════════════════════
def _render_page_module_next(page: dict[str, Any], tree: dict[str, Any]) -> tuple[str, str]:
    """Return (path, source) for a Next.js app-router page."""
    raw_path = page.get("path", "/")
    slug_path = "app" + (raw_path if raw_path.startswith("/") else "/" + raw_path)
    if slug_path == "app/":
        out_path = "app/page.tsx"
    else:
        out_path = slug_path.rstrip("/") + "/page.tsx"
    title = page.get("title") or "Page"
    fn_name = _component_function_name(title) + "Page"
    jsx = tree_to_jsx(tree or {}, depth=2)

    src = (
        "import { actions } from '@/lib/actions';\n"
        "import { dataSources } from '@/lib/data';\n"
        "\n"
        f"export const metadata = {{ title: {json.dumps(title, ensure_ascii=False)} }};\n"
        "\n"
        f"export default function {fn_name}() {{\n"
        "  return (\n"
        f"{jsx}\n"
        "  );\n"
        "}\n"
    )
    return out_path, src


def _render_page_module_react(page: dict[str, Any], tree: dict[str, Any]) -> tuple[str, str]:
    raw_path = page.get("path", "/")
    slug = _slugify(raw_path) if raw_path != "/" else "home"
    out_path = f"src/pages/{slug}.jsx"
    title = page.get("title") or "Page"
    fn_name = _component_function_name(title) + "Page"
    jsx = tree_to_jsx(tree or {}, depth=2)

    src = (
        "import { actions } from '../lib/actions';\n"
        "import { dataSources } from '../lib/data';\n"
        "\n"
        f"export default function {fn_name}() {{\n"
        "  return (\n"
        f"{jsx}\n"
        "  );\n"
        "}\n"
    )
    return out_path, src


# ════════════════════════════════════════════════════════════════════════════
# Project assembly — produce a zip
# ════════════════════════════════════════════════════════════════════════════
def _common_assets(project: dict[str, Any], theme: dict[str, Any] | None) -> dict[str, str]:
    """Files shared across both Next + React renderers."""
    return {
        "README.md": (
            f"# {project.get('name', 'Zeni Studio Project')}\n\n"
            f"{project.get('description') or 'Generated by Zeni Studio.'}\n\n"
            f"## Getting started\n\n"
            f"```\nnpm install\nnpm run dev\n```\n"
        ),
        ".gitignore": "node_modules/\n.next/\ndist/\n.env\n.env.*\n",
        "tailwind.config.js": (
            "module.exports = {\n"
            "  content: ['./app/**/*.{ts,tsx,js,jsx}', './src/**/*.{ts,tsx,js,jsx}'],\n"
            "  theme: { extend: {} },\n"
            "  plugins: [],\n"
            "};\n"
        ),
        "postcss.config.js": (
            "module.exports = { plugins: { tailwindcss: {}, autoprefixer: {} } };\n"
        ),
        "styles/globals.css": (
            "@tailwind base;\n@tailwind components;\n@tailwind utilities;\n\n"
            f"{tokens_to_css_vars(theme)}\n"
        ),
    }


def render_next_project(
    project: dict[str, Any],
    *,
    pages: list[dict[str, Any]] | None = None,
    data_sources: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    theme: dict[str, Any] | None = None,
) -> bytes:
    """Render a project as a Next.js 14 (app router) zip."""
    pages = pages or []
    data_sources = data_sources or []
    actions = actions or []
    name = project.get("name", "zeni-app")
    slug = _slugify(name)

    files: dict[str, str] = {
        "package.json": json.dumps({
            "name": slug,
            "version": "0.1.0",
            "private": True,
            "scripts": {
                "dev": "next dev",
                "build": "next build",
                "start": "next start",
                "lint": "next lint",
            },
            "dependencies": {
                "next": "^14.0.0",
                "react": "^18.2.0",
                "react-dom": "^18.2.0",
            },
            "devDependencies": {
                "tailwindcss": "^3.4.0",
                "postcss": "^8.4.0",
                "autoprefixer": "^10.4.0",
                "typescript": "^5.0.0",
            },
        }, indent=2),
        "tsconfig.json": json.dumps({
            "compilerOptions": {
                "target": "ES2020", "lib": ["dom", "dom.iterable", "esnext"],
                "allowJs": True, "skipLibCheck": True, "strict": False,
                "forceConsistentCasingInFileNames": True, "noEmit": True,
                "esModuleInterop": True, "module": "esnext",
                "moduleResolution": "bundler", "resolveJsonModule": True,
                "isolatedModules": True, "jsx": "preserve",
                "incremental": True, "baseUrl": ".",
                "paths": {"@/*": ["./*"]},
            },
            "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx"],
            "exclude": ["node_modules"],
        }, indent=2),
        "next.config.js": "module.exports = { reactStrictMode: true };\n",
        "lib/data.ts": _render_data_module(data_sources),
        "lib/actions.ts": _render_actions_module(actions),
        "app/layout.tsx": (
            "import '../styles/globals.css';\n\n"
            f"export const metadata = {{ title: {json.dumps(name, ensure_ascii=False)} }};\n"
            "\n"
            "export default function RootLayout({ children }: { children: React.ReactNode }) {\n"
            "  return (<html lang=\"vi\"><body>{children}</body></html>);\n"
            "}\n"
        ),
    }
    files.update(_common_assets(project, theme))

    # Pages — fall back to rendering the canvas tree as the home page if the
    # project has no explicit pages yet.
    if not pages:
        canvas = project.get("canvas_tree") or {}
        out_path, src = _render_page_module_next({"path": "/", "title": name}, canvas)
        files[out_path] = src
    else:
        for page in pages:
            tree = page.get("tree") or {}
            out_path, src = _render_page_module_next(page, tree)
            files[out_path] = src

    return _zip_files(files)


def render_react_project(
    project: dict[str, Any],
    *,
    pages: list[dict[str, Any]] | None = None,
    data_sources: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    theme: dict[str, Any] | None = None,
) -> bytes:
    """Render a project as a Vite + React zip."""
    pages = pages or []
    data_sources = data_sources or []
    actions = actions or []
    name = project.get("name", "zeni-app")
    slug = _slugify(name)

    files: dict[str, str] = {
        "package.json": json.dumps({
            "name": slug,
            "version": "0.1.0",
            "private": True,
            "scripts": {
                "dev": "vite",
                "build": "vite build",
                "preview": "vite preview",
            },
            "dependencies": {
                "react": "^18.2.0",
                "react-dom": "^18.2.0",
                "react-router-dom": "^6.20.0",
            },
            "devDependencies": {
                "@vitejs/plugin-react": "^4.0.0",
                "vite": "^5.0.0",
                "tailwindcss": "^3.4.0",
                "postcss": "^8.4.0",
                "autoprefixer": "^10.4.0",
            },
        }, indent=2),
        "vite.config.js": (
            "import { defineConfig } from 'vite';\n"
            "import react from '@vitejs/plugin-react';\n"
            "export default defineConfig({ plugins: [react()] });\n"
        ),
        "index.html": (
            "<!DOCTYPE html><html lang=\"vi\"><head>"
            f"<meta charset=\"utf-8\"><title>{name}</title></head>"
            "<body><div id=\"root\"></div>"
            "<script type=\"module\" src=\"/src/main.jsx\"></script>"
            "</body></html>"
        ),
        "src/main.jsx": (
            "import React from 'react';\n"
            "import ReactDOM from 'react-dom/client';\n"
            "import { BrowserRouter, Routes, Route, Link } from 'react-router-dom';\n"
            "import '../styles/globals.css';\n"
            "import App from './App.jsx';\n\n"
            "ReactDOM.createRoot(document.getElementById('root')).render(<BrowserRouter><App /></BrowserRouter>);\n"
        ),
        "src/lib/data.js": _render_data_module(data_sources),
        "src/lib/actions.js": _render_actions_module(actions),
    }
    files.update(_common_assets(project, theme))

    # Page modules + App.jsx router
    page_routes: list[tuple[str, str, str]] = []
    if not pages:
        canvas = project.get("canvas_tree") or {}
        out_path, src = _render_page_module_react({"path": "/", "title": name}, canvas)
        files[out_path] = src
        page_routes.append(("/", _component_function_name(name) + "Page", out_path))
    else:
        for page in pages:
            tree = page.get("tree") or {}
            out_path, src = _render_page_module_react(page, tree)
            files[out_path] = src
            fn = _component_function_name(page.get("title") or "Page") + "Page"
            page_routes.append((page.get("path", "/"), fn, out_path))

    # Build App.jsx router
    imports = "\n".join(
        f"import {fn} from './{p.replace('src/', '')[:-4]}.jsx';"
        for path, fn, p in page_routes
    )
    routes = "\n".join(
        f"        <Route path=\"{path}\" element={{<{fn} />}} />"
        for path, fn, _ in page_routes
    )
    files["src/App.jsx"] = (
        "import React from 'react';\n"
        "import { Routes, Route } from 'react-router-dom';\n"
        f"{imports}\n\n"
        "export default function App() {\n"
        "  return (\n"
        "    <Routes>\n"
        f"{routes}\n"
        "    </Routes>\n"
        "  );\n"
        "}\n"
    )

    return _zip_files(files)


def _zip_files(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(path, content)
    return buf.getvalue()
