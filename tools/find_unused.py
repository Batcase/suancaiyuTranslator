"""开发用：扫描本工程里定义了但从未被引用的函数/类。

用法: python tools\find_unused.py
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 这些是"对外 API / 工具入口"，只被外部调用很正常，不参与检查
EXEMPT = {
    "main",
    "parse_args",
    "apply_overrides",
    "validate",
    "run_headless",
    "_selftest",
    "probe_backends",
    "probe_backends_",
    "from_string",
    "__init__",
    "__enter__",
    "__exit__",
    "read",
    "run",
    "chunks",
    "close",
    "open",
    "send_audio",
    "aclose",
    "translate",
    "submit",
    "write",
    "describe",
    "load_config",
    "create_source",
    "create_asr",
    "create_translator",
    "found",
    "main_",
}


def main() -> int:
    total = 0
    files = [p for p in sorted(ROOT.rglob("*.py")) if "__pycache__" not in str(p)]
    all_src = {p: p.read_text(encoding="utf-8") for p in files}
    for path, src in all_src.items():
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            print(f"[语法错误] {path}: {exc}")
            continue
        defined = {
            node.name: node.lineno
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for name, line in defined.items():
            if name.startswith("__") or name in EXEMPT:
                continue
            # 统计整个工程里该标识符的出现次数（排除定义那一行）
            hits = 0
            pattern = re.compile(rf"\b{re.escape(name)}\b")
            for other_path, other_src in all_src.items():
                if other_path == path:
                    for i, text in enumerate(other_src.splitlines(), start=1):
                        if i == line:
                            continue
                        hits += len(pattern.findall(text))
                else:
                    hits += len(pattern.findall(other_src))
            if hits == 0:
                print(f"  未引用: {path.relative_to(ROOT)}:{line}  {name}")
                total += 1
    print(f"\n共发现 {total} 个未被引用的定义")
    return 0


if __name__ == "__main__":
    sys.exit(main())
