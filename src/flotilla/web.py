"""FastAPI app: host map, quick exec, components, config editor, onboarding."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets as pysecrets
import shlex
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from itsdangerous import BadSignature, TimestampSigner

from . import facts, remote, runner, secrets
from .jobs import Jobs
from .state import Host, host_vars, load_fleet, save_fleet, state_dir

HOME = Path("~/.config/flotilla").expanduser()
REPO = Path(__file__).resolve().parents[2]
COOKIE = "flotilla"
DEFAULT_MODEL = "anthropic/claude-opus-5-5"
DEFAULT_EFFORT = "high"
EFFORTS = ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"]

app = FastAPI(title="flotilla", docs_url=None, redoc_url=None)
jobs = Jobs()
_cache: dict[str, tuple[float, Any]] = {}


# ---------------------------------------------------------------- auth

def _secret(name: str) -> str:
    """Read or create a private value under ``~/.config/flotilla``."""
    path = HOME / name
    if not path.exists():
        HOME.mkdir(parents=True, exist_ok=True)
        path.write_text(pysecrets.token_urlsafe(32))
        path.chmod(0o600)
    return path.read_text().strip()


def _password() -> str:
    return os.environ.get("FLOTILLA_PASSWORD") or _secret("password")


_signer = TimestampSigner(_secret("session.key"))


def _authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE, "")
    try:
        _signer.unsign(token, max_age=30 * 86400)
        return True
    except BadSignature:
        return False


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path in ("/login", "/favicon.ico") or _authed(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login required"}, status_code=401)
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return _static("login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    if not hmac.compare_digest(str(form.get("password", "")), _password()):
        return RedirectResponse("/login?bad=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, _signer.sign(b"ok").decode(), httponly=True,
                    samesite="strict", max_age=30 * 86400)
    return resp


# ---------------------------------------------------------------- pages

def _static(name: str) -> str:
    return resources.files("flotilla").joinpath("static", name).read_text()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _static("index.html")


@app.get("/app.js")
def app_js():
    return HTMLResponse(_static("app.js"), media_type="text/javascript")


# ---------------------------------------------------------------- helpers

def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _hosts(names: list[str] | None) -> list[Host]:
    fleet = load_fleet()
    if not names:
        return list(fleet.hosts.values())
    out = []
    for n in names:
        if n in fleet.hosts:
            out.append(fleet.hosts[n])
        else:
            out += [fleet.hosts[m] for m in fleet.groups().get(n, [])]
    if not out:
        raise HTTPException(400, f"no hosts match {names}")
    return out


def _job(job) -> dict[str, Any]:
    return {"job": job.id}


# ---------------------------------------------------------------- hosts

@app.get("/api/fleet")
def fleet_info() -> dict[str, Any]:
    fleet = load_fleet()
    return {"controller": fleet.controller, "groups": fleet.groups(),
            "hosts": [{"name": h.name, "address": h.address, "user": h.user,
                       "platform": h.platform, "groups": h.groups,
                       "names": h.names, "roles": h.roles}
                      for h in fleet.hosts.values()],
            "components": runner.COMPONENTS,
            "vault_accounts": (host_vars(fleet.host(fleet.controller))
                               .get("vault") or {}).get("accounts", [])}


@app.get("/api/status")
def status(fresh: bool = False) -> list[dict[str, Any]]:
    if fresh:
        _cache.pop("status", None)
    return _cached("status", 30, lambda: facts.fleet_status(load_fleet()))


@app.post("/api/exec")
async def exec_cmd(request: Request) -> dict[str, Any]:
    body = await request.json()
    command = str(body.get("command", "")).strip()
    if not command:
        raise HTTPException(400, "command is empty")
    hosts = _hosts(body.get("hosts"))
    fleet = load_fleet()

    def work(emit):
        code_all = 0
        for h in hosts:
            emit(f"### {h.name}$ {command}")
            proc = subprocess.Popen(remote.argv(fleet, h, command),
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    stdin=subprocess.DEVNULL)
            assert proc.stdout
            for line in proc.stdout:
                if "post-quantum" in line or "store now" in line:
                    continue
                emit(runner.mask_plain(line))
            code = proc.wait()
            emit(f"### {h.name} exit {code}")
            code_all |= code
        yield ("exit", code_all)

    names = ", ".join(h.name for h in hosts)
    return _job(jobs.start(f"exec on {names}: {command[:60]}", work))


@app.post("/api/mesh")
def mesh() -> dict[str, Any]:
    def work(emit):
        emit("checking ssh from every host to every other host…")
        result = facts.mesh(load_fleet())
        bad = sum(v != "ok" for row in result["cells"].values()
                  for v in row.values())
        emit(f"{bad} failing link(s)")
        yield ("result", result)
        yield ("exit", 1 if bad else 0)
    return _job(jobs.start("ssh mesh check", work))


# ---------------------------------------------------------------- jobs

@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return jobs.recent()


@app.get("/api/jobs/{job_id}")
def job_info(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)
    return {**job.summary(), "log": job.lines}


@app.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404)

    async def events():
        sent = 0
        while True:
            while sent < len(job.lines):
                yield f"data: {json.dumps(job.lines[sent])}\n\n"
                sent += 1
            if job.status != "running" and sent >= len(job.lines):
                yield (f"event: done\ndata: "
                       f"{json.dumps(job.summary())}\n\n")
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(events(), media_type="text/event-stream")


# ---------------------------------------------------------------- components

@app.post("/api/components/{component}/{action}")
async def component_run(component: str, action: str, request: Request):
    if component not in runner.COMPONENTS or action not in ("plan", "apply"):
        raise HTTPException(404)
    body = await request.json() if await request.body() else {}
    hosts = _hosts(body.get("hosts"))
    fleet = load_fleet()
    names = ", ".join(h.name for h in hosts)
    return _job(jobs.start(
        f"{action} {component} on {names}",
        lambda emit: runner.execute(component, fleet, hosts,
                                    dry=action == "plan")))


@app.get("/api/components/{component}/diff")
def component_diff(component: str, host: str) -> dict[str, Any]:
    if component not in ("ssh", "omp"):
        return {"diffs": [], "note": "this component has no managed files"}
    fleet = load_fleet()
    return {"diffs": runner.file_diffs(component, fleet, fleet.host(host),
                                       secrets.Resolver())}


# ---------------------------------------------------------------- config

def _state_path(rel: str) -> Path:
    root = state_dir().resolve()
    path = (root / rel).resolve()
    if root not in path.parents and path != root:
        raise HTTPException(400, "path outside the state directory")
    return path


@app.get("/api/state/tree")
def state_tree() -> list[str]:
    root = state_dir()
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and ".git" not in p.parts
                  and "import" not in p.parts
                  and p.name != "known_hosts")


@app.get("/api/state/file")
def state_read(path: str) -> dict[str, Any]:
    p = _state_path(path)
    if not p.is_file():
        raise HTTPException(404)
    return {"path": path, "content": p.read_text()}


def _validate(path: str, content: str) -> list[str]:
    """Problems that block a save, and warnings."""
    problems = []
    if path.endswith((".yml", ".yaml")):
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return [f"YAML error: {exc}"]
        for key, _ in secrets.literal_secrets(data):
            problems.append(f"literal secret at {key}: store it in the vault "
                            f"and paste the aside:// reference instead")
    elif path.endswith(".json"):
        try:
            json.loads(content)
        except ValueError as exc:
            return [f"JSON error: {exc}"]
    return problems


def _unknown_models(content: str) -> list[str]:
    try:
        known = {m["selector"] for m in _cached(
            "models", 600, lambda: facts.models(load_fleet()))}
    except Exception:  # noqa: BLE001 - catalog is optional for saving
        return []
    found = set()
    for token in content.replace('"', " ").replace("'", " ").split():
        token = token.strip(",[]-")
        base = token.split(":", 1)[0]
        if "/" in base and not base.startswith(("http", "aside", "/", "~",
                                                  ".")):
            if base.count("/") >= 1 and base not in known and \
                    not base.endswith("*") and base.split("/")[0] in {
                        m.split("/")[0] for m in known}:
                found.add(base)
    return sorted(found)


@app.put("/api/state/file")
async def state_write(request: Request) -> dict[str, Any]:
    body = await request.json()
    path, content = body["path"], body["content"]
    problems = _validate(path, content)
    if problems:
        return JSONResponse({"ok": False, "problems": problems},
                            status_code=422)
    p = _state_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _commit(f"edit {path}")
    return {"ok": True, "warnings": [
        f"unknown model: {m}" for m in _unknown_models(content)]}


@app.delete("/api/state/file")
def state_delete(path: str) -> dict[str, Any]:
    p = _state_path(path)
    if p.is_file():
        p.unlink()
        _commit(f"delete {path}")
    return {"ok": True}


def _commit(message: str) -> None:
    """Record every state change in a local git repo (history + undo)."""
    root = state_dir()
    git = ["git", "-C", str(root)]
    if not (root / ".git").exists():
        subprocess.run(git + ["init", "-q"], check=True)
        (root / ".gitignore").write_text("import/\n")
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["-c", "user.name=flotilla",
                          "-c", "user.email=flotilla@localhost",
                          "commit", "-q", "-m", message],
                   capture_output=True)


@app.get("/api/state/history")
def state_history() -> list[str]:
    out = subprocess.run(["git", "-C", str(state_dir()), "log",
                          "--format=%h %ad %s", "--date=short", "-n", "50"],
                         capture_output=True, text=True)
    return out.stdout.splitlines()


@app.get("/api/catalog/models")
def catalog_models(fresh: bool = False) -> list[dict[str, Any]]:
    if fresh:
        _cache.pop("models", None)
    return _cached("models", 600, lambda: facts.models(load_fleet()))


@app.get("/api/catalog/settings")
def catalog_settings() -> list[dict[str, Any]]:
    return _cached("settings", 3600, lambda: facts.settings(load_fleet()))


@app.post("/api/secrets")
async def store_secret(request: Request) -> dict[str, Any]:
    body = await request.json()
    ref = secrets.store(body["account"], body["title"], body["value"])
    return {"ref": ref}


# ---------------------------------------------------------------- onboarding

@app.get("/api/onboarding/guide")
def guide() -> dict[str, str]:
    return {"markdown": (REPO / "docs" / "onboarding.md").read_text()}


@app.post("/api/onboarding/hosts")
async def add_host(request: Request) -> dict[str, Any]:
    """Add a host to ``hosts.yaml`` (it stays inert until components run)."""
    b = await request.json()
    fleet = load_fleet()
    name = b["name"].strip()
    if not name or name in fleet.hosts:
        raise HTTPException(400, "name is empty or already used")
    fleet.hosts[name] = Host(
        name=name, address=b["address"].strip(), user=b["user"].strip(),
        platform=b["platform"], groups=[g for g in b.get("groups", []) if g],
        names=[n for n in b.get("names", []) if n])
    save_fleet(fleet)
    _commit(f"add host {name}")
    return {"ok": True}


AGENT_PROMPT = """You are onboarding the machine `{name}` into the fleet.

