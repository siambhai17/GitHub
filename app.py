"""
GitHub Manager — Flask web app
================================
Login with a GitHub username + Personal Access Token (PAT), then:
  * create new repositories
  * browse repository contents
  * upload / delete files
  * upload a ZIP archive -> it is unzipped and pushed to the repo

All GitHub interaction is done through the REST API v3 using `requests`.
The PAT is stored only in the Flask session (signed cookie) and is never
written to disk.
"""

import base64
import io
import os
import zipfile
from functools import wraps

import requests
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
API = "https://api.github.com"

app = Flask(__name__)
# In production set a real secret via the environment variable.
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production-please")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB upload limit


# --------------------------------------------------------------------------- #
# GitHub API helpers
# --------------------------------------------------------------------------- #
def gh_headers(token):
    """Return the HTTP headers required for an authenticated GitHub call."""
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


class GitHubError(Exception):
    """Raised when the GitHub API returns an error we want to surface."""


def _check(resp):
    """Raise a friendly GitHubError if the response is not successful."""
    if resp.status_code >= 400:
        try:
            msg = resp.json().get("message", resp.text)
        except Exception:
            msg = resp.text
        raise GitHubError(f"{resp.status_code}: {msg}")
    return resp


def get_user(token):
    """Return the authenticated user's profile (login, name, avatar)."""
    r = requests.get(f"{API}/user", headers=gh_headers(token), timeout=30)
    _check(r)
    return r.json()


def list_repos(token):
    """Return all repositories the user can push to (owner, collaborator, org)."""
    repos = []
    page = 1
    while True:
        r = requests.get(
            f"{API}/user/repos",
            headers=gh_headers(token),
            params={
                "per_page": 100,
                "page": page,
                "sort": "updated",
                "affiliation": "owner,collaborator,organization_member",
            },
            timeout=30,
        )
        _check(r)
        batch = r.json()
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def create_repo(token, name, description="", private=False, auto_init=True):
    """Create a new repository for the authenticated user."""
    payload = {
        "name": name,
        "description": description,
        "private": private,
        "auto_init": auto_init,
    }
    r = requests.post(
        f"{API}/user/repos", headers=gh_headers(token), json=payload, timeout=30
    )
    _check(r)
    return r.json()


def get_repo(token, owner, repo):
    r = requests.get(
        f"{API}/repos/{owner}/{repo}", headers=gh_headers(token), timeout=30
    )
    _check(r)
    return r.json()


def get_contents(token, owner, repo, path=""):
    """List the contents of a directory (or return a single file's metadata)."""
    r = requests.get(
        f"{API}/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(token),
        timeout=30,
    )
    if r.status_code == 404:
        return []
    _check(r)
    data = r.json()
    if isinstance(data, dict):  # a single file
        return [data]
    return data


def get_file_sha(token, owner, repo, path, branch=None):
    """Return the blob sha of an existing file, or None if it does not exist."""
    params = {"ref": branch} if branch else {}
    r = requests.get(
        f"{API}/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(token),
        params=params,
        timeout=30,
    )
    if r.status_code == 404:
        return None
    _check(r)
    return r.json().get("sha")


def put_file(token, owner, repo, path, content_bytes, message, branch=None):
    """Create or update a single file through the Contents API."""
    sha = get_file_sha(token, owner, repo, path, branch)
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    if branch:
        payload["branch"] = branch
    if sha:
        payload["sha"] = sha
    r = requests.put(
        f"{API}/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(token),
        json=payload,
        timeout=60,
    )
    _check(r)
    return r.json()


def delete_file(token, owner, repo, path, message, branch=None):
    """Delete a single file through the Contents API."""
    sha = get_file_sha(token, owner, repo, path, branch)
    if not sha:
        raise GitHubError(f"File not found: {path}")
    payload = {"message": message, "sha": sha}
    if branch:
        payload["branch"] = branch
    r = requests.delete(
        f"{API}/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(token),
        json=payload,
        timeout=60,
    )
    _check(r)
    return r.json()


