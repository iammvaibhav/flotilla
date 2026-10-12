"""GitHub: gh CLI, gh login, git identity."""

import shlex

from pyinfra.operations import files

from flotilla.deploys.common import bundle, fact, sh

b = bundle()
home = b["home"]
git = b["data"]["git"]

# Pick the newest gh release that is at least 7 days old (same cooldown
# as uv and pnpm), then install it into ~/.local/bin. Linux only: on
# macOS install gh with Homebrew.
GH_INSTALL = r"""
case "$(uname -m)" in x86_64) a=amd64;; aarch64|arm64) a=arm64;; esac
v=$(curl -fsSL 'https://api.github.com/repos/cli/cli/releases?per_page=20' |
  python3 -c 'import json,sys,datetime as d
now=d.datetime.now(d.timezone.utc)
for r in json.load(sys.stdin):
    t=d.datetime.fromisoformat(r["published_at"].replace("Z","+00:00"))
    if not r["prerelease"] and (now-t).days>=7:
        print(r["tag_name"].lstrip("v")); break')
t=$(mktemp -d)
curl -fsSL "https://github.com/cli/cli/releases/download/v$v/gh_${v}_linux_${a}.tar.gz" | tar -xz -C "$t"
mkdir -p ~/.local/bin && install -m 755 "$t/gh_${v}_linux_${a}/bin/gh" ~/.local/bin/gh
rm -rf "$t"
"""

if not fact("command -v gh"):
    if b["os"] == "darwin":
        sh("install gh", "brew install gh")
    else:
        sh("install gh", GH_INSTALL)

logged_in = fact("gh auth status --hostname github.com >/dev/null && echo ok")
if not logged_in and b["os"] == "darwin" and \
        fact("gh config get -h github.com user"):
    # The token is in the login keychain, which SSH sessions cannot read.
    # Logging in again here would store a plaintext token instead.
    logged_in = "keychain"
if not logged_in:
    tok = f"{home}/.cache/flotilla-gh-token"
    files.directory(name="dir ~/.cache", path=f"{home}/.cache")
    files.put(name="gh token (temporary)", src=b["data"]["token_file"],
              dest=tok, mode="600")
    sh("gh auth login",
       f"gh auth login --hostname github.com --git-protocol ssh "
       f"--with-token < {tok}; rm -f {tok}")

for key in ("name", "email"):
    if fact(f"git config --global user.{key}") != git[key]:
        sh(f"git user.{key}",
           f"git config --global user.{key} {shlex.quote(git[key])}")
