"""FastAPI app: host map, quick exec, components, and the Config page.

Config page: Flotilla's settings and every bundle (base plus group and host
overrides), the file each host ends up with and where each key comes from,
diffs against the hosts, plan and apply. A background thread re-plans every
bundle after each change and every 30 minutes, so the page can show which
hosts are out of date.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets as pysecrets
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
REPLAN_SECONDS = 30 * 60

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

        def loop() -> None:
            while True:
                self._wake.clear()
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001 - try again later
                    pass
                self._wake.wait(REPLAN_SECONDS)
                time.sleep(2)   # let a burst of saves settle

        threading.Thread(target=loop, daemon=True).start()


planner = Planner()


@app.on_event("startup")
def _start_planner() -> None:
    if os.environ.get("FLOTILLA_NO_PLANNER") != "1":
        planner.start()


# ---------------------------------------------------------------- bundles

def _bundle_info(b: bundles.Bundle) -> dict[str, Any]:
    return {"name": b.name, "description": b.description, "dest": b.dest,
            "unit": b.unit, "deployable": b.deployable,
            "owned": b.owned, "after_apply": b.after_apply,
            "plan": planner.plans.get(b.name)}


@app.get("/api/bundles")
def bundle_list() -> list[dict[str, Any]]:
    return [_bundle_info(b) for b in
            [bundles.settings_bundle(), *bundles.load_bundles().values()]]


@app.get("/api/bundles/{name}")
def bundle_detail(name: str) -> dict[str, Any]:
    """Layers, files and, per host, where each file or item comes from."""
    b = _bundle(name)
    fleet = load_fleet()
    layer_ids = b.existing_layers()
    state_files: dict[str, list[str]] = {}
    skips: dict[str, dict[str, str]] = {}
    for lid in layer_ids:
        d = b.layer_dir(lid)
        for rel in bundles.layer_files(d):
            state_files.setdefault(rel, []).append(lid)
        man = bundles.layer_manifest(d)
        for kind in ("skip", "delete"):
            for item in man[kind]:
                skips.setdefault(item, {})[lid] = kind
    hosts: dict[str, Any] = {}
    for h in fleet.managed():
        try:
            comp = bundles.compile_bundle(b, h, fleet)
        except (bundles.BundleError, ValueError) as exc:
            hosts[h.name] = {"error": str(exc)}
            continue
        hosts[h.name] = {"items": comp.items, "skipped": comp.skipped,
                         "delete": comp.delete,
                         "files": {r: f.sources for r, f in comp.files.items()}}
    files = [{"rel": rel, "out": bundles.out_name(rel), "layers": lids,
              "merge": b.rule(bundles.out_name(rel)).merge}
             for rel, lids in sorted(state_files.items())]
    return {**_bundle_info(b), "layers": layer_ids, "files": files,
            "marks": skips, "hosts": hosts,
            "groups": fleet.groups(),
            "host_names": [h.name for h in fleet.managed()],
            "readme": (b.root / "README.md").read_text()
            if (b.root / "README.md").is_file() else ""}


@app.get("/api/bundles/{name}/compiled")
def bundle_compiled(name: str, host: str, file: str) -> dict[str, Any]:
    b = _bundle(name)
    fleet = load_fleet()
    comp = bundles.compile_bundle(b, fleet.host(host), fleet)
    f = comp.files.get(file)
    if f is None:
        raise HTTPException(404, f"{host} gets no {file} from {name}")
    text = (f.text if isinstance(f.text, str)
            else f"(binary file, {len(f.text)} bytes)")
    return {"file": file, "text": text, "sources": f.sources,
            "explain": f.explain, "own_keys": f.own_keys, "secret": f.secret}


@app.get("/api/bundles/{name}/diff")
def bundle_diff(name: str, host: str) -> dict[str, Any]:
    b = _bundle(name)
    if not b.deployable:
        return {"diffs": [], "note": "not copied to hosts"}
    fleet = load_fleet()
    return {"diffs": bundles.diffs(b, fleet, fleet.host(host),
                                   secrets.Resolver())}


def _saved(message: str) -> None:
    commit(message)
    planner.soon()


@app.post("/api/bundles/{name}/override")
async def bundle_override(name: str, request: Request) -> dict[str, Any]:
    """Set (or clear) one key for a group or host."""
    body = await request.json()
    b = _bundle(name)
    layer, rel, key = body["layer"], body["file"], body["key"]
    try:
        path = b.layer_dir(layer) / rel
        if body.get("clear"):
            def change():
                if not bundles.clear_override(b, layer, rel, key):
                    raise HTTPException(400, f"{key} is not set in {layer}")
            message = f"{name}: clear {key} for {layer}"
        else:
            value = yaml.safe_load(body.get("value", ""))
            if secrets.literal_secrets({key: value}):
                raise HTTPException(422, "store the secret in the vault and "
                                    "use its aside:// reference")
            op = body.get("op", "")

            def change():
                bundles.set_override(b, layer, rel, key, value, op)
            message = f"{name}: {key}{op} for {layer}"
        problems = bundles.guarded_edit(b, load_fleet(), path, change)
    except (bundles.BundleError, yaml.YAMLError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if problems:
        return JSONResponse({"ok": False, "problems": problems},
                            status_code=422)
    _saved(message)
    return {"ok": True}


@app.post("/api/bundles/{name}/item")
async def bundle_item(name: str, request: Request) -> dict[str, Any]:
    """Skip, delete or place one item (for example a skill) in a layer."""
    body = await request.json()
    b = _bundle(name)
    kind = body.get("state") or None
    try:
        bundles.set_item_state(b, body["layer"], body["item"], kind)
    except bundles.BundleError as exc:
        raise HTTPException(400, str(exc)) from exc
    _saved(f"{name}: {kind or 'place'} {body['item']} on {body['layer']}")
    return {"ok": True}


@app.post("/api/bundles/{name}/{action}")
async def bundle_run(name: str, action: str, request: Request):
    if action not in ("plan", "apply"):
        raise HTTPException(404)
    b = _bundle(name)
    if not b.deployable:
        raise HTTPException(400, f"{name} is not copied to hosts")
    body = await request.json() if await request.body() else {}
    hosts = _hosts(body.get("hosts"))
    fleet = load_fleet()

    def work(emit):
        for item in bundles.execute(b, fleet, hosts, dry=action == "plan"):
            if isinstance(item, tuple) and item[0] == "plan" and \
                    len(hosts) == len(fleet.managed()):
                planner.record(name, item[1])
            yield item
        if action == "apply":
            planner.soon()

    names = ", ".join(h.name for h in hosts)
    return _job(jobs.start(f"{action} {name} on {names}", work))


# ---------------------------------------------------------------- state files

def _state_path(rel: str) -> Path:
    root = state_dir().resolve()
    path = (root / rel).resolve()
    if root not in path.parents and path != root:
        raise HTTPException(400, "path outside the state directory")
    return path


def _owner(rel: str) -> bundles.Bundle | None:
    """The bundle (or settings) a state file belongs to."""
    parts = rel.split("/")
    if parts[0] == bundles.SETTINGS:
        return bundles.settings_bundle()
    if parts[0] == "bundles" and len(parts) > 2:
        return bundles.get_bundle(parts[1])
    return None


@app.get("/api/state/file")
def state_read(path: str) -> dict[str, Any]:
    p = _state_path(path)
    if not p.is_file():
        return {"path": path, "content": "", "new": True}
    return {"path": path, "content": p.read_text()}


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


@app.put("/api/state/file")
async def state_write(request: Request) -> dict[str, Any]:
    body = await request.json()
    path, content = body["path"], body["content"]
    problems = _validate(path, content)
    p = _state_path(path)
    owner = _owner(path) if not problems else None
    if owner is not None:
        def change():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        problems = bundles.guarded_edit(owner, load_fleet(), p, change)
    elif not problems:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if problems:
        return JSONResponse({"ok": False, "problems": problems},
                            status_code=422)
    _saved(f"edit {path}")
    return {"ok": True, "warnings": [
        f"unknown model: {m}" for m in _unknown_models(content)]}


@app.delete("/api/state/file")
def state_delete(path: str) -> dict[str, Any]:
    p = _state_path(path)
    if p.is_file():
        p.unlink()
        _saved(f"delete {path}")
    return {"ok": True}


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


@app.post("/api/secrets")
async def store_secret(request: Request) -> dict[str, Any]:
    body = await request.json()
    ref = secrets.store(body["account"], body["title"], body["value"])
    return {"ref": ref}
