"""Unit tests cho framework_detector — Phase 1 P1.2."""
from app.services.framework_detector import detect_framework, DEFAULT_PRESET


def test_docker_highest_priority():
    """Dockerfile present → docker framework (overrides everything else)."""
    result = detect_framework(
        ["Dockerfile", "package.json", "requirements.txt"],
        package_json={"dependencies": {"next": "14.0.0"}},
    )
    assert result["framework"] == "docker"
    assert result["confidence"] == 1.0
    assert result["runtime"] == "docker"


def test_nextjs_detected_by_config_file():
    result = detect_framework(
        ["package.json", "next.config.js", "src/pages/index.tsx"],
        package_json={"dependencies": {"next": "14.0.0", "react": "18.0.0"}},
    )
    assert result["framework"] == "nextjs"
    assert result["build_command"] == "npm run build"
    assert result["output_dir"] == ".next"
    assert result["port"] == 3000
    assert result["confidence"] >= 0.95


def test_nextjs_detected_by_dependency():
    """Next without config file but with `next` in deps still detected."""
    result = detect_framework(
        ["package.json"],
        package_json={"dependencies": {"next": "14.0.0"}},
    )
    assert result["framework"] == "nextjs"


def test_nuxt():
    result = detect_framework(
        ["package.json", "nuxt.config.ts"],
        package_json={"dependencies": {"nuxt": "3.5.0"}},
    )
    assert result["framework"] == "nuxt"
    assert result["output_dir"] == ".output"


def test_sveltekit():
    result = detect_framework(
        ["package.json", "svelte.config.js"],
        package_json={"devDependencies": {"@sveltejs/kit": "1.0.0"}},
    )
    assert result["framework"] == "sveltekit"


def test_astro():
    result = detect_framework(
        ["package.json", "astro.config.mjs"],
        package_json={"dependencies": {"astro": "3.0.0"}},
    )
    assert result["framework"] == "astro"


def test_vite():
    result = detect_framework(
        ["package.json", "vite.config.ts"],
        package_json={"devDependencies": {"vite": "5.0.0"}},
    )
    assert result["framework"] == "vite"


def test_cra():
    result = detect_framework(
        ["package.json"],
        package_json={"dependencies": {"react-scripts": "5.0.0"}},
    )
    assert result["framework"] == "cra"


def test_express():
    result = detect_framework(
        ["package.json", "server.js"],
        package_json={"dependencies": {"express": "4.18.0"}},
    )
    assert result["framework"] == "express"
    assert result["port"] == 3000


def test_hono():
    result = detect_framework(
        ["package.json"],
        package_json={"dependencies": {"hono": "3.0.0"}},
    )
    assert result["framework"] == "hono"


def test_nestjs():
    result = detect_framework(
        ["package.json"],
        package_json={"dependencies": {"@nestjs/core": "10.0.0"}},
    )
    assert result["framework"] == "nestjs"
    assert result["output_dir"] == "dist"


def test_fastapi_via_requirements():
    result = detect_framework(
        ["requirements.txt", "main.py"],
        requirements_txt=["fastapi==0.100.0", "uvicorn==0.23.0"],
    )
    assert result["framework"] == "fastapi"
    assert result["port"] == 8000
    assert result["runtime"] == "python3.12"


def test_flask():
    result = detect_framework(
        ["requirements.txt", "app.py"],
        requirements_txt=["flask==3.0.0"],
    )
    assert result["framework"] == "flask"


def test_django_via_manage_py():
    result = detect_framework(
        ["manage.py", "requirements.txt", "myapp/settings.py"],
        requirements_txt=["Django==4.2.0"],
    )
    assert result["framework"] == "django"
    assert "collectstatic" in (result["build_command"] or "")


def test_streamlit():
    result = detect_framework(
        ["requirements.txt", "app.py"],
        requirements_txt=["streamlit==1.28.0"],
    )
    assert result["framework"] == "streamlit"
    assert result["port"] == 8501


def test_go_module():
    result = detect_framework(["go.mod", "main.go"])
    assert result["framework"] == "go"
    assert result["build_command"] == "go build -o app ."
    assert result["runtime"] == "go1.22"


def test_rust():
    result = detect_framework(["Cargo.toml", "src/main.rs"])
    assert result["framework"] == "rust"
    assert "cargo build" in (result["build_command"] or "")


def test_static_html():
    result = detect_framework(["index.html", "style.css"])
    assert result["framework"] == "static"
    assert result["runtime"] == "nginx"


def test_unknown_fallback():
    result = detect_framework(["README.md", "LICENSE"])
    assert result["framework"] == "unknown"
    assert result["confidence"] == 0.0
    # Hints should mention manual config needed
    assert any("thủ công" in h for h in result["hints"])


def test_generic_python_when_no_specific_framework():
    """requirements.txt without recognized framework → generic python."""
    result = detect_framework(
        ["requirements.txt", "script.py"],
        requirements_txt=["numpy==1.24", "pandas==2.0"],
    )
    assert result["framework"] == "python"


def test_generic_node_when_no_specific_framework():
    """package.json without recognized framework → generic node."""
    result = detect_framework(
        ["package.json"],
        package_json={"dependencies": {"lodash": "^4.17.0"}, "scripts": {"build": "tsc"}},
    )
    assert result["framework"] == "node"
    assert result["build_command"] == "npm run build"


def test_priority_order_dockerfile_beats_node():
    """Dockerfile should always take priority over framework dependencies."""
    result = detect_framework(
        ["Dockerfile", "package.json", "next.config.js"],
        package_json={"dependencies": {"next": "14"}},
    )
    assert result["framework"] == "docker"


def test_default_preset_immutability():
    """DEFAULT_PRESET should not be mutated when returned."""
    r1 = detect_framework(["foo.txt"])
    r1["framework"] = "MUTATED"
    r2 = detect_framework(["bar.txt"])
    assert r2["framework"] == "unknown"  # not mutated


def test_returns_required_keys():
    """All returns must have core keys for consumers."""
    cases = [
        (["Dockerfile"], None, None),
        (["package.json", "next.config.js"], {"dependencies": {"next": "14"}}, None),
        (["requirements.txt"], None, ["fastapi"]),
        (["foo.txt"], None, None),
    ]
    for files, pkg, reqs in cases:
        r = detect_framework(files, package_json=pkg, requirements_txt=reqs)
        for k in ("framework", "build_command", "install_command", "output_dir",
                  "port", "runtime", "confidence", "hints"):
            assert k in r, f"Missing key {k} for files={files}"
