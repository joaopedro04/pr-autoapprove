import json
import os
import subprocess
from urllib.parse import quote

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

THREADS_QUERY = """
query($owner:String!,$name:String!,$n:Int!,$after:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$n){
      reviewThreads(first:100,after:$after){
        pageInfo{hasNextPage endCursor}
        nodes{
          id isResolved isOutdated path line
          comments(first:1){nodes{databaseId body author{login} pullRequestReview{databaseId}}}
        }
      }
    }
  }
}
"""

REPLY_MUTATION = """
mutation($id:ID!,$body:String!){
  addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$id,body:$body}){comment{id}}
}
"""

RESOLVE_MUTATION = """
mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}
"""


class GhError(RuntimeError):
    def __init__(self, args, code, stderr):
        super().__init__(f"gh {' '.join(args[:3])} saiu com {code}: {stderr.strip()[:600]}")
        self.code = code
        self.stderr = stderr

    @property
    def not_found(self):
        return "HTTP 404" in self.stderr or "Not Found" in self.stderr

    @property
    def server_error(self):
        return any(f"HTTP {c}" in self.stderr for c in (500, 502, 503, 504))


class Gh:
    def __init__(self, path):
        self.path = path
        self.env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1"}

    def raw(self, *args, timeout=300):
        return subprocess.run(
            [self.path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.env,
            creationflags=NO_WINDOW,
            timeout=timeout,
        )

    def run(self, *args):
        p = self.raw(*args)
        if p.returncode != 0:
            raise GhError(list(args), p.returncode, p.stderr)
        return p.stdout

    def json(self, *args):
        out = self.run(*args).strip()
        return json.loads(out) if out else None

    def json_lines(self, *args):
        return [json.loads(line) for line in self.run(*args).splitlines() if line.strip()]

    def login(self):
        return self.run("api", "user", "--jq", ".login").strip()

    def search_prs(self, query):
        return self.json_lines(
            "api", "-X", "GET", "search/issues", "-f", f"q={query}", "-f", "per_page=100", "--paginate",
            "--jq", ".items[] | {url: .html_url, number, draft, author: .user.login, repo_url: .repository_url}",
        )

    def pr_view(self, owner, repo, n):
        return self.json(
            "pr", "view", str(n), "-R", f"{owner}/{repo}",
            "--json", "state,isDraft,headRefOid,baseRefName,headRefName,author,url,title,labels",
        )

    def label_exists(self, owner, repo, name):
        try:
            self.run("api", f"repos/{owner}/{repo}/labels/{quote(name, safe='')}")
            return True
        except GhError as e:
            if e.not_found:
                return False
            raise

    def create_label(self, owner, repo, label):
        args = ["api", "-X", "POST", f"repos/{owner}/{repo}/labels", "-f", f"name={label['name']}"]
        if label.get("color"):
            args += ["-f", f"color={label['color']}"]
        if label.get("description"):
            args += ["-f", f"description={label['description']}"]
        try:
            self.run(*args)
        except GhError as e:
            if "already_exists" not in e.stderr and "HTTP 422" not in e.stderr:
                raise

    def add_labels(self, owner, repo, n, names):
        args = ["api", "-X", "POST", f"repos/{owner}/{repo}/issues/{n}/labels"]
        for name in names:
            args += ["-f", f"labels[]={name}"]
        self.run(*args)

    def requested_reviewers(self, owner, repo, n):
        return self.json(
            "api", f"repos/{owner}/{repo}/pulls/{n}/requested_reviewers",
            "--jq", "{users: [.users[].login], teams: [.teams[].slug]}",
        )

    def pr_files(self, owner, repo, n):
        return self.json_lines(
            "api", f"repos/{owner}/{repo}/pulls/{n}/files", "--paginate", "--jq", ".[] | {filename, status}",
        )

    def pr_diff(self, owner, repo, n):
        return self.run("pr", "diff", str(n), "-R", f"{owner}/{repo}", "--color", "never")

    def file_at(self, owner, repo, path, ref):
        try:
            return self.run("api", f"repos/{owner}/{repo}/contents/{path}?ref={ref}", "-H", "Accept: application/vnd.github.raw")
        except GhError as e:
            if e.not_found:
                return None
            raise

    def tree(self, owner, repo, sha):
        return self.run("api", f"repos/{owner}/{repo}/git/trees/{sha}?recursive=1", "--jq", ".tree[].path")

    def compare(self, owner, repo, base, head):
        try:
            return self.json(
                "api", f"repos/{owner}/{repo}/compare/{base}...{head}", "--jq", "{status, files: [.files[]?.filename]}",
            )
        except GhError as e:
            if e.not_found:
                return None
            raise

    def compare_diff(self, owner, repo, base, head):
        return self.run("api", f"repos/{owner}/{repo}/compare/{base}...{head}", "-H", "Accept: application/vnd.github.diff")

    def checks(self, owner, repo, n):
        args = ["pr", "checks", str(n), "-R", f"{owner}/{repo}", "--json", "name,bucket,state,workflow"]
        p = self.raw(*args)
        out = p.stdout.strip()
        if out.startswith("["):
            return json.loads(out)
        if "no checks reported" in p.stderr:
            return []
        raise GhError(args, p.returncode, p.stderr)

    def review_threads(self, owner, repo, n):
        nodes, after = [], None
        while True:
            args = ["api", "graphql", "-f", f"query={THREADS_QUERY}", "-f", f"owner={owner}", "-f", f"name={repo}", "-F", f"n={n}"]
            if after:
                args += ["-f", f"after={after}"]
            page = self.json(*args)["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return nodes
            after = page["pageInfo"]["endCursor"]

    def reviews(self, owner, repo, n):
        return self.json_lines(
            "api", f"repos/{owner}/{repo}/pulls/{n}/reviews", "--paginate",
            "--jq", ".[] | {id, user: .user.login, state, commit_id, body}",
        )

    def post_review(self, owner, repo, n, payload_path):
        return self.json("api", "-X", "POST", f"repos/{owner}/{repo}/pulls/{n}/reviews", "--input", str(payload_path))

    def approve(self, owner, repo, n, sha, body):
        return self.json(
            "api", "-X", "POST", f"repos/{owner}/{repo}/pulls/{n}/reviews",
            "-f", "event=APPROVE", "-f", f"commit_id={sha}", "-f", f"body={body}",
        )

    def reply_and_resolve(self, thread_id, body):
        self.json("api", "graphql", "-f", f"query={REPLY_MUTATION}", "-f", f"id={thread_id}", "-f", f"body={body}")
        self.json("api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-f", f"id={thread_id}")