def push_files(token, owner, repo, files, message, branch=None):
    """
    Push many files in a single commit using the Git Data API.

    `files` is a dict of {relative_path: bytes}. This is far more efficient
    than the Contents API for a ZIP containing many files.
    """
    h = gh_headers(token)

    # 1. Resolve the target branch and its current commit / tree.
    repo_info = get_repo(token, owner, repo)
    if branch is None:
        branch = repo_info["default_branch"]

    parent_sha = None
    base_tree = None
    r = requests.get(
        f"{API}/repos/{owner}/{repo}/git/ref/heads/{branch}", headers=h, timeout=30
    )
    if r.status_code == 200:
        parent_sha = r.json()["object"]["sha"]
        rc = requests.get(
            f"{API}/repos/{owner}/{repo}/git/commits/{parent_sha}",
            headers=h,
            timeout=30,
        )
        _check(rc)
        base_tree = rc.json()["tree"]["sha"]

    # 2. Create a blob for every file.
    tree_items = []
    for path, content in files.items():
        rb = requests.post(
            f"{API}/repos/{owner}/{repo}/git/blobs",
            headers=h,
            json={
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
            timeout=60,
        )
        _check(rb)
        tree_items.append(
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": rb.json()["sha"],
            }
        )

    # 3. Create a tree (on top of the existing tree so nothing is lost).
    tree_payload = {"tree": tree_items}
    if base_tree:
        tree_payload["base_tree"] = base_tree
    rt = requests.post(
        f"{API}/repos/{owner}/{repo}/git/trees",
        headers=h,
        json=tree_payload,
        timeout=60,
    )
    _check(rt)
    new_tree = rt.json()["sha"]

    # 4. Create the commit.
    commit_payload = {"message": message, "tree": new_tree}
    if parent_sha:
        commit_payload["parents"] = [parent_sha]
    rcm = requests.post(
        f"{API}/repos/{owner}/{repo}/git/commits",
        headers=h,
        json=commit_payload,
        timeout=60,
    )
    _check(rcm)
    new_commit = rcm.json()["sha"]

    # 5. Point the branch at the new commit.
    if parent_sha:
        rr = requests.patch(
            f"{API}/repos/{owner}/{repo}/git/refs/heads/{branch}",
            headers=h,
            json={"sha": new_commit, "force": False},
            timeout=30,
        )
    else:
        rr = requests.post(
            f"{API}/repos/{owner}/{repo}/git/refs",
            headers=h,
            json={"ref": f"refs/heads/{branch}", "sha": new_commit},
            timeout=30,
        )
    _check(rr)
    return new_commit


