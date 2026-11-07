"""Docker Engine (Linux): install it and let the host's user run it.

Only on hosts whose settings have ``docker.enabled: true``. Needs passwordless
sudo for the install. Existing installs are left as they are.
"""

from flotilla.deploys.common import bundle, fact, fail, sh

b = bundle()
d = b["data"]

if not d.get("enabled"):
    pass
elif b["os"] == "darwin":
    fail("docker on macOS", "install Docker Desktop or OrbStack on macOS by hand")
elif not fact("command -v docker") or not fact("id -un | xargs -I{} sh -c 'getent group docker | grep -qw {} && echo yes'"):
    if not fact("sudo -n true && echo yes"):
        fail("docker needs sudo", "passwordless sudo is required to install Docker")
    else:
        if not fact("command -v docker"):
            sh("install docker", "curl -fsSL https://get.docker.com | sudo sh")
        sh("user in docker group", 'sudo usermod -aG docker "$(id -un)"')
