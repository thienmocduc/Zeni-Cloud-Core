"""
Framework Auto-Detector (Phase 1 P1.2 — chairman approved 2026-05-11)

Pattern lấy cảm hứng:
  - Vercel: tự detect framework qua package.json → preset build/output
  - Railway: scan repo → guess service type (Node, Python, Go, Docker)
  - Cloud Run --source: Buildpacks fallback nếu không có Dockerfile

Mục tiêu: customer không cần điền build_command/output_dir/port — Zeni tự
detect đúng cho 15+ framework phổ biến.

Input: danh sách file ở root repo + (optional) package.json/requirements.txt content
Output: {
  "framework":      str (e.g., "nextjs", "fastapi", "go", ...),
  "build_command":  str | None,
  "install_command":str | None,
  "output_dir":     str | None,
  "port":           int,
  "runtime":        str (e.g., "node20", "python3.12", "go1.22"),
  "confidence":     float (0..1),
  "hints":          list[str],
}

KHÔNG đụng code cũ — đây là file mới hoàn toàn. Caller chỉ cần import:
    from app.services.framework_detector import detect_framework
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

log = logging.getLogger("zeni.framework_detector")


# ─── Default deploy preset (fallback nếu không detect được) ──────────
DEFAULT_PRESET = {
    "framework": "unknown",
    "build_command": None,
    "install_command": None,
    "output_dir": None,
    "port": 8080,
    "runtime": "node20",  # safest default
    "confidence": 0.0,
    "hints": ["Không detect được framework. Customer cần điền build_command + port thủ công."],
}


# ─── Framework detection rules (priority order) ───────────────────────
# Mỗi rule: condition function → preset dict
# Detect in order; first match wins. Higher priority rules at top.
def _has_file(files: set[str], *names: str) -> bool:
    return any(n in files for n in names)


def _pkg_deps(package_json: dict | None, dep_name: str) -> bool:
    """Check if dep_name in dependencies or devDependencies."""
    if not package_json:
        return False
    for k in ("dependencies", "devDependencies"):
        if dep_name in (package_json.get(k) or {}):
            return True
    return False


def _req_deps(requirements: list[str], dep_name: str) -> bool:
    """Check if dep_name in requirements.txt entries."""
    dn = dep_name.lower()
    return any(dn in line.lower().split("=")[0].split(">")[0].split("<")[0].strip()
               for line in requirements)


# ─── PUBLIC API ──────────────────────────────────────────────────────
def detect_framework(
    files: list[str],
    *,
    package_json: dict | None = None,
    requirements_txt: list[str] | None = None,
    pyproject_toml: dict | None = None,
    go_mod: str | None = None,
    cargo_toml: dict | None = None,
) -> dict[str, Any]:
    """
    Detect framework from repo file listing.

    Args:
      files: list of filenames at repo root (e.g., ["package.json", "Dockerfile"])
      package_json: parsed package.json (optional, for Node frameworks)
      requirements_txt: lines of requirements.txt (optional, for Python)
      pyproject_toml: parsed pyproject.toml (optional)
      go_mod: content of go.mod (optional)
      cargo_toml: parsed Cargo.toml (optional)

    Returns: dict with framework + build config (see module docstring)
    """
    file_set = set(files)
    requirements = requirements_txt or []
    hints: list[str] = []

    # ─────────────────────────────────────────────────────────────
    # PRIORITY 0: Explicit Dockerfile → highest confidence
    # ─────────────────────────────────────────────────────────────
    if _has_file(file_set, "Dockerfile", "dockerfile"):
        return {
            "framework": "docker",
            "build_command": None,  # Docker handles build
            "install_command": None,
            "output_dir": None,
            "port": _guess_port_from_dockerfile(file_set, files) or 8080,
            "runtime": "docker",
            "confidence": 1.0,
            "hints": ["Dockerfile detected — Zeni dùng nguyên Dockerfile, không inject buildpack."],
        }

    # ─────────────────────────────────────────────────────────────
    # PRIORITY 1: Node.js frameworks (most common)
    # ─────────────────────────────────────────────────────────────
    if _has_file(file_set, "package.json"):
        # Next.js
        if _has_file(file_set, "next.config.js", "next.config.mjs", "next.config.ts") \
                or _pkg_deps(package_json, "next"):
            return {
                "framework": "nextjs",
                "build_command": "npm run build",
                "install_command": "npm ci",
                "output_dir": ".next",
                "port": 3000,
                "runtime": "node20",
                "confidence": 0.98,
                "hints": ["Next.js detected. Production: standalone output recommended (next.config: output: 'standalone')."],
            }

        # Nuxt.js
        if _has_file(file_set, "nuxt.config.js", "nuxt.config.ts") \
                or _pkg_deps(package_json, "nuxt"):
            return {
                "framework": "nuxt",
                "build_command": "npm run build",
                "install_command": "npm ci",
                "output_dir": ".output",
                "port": 3000,
                "runtime": "node20",
                "confidence": 0.97,
                "hints": ["Nuxt 3 detected. node-server preset auto-active in production."],
            }

        # SvelteKit
        if _has_file(file_set, "svelte.config.js", "svelte.config.ts") \
                or _pkg_deps(package_json, "@sveltejs/kit"):
            return {
                "framework": "sveltekit",
                "build_command": "npm run build",
                "install_command": "npm ci",
                "output_dir": "build",
                "port": 3000,
                "runtime": "node20",
                "confidence": 0.96,
                "hints": ["SvelteKit detected. Use adapter-node for Cloud Run."],
            }

        # Remix
        if _has_file(file_set, "remix.config.js") or _pkg_deps(package_json, "@remix-run/node"):
            return {
                "framework": "remix",
                "build_command": "npm run build",
                "install_command": "npm ci",
                "output_dir": "build",
                "port": 3000,
                "runtime": "node20",
                "confidence": 0.95,
                "hints": ["Remix detected."],
            }

        # Astro
        if _has_file(file_set, "astro.config.mjs", "astro.config.js", "astro.config.ts") \
                or _pkg_deps(package_json, "astro"):
            return {
                "framework": "astro",
                "build_command": "npm run build",
                "install_command": "npm ci",
                "output_dir": "dist",
                "port": 4321,
                "runtime": "node20",
                "confidence": 0.95,
                "hints": ["Astro detected. SSR mode requires adapter-node."],
            }

        # Vite + React
        if _has_file(file_set, "vite.config.js", "vite.config.ts") \
                or _pkg_deps(package_json, "vite"):
            return {
                "framework": "vite",
                "build_command": "npm run build",
                "install_command": "npm ci",
                "output_dir": "dist",
                "port": 4173,
                "runtime": "node20",
                "confidence": 0.92,
                "hints": ["Vite detected. Static build output — recommend serve via nginx or zeni edge."],
            }

        # Create React App
        if _pkg_deps(package_json, "react-scripts"):
            return {
                "framework": "cra",
                "build_command": "npm run build",
                "install_command": "npm ci",
                "output_dir": "build",
                "port": 80,
                "runtime": "node20",
                "confidence": 0.90,
                "hints": ["Create React App detected. Static build — serve via nginx."],
            }

        # Express / Hono / Fastify / NestJS (generic Node API)
        if _pkg_deps(package_json, "express") or _pkg_deps(package_json, "hono") \
                or _pkg_deps(package_json, "fastify"):
            framework = "express" if _pkg_deps(package_json, "express") \
                       else ("hono" if _pkg_deps(package_json, "hono") else "fastify")
            return {
                "framework": framework,
                "build_command": "npm run build" if (package_json and "build" in (package_json.get("scripts") or {})) else None,
                "install_command": "npm ci",
                "output_dir": None,
                "port": 3000,
                "runtime": "node20",
                "confidence": 0.88,
                "hints": [f"{framework.capitalize()} detected. Default port 3000."],
            }

        if _pkg_deps(package_json, "@nestjs/core"):
            return {
                "framework": "nestjs",
                "build_command": "npm run build",
                "install_command": "npm ci",
                "output_dir": "dist",
                "port": 3000,
                "runtime": "node20",
                "confidence": 0.92,
                "hints": ["NestJS detected."],
            }

        # Generic Node fallback (has package.json but no recognized framework)
        scripts = (package_json or {}).get("scripts") or {}
        return {
            "framework": "node",
            "build_command": "npm run build" if "build" in scripts else None,
            "install_command": "npm ci",
            "output_dir": None,
            "port": 3000,
            "runtime": "node20",
            "confidence": 0.70,
            "hints": ["Generic Node.js project. Detected package.json but no specific framework."],
        }

    # ─────────────────────────────────────────────────────────────
    # PRIORITY 2: Python frameworks
    # ─────────────────────────────────────────────────────────────
    if _has_file(file_set, "requirements.txt") or _has_file(file_set, "pyproject.toml"):
        # FastAPI
        if _req_deps(requirements, "fastapi") or (pyproject_toml and "fastapi" in str(pyproject_toml).lower()):
            return {
                "framework": "fastapi",
                "build_command": None,
                "install_command": "pip install -r requirements.txt",
                "output_dir": None,
                "port": 8000,
                "runtime": "python3.12",
                "confidence": 0.95,
                "hints": ["FastAPI detected. Recommend uvicorn or gunicorn ASGI worker."],
            }

        # Flask
        if _req_deps(requirements, "flask"):
            return {
                "framework": "flask",
                "build_command": None,
                "install_command": "pip install -r requirements.txt",
                "output_dir": None,
                "port": 5000,
                "runtime": "python3.12",
                "confidence": 0.93,
                "hints": ["Flask detected. Use gunicorn for production."],
            }

        # Django
        if _has_file(file_set, "manage.py") or _req_deps(requirements, "django"):
            return {
                "framework": "django",
                "build_command": "python manage.py collectstatic --noinput",
                "install_command": "pip install -r requirements.txt",
                "output_dir": "staticfiles",
                "port": 8000,
                "runtime": "python3.12",
                "confidence": 0.95,
                "hints": ["Django detected. Run migrations + collectstatic on deploy."],
            }

        # Streamlit
        if _req_deps(requirements, "streamlit"):
            return {
                "framework": "streamlit",
                "build_command": None,
                "install_command": "pip install -r requirements.txt",
                "output_dir": None,
                "port": 8501,
                "runtime": "python3.12",
                "confidence": 0.92,
                "hints": ["Streamlit detected. Command: streamlit run app.py --server.port 8501"],
            }

        # Generic Python
        return {
            "framework": "python",
            "build_command": None,
            "install_command": "pip install -r requirements.txt" if "requirements.txt" in file_set else "pip install .",
            "output_dir": None,
            "port": 8000,
            "runtime": "python3.12",
            "confidence": 0.65,
            "hints": ["Generic Python project. Detected requirements/pyproject but no specific framework."],
        }

    # ─────────────────────────────────────────────────────────────
    # PRIORITY 3: Go
    # ─────────────────────────────────────────────────────────────
    if _has_file(file_set, "go.mod"):
        return {
            "framework": "go",
            "build_command": "go build -o app .",
            "install_command": "go mod download",
            "output_dir": None,
            "port": 8080,
            "runtime": "go1.22",
            "confidence": 0.92,
            "hints": ["Go module detected. Build static binary recommended."],
        }

    # ─────────────────────────────────────────────────────────────
    # PRIORITY 4: Rust
    # ─────────────────────────────────────────────────────────────
    if _has_file(file_set, "Cargo.toml"):
        return {
            "framework": "rust",
            "build_command": "cargo build --release",
            "install_command": None,
            "output_dir": "target/release",
            "port": 8080,
            "runtime": "rust1.80",
            "confidence": 0.92,
            "hints": ["Rust project detected. Recommend musl target for smaller image."],
        }

    # ─────────────────────────────────────────────────────────────
    # PRIORITY 5: Static sites (HTML only)
    # ─────────────────────────────────────────────────────────────
    if _has_file(file_set, "index.html") and not _has_file(file_set, "package.json", "requirements.txt"):
        return {
            "framework": "static",
            "build_command": None,
            "install_command": None,
            "output_dir": ".",
            "port": 80,
            "runtime": "nginx",
            "confidence": 0.85,
            "hints": ["Static HTML site. Serve via nginx — no build needed."],
        }

    # ─────────────────────────────────────────────────────────────
    # FALLBACK: unknown
    # ─────────────────────────────────────────────────────────────
    log.info("[framework_detector] no framework matched, returning DEFAULT_PRESET")
    return dict(DEFAULT_PRESET)


def _guess_port_from_dockerfile(file_set: set[str], files: list[str]) -> Optional[int]:
    """If Dockerfile content is available, try to find EXPOSE directive port."""
    # Caller usually doesn't pass dockerfile content yet — placeholder for future
    return None


# ─── Convenience: detect from GitHub API tree response ────────────────
def detect_from_github_tree(tree_response: dict) -> dict[str, Any]:
    """
    Detect framework from GitHub API repos/{owner}/{repo}/git/trees/{branch} response.

    Caller fetches GitHub tree (recursive=0, root only) → passes here.
    """
    files = []
    for entry in tree_response.get("tree", []):
        if entry.get("type") == "blob":
            files.append(entry.get("path", ""))
    return detect_framework(files)