# --------------------------------------------------------------------------- #
# ZIP helpers
# --------------------------------------------------------------------------- #
def extract_zip(file_bytes, strip_root=False):
    """
    Read a ZIP archive from bytes and return {relative_path: bytes}.

    Directories and common OS metadata files are skipped. If `strip_root`
    is True and every entry shares a single top-level folder, that folder is
    removed from the paths (handy for archives exported from GitHub).
    """
    files = {}
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        names = zf.namelist()

        # Keep only the entries we actually care about.
        def _keep(n):
            if n.endswith("/"):
                return False
            base = n.split("/")[-1]
            if "__MACOSX" in n or base in (".DS_Store", "Thumbs.db"):
                return False
            return bool(n.strip("/"))

        kept = [n for n in names if _keep(n)]

        # Optionally compute a common top-level folder to strip.
        prefix = ""
        if strip_root and kept:
            tops = {n.split("/", 1)[0] for n in kept}
            if len(tops) == 1:
                prefix = tops.pop() + "/"

        for name in kept:
            rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
            if not rel:
                continue
            files[rel] = zf.read(name)
    return files


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("token"):
            flash("Please log in first.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def current_token():
    return session.get("token")


# Human-friendly file type label derived from the extension.
_EXT_KINDS = {
    "py": "Python", "js": "JavaScript", "ts": "TypeScript", "jsx": "React",
    "tsx": "React", "html": "HTML", "htm": "HTML", "css": "CSS",
    "scss": "SCSS", "json": "JSON", "md": "Markdown", "txt": "Text",
    "yml": "YAML", "yaml": "YAML", "xml": "XML", "sh": "Shell",
    "java": "Java", "c": "C", "cpp": "C++", "h": "Header", "go": "Go",
    "rs": "Rust", "rb": "Ruby", "php": "PHP", "sql": "SQL", "csv": "CSV",
    "png": "Image", "jpg": "Image", "jpeg": "Image", "gif": "Image",
    "svg": "Image", "webp": "Image", "ico": "Image", "pdf": "PDF",
    "zip": "Archive", "tar": "Archive", "gz": "Archive", "lock": "Lock",
    "env": "Env", "ini": "Config", "toml": "Config", "cfg": "Config",
}


def file_kind(name):
    if "." not in name:
        return "File"
    ext = name.rsplit(".", 1)[-1].lower()
    return _EXT_KINDS.get(ext, ext.upper() + " file" if ext else "File")


def session_stats(repo_count=0):
    """Return the counters shown on the dashboard / settings pages."""
    return {
        "repos": repo_count,
        "files": session.get("files_uploaded", 0),
        "zips": session.get("zip_pushes", 0),
    }


def enrich_items(items):
    """Add a `kind` label and `download_url` fallback to content items."""
    for it in items:
        if it.get("type") == "file":
            it["kind"] = file_kind(it["name"])
            it.setdefault(
                "download_url",
                it.get("download_url")
                or it.get("html_url", "").replace("github.com", "raw.githubusercontent.com")
                .replace("/blob/", "/"),
            )
    return items


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/favicon.ico")
def favicon():
    # No image assets are used anywhere in this project (all icons are pure CSS),
    # so we simply return an empty 204 to avoid 404 noise in the logs.
    return ("", 204)


@app.route("/")
def index():
    if session.get("token"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        token = request.form.get("token", "").strip()
        username = request.form.get("username", "").strip()
        if not token:
            flash("A Personal Access Token is required.", "error")
            return render_template("login.html")

        try:
            user = get_user(token)
        except GitHubError as exc:
            flash(f"Login failed — {exc}", "error")
            return render_template("login.html")
        except requests.RequestException as exc:
            flash(f"Network error: {exc}", "error")
            return render_template("login.html")

        # If a username was supplied, make sure it matches the token owner.
        if username and username.lower() != user["login"].lower():
            flash(
                f"Warning: the token belongs to '{user['login']}', "
                f"not '{username}'. Logging in as '{user['login']}'.",
                "warning",
            )

        session["token"] = token
        session["login"] = user["login"]
        session["name"] = user.get("name") or user["login"]
        session.setdefault("files_uploaded", 0)
        session.setdefault("zip_pushes", 0)
        flash(f"Welcome, {user['login']}!", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    try:
        repos = list_repos(current_token())
    except GitHubError as exc:
        flash(f"Could not load repositories — {exc}", "error")
        repos = []
    return render_template(
        "dashboard.html", repos=repos, stats=session_stats(len(repos))
    )


@app.route("/repositories")
@login_required
def repositories():
    try:
        repos = list_repos(current_token())
    except GitHubError as exc:
        flash(f"Could not load repositories — {exc}", "error")
        repos = []
    return render_template(
        "repositories.html", repos=repos, stats=session_stats(len(repos))
    )


@app.route("/create")
@login_required
def create_page():
    return render_template("create.html", stats=session_stats())


@app.route("/settings")
@login_required
def settings():
    try:
        repo_count = len(list_repos(current_token()))
    except GitHubError:
        repo_count = 0
    return render_template("settings.html", stats=session_stats(repo_count))


@app.route("/create-repo", methods=["POST"])
@login_required
def create_repo_route():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    private = request.form.get("private") == "on"
    auto_init = request.form.get("auto_init") == "on"

    if not name:
        flash("Repository name is required.", "error")
        return redirect(url_for("dashboard"))

    try:
        repo = create_repo(
            current_token(), name, description, private, auto_init
        )
        flash(f"Repository '{repo['full_name']}' created.", "success")
        return redirect(
            url_for("repo_view", owner=repo["owner"]["login"], repo=repo["name"])
        )
    except GitHubError as exc:
        flash(f"Could not create repository — {exc}", "error")
        return redirect(url_for("dashboard"))


@app.route("/repo/<owner>/<repo>")
@login_required
def repo_view(owner, repo):
    path = request.args.get("path", "")
    try:
        info = get_repo(current_token(), owner, repo)
        items = get_contents(current_token(), owner, repo, path)
    except GitHubError as exc:
        flash(f"Could not open repository — {exc}", "error")
        return redirect(url_for("dashboard"))

    # Sort: directories first, then files, alphabetically.
    items.sort(key=lambda i: (i["type"] != "dir", i["name"].lower()))
    items = enrich_items(items)

    # Build breadcrumb parts for navigation.
    crumbs = []
    acc = ""
    for part in [p for p in path.split("/") if p]:
        acc = f"{acc}/{part}" if acc else part
        crumbs.append({"name": part, "path": acc})

    return render_template(
        "repo.html",
        owner=owner,
        repo=repo,
        info=info,
        items=items,
        path=path,
        crumbs=crumbs,
    )


@app.route("/repo/<owner>/<repo>/upload", methods=["POST"])
@login_required
def upload_files(owner, repo):
    path = request.form.get("path", "").strip("/")
    uploaded = request.files.getlist("files")
    message = request.form.get("message") or "Upload files via GitHub Manager"

    saved = 0
    errors = []
    for f in uploaded:
        if not f or not f.filename:
            continue
        rel = f"{path}/{f.filename}" if path else f.filename
        try:
            put_file(
                current_token(), owner, repo, rel, f.read(), message
            )
            saved += 1
        except GitHubError as exc:
            errors.append(f"{f.filename}: {exc}")

    if saved:
        session["files_uploaded"] = session.get("files_uploaded", 0) + saved
        flash(f"Uploaded {saved} file(s).", "success")
    for e in errors:
        flash(f"Failed — {e}", "error")
    if not saved and not errors:
        flash("No files selected.", "error")

    return redirect(url_for("repo_view", owner=owner, repo=repo, path=path))


@app.route("/repo/<owner>/<repo>/delete", methods=["POST"])
@login_required
def delete_file_route(owner, repo):
    path = request.form.get("path", "").strip("/")
    message = request.form.get("message") or f"Delete {path} via GitHub Manager"
    if not path:
        flash("No file specified.", "error")
        return redirect(url_for("repo_view", owner=owner, repo=repo))

    try:
        delete_file(current_token(), owner, repo, path, message)
        flash(f"Deleted '{path}'.", "success")
    except GitHubError as exc:
        flash(f"Could not delete — {exc}", "error")

    parent = os.path.dirname(path)
    return redirect(url_for("repo_view", owner=owner, repo=repo, path=parent))


@app.route("/repo/<owner>/<repo>/upload-zip", methods=["POST"])
@login_required
def upload_zip(owner, repo):
    path = request.form.get("path", "").strip("/")
    strip_root = request.form.get("strip_root") == "on"
    message = request.form.get("message") or "Upload ZIP via GitHub Manager"
    zf = request.files.get("zipfile")

    if not zf or not zf.filename:
        flash("Please choose a .zip file.", "error")
        return redirect(url_for("repo_view", owner=owner, repo=repo, path=path))

    if not zf.filename.lower().endswith(".zip"):
        flash("Only .zip archives are supported.", "error")
        return redirect(url_for("repo_view", owner=owner, repo=repo, path=path))

    try:
        files = extract_zip(zf.read(), strip_root=strip_root)
    except zipfile.BadZipFile:
        flash("The uploaded file is not a valid ZIP archive.", "error")
        return redirect(url_for("repo_view", owner=owner, repo=repo, path=path))

    if not files:
        flash("The ZIP archive is empty.", "error")
        return redirect(url_for("repo_view", owner=owner, repo=repo, path=path))

    # Prefix with the target sub-directory if one was selected.
    if path:
        files = {f"{path}/{k}": v for k, v in files.items()}

    try:
        push_files(current_token(), owner, repo, files, message)
        session["zip_pushes"] = session.get("zip_pushes", 0) + 1
        session["files_uploaded"] = session.get("files_uploaded", 0) + len(files)
        flash(
            f"Unzipped and pushed {len(files)} file(s) in one commit.",
            "success",
        )
    except GitHubError as exc:
        flash(f"Could not push archive — {exc}", "error")

    return redirect(url_for("repo_view", owner=owner, repo=repo, path=path))


@app.errorhandler(413)
def too_large(_e):
    flash("File too large (max 100 MB).", "error")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=False, use_reloader=False)
