"""
AyTree module backend — legacy file browser power layer for Artifact Scanner.

Ported from AyTree's aytree_server (tree scan · notes · extend) as pure handlers
so win_serve can mount them under /api/aytree/* without a second process.

UI SSOT: experimental/aytree/index_tree.html (green gold palette — keep it).
Feature work lands here + the HTML; derivation/radial are intentionally out of scope.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

# Module home (this package dir under experimental/)
MODULE_DIR = Path(__file__).resolve().parent
# Notes live with the scanner's app data so reloads / rebuilds don't wipe them.
_HOME = Path(os.environ.get("USERPROFILE") or Path.home())
NOTES_FILE = Path(
    os.environ.get("AYTREE_NOTES")
    or (_HOME / ".artifact-scanner" / "aytree_notes.json")
)

# Windows: git.exe flashes a console per call unless CREATE_NO_WINDOW is set.
# AyTree polls /api/aytree/tree every 10s and runs git once per repo → spam.
_SUBPROCESS_NO_WINDOW: dict[str, Any] = {}
if sys.platform == "win32":
    _SUBPROCESS_NO_WINDOW["creationflags"] = getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000
    )

# Scan root: prefer env, else ~/dev (this machine's primary dev drive sibling).
# Same "scan my siblings" intent as standalone AyTree under C:\Users\bardw\dev\AyTree.
def _default_scan_root() -> Path:
    env = os.environ.get("AYTREE_SCAN_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    candidates = [
        Path(r"C:\Users\bardw\dev"),
        Path(r"C:\dev"),
        _HOME / "dev",
        _HOME,
    ]
    for c in candidates:
        if c.is_dir():
            return c.resolve()
    return _HOME.resolve()


SCAN_ROOT = _default_scan_root()

IGNORED_DIRS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    ".vscode",
    ".idea",
    ".gemini",
    "obj",
    "bin",
    "fooocus_env",
}
IGNORED_FILES = {".DS_Store", "desktop.ini", "thumbs.db", ".aytree_notes.json"}

# Path-segment origin: user-requested vs agent-suggested (see jwrangle/YOU-AGENT.md).
# Nearest matching segment wins (leaf side).
_ORIGIN_YOU = frozenset({"you", "user", "user-requested"})
_ORIGIN_AGENT = frozenset({"agent", "agent-suggested", "suggested"})


def path_origin(path: str | Path | None) -> str:
    """Return 'you' | 'agent' | '' from path folder names."""
    if not path:
        return ""
    try:
        parts = Path(path).parts
    except (TypeError, ValueError):
        return ""
    for part in reversed(parts):
        low = part.lower()
        if low in _ORIGIN_AGENT:
            return "agent"
        if low in _ORIGIN_YOU:
            return "you"
    return ""


_notes_lock = threading.Lock()


def _under_scan_root(path: str | Path) -> bool:
    """True if path is SCAN_ROOT or a child (Windows-safe)."""
    try:
        p = Path(path).resolve()
        root = SCAN_ROOT.resolve()
        return p == root or root in p.parents
    except OSError:
        return False


def load_notes_db() -> dict[str, Any]:
    with _notes_lock:
        if NOTES_FILE.is_file():
            try:
                return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return {"notes": {}, "virtual_nodes": {}}


def save_notes_db(db: dict[str, Any]) -> bool:
    with _notes_lock:
        try:
            NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
            NOTES_FILE.write_text(
                json.dumps(db, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return True
        except OSError:
            return False


def get_git_branches(repo_path: str) -> dict[str, Any]:
    branches: dict[str, Any] = {"local": [], "remote": [], "current": None}
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "branch", "-a"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
            **_SUBPROCESS_NO_WINDOW,
        )
        if result.returncode != 0:
            return branches
        for line in result.stdout.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            is_current = line.startswith("*")
            name = line_stripped.replace("*", "").strip()
            if "-> " in name:
                continue
            if name.startswith("remotes/"):
                branches["remote"].append(name[len("remotes/") :])
            else:
                branches["local"].append(name)
                if is_current:
                    branches["current"] = name
    except OSError:
        pass
    return branches


def _clean_branch_ref(name: str) -> str:
    """Strip worktree markers and noise from a branch display name → git ref."""
    n = (name or "").strip()
    # `git branch` can show `+ branch` for a worktree-checked-out branch
    if n.startswith("+ "):
        n = n[2:].strip()
    if n.startswith("* "):
        n = n[2:].strip()
    return n


def _git(repo: str, *args: str, timeout: float = 12.0) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["git", "-C", repo, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            **_SUBPROCESS_NO_WINDOW,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out"
    except OSError as e:
        return 127, "", str(e)


def _resolve_git_repo(path: str) -> Path | None:
    """Accept a worktree or main checkout that has a .git file/dir."""
    try:
        p = Path(path).expanduser().resolve()
    except OSError:
        return None
    if not p.is_dir():
        return None
    git = p / ".git"
    if git.exists():
        return p
    return None


def get_branch_preview(
    repo: str,
    ref: str,
    base: str | None = None,
    *,
    max_commits: int = 12,
    max_patch_bytes: int = 72_000,
) -> dict[str, Any]:
    """
    Quick sneak-peek of a branch (esp. remote) vs base (default HEAD).

    Returns tip metadata, ahead/behind, recent commits, --stat, and a truncated
    unified diff. Read-only — never checks out or fetches.
    """
    repo_path = _resolve_git_repo(repo or "")
    if repo_path is None:
        return {"ok": False, "error": "not a git repo", "repo": repo}
    ref_clean = _clean_branch_ref(ref or "")
    if not ref_clean or any(c in ref_clean for c in ("\n", "\r", "\x00")):
        return {"ok": False, "error": "missing or invalid ref", "repo": str(repo_path)}

    # Prefer remotes/X when user passed origin/foo and only remotes form resolves
    candidates = [ref_clean]
    if not ref_clean.startswith("remotes/") and "/" in ref_clean:
        candidates.append(f"remotes/{ref_clean}")
    resolved_ref = None
    tip_sha = None
    for cand in candidates:
        code, out, err = _git(str(repo_path), "rev-parse", "--verify", cand)
        if code == 0 and out.strip():
            resolved_ref = cand
            tip_sha = out.strip()
            break
    if not resolved_ref or not tip_sha:
        return {
            "ok": False,
            "error": "ref not found locally (fetch may be needed)",
            "repo": str(repo_path),
            "ref": ref_clean,
        }

    asked = (base or "").strip()
    # HTML used to hardcode base=HEAD. On the current branch that yields an
    # empty peek (same SHA). First-time "diff" looks broken. Fall through to
    # the GitHub compare trunk (origin/HEAD · main · master).
    if not asked or asked.upper() == "HEAD":
        base_ref = default_compare_ref(str(repo_path))
    else:
        base_ref = asked
    code_b, base_sha, err_b = _git(str(repo_path), "rev-parse", "--verify", base_ref)
    if code_b != 0 or not base_sha.strip():
        return {
            "ok": False,
            "error": f"base not found: {base_ref}",
            "repo": str(repo_path),
            "ref": resolved_ref,
        }
    base_sha = base_sha.strip()

    # Tip subject / author / date
    code_t, tip_meta, _ = _git(
        str(repo_path),
        "log",
        "-1",
        "--format=%H%n%h%n%s%n%an%n%ar%n%ci",
        tip_sha,
    )
    tip = {}
    if code_t == 0 and tip_meta.strip():
        parts = tip_meta.strip().split("\n")
        while len(parts) < 6:
            parts.append("")
        tip = {
            "sha": parts[0],
            "short": parts[1],
            "subject": parts[2],
            "author": parts[3],
            "rel_date": parts[4],
            "date": parts[5],
        }

    # ahead / behind: left=base, right=ref  →  behind  ahead
    code_ab, ab_out, _ = _git(
        str(repo_path), "rev-list", "--left-right", "--count", f"{base_sha}...{tip_sha}"
    )
    behind, ahead = 0, 0
    if code_ab == 0 and ab_out.strip():
        bits = ab_out.strip().split()
        if len(bits) >= 2:
            try:
                behind, ahead = int(bits[0]), int(bits[1])
            except ValueError:
                pass

    # Recent commits on ref not in base (symmetric range for peek)
    n = max(1, min(int(max_commits or 12), 40))
    code_log, log_out, _ = _git(
        str(repo_path),
        "log",
        f"--max-count={n}",
        "--format=%h\t%ar\t%s",
        f"{base_sha}..{tip_sha}",
    )
    commits: list[dict[str, str]] = []
    if code_log == 0:
        for line in log_out.splitlines():
            if not line.strip():
                continue
            bits = line.split("\t", 2)
            commits.append(
                {
                    "short": bits[0] if bits else "",
                    "rel_date": bits[1] if len(bits) > 1 else "",
                    "subject": bits[2] if len(bits) > 2 else line,
                }
            )

    # File stat (merge-base triple-dot is usually the “what’s on the branch” view)
    code_st, stat_out, _ = _git(
        str(repo_path), "diff", "--stat", f"{base_sha}...{tip_sha}"
    )
    stat = stat_out if code_st == 0 else ""

    # Truncated patch
    code_d, diff_out, _ = _git(
        str(repo_path),
        "diff",
        "--no-color",
        f"{base_sha}...{tip_sha}",
        timeout=20.0,
    )
    patch = diff_out if code_d == 0 else ""
    truncated = False
    cap = max(4_000, int(max_patch_bytes or 72_000))
    if len(patch.encode("utf-8", errors="replace")) > cap:
        # cut on a line boundary near cap
        raw = patch.encode("utf-8", errors="replace")[:cap]
        patch = raw.decode("utf-8", errors="replace")
        if "\n" in patch:
            patch = patch.rsplit("\n", 1)[0] + "\n"
        truncated = True

    same = base_sha == tip_sha
    gh = github_urls_for_ref(str(repo_path), ref_clean, tip_sha=tip_sha)
    return {
        "ok": True,
        "repo": str(repo_path),
        "ref": resolved_ref,
        "ref_display": ref_clean,
        "base": base_ref,
        "base_sha": base_sha,
        "tip": tip,
        "ahead": ahead,
        "behind": behind,
        "same_as_base": same,
        "commits": commits,
        "stat": stat,
        "patch": patch,
        "patch_truncated": truncated,
        "commit_count_shown": len(commits),
        "github": gh,
    }


def _parse_github_remote(remote_url: str) -> str | None:
    """Return https://github.com/owner/repo or None."""
    u = (remote_url or "").strip()
    if not u:
        return None
    # git@github.com:owner/repo.git
    if u.startswith("git@") and "github.com" in u:
        try:
            path = u.split(":", 1)[1]
        except IndexError:
            return None
        path = path.removesuffix(".git").strip("/")
        if path.count("/") < 1:
            return None
        return f"https://github.com/{path}"
    # https://github.com/owner/repo.git  ·  ssh://git@github.com/owner/repo.git
    if "github.com" in u:
        rest = u
        for prefix in (
            "https://github.com/",
            "http://github.com/",
            "ssh://git@github.com/",
            "git://github.com/",
        ):
            if rest.lower().startswith(prefix):
                rest = rest[len(prefix) :]
                break
        else:
            # last resort: after github.com/
            idx = rest.lower().find("github.com/")
            if idx < 0:
                return None
            rest = rest[idx + len("github.com/") :]
        rest = rest.split("?")[0].split("#")[0]
        rest = rest.removesuffix(".git").strip("/")
        if rest.count("/") < 1:
            return None
        return f"https://github.com/{rest}"
    return None


