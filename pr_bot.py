import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import config
import gate
import inbox
import procs
import reviewer
from gh_client import Gh, GhError

BOT_DIR = Path(__file__).resolve().parent
PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
LOCK_STALE_MINUTES = 150
RUNS_KEEP_DAYS = 7
DONE_KEEP = 30
REVIEWS_KEEP = 20
REVIEW_ACTIONS = ("full_review", "delta_review")
LABEL_ATTEMPTS = 3
SAVE_ATTEMPTS = 40
SAVE_RETRY_SECONDS = 0.05
INBOX_ROUNDS = 3


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
        self.stale_owner = None

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
                owner = procs.lock_owner(self.path)
                if owner is not None and not procs.pid_alive(owner):
                    self.stale_owner = owner
                elif age < LOCK_STALE_MINUTES:
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
        for attempt in range(SAVE_ATTEMPTS):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == SAVE_ATTEMPTS - 1:
                    raise
                time.sleep(SAVE_RETRY_SECONDS)


def new_entry(owner, repo, number, url, source="discovery"):
    return {
        "owner": owner, "repo": repo, "number": number, "url": url, "status": "NEW", "source": source,
        "discovered_at": now_iso(), "review_ids": [], "failures": 0, "labels_pending": True,
    }


@dataclass
class Candidate:
    key: str
    entry: dict
    pr: dict
    action: str
    forced: bool
    reviews_before: int


@dataclass
class Job:
    candidate: Candidate
    kind: str
    head: str
    template: str
    values: dict
    run_dir: Path
    diff: str
    open_threads: list = field(default_factory=list)
    record: dict = None

    @property
    def key(self):
        return self.candidate.key

    @property
    def entry(self):
        return self.candidate.entry


