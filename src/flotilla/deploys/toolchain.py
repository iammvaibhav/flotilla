"""Toolchain: uv, pnpm (standalone), their release cooldowns, Node.js, agent-browser,
Obscura and ffmpeg."""

from pyinfra.operations import files

from flotilla.deploys.common import bundle, fact, fail, sh

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

# Node only where there is none at all. Hosts with nvm keep their nvm node: nvm is not on
# this shell's PATH, so look in ~/.nvm too, because a ~/.local/bin/node would shadow it.
if tc.get("node") and not fact("command -v node || ls ~/.nvm/versions/node/*/bin/node"):
    sh(f"node {tc['node']} via pnpm",
       f"export PNPM_HOME={pnpm_home} PATH={pnpm_home}/bin:$PATH;"
       f" {pnpm_home}/bin/pnpm runtime set node {tc['node']} -g")
    files.link(name="link node", path=f"{home}/.local/bin/node",
               target=f"{pnpm_home}/bin/node")

# agent-browser: the native binary from the npm tarball, pinned. Not the npm/pnpm package,
# whose Node shim adds ~110 ms to every command; the fleet-browser skill relies on the
# pinned version's behaviour.
ab = tc.get("agent_browser")
if ab and fact("~/.local/bin/agent-browser --version") != f"agent-browser {ab}":
    os_name = "darwin" if b["os"] == "darwin" else "linux"
    sh(f"agent-browser {ab}",
       "arch=$(uname -m | sed -e s/x86_64/x64/ -e s/aarch64/arm64/); "
       f"curl -fsSL https://registry.npmjs.org/agent-browser/-/agent-browser-{ab}.tgz"
       f" | tar -xzO package/bin/agent-browser-{os_name}-$arch"
       " > ~/.local/bin/agent-browser.new"
       " && chmod +x ~/.local/bin/agent-browser.new"
       " && mv ~/.local/bin/agent-browser.new ~/.local/bin/agent-browser")

# Obscura: headless browser used as agent-browser's `--engine obscura` and for scripts.
# The release's stealth build (TLS impersonation, tracker blocking), pinned. The tarball
# holds `obscura` and `obscura-worker`; both go to ~/.local/bin.
ob = tc.get("obscura")
if ob and fact("~/.local/bin/obscura --version") != f"obscura {ob}":
    os_name = "macos" if b["os"] == "darwin" else "linux"
    sh(f"obscura {ob}",
       "arch=$(uname -m | sed -e s/arm64/aarch64/); tmp=$(mktemp -d); "
       "curl -fsSL https://github.com/h4ckf0r0day/obscura/releases/download/"
       f"v{ob}/obscura-$arch-{os_name}-stealth.tar.gz | tar -xzC $tmp"
       " && install -m 755 $tmp/obscura $tmp/obscura-worker ~/.local/bin/"
       " && rm -rf $tmp")

# ffmpeg: agent-browser's `record` encodes video with it.
if tc.get("ffmpeg") and not fact("command -v ffmpeg"):
    if b["os"] == "darwin":
        sh("install ffmpeg", "brew install ffmpeg")
    elif not fact("sudo -n true && echo yes"):
        fail("ffmpeg needs sudo", "passwordless sudo is required to install ffmpeg")
    else:
        sh("install ffmpeg",
           "sudo -n apt-get update -qq"
           " && sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg")