def _branch_name_for_web(ref: str) -> str:
    """origin/foo · remotes/origin/foo → foo (path segment for GH)."""
    n = _clean_branch_ref(ref)
    if n.startswith("remotes/"):
        n = n[len("remotes/") :]
    if n.startswith("origin/"):
        n = n[len("origin/") :]
    # other remotes: remote/name → name if single slash after remote
    if "/" in n and not n.startswith("heads/"):
        # keep multi-segment branch names (feature/x); strip only first remote token
        # if it looks like remotes/origin/feature/x already cleaned to origin/feature/x
        parts = n.split("/", 1)
        if parts[0] in ("origin", "upstream", "github") and len(parts) == 2:
            n = parts[1]
    if n.startswith("heads/"):
        n = n[len("heads/") :]
    return n


def default_compare_ref(repo: str) -> str:
    """Trunk to diff against: origin/HEAD, else origin/main|master, else HEAD."""
    code, out, _ = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if code == 0 and out.strip():
        return out.strip()  # refs/remotes/origin/main
    for cand in ("origin/main", "origin/master", "main", "master"):
        c, _, _ = _git(repo, "rev-parse", "--verify", cand)
        if c == 0:
            return cand
    return "HEAD"


def github_urls_for_ref(
    repo: str, ref: str, *, tip_sha: str | None = None
) -> dict[str, Any]:
    """
    Build GitHub browse/compare URLs for a local or remote-tracking ref.
    No network — reads origin remote only.
    """
    repo_path = _resolve_git_repo(repo or "")
    if repo_path is None:
        return {"ok": False, "error": "not a git repo"}
    code, out, err = _git(str(repo_path), "remote", "get-url", "origin")
    if code != 0 or not (out or "").strip():
        # try first remote
        code2, remotes, _ = _git(str(repo_path), "remote")
        first = (remotes or "").splitlines()[0].strip() if remotes else ""
        if first:
            code, out, err = _git(str(repo_path), "remote", "get-url", first)
        if code != 0 or not (out or "").strip():
            return {
                "ok": False,
                "error": "no origin remote (or remote URL)",
                "remote": None,
            }
    remote_url = out.strip()
    base = _parse_github_remote(remote_url)
    if not base:
        return {
            "ok": False,
            "error": "remote is not GitHub",
            "remote": remote_url,
        }
    branch = _branch_name_for_web(ref)
    # default branch for compare left side
    code_d, def_out, _ = _git(
        str(repo_path), "symbolic-ref", "refs/remotes/origin/HEAD"
    )
    default_branch = "main"
    if code_d == 0 and def_out.strip():
        # refs/remotes/origin/main
        default_branch = def_out.strip().rsplit("/", 1)[-1] or "main"
    else:
        for cand in ("main", "master"):
            c, _, _ = _git(str(repo_path), "rev-parse", "--verify", f"origin/{cand}")
            if c == 0:
                default_branch = cand
                break
    from urllib.parse import quote

    branch_q = quote(branch, safe="/")
    tree_url = f"{base}/tree/{branch_q}"
    compare_url = f"{base}/compare/{quote(default_branch, safe='')}...{branch_q}"
    commit_url = f"{base}/commit/{tip_sha}" if tip_sha else None
    return {
        "ok": True,
        "remote": remote_url,
        "base": base,
        "branch": branch,
        "default_branch": default_branch,
        "tree_url": tree_url,
        "compare_url": compare_url,
        "commit_url": commit_url,
        "url": compare_url,  # primary open target: diffs on GitHub
    }