class Bot:
    def __init__(self, cfg, gh, state, log, dry_run):
        self.cfg = cfg
        self.gh = gh
        self.state = state
        self.log = log
        self.dry_run = dry_run
        self.login = cfg["login"]
        self.reviews_left = cfg["max_reviews_per_run"]
        self.concurrency = max(1, cfg.get("review_concurrency", 1))
        self.claude_down = None

    def save(self):
        self.state.save()

    def note(self, key, entry, msg, level=logging.INFO):
        self.log.log(level, "%s: %s", key, msg)
        entry["note"] = msg
        entry["note_at"] = now_iso()

    def retire(self, key, entry, reason, msg):
        done = self.state.data.setdefault("done", [])
        done.insert(0, {
            "key": key, "url": entry.get("url"), "title": entry.get("title"), "author": entry.get("author"),
            "status": entry["status"], "reason": reason, "left_at": now_iso(), "reviews": entry.get("reviews", []),
        })
        del done[DONE_KEEP:]
        del self.state.prs[key]
        self.log.info("%s: %s", key, msg)

    def guarded(self, key, entry, fn, reset):
        try:
            result = fn()
        except reviewer.ClaudeUnavailable as e:
            self.claude_down = str(e)
            self.state.data["claude_down"] = {"message": str(e)[:400], "at": now_iso()}
            self.note(key, entry, f"review adiada: o Claude está indisponível ({str(e)[:160]})", logging.ERROR)
            return None
        except Exception as e:
            entry["failures"] = entry.get("failures", 0) + 1
            entry["last_error"] = str(e)[:1000]
            if isinstance(e, (GhError, reviewer.ReviewError, gate.PayloadError)):
                self.log.error("%s: %s", key, e)
            else:
                self.log.exception("%s: erro inesperado", key)
            if entry["failures"] >= self.cfg["max_failures"]:
                entry["status"] = "ERROR"
                self.note(key, entry, f"marcado como ERROR depois de {entry['failures']} falhas seguidas", logging.ERROR)
            return None
        if reset(result):
            entry["failures"] = 0
        return result

    def ingest_inbox(self):
        added = []
        for request in inbox.drain(BOT_DIR):
            parsed = inbox.parse_pr_url(request.get("url"))
            if not parsed:
                self.log.warning("pedido inválido no inbox: %r", request)
                continue
            owner, repo, n = parsed
            key = f"{owner}/{repo}#{n}"
            entry = self.state.prs.get(key)
            if entry is None:
                entry = self.state.prs[key] = new_entry(owner, repo, n, f"https://github.com/{owner}/{repo}/pull/{n}", "manual")
                msg = "adicionado pelo painel"
            elif entry["status"] in ("ERROR", "SKIPPED"):
                entry.update(status="NEW", failures=0, source="manual")
                for stale in ("last_error", "skipped_sha", "skip_reason"):
                    entry.pop(stale, None)
                msg = "reenfileirado pelo painel"
            else:
                entry["source"] = "manual"
                msg = "review pedido pelo painel"
            entry["force"] = True
            if request.get("full"):
                entry["force_full"] = True
                msg = "review completa forçada pelo painel"
            self.note(key, entry, msg)
            added.append(key)
        if added:
            self.save()
        return added

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
                self.note(key, self.state.prs[key], "entrou na fila")
        self.save()

    def process_all(self, only_key=None, keys=None):
        order = {"REVIEWED": 0, "APPROVED": 1, "SKIPPED": 2, "NEW": 3, "ERROR": 4}
        prs = self.state.prs
        if only_key:
            keys = [only_key]
        if keys is None:
            keys = sorted(prs, key=lambda k: (not prs[k].get("force"), order[prs[k]["status"]]))
        candidates = []
        for key in keys:
            entry = prs.get(key)
            if not entry:
                continue
            candidate = self.guarded(key, entry, lambda: self.process(key, entry), reset=lambda c: c is None)
            if candidate:
                candidates.append(candidate)
            self.save()
        self.run_reviews(candidates)

    def run_reviews(self, candidates):
        pending = list(candidates)
        running = {}
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="review") as pool:
            while pending or running:
                if self.claude_down:
                    pending.clear()
                while pending and len(running) < self.concurrency:
                    c = pending.pop(0)
                    if c.key not in self.state.prs:
                        continue
                    job = self.guarded(c.key, c.entry, lambda: self.prepare(c), reset=lambda j: j is None)
                    if job:
                        self.start(job)
                    self.save()
                    if job:
                        running[pool.submit(self.execute, job)] = job
                if not running:
                    continue
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    job = running.pop(future)
                    self.guarded(job.key, job.entry, lambda: self.complete(job, future), reset=lambda _: True)
                    self.save()

    def process(self, key, entry):
        owner, repo, n = entry["owner"], entry["repo"], entry["number"]
        entry.pop("activity", None)
        try:
            pr = self.gh.pr_view(owner, repo, n)
        except GhError as e:
            if e.not_found:
                return self.retire(key, entry, "not_found", f"PR ou repo não existe mais ({str(e)[:120]}), saiu da fila")
            raise
        entry["title"] = pr.get("title")
        entry["author"] = (pr.get("author") or {}).get("login")
        if pr["state"] != "OPEN":
            return self.retire(key, entry, pr["state"].lower(), f"{pr['state'].lower()}, saiu da fila")
        if pr["isDraft"]:
            return self.note(key, entry, "virou draft, esperando")
        head = pr["headRefOid"]
        if entry.get("head_sha") != head:
            entry["head_sha"] = head
            entry["head_seen_at"] = now_iso()
        if (
            entry["status"] == "NEW"
            and entry.get("source", "discovery") == "discovery"
            and not self.is_requested(owner, repo, n)
        ):
            return self.retire(key, entry, "not_requested", f"review não está mais pedido a {self.login} nem aos times {self.cfg['review_teams']}, saiu da fila")
        self.apply_labels(key, entry, pr)
        forced = bool(entry.get("force"))
        action = "full_review" if entry.get("force_full") and entry["status"] != "ERROR" else gate.next_action(entry, head)
        if action in REVIEW_ACTIONS and not forced and not gate.settled(entry, self.cfg["review_debounce_minutes"]):
            return self.note(key, entry, f"head {head[:7]} mudou há menos de {self.cfg['review_debounce_minutes']} min, esperando novos pushes antes de revisar")
        candidate = Candidate(key, entry, pr, action, forced, len(entry.get("reviews", [])))
        if action in REVIEW_ACTIONS:
            return candidate
        if forced:
            self.note(key, entry, f"nada novo para revisar em {head[:7]}: o bot já revisou este head (use Forçar review para revisar de novo)")
        self.after_action(candidate)
        return None

    def apply_labels(self, key, entry, pr):
        wanted = self.cfg.get("pr_labels") or []
        if not entry.get("labels_pending") or not wanted:
            return
        owner, repo, n = entry["owner"], entry["repo"], entry["number"]
        current = {label["name"] for label in pr.get("labels") or []}
        missing = [label for label in wanted if label["name"] not in current]
        names = [label["name"] for label in missing]
        if not missing:
            entry.pop("labels_pending", None)
            entry["labels"] = {"applied": [], "created": [], "at": now_iso()}
            return
        if self.dry_run:
            entry.pop("labels_pending", None)
            entry["labels"] = {"applied": [], "created": [], "dry_run": names, "at": now_iso()}
            self.log.info("%s: [dry-run] aplicaria as labels %s", key, names)
            return
        try:
            created = []
            for label in missing:
                if not self.gh.label_exists(owner, repo, label["name"]):
                    self.gh.create_label(owner, repo, label)
                    created.append(label["name"])
            self.gh.add_labels(owner, repo, n, names)
        except GhError as e:
            attempts = entry.get("label_attempts", 0) + 1
            entry["label_attempts"] = attempts
            if attempts >= LABEL_ATTEMPTS:
                entry.pop("labels_pending", None)
                entry["labels"] = {"applied": [], "created": [], "error": str(e)[:300], "at": now_iso()}
                self.log.warning("%s: desisti de aplicar as labels %s depois de %d tentativas: %s", key, names, attempts, e)
            else:
                self.log.warning("%s: falha ao aplicar as labels %s (tentativa %d): %s", key, names, attempts, e)
            return
        entry.pop("labels_pending", None)
        entry.pop("label_attempts", None)
        entry["labels"] = {"applied": names, "created": created, "at": now_iso()}
        suffix = f" (criada(s) no repo: {', '.join(created)})" if created else ""
        self.log.info("%s: labels aplicadas: %s%s", key, ", ".join(names), suffix)

    def after_action(self, c):
        if c.key not in self.state.prs:
            return
        entry, head = c.entry, c.pr["headRefOid"]
        ran = len(entry.get("reviews", [])) > c.reviews_before or entry["status"] == "SKIPPED"
        if c.forced and (c.action not in REVIEW_ACTIONS or ran or (entry.get("reviewed_sha") == head and not entry.get("force_full"))):
            entry.pop("force", None)
            entry.pop("force_full", None)
        if entry["status"] == "REVIEWED" and entry.get("reviewed_sha") == head and not entry.get("force_full"):
            self.approval_gate(c.key, entry, c.pr)

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
        self.note(key, entry, f"ignorado em {head[:7]} ({reason})")

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

    def prepare(self, c):
        job = self.prepare_full(c) if c.action == "full_review" else self.prepare_delta(c)
        if job is None:
            self.after_action(c)
        return job

    def prepare_full(self, c, open_threads=None):
        key, entry, pr = c.key, c.entry, c.pr
        owner, repo, n, head = entry["owner"], entry["repo"], entry["number"], pr["headRefOid"]
        adopted = entry["status"] == "NEW" and not self.dry_run and self.adopt_existing_review(key, entry)
        if adopted and not entry.get("force_full"):
            return None
        if open_threads is None and entry.get("force_full") and entry.get("review_ids"):
            mine = gate.bot_threads(self.gh.review_threads(owner, repo, n), entry["review_ids"])
            open_threads = gate.open_thread_summaries(mine)
        files = [f["filename"] for f in self.gh.pr_files(owner, repo, n)]
        if not gate.reviewable_files(files):
            return self.skip(key, entry, head, "nenhum arquivo revisável")
        if not gate.is_angular_package(self.gh.file_at(owner, repo, "package.json", head)):
            return self.skip(key, entry, head, "package.json sem @angular/core")
        if self.reviews_left <= 0:
            return self.note(key, entry, "review adiada, limite de reviews da execução atingido")
        run_dir = self.run_dir(entry, head, "full")
        try:
            _, diff = self.load_inputs(entry, pr, run_dir)
        except GhError as e:
            if e.diff_too_large:
                return self.skip(key, entry, head, "diff grande demais para a API do GitHub (mais de 20 mil linhas)")
            raise
        values = self.prompt_values(entry, pr, run_dir)
        values["OPEN_THREADS_SECTION"] = ""
        values["FIXED_THREADS_FIELD"] = ""
        if open_threads:
            (run_dir / "open_threads.json").write_text(json.dumps(open_threads, ensure_ascii=False, indent=2), encoding="utf-8")
            values["OPEN_THREADS_SECTION"] = reviewer.OPEN_THREADS_SECTION.format(run_dir=run_dir.as_posix())
            values["FIXED_THREADS_FIELD"] = reviewer.FIXED_THREADS_FIELD
        self.reviews_left -= 1
        self.note(key, entry, f"review completa{' forçada' if entry.get('force_full') else ''} em {head[:7]}")
        return Job(c, "full", head, "full_review.md", values, run_dir, diff, open_threads or [])

    def prepare_delta(self, c):
        key, entry, pr = c.key, c.entry, c.pr
        owner, repo, n, head = entry["owner"], entry["repo"], entry["number"], pr["headRefOid"]
        base = entry["reviewed_sha"]
        mine = gate.bot_threads(self.gh.review_threads(owner, repo, n), entry.get("review_ids", []))
        open_threads = gate.open_thread_summaries(mine)
        cmp = self.gh.compare(owner, repo, base, head)
        if cmp is None or cmp["status"] in ("diverged", "behind"):
            self.note(key, entry, f"histórico reescrito desde {base[:7]} ({cmp and cmp['status']}), indo de review completa")
            return self.prepare_full(c, open_threads)
        files = [f["filename"] for f in self.gh.pr_files(owner, repo, n)]
        delta = gate.filter_diff_to_files(self.gh.compare_diff(owner, repo, base, head), files)
        delta_files = [gate.block_path(b) for b in gate.split_diff(delta)]
        if not gate.reviewable_files(delta_files):
            self.note(key, entry, f"delta {base[:7]}..{head[:7]} sem arquivo revisável do PR")
            entry.update(status="REVIEWED", reviewed_sha=head)
            return None
        if self.reviews_left <= 0:
            return self.note(key, entry, "review do delta adiada, limite de reviews da execução atingido")
        run_dir = self.run_dir(entry, head, "delta")
        _, diff = self.load_inputs(entry, pr, run_dir)
        (run_dir / "delta.patch").write_text(delta, encoding="utf-8", newline="")
        (run_dir / "open_threads.json").write_text(json.dumps(open_threads, ensure_ascii=False, indent=2), encoding="utf-8")
        values = self.prompt_values(entry, pr, run_dir)
        values.update(REVIEWED_SHA=base, REVIEWED_SHA7=base[:7])
        self.reviews_left -= 1
        self.note(key, entry, f"review do delta {base[:7]}..{head[:7]} ({len(open_threads)} threads abertos)")
        return Job(c, "delta", head, "delta_review.md", values, run_dir, diff, open_threads)

    def start(self, job):
        entry = job.entry
        entry["activity"] = {"kind": job.kind, "sha": job.head, "started_at": now_iso()}
        job.record = {"kind": job.kind, "sha": job.head, "at": now_iso(), "counts": None, "cost_usd": 0.0, "id": None}
        reviews = entry.setdefault("reviews", [])
        reviews.append(job.record)
        del reviews[:-REVIEWS_KEEP]

    def execute(self, job):
        return reviewer.run_claude(self.cfg, reviewer.render(job.template, job.values), job.run_dir, self.log)

    def complete(self, job, future):
        key, entry, head = job.key, job.entry, job.head
        try:
            payload = future.result()
        except reviewer.ClaudeUnavailable:
            reviews = entry.get("reviews", [])
            if job.record in reviews:
                reviews.remove(job.record)
            raise
        finally:
            entry.pop("activity", None)
            job.record["cost_usd"] = reviewer.read_cost(job.run_dir)
        self.state.data.pop("claude_down", None)
        job.record["counts"] = payload.get("counts")
        if job.kind == "full" and payload.get("body", "").strip() == "Nothing to review":
            self.skip(key, entry, head, "a skill não achou nada para revisar")
        else:
            job.record["id"] = self.publish(key, entry, job.candidate.pr, payload, gate.right_lines(job.diff), job.run_dir, always=job.kind == "full")
            self.close_fixed(key, payload, job.open_threads, head)
            entry.update(status="REVIEWED", reviewed_sha=head, reviewed_at=now_iso())
        latest = self.gh.pr_view(entry["owner"], entry["repo"], entry["number"])
        if latest["headRefOid"] != head:
            entry["head_sha"] = latest["headRefOid"]
            entry["head_seen_at"] = now_iso()
            self.note(key, entry, f"head mudou para {latest['headRefOid'][:7]} durante a review; o delta e o gate ficam para a próxima execução")
            job.candidate.pr = latest
        self.after_action(job.candidate)

    def publish(self, key, entry, pr, payload, anchors, run_dir, always):
        head = pr["headRefOid"]
        clean, moved = gate.validate_payload(payload, head, anchors)
        if not always and not gate.has_actionable(payload, clean):
            self.note(key, entry, f"delta sem blocker nem high novo ({payload.get('counts')}), nada postado")
            return None
        clean["body"] = gate.add_marker(clean["body"], head)
        path = run_dir / "payload.json"
        path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.dry_run:
            self.note(key, entry, f"[dry-run] postaria review com {len(clean['comments'])} inline ({moved} no body)")
            return None
        review = self.post(key, entry, clean, path)
        entry.setdefault("review_ids", []).append(review["id"])
        self.note(key, entry, f"review {review['id']} postada com {len(clean['comments'])} inline ({moved} no body)")
        return review["id"]

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
            return self.note(key, entry, f"head {head[:7]} visto há menos de {self.cfg['settle_minutes']} min, esperando o CI registrar")
        ignored = self.cfg["ignore_checks"].get(repo, [])
        verdict, names = gate.ci_verdict(self.gh.checks(owner, repo, n), ignored, self.cfg["require_checks"])
        threads = gate.bot_threads(self.gh.review_threads(owner, repo, n), entry.get("review_ids", []))
        still_open = [t for t in threads if not t["isResolved"]]
        entry["gate"] = {
            "sha": head, "ci": verdict, "ci_names": names[:5], "open_threads": len(still_open),
            "threads": len(threads), "checked_at": now_iso(),
        }
        if still_open:
            return self.note(key, entry, f"{len(still_open)} de {len(threads)} threads do bot abertos (CI {verdict})")
        if verdict != "pass":
            return self.note(key, entry, f"CI {verdict} em {head[:7]} ({', '.join(names[:5])})")
        if any(
            r["user"] == self.login and r["state"] == "APPROVED" and r["commit_id"] == head
            for r in self.gh.reviews(owner, repo, n)
        ):
            entry.update(status="APPROVED", approved_sha=head)
            return self.note(key, entry, f"já aprovado em {head[:7]}")
        if self.dry_run:
            entry.update(status="APPROVED", approved_sha=head)
            return self.note(key, entry, f"[dry-run] aprovaria em {head[:7]} ({len(threads)} threads do bot, CI verde)")
        body = (
            f"✅ Aprovado pelo bot de review: {len(threads)} thread(s) do bot resolvido(s) e CI verde em `{head[:7]}`."
            f"\n\n<!-- pr-autoapprove approve sha={head} -->"
        )
        self.gh.approve(owner, repo, n, head, body)
        entry.update(status="APPROVED", approved_sha=head, approved_at=now_iso())
        self.note(key, entry, f"APROVADO em {head[:7]}")


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
    state.data["run"] = {"mode": mode, "started_at": now_iso(), "finished_at": None, "reviews": 0}
    bot.save()
    try:
        if pr_url:
            m = PR_URL_RE.search(pr_url)
            if not m:
                log.error("URL de PR inválida: %s", pr_url)
                return 2
            owner, repo, n = m.group(1), m.group(2), int(m.group(3))
            key = f"{owner}/{repo}#{n}"
            state.prs.setdefault(key, new_entry(owner, repo, n, f"https://github.com/{owner}/{repo}/pull/{n}", "manual"))
            bot.process_all(keys=[key])
        else:
            bot.ingest_inbox()
            bot.discover()
            bot.process_all()
            for _ in range(INBOX_ROUNDS):
                extra = bot.ingest_inbox()
                if not extra:
                    break
                bot.process_all(keys=extra)
    finally:
        done = cfg["max_reviews_per_run"] - bot.reviews_left
        state.data["run"].update(finished_at=now_iso(), reviews=done)
        bot.save()
    log.info("fim: %d review(s) feita(s), %d PRs na fila", done, len(state.prs))
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
            log.info("outra execução em andamento (pid %s), saindo", procs.lock_owner(lock.path))
            return 0
        if lock.stale_owner:
            log.warning("lock do processo %s, que já tinha morrido, foi assumido", lock.stale_owner)
        try:
            return run(cfg, log, mode, mode == "dry-run", args.pr)
        except Exception:
            log.exception("falha geral na execução")
            return 1


if __name__ == "__main__":
    sys.exit(main())
