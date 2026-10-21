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
    pass


class GitHub:
    """REST calls with one token: an installation token, or a personal token for a trial."""

    def __init__(self, token, api=API):
        self.token = token
        self.api = api

    @classmethod
    def as_installation(cls, app_id, private_key_pem, installation_id, api=API):
        """Mint an installation access token (valid for an hour)."""
        req = urllib.request.Request(
            f"{api}/app/installations/{installation_id}/access_tokens", method="POST", data=b"{}",
            headers={"Authorization": f"Bearer {app_jwt(app_id, private_key_pem)}",
                     "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.load(r)
        gh = cls(tok["token"], api)
        gh.expires_at = tok.get("expires_at")
        return gh

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, method=method, data=data, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode()
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as e:
            raise GitHubError(f"{method} {path}: {e.code} {e.read().decode()[:300]}") from None

    def git_auth_header(self):
        """`http.extraheader` value so git can fetch a private repository with this token."""
        return "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{self.token}".encode()).decode()

    def pull(self, owner, repo, number):
        return self.request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    def update_pull_body(self, owner, repo, number, body):
        return self.request("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", {"body": body})

    def create_comment(self, owner, repo, number, body):
        return self.request("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})

    def comments(self, owner, repo, number):
        return self.request("GET", f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=100")

    def reactions(self, owner, repo, comment_id):
        return self.request("GET", f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions?per_page=100")
