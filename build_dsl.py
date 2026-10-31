"""Codeノードの正本ロジック（code/*.py）をDSL（YAML）へ注入するビルドスクリプト.

使い方:
    python3 build_dsl.py

code/plan_targets.py と code/build_report.py を読み、line-segment-broadcast.yml の
該当ノードの `code: |` ブロックを丸ごと差し替える。YAMLの他の部分（ノード配線・
パラメータ・環境変数）は一切変更しない。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
DSL_PATH = ROOT / "line-segment-broadcast.yml"
GSHEETS_DSL_PATH = ROOT / "line-segment-broadcast-gsheets.yml"

DSL_FILES = {
    DSL_PATH: {
        "code_token": ROOT / "code" / "extract_token.py",
        "code_plan": ROOT / "code" / "plan_targets.py",
        "code_report": ROOT / "code" / "build_report.py",
    },
    GSHEETS_DSL_PATH: {
        "code_plan": ROOT / "code" / "gsheets_plan.py",
        "code_report": ROOT / "code" / "gsheets_report.py",
    },
}

CODE_INDENT = 12  # `code: |` の内容は12スペース字下げ


def find_code_block(lines, node_id):
    """ノードID配下の `code: |` 行と、ブロックの終了行を探す."""
    node_start = None
    for index, line in enumerate(lines):
        if line.strip() == "- id: %s" % node_id:
            node_start = index
            break
    if node_start is None:
        raise SystemExit("node not found: %s" % node_id)

    node_end = len(lines)
    code_line = None
    for index in range(node_start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("- id: "):
            node_end = index
            break
        if stripped == "code: |" and code_line is None:
            code_line = index
    if code_line is None:
        raise SystemExit("code block not found: %s" % node_id)

    block_end = node_end
    for index in range(code_line + 1, node_end):
        line = lines[index]
        if line.strip() and (len(line) - len(line.lstrip())) < CODE_INDENT:
            block_end = index
            break
    return code_line, block_end


def inject(yaml_text, node_id, code):
    lines = yaml_text.split("\n")
    code_line, block_end = find_code_block(lines, node_id)
    indented = []
    for line in code.rstrip("\n").split("\n"):
        indented.append((" " * CODE_INDENT + line) if line.strip() else "")
    return "\n".join(lines[: code_line + 1] + indented + lines[block_end:])


def main():
    for dsl_path, code_files in DSL_FILES.items():
        text = dsl_path.read_text(encoding="utf-8")
        for node_id, path in code_files.items():
            text = inject(text, node_id, path.read_text(encoding="utf-8"))
        dsl_path.write_text(text, encoding="utf-8")
        print("injected into %s: %s" % (dsl_path.name, ", ".join(code_files)))


if __name__ == "__main__":
    main()
