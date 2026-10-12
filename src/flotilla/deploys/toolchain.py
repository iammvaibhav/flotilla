"""Toolchain: uv, pnpm (standalone) and their release cooldowns."""

from pyinfra.operations import files

from flotilla.deploys.common import bundle, fact, sh

b = bundle()
home = b["home"]
tc = b["data"]["toolchain"]
pnpm_home = (f"{home}/Library/pnpm" if b["os"] == "darwin"
             else f"{home}/.local/share/pnpm")

if not fact("command -v uv"):
    sh("install uv", "curl -LsSf https://astral.sh/uv/install.sh | sh")

files.directory(name="dir ~/.config/uv", path=f"{home}/.config/uv")
files.line(name="uv exclude-newer", path=f"{home}/.config/uv/uv.toml",
           line=r"^exclude-newer\s*=.*$",
           replace=f'exclude-newer = "{tc["uv_exclude_newer"]}"')

if not fact(f"test -x {pnpm_home}/bin/pnpm && echo yes"):
    # The installer also edits shell rc files; it can fail on a non-UTF-8
    # rc file. The binary is what matters, so check for it afterwards.
    sh("install pnpm",
       f"curl -fsSL https://get.pnpm.io/install.sh | sh - >/dev/null 2>&1"
       f" || test -x {pnpm_home}/bin/pnpm")

# Agent shells do not read .bashrc/.zshrc: expose pnpm via ~/.local/bin.
for name in ("pnpm", "pnpx", "pn", "pnx"):
    files.link(name=f"link {name}", path=f"{home}/.local/bin/{name}",
               target=f"{pnpm_home}/bin/{name}")

age = str(tc["pnpm_minimum_release_age"])
if fact(f"{pnpm_home}/bin/pnpm config get minimumReleaseAge") != age:
    sh("pnpm minimumReleaseAge",
       f"{pnpm_home}/bin/pnpm config set --global minimumReleaseAge {age}")

if tc.get("node") and not fact("command -v node"):
    sh(f"node {tc['node']} via pnpm",
       f"{pnpm_home}/bin/pnpm runtime set node {tc['node']} -g")