def open_url_in_chrome(url: str) -> dict[str, Any]:
    """Launch Google Chrome with url (Windows-first). Fallback: start chrome / default."""
    u = (url or "").strip()
    if not u or any(c in u for c in ("\n", "\r", "\x00")):
        return {"ok": False, "error": "missing or invalid url"}
    if not (u.startswith("https://") or u.startswith("http://")):
        return {"ok": False, "error": "only http(s) urls"}
    chrome_candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
    ]
    for c in chrome_candidates:
        if c.is_file():
            try:
                subprocess.Popen(
                    [str(c), u],
                    close_fds=True,
                    **_SUBPROCESS_NO_WINDOW,
                )
                return {"ok": True, "browser": "chrome", "path": str(c), "url": u}
            except OSError as e:
                return {"ok": False, "error": str(e), "path": str(c)}
    # start chrome via cmd association
    if sys.platform == "win32":
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", "chrome", u],
                close_fds=True,
                **_SUBPROCESS_NO_WINDOW,
            )
            return {"ok": True, "browser": "chrome-start", "url": u}
        except OSError as e:
            return {"ok": False, "error": str(e)}
    try:
        import webbrowser

        webbrowser.open(u)
        return {"ok": True, "browser": "webbrowser", "url": u}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def build_tree_recursive(
    current_path: str,
    notes_db: dict[str, Any],
    depth: int = 0,
    max_depth: int = 6,
    *,
    root: str | None = None,
) -> dict[str, Any]:
    scan_root = root if root is not None else str(SCAN_ROOT)
    name = os.path.basename(current_path) or current_path
    try:
        rel_path = os.path.relpath(current_path, scan_root).replace("\\", "/")
    except ValueError:
        # Different drive on Windows — use basename-relative fallthrough
        rel_path = name
    if rel_path == ".":
        rel_path = ""

    origin = path_origin(current_path)
    node: dict[str, Any] = {
        "name": name,
        "path": current_path,
        "rel_path": rel_path,
        "type": "directory",
        "notes": notes_db.get("notes", {}).get(rel_path, {}).get("notes", ""),
        "status": notes_db.get("notes", {}).get(rel_path, {}).get("status", ""),
        "origin": origin,
        "children": [],
    }

    is_git_repo = os.path.exists(os.path.join(current_path, ".git"))
    if is_git_repo:
        node["type"] = "repository"
        node["is_git"] = True
        git_info = get_git_branches(current_path)
        node["current_branch"] = git_info["current"]

        branches_node: dict[str, Any] = {
            "name": "Branches",
            "type": "branch_group",
            "repo_path": current_path,
            "children": [],
        }
        for lb in git_info["local"]:
            b_key = f"{name}::local::{lb}"
            branches_node["children"].append(
                {
                    "name": lb,
                    "type": "branch_local",
                    "is_current": lb == git_info["current"],
                    "key": b_key,
                    "repo_path": current_path,
                    "ref": _clean_branch_ref(lb),
                    "notes": notes_db.get("notes", {}).get(b_key, {}).get("notes", ""),
                    "status": notes_db.get("notes", {}).get(b_key, {}).get("status", ""),
                }
            )
        for rb in git_info["remote"]:
            b_key = f"{name}::remote::{rb}"
            branches_node["children"].append(
                {
                    "name": rb,
                    "type": "branch_remote",
                    "key": b_key,
                    "repo_path": current_path,
                    "ref": _clean_branch_ref(rb),
                    "notes": notes_db.get("notes", {}).get(b_key, {}).get("notes", ""),
                    "status": notes_db.get("notes", {}).get(b_key, {}).get("status", ""),
                }
            )
        if branches_node["children"]:
            node["children"].append(branches_node)

    virtual_key = rel_path if rel_path else "root"
    for vn in notes_db.get("virtual_nodes", {}).get(virtual_key, []):
        node["children"].append(
            {
                "id": vn.get("id"),
                "name": vn.get("name"),
                "type": "virtual_note",
                "notes": vn.get("notes", ""),
                "status": vn.get("status", ""),
            }
        )

    if depth >= max_depth:
        return node

    try:
        entries = sorted(
            list(os.scandir(current_path)),
            key=lambda e: (not e.is_dir(), e.name.lower()),
        )
        for entry in entries:
            if entry.is_dir():
                if entry.name in IGNORED_DIRS:
                    continue
                node["children"].append(
                    build_tree_recursive(
                        entry.path, notes_db, depth + 1, max_depth, root=scan_root
                    )
                )
            else:
                if entry.name in IGNORED_FILES:
                    continue
                try:
                    file_rel = os.path.relpath(entry.path, scan_root).replace("\\", "/")
                except ValueError:
                    file_rel = entry.name
                node["children"].append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "rel_path": file_rel,
                        "type": "file",
                        "notes": notes_db.get("notes", {}).get(file_rel, {}).get("notes", ""),
                        "status": notes_db.get("notes", {}).get(file_rel, {}).get("status", ""),
                        "origin": path_origin(entry.path),
                    }
                )
    except OSError:
        pass

    return node


