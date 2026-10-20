"""Generate web CSS variables and the Android Compose theme from design/tokens.json.

    python design/gen_tokens.py

Outputs:
  server/paddock/web/static/tokens.css
  android/app/src/main/java/app/paddock/ui/theme/Tokens.kt
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = json.loads((ROOT / "design" / "tokens.json").read_text())


def kebab(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z0-9])", "-", s).lower()


def css_color(hex_: str) -> str:
    h = hex_.lstrip("#")
    if len(h) == 8:  # RRGGBBAA → rgba()
        r, g, b, a = (int(h[i:i + 2], 16) for i in (0, 2, 4, 6))
        return f"rgba({r}, {g}, {b}, {round(a / 255, 3)})"
    return f"#{h.lower()}"


def kt_color(hex_: str) -> str:
    h = hex_.lstrip("#").upper()
    if len(h) == 8:
        h = h[6:] + h[:6]  # RRGGBBAA → AARRGGBB
    else:
        h = "FF" + h
    return f"Color(0x{h})"


def css() -> str:
    def block(theme: str) -> str:
        return "\n".join(f"  --{kebab(k)}: {css_color(v)};" for k, v in T["color"][theme].items())

    m = T["motion"]
    lines = ["/* Generated from design/tokens.json — do not edit by hand. */", ":root {", block("dark"),
             *(f"  --radius-{k}: {v}px;" for k, v in T["radius"].items()),
             *(f"  --space-{k}: {v}px;" for k, v in T["space"].items()),
             f"  --dur-fast: {m['fast']}ms;", f"  --dur-base: {m['base']}ms;", f"  --dur-slow: {m['slow']}ms;", f"  --dur-page: {m['page']}ms;",
             f"  --ease-standard: cubic-bezier({', '.join(map(str, m['standard']))});",
             f"  --ease-decelerate: cubic-bezier({', '.join(map(str, m['decelerate']))});",
             f"  --ease-accelerate: cubic-bezier({', '.join(map(str, m['accelerate']))});",
             "  color-scheme: dark;", "}",
             "@media (prefers-color-scheme: light) {", "  :root:not([data-theme=\"dark\"]) {",
             "\n".join("  " + l for l in block("light").splitlines()), "    color-scheme: light;", "  }", "}",
             ":root[data-theme=\"light\"] {", block("light"), "  color-scheme: light;", "}", ""]
    return "\n".join(lines)


def kotlin() -> str:
    def scheme(theme: str) -> str:
        return "\n".join(f"        val {k} = {kt_color(v)}" for k, v in T["color"][theme].items())

    m = T["motion"]
    out = [
        "// Generated from design/tokens.json by design/gen_tokens.py — do not edit by hand.",
        "package app.paddock.ui.theme", "",
        "import androidx.compose.animation.core.CubicBezierEasing",
        "import androidx.compose.ui.graphics.Color",
        "import androidx.compose.ui.unit.dp",
        "import androidx.compose.ui.unit.sp", "",
        "object Tokens {",
        "    object Dark {", scheme("dark"), "    }",
        "    object Light {", scheme("light"), "    }",
        "    object Radius {", *(f"        val {k} = {v}.dp" for k, v in T["radius"].items()), "    }",
        "    object Space {", *(f"        val {k} = {v}.dp" for k, v in T["space"].items()), "    }",
        "    object Type {",
        *(f"        val {k}Size = {v['size']}.sp\n        val {k}Line = {v['line']}.sp\n        const val {k}Weight = {v['weight']}"
          for k, v in T["type"].items()),
        "    }",
        "    object Motion {",
        f"        const val fast = {m['fast']}", f"        const val base = {m['base']}", f"        const val slow = {m['slow']}",
        f"        const val page = {m['page']}",
        f"        val standard = CubicBezierEasing({', '.join(f'{x}f' for x in m['standard'])})",
        f"        val decelerate = CubicBezierEasing({', '.join(f'{x}f' for x in m['decelerate'])})",
        f"        val accelerate = CubicBezierEasing({', '.join(f'{x}f' for x in m['accelerate'])})",
        "    }",
        "}", "",
    ]
    return "\n".join(out)


if __name__ == "__main__":
    css_out = ROOT / "server" / "paddock" / "web" / "static" / "tokens.css"
    kt_out = ROOT / "android" / "app" / "src" / "main" / "java" / "app" / "paddock" / "ui" / "theme" / "Tokens.kt"
    css_out.write_text(css(), encoding="utf-8")
    kt_out.parent.mkdir(parents=True, exist_ok=True)
    kt_out.write_text(kotlin(), encoding="utf-8")
    print(f"wrote {css_out.relative_to(ROOT)} and {kt_out.relative_to(ROOT)}")
