import re
from datetime import datetime, timedelta, timezone

MARKER_RE = re.compile(r"<!-- pr-autoapprove sha=([0-9a-f]{40}) -->")
REVIEWABLE_EXT = (".ts", ".html", ".scss", ".json", ".js")
SKIP_FILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")
INLINE_SEVERITIES = ("blocker", "high")


def repo_in_scope(name, cfg):
    if name in cfg.get("exclude_repos", []):
        return False
    if name in cfg.get("extra_repos", []):
        return True
    return any(name.endswith(s) for s in cfg["repo_suffixes"])


def reviewable_files(files):
    out = []
    for f in files:
        base = f.rsplit("/", 1)[-1]
        if "node_modules/" in f or f.endswith(".spec.ts") or base in SKIP_FILES:
            continue
        if f.endswith(REVIEWABLE_EXT) or base == ".npmrc":
            out.append(f)
    return out


def is_angular_package(package_json):
    return bool(package_json) and '"@angular/core"' in package_json


def ci_verdict(checks, ignored=(), require_checks=True):
    relevant = [c for c in checks if c.get("name") not in ignored]
    if not relevant and require_checks:
        return "pending", ["nenhum check registrado ainda"]
    failed = [c["name"] for c in relevant if c.get("bucket") in ("fail", "cancel")]
    if failed:
        return "fail", failed
    pending = [c["name"] for c in relevant if c.get("bucket") == "pending"]
    if pending:
        return "pending", pending
    return "pass", []


def bot_threads(threads, review_ids):
    ids = set(review_ids)
    out = []
    for t in threads:
        first = (t.get("comments") or {}).get("nodes") or []
        review = (first[0].get("pullRequestReview") or {}) if first else {}
        if review.get("databaseId") in ids:
            out.append(t)
    return out


def open_thread_summaries(threads):
    out = []
    for t in threads:
        if t.get("isResolved"):
            continue
        first = t["comments"]["nodes"][0]
        out.append({"thread_id": t["id"], "path": t.get("path"), "line": t.get("line"), "body": first.get("body", "")})
    return out


def split_diff(diff):
    blocks, current = [], []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            blocks.append("".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("".join(current))
    return [b for b in blocks if b.startswith("diff --git ")]


def block_path(block):
    minus = plus = None
    for line in block.splitlines():
        if line.startswith("+++ "):
            plus = line[4:].strip()
        elif line.startswith("--- "):
            minus = line[4:].strip()
        elif line.startswith("@@"):
            break
    if plus and plus != "/dev/null":
        return plus[2:] if plus.startswith("b/") else plus
    if minus and minus != "/dev/null":
        return minus[2:] if minus.startswith("a/") else minus
    m = re.match(r"diff --git a/(.+) b/(.+)", block.splitlines()[0])
    return m.group(2) if m else None


def filter_diff_to_files(diff, files):
    keep = set(files)
    return "".join(b for b in split_diff(diff) if block_path(b) in keep)


def right_lines(diff):
    out = {}
    hunk = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for block in split_diff(diff):
        path = block_path(block)
        lines, new_no, in_hunk = set(), 0, False
        for line in block.splitlines():
            m = hunk.match(line)
            if m:
                new_no, in_hunk = int(m.group(1)), True
                continue
            if not in_hunk or line.startswith("\\"):
                continue
            if line.startswith("+") or line.startswith(" ") or line == "":
                lines.add(new_no)
                new_no += 1
        if path:
            out[path] = lines
    return out


class PayloadError(ValueError):
    pass


def compact(body):
    parts = [p.strip() for p in body.splitlines() if p.strip() and not p.strip().startswith("```")]
    return " ".join(parts[:2])[:300]


def validate_payload(payload, head_sha, anchors, max_inline=30):
    if not isinstance(payload, dict):
        raise PayloadError("payload não é objeto JSON")
    if payload.get("event") != "COMMENT":
        raise PayloadError(f"event inválido: {payload.get('event')!r}")
    if payload.get("commit_id") != head_sha:
        raise PayloadError(f"commit_id {payload.get('commit_id')!r} difere do head {head_sha}")
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        raise PayloadError("body vazio")
    inline, moved = [], []
    for c in payload.get("comments") or []:
        path, line, start = c.get("path"), c.get("line"), c.get("start_line")
        ok = (
            c.get("severity", "high") in INLINE_SEVERITIES
            and isinstance(line, int)
            and line in anchors.get(path, ())
            and (start is None or start == line or (isinstance(start, int) and start < line and start in anchors.get(path, ())))
            and isinstance(c.get("body"), str)
            and len(inline) < max_inline
        )
        if not ok:
            moved.append(c)
            continue
        clean = {"path": path, "side": "RIGHT", "line": line, "body": c["body"]}
        if isinstance(start, int) and start != line:
            clean.update(start_line=start, start_side="RIGHT")
        inline.append(clean)
    if moved:
        bullets = "\n".join(f"- `{c.get('path')}:{c.get('line')}` — {compact(c.get('body') or '')}" for c in moved)
        body = f"{body}\n\n### Achados sem âncora inline\n\n{bullets}"
    return {"commit_id": head_sha, "event": "COMMENT", "body": body, "comments": inline}, len(moved)


def add_marker(body, sha):
    return f"{body}\n\n<!-- pr-autoapprove sha={sha} -->"


def marker_sha(body):
    m = MARKER_RE.search(body or "")
    return m.group(1) if m else None


def has_actionable(payload, clean):
    counts = payload.get("counts") or {}
    severe = sum(counts.get(k) or 0 for k in INLINE_SEVERITIES if isinstance(counts.get(k), int))
    return bool(clean["comments"]) or severe > 0


def next_action(entry, head):
    status = entry["status"]
    if status == "ERROR":
        return "skip"
    if status == "NEW":
        return "full_review"
    if status == "SKIPPED":
        return "full_review" if head != entry.get("skipped_sha") else "skip"
    if status == "REVIEWED":
        return "delta_review" if head != entry["reviewed_sha"] else "gate"
    if status == "APPROVED":
        return "delta_review" if head != entry["approved_sha"] else "skip"
    raise ValueError(f"status desconhecido: {status}")


def settled(entry, minutes, now=None):
    seen = entry.get("head_seen_at")
    if not seen:
        return False
    now = now or datetime.now(timezone.utc)
    return now - datetime.fromisoformat(seen) >= timedelta(minutes=minutes)
