"""FastAPI app: host map, quick exec, components, and the Config page.

Config page: the state directory as it is on disk, in a file tree and a code
editor. A bundle's overview lists its files and the layers that override
them; a group or host file compares with base; Apply shows what would change
on every host, then applies. A background thread re-plans every bundle after
each change and every 30 minutes, so the tree can show what is not applied.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets as pysecrets
import shutil
import subprocess
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from itsdangerous import BadSignature, TimestampSigner

from . import bundles, facts, remote, runner, secrets
from .jobs import Jobs
from .state import Host, commit, load_fleet, state_dir

HOME = Path("~/.config/flotilla").expanduser()
COOKIE = "flotilla"
REPLAN_SECONDS = 30 * 60      # also on every change to the state

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
        return fleet.managed()
    out = []
    for n in names:
        if n in fleet.hosts and fleet.hosts[n].managed:
            out.append(fleet.hosts[n])
        else:
            out += [fleet.hosts[m] for m in fleet.groups().get(n, [])]
    if not out:
        raise HTTPException(400, f"no managed hosts match {names}")
    return out


def _job(job) -> dict[str, Any]:
    return {"job": job.id}


def _bundle(name: str) -> bundles.Bundle:
    try:
        return bundles.get_bundle(name)
    except bundles.BundleError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------- hosts

@app.get("/api/fleet")
def fleet_info() -> dict[str, Any]:
    fleet = load_fleet()
    return {"controller": fleet.controller, "groups": fleet.groups(),
            "hosts": [{"name": h.name, "address": h.address, "user": h.user,
                       "platform": h.platform, "groups": h.groups,
                       "names": h.names, "roles": h.roles}
                      for h in fleet.managed()],
            "components": runner.COMPONENTS,
            "vault_accounts": (bundles.host_vars(fleet.host(fleet.controller))
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
    if component != "ssh":
        return {"diffs": [], "note": "this component has no managed files"}
    fleet = load_fleet()
    return {"diffs": runner.file_diffs(component, fleet, fleet.host(host),
                                       secrets.Resolver())}


# ---------------------------------------------------------------- plans

class Planner:
    """Latest plan of every bundle, refreshed in the background."""

    def __init__(self) -> None:
        self.plans: dict[str, dict[str, Any]] = {}
        self._wake = threading.Event()
        self._started = False

    def record(self, name: str, result: dict[str, Any]) -> None:
        self.plans[name] = {"at": time.time(), **result}

    def run_once(self) -> None:
        fleet = load_fleet()
        resolver = secrets.Resolver()
        for name, b in bundles.load_bundles().items():
            result: dict[str, Any] = {}
            try:
                for item in bundles.execute(b, fleet, fleet.managed(),
                                            dry=True, resolver=resolver):
                    if isinstance(item, tuple) and item[0] == "plan":
                        result = item[1]
            except Exception as exc:  # noqa: BLE001 - shown in the UI
                result = {"plan": {}, "errors": {"*": str(exc)}}
            self.record(name, result)

    def soon(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        def head() -> str:
            out = subprocess.run(["git", "-C", str(state_dir()), "rev-parse",
                                  "HEAD"], capture_output=True, text=True)
            return out.stdout.strip()

        def loop() -> None:
            while True:
                self._wake.clear()
                seen, started = head(), time.time()
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001 - try again later
                    pass
                # Wake on a UI change, a commit made elsewhere (CLI, an
                # agent), or after REPLAN_SECONDS.
                while not self._wake.wait(60):
                    if head() != seen or \
                            time.time() - started > REPLAN_SECONDS:
                        break
                time.sleep(2)   # let a burst of saves settle

        threading.Thread(target=loop, daemon=True).start()


planner = Planner()


@app.on_event("startup")
def _start_planner() -> None:
    if os.environ.get("FLOTILLA_NO_PLANNER") != "1":
        planner.start()


# ---------------------------------------------------------------- bundles

def _bundle_info(b: bundles.Bundle) -> dict[str, Any]:
    return {"name": b.name, "prefix": b.prefix, "description": b.description,
            "dest": b.dest, "unit": b.unit, "deployable": b.deployable,
            "plan": planner.plans.get(b.name)}


@app.get("/api/bundles")
def bundle_list() -> list[dict[str, Any]]:
    """Every bundle, with its latest background plan (who differs)."""
    return [_bundle_info(b) for b in
            [*bundles.load_bundles().values(), bundles.settings_bundle()]]


@app.get("/api/bundles/{name}/overview")
def bundle_overview(name: str) -> dict[str, Any]:
    """Every file (or item) of the bundle and the layers that have it."""
    b = _bundle(name)
    try:
        rows = bundles.layout(b)
    except bundles.BundleError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {**_bundle_info(b), "rows": rows}


@app.post("/api/bundles/{name}/review")
def bundle_review(name: str) -> dict[str, Any]:
    """What applying would change on every managed host, with diffs."""
    b = _bundle(name)
    if not b.deployable:
        raise HTTPException(400, f"{name} is not copied to hosts")
    fleet = load_fleet()
    result = bundles.review_all(b, fleet, fleet.managed())
    planner.record(name, {"plan": {h: r["ops"] for h, r in
                                   result["hosts"].items()},
                          "errors": result["errors"]})
    return result


@app.post("/api/bundles/{name}/apply")
async def bundle_apply(name: str, request: Request):
    b = _bundle(name)
    if not b.deployable:
        raise HTTPException(400, f"{name} is not copied to hosts")
    body = await request.json() if await request.body() else {}
    hosts = _hosts(body.get("hosts"))
    fleet = load_fleet()

    def work(emit):
        yield from bundles.execute(b, fleet, hosts, dry=False)
        planner.soon()

    names = ", ".join(h.name for h in hosts)
    return _job(jobs.start(f"apply {name} on {names}", work))


# ---------------------------------------------------------------- state files

def _state_path(rel: str) -> Path:
    root = state_dir().resolve()
    path = (root / rel).resolve()
    if root not in path.parents or \
            any(part.startswith(".") for part in path.relative_to(root).parts):
        raise HTTPException(400, f"not an editable path: {rel}")
    return path


@app.get("/api/state/tree")
def state_tree() -> list[str]:
    """Every editable file in the state directory, as stored on disk."""
    root = state_dir()
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not x.startswith(".")]
        for f in files:
            rel = (Path(d) / f).relative_to(root).as_posix()
            if not f.startswith(".") and rel != "known_hosts":
                out.append(rel)
    return sorted(out)


def _meta(rel: str) -> dict[str, Any] | None:
    """Where a state path sits: its bundle, layer and path in the layer."""
    try:
        found = bundles.bundle_of(rel)
    except bundles.BundleError:
        return None
    if found is None:
        return None
    b, layer, inner = found
    return {"bundle": b.name, "prefix": b.prefix, "unit": b.unit,
            "deployable": b.deployable, "layer": layer, "rel": inner,
            "merge": b.rule(bundles.out_name(inner)).merge if inner else None}


@app.get("/api/state/file")
def state_read(path: str) -> dict[str, Any]:
    p = _state_path(path)
    out = {"path": path, "meta": _meta(path)}
    if not p.is_file():
        return {**out, "content": "", "new": True}
    try:
        return {**out, "content": p.read_text()}
    except UnicodeDecodeError:
        return {**out, "content": "", "binary": True}


def _validate(path: str, content: str) -> list[str]:
    """Problems that block a save."""
    problems = []
    if path.endswith((".yml", ".yaml", ".json")):
        try:
            data = (json.loads(content) if path.endswith(".json")
                    else yaml.safe_load(content))
        except (ValueError, yaml.YAMLError) as exc:
            return [f"{'JSON' if path.endswith('.json') else 'YAML'} "
                    f"error: {exc}"]
        for key, _ in secrets.literal_secrets(data):
            problems.append(f"literal secret at {key}: store it in the vault "
                            f"and paste the aside:// reference instead")
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


def _saved(message: str) -> None:
    commit(message)
    planner.soon()


@app.put("/api/state/file")
async def state_write(request: Request) -> dict[str, Any]:
    """Create or replace a file; refused if the state would break."""
    body = await request.json()
    path, content = body["path"], body["content"]
    p = _state_path(path)
    if p.is_dir():
        raise HTTPException(400, f"{path} is a folder")
    problems = _validate(path, content)
    if not problems:
        def change():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        problems = bundles.guarded_edit(p, change)
    if problems:
        return JSONResponse({"ok": False, "problems": problems},
                            status_code=422)
    _saved(f"edit {path}")
    return {"ok": True, "warnings": [
        f"unknown model: {m}" for m in _unknown_models(content)]}


@app.delete("/api/state/file")
def state_delete(path: str) -> dict[str, Any]:
    """Delete a file or a folder; refused if the state would break."""
    p = _state_path(path)
    if not p.exists():
        return {"ok": True}
    if p.is_dir() and p.parent == state_dir().resolve():
        raise HTTPException(400, f"{path} cannot be deleted")

    def change():
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    problems = bundles.guarded_edit(p, change)
    if problems:
        return JSONResponse({"ok": False, "problems": problems},
                            status_code=422)
    bundles.prune_empty(p.parent, state_dir())
    _saved(f"delete {path}")
    return {"ok": True}


@app.post("/api/state/compare")
async def state_compare(request: Request) -> dict[str, Any]:
    """A layer file against base; ``content`` stands in for unsaved edits."""
    body = await request.json()
    path = body["path"]
    _state_path(path)
    meta = _meta(path)
    if not meta or not meta["rel"] or meta["layer"] in (None, "base"):
        raise HTTPException(400, "only group and host files compare with "
                            "base")
    b = bundles.get_bundle(meta["bundle"])
    try:
        return bundles.compare(b, meta["rel"], meta["layer"], load_fleet(),
                               body.get("content"))
    except bundles.BundleError as exc:
        return {"diff": "", "note": str(exc)}


@app.get("/api/catalog/models")
def catalog_models(fresh: bool = False) -> list[dict[str, Any]]:
    if fresh:
        _cache.pop("models", None)
    return _cached("models", 600, lambda: facts.models(load_fleet()))


@app.post("/api/secrets")
async def store_secret(request: Request) -> dict[str, Any]:
    body = await request.json()
    ref = secrets.store(body["account"], body["title"], body["value"])
    return {"ref": ref}
