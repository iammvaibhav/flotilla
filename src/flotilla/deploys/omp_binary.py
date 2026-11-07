"""omp binary: install it if missing, remove old npm-global copies.

omp's config, agents, rules and AGENTS.md are the ``omp`` bundle.
"""

from flotilla.deploys.common import bundle, fact, sh

b = bundle()

if not fact("test -x ~/.local/bin/omp && echo yes"):
    sh("install omp", "curl -fsSL https://omp.sh/install | sh -s -- --binary")

for pkg in b["data"].get("remove_npm_global", []):
    if fact(f'. ~/.nvm/nvm.sh >/dev/null; npm ls -g --depth=0 {pkg}'
            f' | grep -F {pkg}'):
        sh(f"remove npm global {pkg}",
           f". ~/.nvm/nvm.sh >/dev/null; npm rm -g {pkg}")
