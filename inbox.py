import json
import os
import re
import time
from datetime import datetime, timezone

PR_URL = re.compile(r"^https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)/pull/(\d+)(?:[/?#].*)?$")


def parse_pr_url(url):
    m = PR_URL.match((url or "").strip())
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def folder(bot_dir):
    return bot_dir / "inbox"


def enqueue(bot_dir, url):
    owner, repo, n = parse_pr_url(url)
    data = {
        "url": f"https://github.com/{owner}/{repo}/pull/{n}", "key": f"{owner}/{repo}#{n}",
        "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    d = folder(bot_dir)
    d.mkdir(exist_ok=True)
    path = d / f"{time.time_ns()}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return data


def _files(bot_dir):
    d = folder(bot_dir)
    return sorted(d.glob("*.json")) if d.exists() else []


def pending(bot_dir):
    out = []
    for p in _files(bot_dir):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def drain(bot_dir):
    items = []
    for p in _files(bot_dir):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        p.unlink(missing_ok=True)
        if isinstance(data, dict):
            items.append(data)
    return items


def signature(bot_dir):
    return tuple(p.name for p in _files(bot_dir))
