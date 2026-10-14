import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import config
import gate
import reviewer
from gh_client import Gh, GhError

BOT_DIR = Path(__file__).resolve().parent
PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
LOCK_STALE_MINUTES = 150
RUNS_KEEP_DAYS = 7


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def setup_logging():
    (BOT_DIR / "logs").mkdir(exist_ok=True)
    log = logging.getLogger("pr-bot")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(BOT_DIR / "logs" / f"{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log


class Lock:
    def __init__(self, path):
        self.path = path
        self.held = False

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    age = (time.time() - self.path.stat().st_mtime) / 60
                except FileNotFoundError:
                    continue
                if age < LOCK_STALE_MINUTES:
                    return self
                self.path.unlink(missing_ok=True)
        return self

    def __exit__(self, *exc):
        if self.held:
            self.path.unlink(missing_ok=True)


class State:
    def __init__(self, path):
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"prs": {}}

    @property
    def prs(self):
        return self.data["prs"]

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def new_entry(owner, repo, number, url, source="discovery"):
    return {
        "owner": owner, "repo": repo, "number": number, "url": url, "status": "NEW", "source": source,
        "discovered_at": now_iso(), "review_ids": [], "failures": 0,
    }


class Bot:
    def __init__(self, cfg, gh, state, log, dry_run):
        self.cfg = cfg
        self.gh = gh
        self.state = state
        self.log = log
        self.dry_run = dry_run
        self.login = cfg["login"]
        self.reviews_left = cfg["max_reviews_per_run"]

    def discover(self):
        org = self.cfg["org"]
        filters = f"draft:false -author:app/dependabot -author:{self.login} created:>={self.cfg['active_since']}"
        requests = [f"user-review-requested:{self.login}"]
        requests += [f"team-review-requested:{org}/{team}" for team in self.cfg["review_teams"]]
        items = [i for r in requests for i in self.gh.search_prs(f"is:pr is:open org:{org} {r} {filters}")]
        for item in items:
            owner, repo = item["repo_url"].rstrip("/").split("/")[-2:]
            if not gate.repo_in_scope(repo, self.cfg):
                continue
            key = f"{owner}/{repo}#{item['number']}"
            if key not in self.state.prs:
                self.state.prs[key] = new_entry(owner, repo, item["number"], item["url"])
                self.log.info("%s: novo PR na fila", key)
        self.state.save()

    def process_all(self, only_key=None):
        order = {"REVIEWED": 0, "APPROVED": 1, "SKIPPED": 2, "NEW": 3, "ERROR": 4}
        keys = [only_key] if only_key else sorted(self.state.prs, key=lambda k: order[self.state.prs[k]["status"]])
        for key in keys:
            entry = self.state.prs.get(key)
            if not entry:
                continue
            try:
                self.process(key, entry)
                entry["failures"] = 0
            except Exception as e:
                entry["failures"] = entry.get("failures", 0) + 1
                entry["last_error"] = str(e)[:1000]
                if isinstance(e, (GhError, reviewer.ReviewError, gate.PayloadError)):
                    self.log.error("%s: %s", key, e)
                else:
                    self.log.exception("%s: erro inesperado", key)
                if entry["failures"] >= self.cfg["max_failures"]:
                    entry["status"] = "ERROR"
                    self.log.error("%s: marcado como ERROR depois de %d falhas seguidas", key, entry["failures"])
            finally:
                self.state.save()

    def process(self, key, entry):
        owner, repo, n = entry["owner"], entry["repo"], entry["number"]
        pr = self.gh.pr_view(owner, repo, n)
        if pr["state"] != "OPEN":
            del self.state.prs[key]
            self.log.info("%s: %s, saiu da fila", key, pr["state"].lower())
            return
        if pr["isDraft"]:
            self.log.info("%s: virou draft, esperando", key)
            return
        head = pr["headRefOid"]
        if entry.get("head_sha") != head:
            entry["head_sha"] = head
            entry["head_seen_at"] = now_iso()
        action = gate.next_action(entry, head)
        if (
            entry["status"] == "NEW"
            and entry.get("source", "discovery") == "discovery"
            and not self.is_requested(owner, repo, n)
        ):
            del self.state.prs[key]
            self.log.info("%s: review não está mais pedido a %s nem aos times %s, saiu da fila", key, self.login, self.cfg["review_teams"])
            return
        if action == "full_review":
            self.full_review(key, entry, pr)
        elif action == "delta_review":
            self.delta_review(key, entry, pr)
        if entry["status"] == "REVIEWED" and entry.get("reviewed_sha") == head:
            self.approval_gate(key, entry, pr)

    def is_requested(self, owner, repo, n):
        req = self.gh.requested_reviewers(owner, repo, n)
        return self.login in req["users"] or bool(set(req["teams"]) & set(self.cfg["review_teams"]))

    def run_dir(self, entry, head, kind):
        d = BOT_DIR / "runs" / f"{entry['repo']}-{entry['number']}-{head[:7]}-{kind}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def prompt_values(self, entry, pr, run_dir):
        head = pr["headRefOid"]
        return {
            "SKILL": self.cfg["review_skill"], "PR_URL": pr["url"], "LOGIN": self.login, "RUN_DIR": run_dir.as_posix(), "OWNER": entry["owner"],
            "REPO": entry["repo"], "NUMBER": str(entry["number"]), "HEAD_SHA": head, "HEAD_SHA7": head[:7],
        }

    def skip(self, key, entry, head, reason):
        entry.update(status="SKIPPED", skipped_sha=head, skip_reason=reason)
        self.log.info("%s: ignorado em %s (%s)", key, head[:7], reason)

    def adopt_existing_review(self, key, entry):
        mine = [
            r for r in self.gh.reviews(entry["owner"], entry["repo"], entry["number"])
            if r["user"] == self.login and gate.marker_sha(r["body"])
        ]
        if not mine:
            return False
        entry.update(status="REVIEWED", review_ids=[r["id"] for r in mine], reviewed_sha=gate.marker_sha(mine[-1]["body"]))
        self.log.info("%s: já havia %d review(s) do bot, estado recuperado", key, len(mine))
        return True

    def load_inputs(self, entry, pr, run_dir):
        owner, repo, n, head = entry["owner"], entry["repo"], entry["number"], pr["headRefOid"]
        files = [f["filename"] for f in self.gh.pr_files(owner, repo, n)]
        diff = self.gh.pr_diff(owner, repo, n)
        package_json = self.gh.file_at(owner, repo, "package.json", head)
        angular_json = self.gh.file_at(owner, repo, "angular.json", head)
        tree = self.gh.tree(owner, repo, head)
        reviewer.write_inputs(run_dir, pr, files, diff, package_json, angular_json, tree)
        return files, diff

    def full_review(self, key, entry, pr, open_threads=None):
        owner, repo, n, head = entry["owner"], entry["repo"], entry["number"], pr["headRefOid"]
        if entry["status"] == "NEW" and not self.dry_run and self.adopt_existing_review(key, entry):
            return
        files = [f["filename"] for f in self.gh.pr_files(owner, repo, n)]
        if not gate.reviewable_files(files):
            return self.skip(key, entry, head, "nenhum arquivo revisável")
        if not gate.is_angular_package(self.gh.file_at(owner, repo, "package.json", head)):
            return self.skip(key, entry, head, "package.json sem @angular/core")
        if self.reviews_left <= 0:
            self.log.info("%s: review adiada, limite da execução atingido", key)
            return
        run_dir = self.run_dir(entry, head, "full")
        _, diff = self.load_inputs(entry, pr, run_dir)
        values = self.prompt_values(entry, pr, run_dir)
        values["OPEN_THREADS_SECTION"] = ""
        values["FIXED_THREADS_FIELD"] = ""
        if open_threads:
            (run_dir / "open_threads.json").write_text(json.dumps(open_threads, ensure_ascii=False, indent=2), encoding="utf-8")
            values["OPEN_THREADS_SECTION"] = reviewer.OPEN_THREADS_SECTION.format(run_dir=run_dir.as_posix())
            values["FIXED_THREADS_FIELD"] = reviewer.FIXED_THREADS_FIELD
        self.reviews_left -= 1
        self.log.info("%s: review completa em %s", key, head[:7])
        payload = reviewer.run_claude(self.cfg, reviewer.render("full_review.md", values), run_dir, self.log)
        if payload.get("body", "").strip() == "Nothing to review":
            return self.skip(key, entry, head, "a skill não achou nada para revisar")
        self.publish(key, entry, pr, payload, gate.right_lines(diff), run_dir, always=True)
        self.close_fixed(key, payload, open_threads or [], head)
        entry.update(status="REVIEWED", reviewed_sha=head, reviewed_at=now_iso())

    def delta_review(self, key, entry, pr):
        owner, repo, n, head = entry["owner"], entry["repo"], entry["number"], pr["headRefOid"]
        base = entry["reviewed_sha"]
        mine = gate.bot_threads(self.gh.review_threads(owner, repo, n), entry.get("review_ids", []))
        open_threads = gate.open_thread_summaries(mine)
        cmp = self.gh.compare(owner, repo, base, head)
        if cmp is None or cmp["status"] in ("diverged", "behind"):
            self.log.info("%s: histórico reescrito desde %s (%s), indo de review completa", key, base[:7], cmp and cmp["status"])
            return self.full_review(key, entry, pr, open_threads)
        files = [f["filename"] for f in self.gh.pr_files(owner, repo, n)]
        delta = gate.filter_diff_to_files(self.gh.compare_diff(owner, repo, base, head), files)
        delta_files = [gate.block_path(b) for b in gate.split_diff(delta)]
        if not gate.reviewable_files(delta_files):
            self.log.info("%s: delta %s..%s sem arquivo revisável do PR", key, base[:7], head[:7])
            entry.update(status="REVIEWED", reviewed_sha=head)
            return
        if self.reviews_left <= 0:
            self.log.info("%s: review do delta adiada, limite da execução atingido", key)
            return
        run_dir = self.run_dir(entry, head, "delta")
        _, diff = self.load_inputs(entry, pr, run_dir)
        (run_dir / "delta.patch").write_text(delta, encoding="utf-8", newline="")
        (run_dir / "open_threads.json").write_text(json.dumps(open_threads, ensure_ascii=False, indent=2), encoding="utf-8")
        values = self.prompt_values(entry, pr, run_dir)
        values.update(REVIEWED_SHA=base, REVIEWED_SHA7=base[:7])
        self.reviews_left -= 1
        self.log.info("%s: review do delta %s..%s (%d threads abertos)", key, base[:7], head[:7], len(open_threads))
        payload = reviewer.run_claude(self.cfg, reviewer.render("delta_review.md", values), run_dir, self.log)
        self.publish(key, entry, pr, payload, gate.right_lines(diff), run_dir, always=False)
        self.close_fixed(key, payload, open_threads, head)
        entry.update(status="REVIEWED", reviewed_sha=head, reviewed_at=now_iso())

    def publish(self, key, entry, pr, payload, anchors, run_dir, always):
        head = pr["headRefOid"]
        clean, moved = gate.validate_payload(payload, head, anchors)
        if not always and not clean["comments"] and not moved and gate.counts_total(payload) == 0:
            self.log.info("%s: nenhum achado novo no delta", key)
            return
        clean["body"] = gate.add_marker(clean["body"], head)
        path = run_dir / "payload.json"
        path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.dry_run:
            self.log.info("%s: [dry-run] postaria review com %d inline (%d no body) — %s", key, len(clean["comments"]), moved, path)
            return
        review = self.post(key, entry, clean, path)
        entry.setdefault("review_ids", []).append(review["id"])
        self.log.info("%s: review %s postada com %d inline (%d no body)", key, review["id"], len(clean["comments"]), moved)

    def post(self, key, entry, clean, path):
        owner, repo, n = entry["owner"], entry["repo"], entry["number"]
        try:
            return self.gh.post_review(owner, repo, n, path)
        except GhError as e:
            if e.server_error:
                found = self.find_bot_review(entry, clean["commit_id"])
                if found:
                    self.log.warning("%s: POST deu erro de servidor, mas a review foi criada", key)
                    return found
                raise
            if "HTTP 422" not in e.stderr or not clean["comments"]:
                raise
        self.log.warning("%s: 422 nas âncoras, movendo os comentários inline para o body", key)
        bullets = "\n".join(f"- `{c['path']}:{c['line']}` — {gate.compact(c['body'])}" for c in clean["comments"])
        fallback = {**clean, "comments": [], "body": f"{clean['body']}\n\n### Achados inline (sem âncora)\n\n{bullets}"}
        path.write_text(json.dumps(fallback, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.gh.post_review(owner, repo, n, path)

    def find_bot_review(self, entry, sha):
        for r in self.gh.reviews(entry["owner"], entry["repo"], entry["number"]):
            if r["user"] == self.login and gate.marker_sha(r["body"]) == sha and r["id"] not in entry.get("review_ids", []):
                return r
        return None

    def close_fixed(self, key, payload, open_threads, head):
        valid = {t["thread_id"] for t in open_threads}
        for item in payload.get("fixed_threads") or []:
            tid = item.get("thread_id")
            if tid not in valid:
                self.log.warning("%s: fixed_thread %r ignorado, não é thread aberto do bot", key, tid)
                continue
            body = f"✅ Corrigido em `{head[:7]}`: {str(item.get('evidence', '')).strip()[:500]}"
            if self.dry_run:
                self.log.info("%s: [dry-run] resolveria o thread %s", key, tid)
                continue
            self.gh.reply_and_resolve(tid, body)
            self.log.info("%s: thread %s respondido e resolvido", key, tid)

    def approval_gate(self, key, entry, pr):
        owner, repo, n, head = entry["owner"], entry["repo"], entry["number"], pr["headRefOid"]
        if not gate.settled(entry, self.cfg["settle_minutes"]):
            self.log.info("%s: head %s visto há menos de %d min, esperando o CI registrar", key, head[:7], self.cfg["settle_minutes"])
            return
        ignored = self.cfg["ignore_checks"].get(repo, [])
        verdict, names = gate.ci_verdict(self.gh.checks(owner, repo, n), ignored, self.cfg["require_checks"])
        if verdict != "pass":
            self.log.info("%s: CI %s em %s (%s)", key, verdict, head[:7], ", ".join(names[:5]))
            return
        threads = gate.bot_threads(self.gh.review_threads(owner, repo, n), entry.get("review_ids", []))
        still_open = [t for t in threads if not t["isResolved"]]
        if still_open:
            self.log.info("%s: CI verde, %d de %d threads do bot abertos", key, len(still_open), len(threads))
            return
        if any(
            r["user"] == self.login and r["state"] == "APPROVED" and r["commit_id"] == head
            for r in self.gh.reviews(owner, repo, n)
        ):
            entry.update(status="APPROVED", approved_sha=head)
            self.log.info("%s: já aprovado em %s", key, head[:7])
            return
        if self.dry_run:
            entry.update(status="APPROVED", approved_sha=head)
            self.log.info("%s: [dry-run] aprovaria em %s (%d threads do bot, CI verde)", key, head[:7], len(threads))
            return
        body = (
            f"✅ Aprovado pelo bot de review: {len(threads)} thread(s) do bot resolvido(s) e CI verde em `{head[:7]}`."
            f"\n\n<!-- pr-autoapprove approve sha={head} -->"
        )
        self.gh.approve(owner, repo, n, head, body)
        entry.update(status="APPROVED", approved_sha=head, approved_at=now_iso())
        self.log.info("%s: APROVADO em %s", key, head[:7])


def cleanup_runs():
    runs = BOT_DIR / "runs"
    if not runs.exists():
        return
    limit = time.time() - RUNS_KEEP_DAYS * 86400
    for d in runs.iterdir():
        if d.is_dir() and d.stat().st_mtime < limit:
            shutil.rmtree(d, ignore_errors=True)


def run(cfg, log, mode, dry_run, pr_url):
    gh = Gh(cfg["gh_path"])
    login = gh.login()
    if login != cfg["login"]:
        log.error("gh autenticado como %s, esperado %s", login, cfg["login"])
        return 1
    state = State(BOT_DIR / ("state.dry-run.json" if dry_run else "state.json"))
    bot = Bot(cfg, gh, state, log, dry_run)
    cleanup_runs()
    log.info("início: mode=%s, %d PRs na fila", mode, len(state.prs))
    if pr_url:
        m = PR_URL_RE.search(pr_url)
        if not m:
            log.error("URL de PR inválida: %s", pr_url)
            return 2
        owner, repo, n = m.group(1), m.group(2), int(m.group(3))
        key = f"{owner}/{repo}#{n}"
        state.prs.setdefault(key, new_entry(owner, repo, n, f"https://github.com/{owner}/{repo}/pull/{n}", "manual"))
        bot.process_all(only_key=key)
    else:
        bot.discover()
        bot.process_all()
    log.info("fim: %d review(s) feita(s), %d PRs na fila", cfg["max_reviews_per_run"] - bot.reviews_left, len(state.prs))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Revisa PRs com uma skill do Claude Code e aprova quando os threads do bot estão resolvidos e o CI está verde")
    parser.add_argument("--pr", help="URL de um PR para processar só ele, sem descoberta")
    parser.add_argument("--dry-run", action="store_true", help="não posta, não resolve thread e não aprova")
    args = parser.parse_args()

    log = setup_logging()
    try:
        cfg = config.load_config()
    except config.ConfigError as e:
        log.error("config inválida: %s", e)
        return 1
    mode = "dry-run" if args.dry_run else cfg["mode"]
    if mode == "off":
        log.info("mode=off, nada a fazer")
        return 0

    with Lock(BOT_DIR / "state.lock") as lock:
        if not lock.held:
            log.info("outra execução em andamento, saindo")
            return 0
        try:
            return run(cfg, log, mode, mode == "dry-run", args.pr)
        except Exception:
            log.exception("falha geral na execução")
            return 1


if __name__ == "__main__":
    sys.exit(main())
