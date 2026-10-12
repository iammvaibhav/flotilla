"""omp: config layers, agents, rules and the fleet AGENTS.md."""

from pyinfra.operations import files

from flotilla.deploys.common import bundle, fact, put_files, sh

b = bundle()
home = b["home"]

if not fact("test -x ~/.local/bin/omp && echo yes"):
    sh("install omp", "curl -fsSL https://omp.sh/install | sh -s -- --binary")

put_files(b)

# agents/ and rules/ are owned by flotilla: drop files it does not render.
for d, keep in b["data"]["keep"].items():
    listing = fact(f"ls -1A ~/.omp/agent/{d}")
    for name in listing.splitlines():
        if name and name not in keep:
            files.file(name=f"remove {d}/{name}",
                       path=f"{home}/.omp/agent/{d}/{name}", present=False)

for pkg in b["data"].get("remove_npm_global", []):
    if fact(f'. ~/.nvm/nvm.sh >/dev/null; npm ls -g --depth=0 {pkg}'
            f' | grep -F {pkg}'):
        sh(f"remove npm global {pkg}",
           f". ~/.nvm/nvm.sh >/dev/null; npm rm -g {pkg}")