def _resolve_tree_root(root: str | None) -> tuple[Path | None, str | None]:
    """Optional quick-folder root. Must exist and be a directory."""
    if root is None or str(root).strip() == "":
        return SCAN_ROOT, None
    try:
        p = Path(str(root).strip()).expanduser().resolve()
    except OSError as e:
        return None, f"bad path: {e}"
    if not p.is_dir():
        return None, f"not a directory: {p}"
    return p, None


def get_tree(root: str | None = None) -> dict[str, Any]:
    notes_db = load_notes_db()
    scan, err = _resolve_tree_root(root)
    if scan is None:
        return {"ok": False, "error": err or "invalid root", "scan_root": str(SCAN_ROOT)}
    tree = build_tree_recursive(str(scan), notes_db, depth=0, root=str(scan))
    tree["scan_root"] = str(scan)
    tree["default_scan_root"] = str(SCAN_ROOT)
    tree["module"] = "aytree"
    tree["ok"] = True
    return tree


def get_notes() -> dict[str, Any]:
    return load_notes_db().get("notes", {})


def post_notes(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    key = req.get("key")
    if not key:
        return 400, {"success": False, "error": "Missing 'key'"}
    is_virtual = bool(req.get("is_virtual", False))
    db = load_notes_db()

    if is_virtual:
        parent_key = req.get("parent_key", "root")
        virtual_list = db.setdefault("virtual_nodes", {}).setdefault(parent_key, [])
        updated = False
        for vn in virtual_list:
            if vn.get("id") == key:
                vn["name"] = req.get("name", vn["name"])
                vn["notes"] = req.get("notes", "")
                vn["status"] = req.get("status", "")
                updated = True
                break
        if not updated:
            virtual_list.append(
                {
                    "id": key,
                    "name": req.get("name", "New Milestone"),
                    "notes": req.get("notes", ""),
                    "status": req.get("status", ""),
                }
            )
    else:
        db.setdefault("notes", {})[key] = {
            "notes": req.get("notes", ""),
            "status": req.get("status", ""),
        }

    if save_notes_db(db):
        return 200, {"success": True}
    return 500, {"success": False, "error": "Failed to save database"}


def post_extend(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    action = req.get("action")
    root = str(SCAN_ROOT)

    if action == "create_folder":
        parent_path = req.get("parent_path") or root
        folder_name = req.get("folder_name")
        if not folder_name:
            return 400, {"success": False, "error": "Folder name is required"}
        folder_name = os.path.basename(str(folder_name))
        new_path = os.path.join(parent_path, folder_name)
        if not _under_scan_root(new_path):
            return 403, {"success": False, "error": "Access Denied: Path outside workspace"}
        try:
            os.makedirs(new_path, exist_ok=True)
        except OSError as e:
            return 500, {"success": False, "error": str(e)}
        return 200, {"success": True, "message": f"Created folder: {folder_name}"}

    if action == "create_branch":
        repo_path = req.get("repo_path")
        branch_name = req.get("branch_name")
        if not repo_path or not branch_name:
            return 400, {
                "success": False,
                "error": "Repository path and branch name are required",
            }
        if not _under_scan_root(repo_path):
            return 403, {"success": False, "error": "Access Denied: Path outside workspace"}
        if not os.path.exists(os.path.join(repo_path, ".git")):
            return 400, {"success": False, "error": "Not a valid Git repository"}
        if any(c in str(branch_name) for c in (" ", ";", "&", "|", "`", "$")):
            return 400, {"success": False, "error": "Invalid characters in branch name"}
        res = subprocess.run(
            ["git", "-C", repo_path, "branch", branch_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            **_SUBPROCESS_NO_WINDOW,
        )
        if res.returncode == 0:
            return 200, {
                "success": True,
                "message": f"Created branch '{branch_name}' successfully.",
            }
        return 400, {"success": False, "error": (res.stderr or "").strip() or "git failed"}

    if action == "delete_virtual":
        node_id = req.get("id")
        parent_key = req.get("parent_key", "root")
        if not node_id:
            return 400, {"success": False, "error": "ID is required"}
        db = load_notes_db()
        virtual_list = db.get("virtual_nodes", {}).get(parent_key, [])
        db.setdefault("virtual_nodes", {})[parent_key] = [
            vn for vn in virtual_list if vn.get("id") != node_id
        ]
        if save_notes_db(db):
            return 200, {"success": True, "message": "Virtual node deleted"}
        return 500, {"success": False, "error": "Failed to save database"}

    return 400, {"success": False, "error": f"Unknown action: {action}"}


# ── Quick access pins (Explorer-style rail) ────────────────────────────────
# Pins live in the same notes DB so they survive rebuilds and are shared by the
# tree + info windows. Unlike create_folder/create_branch these are bookkeeping
# only, so a pin may point anywhere on disk — out-of-root pins just lose the
# jump-in-tree affordance and open in Explorer instead.
PINS_MAX = 40


def _pin_short_name(path: str) -> str:
    """Leaf folder only — favorites never store long path twins."""
    p = str(path or "").rstrip("\\/")
    if not p:
        return "default"
    # Drive root
    if len(p) == 2 and p[1] == ":":
        return p.upper()
    # User home alone → ~
    low = p.replace("/", "\\")
    parts = [x for x in low.split("\\") if x]
    if len(parts) == 3 and parts[1].lower() == "users":
        return "~"
    if len(parts) == 3 and parts[0] == "home":
        return "~"
    leaf = os.path.basename(p) or p
    return leaf[:80]


def _pin_row(path: str, name: str = "") -> dict[str, Any]:
    p = str(path or "").strip().strip('"')
    try:
        resolved = str(Path(p).resolve())
    except OSError:
        resolved = p
    # Always short leaf; ignore long client labels (path twins)
    return {
        "path": resolved,
        "name": _pin_short_name(resolved),
    }


def _pin_decorate(row: dict[str, Any]) -> dict[str, Any]:
    p = str(row.get("path") or "")
    try:
        fp = Path(p)
        exists = fp.exists()
        is_dir = fp.is_dir() if exists else False
    except OSError:
        exists = False
        is_dir = False
    return {
        "path": p,
        "name": _pin_short_name(p),
        "exists": exists,
        "is_dir": is_dir,
        "in_root": _under_scan_root(p) if exists else False,
        "is_git": os.path.exists(os.path.join(p, ".git")) if is_dir else False,
    }


def get_pins() -> dict[str, Any]:
    db = load_notes_db()
    rows = db.get("pins", [])
    items = [_pin_decorate(r) for r in rows if isinstance(r, dict) and r.get("path")]
    return {"ok": True, "items": items, "count": len(items), "scan_root": str(SCAN_ROOT)}


def post_pins(req: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """add · remove · reorder · clear — Quick access pin list."""
    action = (req.get("action") or "add").lower()
    db = load_notes_db()
    rows = [r for r in db.get("pins", []) if isinstance(r, dict) and r.get("path")]

    if action == "add":
        target = str(req.get("path") or "").strip()
        if not target:
            return 400, {"success": False, "error": "Missing 'path'"}
        row = _pin_row(target, str(req.get("name") or ""))
        rows = [r for r in rows if str(r.get("path", "")).lower() != row["path"].lower()]
        rows.append(row)
        rows = rows[-PINS_MAX:]
    elif action in ("remove", "delete", "unpin"):
        target = str(req.get("path") or "").strip()
        if not target:
            return 400, {"success": False, "error": "Missing 'path'"}
        try:
            target = str(Path(target).resolve())
        except OSError:
            pass
        rows = [r for r in rows if str(r.get("path", "")).lower() != target.lower()]
    elif action == "reorder":
        order = [str(p).lower() for p in (req.get("order") or [])]
        if not order:
            return 400, {"success": False, "error": "Missing 'order'"}
        by_path = {str(r.get("path", "")).lower(): r for r in rows}
        reordered = [by_path.pop(p) for p in order if p in by_path]
        # anything the client didn't know about keeps its place at the end
        reordered.extend(by_path.values())
        rows = reordered
    elif action == "replace":
        # Whole-list write — used by the quick-folder tab strip, which owns its
        # own order and labels. Keeps rail + tabs on one server-side list.
        items = req.get("items") or []
        rows = []
        seen: set[str] = set()
        for it in items:
            if isinstance(it, str):
                it = {"path": it}
            if not isinstance(it, dict) or not it.get("path"):
                continue
            row = _pin_row(str(it["path"]), str(it.get("name") or it.get("label") or ""))
            if row["path"].lower() in seen:
                continue
            seen.add(row["path"].lower())
            rows.append(row)
        rows = rows[:PINS_MAX]
    elif action == "clear":
        rows = []
    else:
        return 400, {"success": False, "error": f"Unknown action: {action}"}

    db["pins"] = rows
    if not save_notes_db(db):
        return 500, {"success": False, "error": "Failed to save database"}
    return 200, {
        "success": True,
        "items": [_pin_decorate(r) for r in rows],
        "count": len(rows),
    }


def module_info() -> dict[str, Any]:
    return {
        "ok": True,
        "module": "aytree",
        "title": "AyTree (legacy green file browser)",
        "scan_root": str(SCAN_ROOT),
        "notes_file": str(NOTES_FILE),
        "ui": "/aytree",
        "apis": [
            "/api/aytree/tree",
            "/api/aytree/notes",
            "/api/aytree/extend",
            "/api/aytree/pins",
            "/api/aytree/branch-preview",
            "/api/aytree/github-url",
            "/api/aytree/open-chrome",
        ],
    }
