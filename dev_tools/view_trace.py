"""Generate a static HTML trace viewer.

Usage: uv run python -m dev_tools.view_trace <task_id>
"""

import html
import json
import logging
import sys
from pathlib import Path

from agent.observability.log_setup import setup_logging

log = logging.getLogger(__name__)


def build_trace_index(task_id: str, data_dir: Path = Path("data")) -> Path:
    """Generate and return an HTML index for a persisted trace."""
    trace_dir = data_dir / "traces" / task_id
    actions_path = trace_dir / "actions.jsonl"
    if not actions_path.is_file():
        raise FileNotFoundError(f"no existe la traza {task_id}")
    records = [json.loads(line) for line in actions_path.read_text(encoding="utf-8").splitlines() if line]
    sections = []
    for record in records:
        action = html.escape(json.dumps(record["action"], ensure_ascii=False, indent=2))
        note = html.escape(str(record["note"]))
        image = html.escape(str(record["image"]))
        sections.append(
            f"<section><h2>Paso {record['step']}</h2><img src=\"{image}\" alt=\"Paso {record['step']}\">"
            f"<pre>{action}</pre><p>{note}</p></section>"
        )
    page = (
        "<!doctype html><html lang=\"es\"><meta charset=\"utf-8\"><title>Traza "
        f"{html.escape(task_id)}</title><style>body{{font-family:sans-serif;max-width:1200px;margin:auto}}"
        "img{max-width:100%;border:1px solid #ccc}pre{white-space:pre-wrap}</style><body>"
        f"<h1>Traza {html.escape(task_id)}</h1>{''.join(sections)}</body></html>"
    )
    index_path = trace_dir / "index.html"
    index_path.write_text(page, encoding="utf-8")
    return index_path


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        log.error("uso: python -m dev_tools.view_trace <task_id>")
        return 2
    try:
        path = build_trace_index(args[0])
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1
    log.info("visor de traza generado en %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())