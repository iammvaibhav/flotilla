"""NetBird: install the client and join with a setup key (Linux).

Existing peers are left alone. A new or rebuilt machine needs
``netbird.setup_key`` (an aside:// reference) in its vars, and passwordless
sudo for the install.
"""

from pyinfra.operations import files

from flotilla.deploys.common import bundle, fact, fail, sh

b = bundle()
home = b["home"]
key_file = b["data"].get("setup_key_file")

if fact("netbird status | grep -E '^Signal: Connected'"):
    pass  # already a connected peer
elif b["os"] == "darwin":
    fail("netbird on macOS",
         "install the NetBird app on macOS by hand, then sign in")
elif not key_file:
    fail("netbird setup key",
         "netbird is not connected; set netbird.setup_key in the host vars")
elif not fact("sudo -n true && echo yes"):
    fail("netbird needs sudo", "passwordless sudo is required to install")
else:
    tmp = f"{home}/.cache/flotilla-nb-key"
    files.directory(name="dir ~/.cache", path=f"{home}/.cache")
    files.put(name="setup key (temporary)", src=key_file, dest=tmp,
              mode="600")
    sh("install and join netbird",
       f"command -v netbird >/dev/null || "
       f"curl -fsSL https://pkgs.netbird.io/install.sh | sudo -n sh; "
       f"sudo -n netbird up --setup-key \"$(cat {tmp})\"; rm -f {tmp}")
