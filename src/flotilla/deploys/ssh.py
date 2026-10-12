"""SSH: fleet key, authorized_keys, config include, known hosts."""

import shlex

from pyinfra.operations import files

from flotilla.deploys.common import bundle, fact, fail, put_files, sh

b = bundle()
home = b["home"]
data = b["data"]
include = "Include ~/.ssh/config.d/flotilla.conf"

files.directory(name="dir ~/.ssh", path=f"{home}/.ssh", mode="700")
files.directory(name="dir ~/.ssh/config.d", path=f"{home}/.ssh/config.d",
                mode="700")

have = fact("test -f ~/.ssh/id_ed25519 && ssh-keygen -y -f ~/.ssh/id_ed25519"
            " | ssh-keygen -lf - | awk '{print $2}'")
if have and have != data["fingerprint"]:
    # Never overwrite a different private key: it may be in use elsewhere.
    fail("fleet key conflict",
         f"~/.ssh/id_ed25519 is {have}, fleet key is {data['fingerprint']}")
    b["files"] = [f for f in b["files"]
                  if not f["dest"].endswith(("/id_ed25519",
                                             "/id_ed25519.pub"))]
put_files(b)

# Match on "type base64" only, so an existing line with another comment
# counts as present.
files.line(name="authorize fleet key", path=f"{home}/.ssh/authorized_keys",
           line=data["pubkey_match"], escape_regex_characters=True)
for key in data["remove_pubkeys"]:
    files.line(name=f"remove key {key[:30]}…",
               path=f"{home}/.ssh/authorized_keys", line=key,
               present=False, escape_regex_characters=True)

if fact("head -n1 ~/.ssh/config") != include:
    # ssh_config uses the first value it finds, so the include goes first.
    sh("include flotilla.conf first in ~/.ssh/config",
       f"f=~/.ssh/config; touch $f; chmod 600 $f; "
       f"grep -vxF {shlex.quote(include)} $f > $f.flotilla || true; "
       f"{{ echo {shlex.quote(include)}; cat $f.flotilla; }} > $f; "
       f"rm -f $f.flotilla")
