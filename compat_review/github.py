"""GitHub as a GitHub App: installation tokens and the few REST calls the service makes."""

import base64
import json
import time
import urllib.error
import urllib.request

API = "https://api.github.com"


def app_jwt(app_id, private_key_pem):
    import jwt  # PyJWT[crypto]

    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, private_key_pem, algorithm="RS256")


class GitHubError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GitHub:
    """REST calls with one token: an installation token, or a personal token for a trial."""

    def __init__(self, token, api=API):
        self.token = token
        self.api = api
        self.minted_at = None

    @staticmethod
    def _as_app(method, path, app_id, private_key_pem, api=API):
        """One call authenticated as the App itself (a JWT), not as an installation."""
        req = urllib.request.Request(api + path, method=method, data=b"{}" if method == "POST" else None,
                                     headers={"Authorization": f"Bearer {app_jwt(app_id, private_key_pem)}",
                                              "Accept": "application/vnd.github+json",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise GitHubError(f"{method} {path}: {e.code} {e.read().decode()[:300]}") from None

    @classmethod
    def as_installation(cls, app_id, private_key_pem, installation_id, api=API):
        """Mint an installation access token (valid for an hour)."""
        tok = cls._as_app("POST", f"/app/installations/{installation_id}/access_tokens", app_id, private_key_pem, api)
        gh = cls(tok["token"], api)
        gh.minted_at = time.time()
        gh.expires_at = tok.get("expires_at")
        return gh

    @classmethod
    def installation_id(cls, app_id, private_key_pem, owner, repo, api=API):
        """The App's installation on a repository named on the command line rather than in an event."""
        try:
            return cls._as_app("GET", f"/repos/{owner}/{repo}/installation", app_id, private_key_pem, api)["id"]
        except GitHubError as e:
            raise GitHubError(f"the App is not installed on {owner}/{repo} ({e})") from None

    def stale(self):
        """An installation token older than 50 minutes (they last an hour); a personal token never."""
        return self.minted_at is not None and time.time() - self.minted_at > 50 * 60

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"}

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, method=method, data=data, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            raise GitHubError(f"{method} {path}: {e.code} {e.read().decode()[:300]}", status=e.code) from None

    def get_if_changed(self, path, etag=None):
        """(data, etag) for a GET; (None, etag) when the resource is unchanged since `etag`. GitHub
        answers that with 304 and does not count it against the rate limit."""
        headers = self._headers()
        if etag:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(self.api + path, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode()
                return (json.loads(text) if text else {}), r.headers.get("ETag")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, etag
            raise GitHubError(f"GET {path}: {e.code} {e.read().decode()[:300]}", status=e.code) from None

    def git_auth_header(self):
        """`http.extraheader` value so git can fetch a private repository with this token."""
        return "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{self.token}".encode()).decode()

    def pull(self, owner, repo, number):
        return self.request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    def open_pulls(self, owner, repo):
        """Every open pull request, newest first; one request per hundred."""
        out, page = [], 1
        while True:
            batch = self.request("GET", f"/repos/{owner}/{repo}/pulls?state=open&per_page=100&page={page}")
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def update_pull_body(self, owner, repo, number, body):
        return self.request("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", {"body": body})

    def create_comment(self, owner, repo, number, body):
        return self.request("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})

    def comments(self, owner, repo, number):
        return self.request("GET", f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=100")

    def reactions(self, owner, repo, comment_id):
        return self.request("GET", f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions?per_page=100")
