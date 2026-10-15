import json
import subprocess
from pathlib import Path

from gh_client import NO_WINDOW

BOT_DIR = Path(__file__).resolve().parent
PROMPTS = BOT_DIR / "prompts"
ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Agent", "Skill"]
DISALLOWED_TOOLS = [
    "Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Artifact", "ArtifactComments",
    "ArtifactData", "ShareOnboardingGuide", "Workflow", "ScheduleWakeup", "RemoteTrigger", "PushNotification",
    "CronCreate", "CronDelete", "Monitor", "SendMessage", "EnterWorktree", "ExitWorktree", "DesignSync",
    "ReportFindings", "TaskStop",
]
START, END = "<<<PAYLOAD", "PAYLOAD>>>"

OPEN_THREADS_SECTION = """
## Threads abertos de reviews anteriores

Este PR já tem threads do bot abertos de reviews anteriores, e esta é uma review completa de novo (o histórico foi reescrito ou o dono do bot pediu). `{run_dir}/open_threads.json` lista os threads do bot que continuam abertos, no formato `[{{"thread_id", "path", "line", "body"}}]`. Não repita esses achados. Para cada um, decida se o código atual ainda tem o problema; se não tiver, inclua o thread em `fixed_threads` com uma frase de evidência. Na dúvida, não inclua.
"""
FIXED_THREADS_FIELD = ', "fixed_threads": [{"thread_id": "<id do open_threads.json>", "evidence": "<uma frase>"}]'


class ReviewError(RuntimeError):
    pass


def plugin_root(plugin_key):
    installed = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    entries = json.loads(installed.read_text(encoding="utf-8"))["plugins"][plugin_key]
    return Path(entries[0]["installPath"])


def render(name, values):
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def extract_payload(text):
    start = text.rfind(START)
    if start < 0:
        raise ReviewError("resposta sem o bloco <<<PAYLOAD")
    end = text.find(END, start)
    if end < 0:
        raise ReviewError("bloco <<<PAYLOAD sem fechamento")
    raw = text[start + len(START):end].strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else ""
        raw = raw.rsplit("```", 1)[0]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ReviewError(f"payload não é JSON válido: {e}") from e


def read_cost(run_dir):
    try:
        data = json.loads((run_dir / "claude-output.json").read_text(encoding="utf-8"))
        return float(data.get("total_cost_usd") or 0)
    except (OSError, ValueError, AttributeError):
        return 0.0


def write_inputs(run_dir, pr, files, diff, package_json, angular_json, tree):
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = {**pr, "files": [{"path": f} for f in files]}
    (run_dir / "pr.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "diff.patch").write_text(diff, encoding="utf-8", newline="")
    (run_dir / "package.json").write_text(package_json or "", encoding="utf-8", newline="")
    if angular_json:
        (run_dir / "angular.json").write_text(angular_json, encoding="utf-8", newline="")
    (run_dir / "tree.txt").write_text(tree, encoding="utf-8", newline="")


def run_claude(cfg, prompt, run_dir, log):
    args = [
        cfg["claude_path"], "-p", "--output-format", "json", "--strict-mcp-config", "--permission-mode", "dontAsk",
        "--max-budget-usd", str(cfg["claude_max_budget_usd"]),
        "--allowedTools", *ALLOWED_TOOLS,
        "--disallowedTools", *DISALLOWED_TOOLS,
        "--add-dir", str(run_dir), str(plugin_root(cfg["review_plugin"])),
    ]
    if cfg.get("claude_model"):
        args += ["--model", cfg["claude_model"]]
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    try:
        p = subprocess.run(
            args, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=run_dir, creationflags=NO_WINDOW, timeout=cfg["claude_timeout_minutes"] * 60,
        )
    except subprocess.TimeoutExpired as e:
        raise ReviewError(f"claude passou de {cfg['claude_timeout_minutes']} min") from e
    (run_dir / "claude-output.json").write_text(p.stdout, encoding="utf-8")
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise ReviewError(f"claude saiu com {p.returncode} sem JSON: {p.stderr.strip()[:400]}") from e
    log.info(
        "claude [%s]: %s em %.0fs, %d turnos, US$ %.2f",
        run_dir.name, data.get("subtype"), (data.get("duration_ms") or 0) / 1000, data.get("num_turns") or 0,
        data.get("total_cost_usd") or 0,
    )
    if data.get("is_error") or data.get("subtype") != "success":
        raise ReviewError(f"claude terminou com {data.get('subtype')}: {str(data.get('result'))[:400]}")
    payload = extract_payload(data.get("result") or "")
    (run_dir / "payload.raw.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