Follow docs/onboarding.md in this repository step by step. Facts:
- name: {name}
- address: {address}
- user: {user}
- platform: {platform}
- groups: {groups}
- user notes: {notes}

Use the `flotilla` CLI for every step it covers (`flotilla plan|apply
<component> {name}`); do not copy config by hand. Read every plan before you
apply it. Stop and report if a step needs a password, a sudo prompt, or a
decision from the user. End with `flotilla plan <component> {name}` for every
component showing no changes, and a short report.
"""


@app.post("/api/onboarding/agent")
async def run_agent(request: Request) -> dict[str, Any]:
    b = await request.json()
    model = b.get("model") or DEFAULT_MODEL
    effort = b.get("effort") or DEFAULT_EFFORT
    if effort not in EFFORTS:
        raise HTTPException(400, f"effort must be one of {EFFORTS}")
    prompt = AGENT_PROMPT.format(
        name=b["name"], address=b.get("address", "?"),
        user=b.get("user", "?"), platform=b.get("platform", "?"),
        groups=", ".join(b.get("groups", [])), notes=b.get("notes", "-"))
    argv = ["omp", "-p", "--no-session", "--mode", "json", "--model", model,
            "--thinking", effort, "--cwd", str(REPO), prompt]
    env = dict(os.environ)
    env["PATH"] = (f"{Path(sys.executable).parent}:"
                   f"{Path('~/.local/bin').expanduser()}:{env['PATH']}")

    def work(emit):
        emit(f"omp {model} (effort {effort}) in {REPO}")
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env,
                                stdin=subprocess.DEVNULL)
        assert proc.stdout
        for line in proc.stdout:
            for out in _agent_event(line):
                emit(runner.mask_plain(out))
        yield ("exit", proc.wait())

    return _job(jobs.start(f"onboarding agent for {b['name']}", work))


def _agent_event(line: str) -> list[str]:
    try:
        e = json.loads(line)
    except ValueError:
        return [line.rstrip()] if line.strip() else []
    kind = e.get("type")
    if kind == "tool_execution_start":
        args = e.get("args") or {}
        what = args.get("command") or args.get("path") or ""
        return [f"▶ {e.get('toolName')}: {e.get('intent') or ''} "
                f"{shlex.quote(what)[:300] if what else ''}"]
    if kind == "tool_execution_end":
        text = "".join(c.get("text", "") for c in
                       (e.get("result") or {}).get("content", []))
        lines = text.rstrip().splitlines()
        tail = lines[:25] + (["…"] if len(lines) > 25 else [])
        mark = "✗" if e.get("isError") else "  "
        return [f"{mark} {ln}" for ln in tail]
    if kind == "message_end":
        msg = e.get("message") or {}
        if msg.get("role") == "assistant":
            return ["", *"".join(c.get("text", "") for c in
                                 msg.get("content", [])
                                 if c.get("type") == "text").splitlines()]
    return []
