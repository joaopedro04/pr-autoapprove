import json
import os
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent


class ConfigError(ValueError):
    pass


def parse_dotenv(text):
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def as_list(value):
    value = value.strip()
    if value.startswith("["):
        items = json.loads(value)
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ValueError("esperado um array de textos")
        return [i.strip() for i in items if i.strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


def as_labels(value):
    value = value.strip()
    if not value:
        return []
    if value.startswith("{"):
        raise ValueError("use um array JSON, por exemplo [{\"name\": \"x\"}]")
    items = json.loads(value) if value.startswith("[") else as_list(value)
    if not isinstance(items, list):
        raise ValueError("esperado um array")
    labels = []
    for item in items:
        if isinstance(item, str):
            item = {"name": item}
        name = str(item.get("name", "")).strip() if isinstance(item, dict) else ""
        if not name:
            raise ValueError("cada label precisa de um name")
        label = {"name": name}
        if item.get("color"):
            label["color"] = str(item["color"]).lstrip("#")
        if item.get("description"):
            label["description"] = str(item["description"])
        labels.append(label)
    return labels


def as_bool(value):
    return value.strip().lower() in ("1", "true", "yes", "sim")


def as_ignore_checks(value):
    out = {}
    for item in value.split(";"):
        if ":" in item:
            repo, name = item.split(":", 1)
            out.setdefault(repo.strip(), []).append(name.strip())
    return out


SPEC = {
    "mode": ("BOT_MODE", "dry-run", str),
    "org": ("GITHUB_ORG", None, str),
    "login": ("GITHUB_LOGIN", None, str),
    "review_teams": ("REVIEW_TEAMS", "", as_list),
    "pr_labels": ("PR_LABELS", "", as_labels),
    "repo_suffixes": ("REPO_SUFFIXES", "-mfe,-frontend,-ui,-front,-container", as_list),
    "extra_repos": ("EXTRA_REPOS", "", as_list),
    "exclude_repos": ("EXCLUDE_REPOS", "", as_list),
    "active_since": ("ACTIVE_SINCE", None, str),
    "max_reviews_per_run": ("MAX_REVIEWS_PER_RUN", "6", int),
    "review_concurrency": ("REVIEW_CONCURRENCY", "3", int),
    "max_failures": ("MAX_FAILURES", "3", int),
    "settle_minutes": ("SETTLE_MINUTES", "10", int),
    "review_debounce_minutes": ("REVIEW_DEBOUNCE_MINUTES", "5", int),
    "schedule_interval_minutes": ("SCHEDULE_INTERVAL_MINUTES", "5", int),
    "schedule_task_name": ("SCHEDULE_TASK_NAME", "pr-autoapprove", str),
    "dashboard_port": ("DASHBOARD_PORT", "8765", int),
    "require_checks": ("REQUIRE_CHECKS", "true", as_bool),
    "ignore_checks": ("IGNORE_CHECKS", "", as_ignore_checks),
    "review_skill": ("REVIEW_SKILL", None, str),
    "review_plugin": ("REVIEW_PLUGIN", None, str),
    "gh_path": ("GH_PATH", "gh", str),
    "claude_path": ("CLAUDE_PATH", "claude", os.path.expanduser),
    "claude_model": ("CLAUDE_MODEL", "", str),
    "claude_timeout_minutes": ("CLAUDE_TIMEOUT_MINUTES", "30", int),
    "claude_max_budget_usd": ("CLAUDE_MAX_BUDGET_USD", "20", float),
}
MODES = ("live", "dry-run", "off")


def load_config(env_file=None, environ=None):
    path = Path(env_file) if env_file else BOT_DIR / ".env"
    values = parse_dotenv(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    environ = os.environ if environ is None else environ
    cfg, missing = {}, []
    for key, (var, default, parse) in SPEC.items():
        raw = environ.get(var, values.get(var, default))
        if raw is None or raw == "":
            if default is None:
                missing.append(var)
                continue
            raw = default
        try:
            cfg[key] = parse(raw)
        except ValueError as e:
            raise ConfigError(f"{var}={raw!r} inválido: {e}") from e
    if missing:
        raise ConfigError(f"faltam no .env: {', '.join(missing)}")
    if cfg["review_concurrency"] < 1:
        raise ConfigError("REVIEW_CONCURRENCY precisa ser 1 ou mais")
    if cfg["mode"] not in MODES:
        raise ConfigError(f"BOT_MODE precisa ser um de {MODES}, veio {cfg['mode']!r}")
    return cfg
