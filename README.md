# flotilla

Manage a small fleet of machines on a NetBird network from one place: groups, layered config, SSH between every host, GitHub access, toolchains, and omp (coding agent) config. Changes are planned first (dry run with diffs) and then applied over SSH with [pyinfra](https://pyinfra.com). Nothing is installed on the managed machines.

- **Hosts**: live status of every machine (versions, GitHub login, NetBird, disk, load), a click-to-run command drawer, and an SSH mesh check (every host to every other host and to GitHub).
- **Components**: plan or apply `ssh`, `toolchain`, `github`, `omp`, `netbird` per host, group or fleet.
- **Config**: edit the layered state in the browser. Model names autocomplete from `omp models` (with their thinking levels); secrets go into the vault and only a reference is saved.
- **Onboard**: add a machine to the inventory and run an omp agent that follows [docs/onboarding.md](docs/onboarding.md). You choose the model and effort.

## How it works

The desired state lives in a **private state directory** (default `~/.config/flotilla/state`, override with `FLOTILLA_STATE`). It is not part of this repository and is versioned in its own local git repo; every save in the web UI is a commit.

```
hosts.yaml                    controller + hosts (address, user, platform, groups)
vars/all.yaml                 vars for every host
vars/groups/<group>.yaml      vars for one group
vars/hosts/<host>.yaml        vars for one host
files/omp/all/...             omp files for every host
files/omp/groups/<group>/...  omp files for one group
files/omp/hosts/<host>/...    omp files for one host
known_hosts                   pinned SSH host keys (flotilla scan-keys)
templates/AGENTS.md.j2        optional override of the fleet AGENTS.md template
```

Layers apply in order: `all`, the host's groups, the host. Later layers win.

- YAML files that more than one layer supplies are deep-merged: maps merge, lists replace, `null` deletes a key, and a map with `$replace: true` replaces instead of merging.
- A file that only one layer supplies is written exactly as it is in the state.
- Other files are taken from the highest layer that has them.

### Secrets

State files never hold secret values. They hold references that the `aside-vault` CLI (a command-line client for the Aside password vault) resolves on the controller when it renders files:

```yaml
apiKey: aside://you@example.com/<item-id>            # password field
token:  aside://you@example.com/<item-id>#notes      # another field
```

Saving a file with a literal secret (a key named like `apiKey`, `token`, `secret`, `password`) is refused. Diffs and logs mask secret values, including literal ones already on a host. Files that hold a secret are written with mode `600`.

### Components

| Component | What it manages |
|---|---|
| `ssh` | Fleet key `~/.ssh/id_ed25519` (never overwrites a different key), `~/.ssh/config.d/flotilla.conf` with short names for every host, pinned host keys, and an `Include` at the top of `~/.ssh/config`. |
| `toolchain` | uv and pnpm with release cooldowns (`exclude-newer`, `minimumReleaseAge`), links in `~/.local/bin`, optional Node through `pnpm runtime`. |
| `github` | `gh` (newest release older than 7 days), `gh` login from a vault token, git name and email per group. |
| `omp` | omp binary if missing; layered `~/.omp/agent` files; `agents/` and `rules/` are owned (extra files are removed); a generated `AGENTS.md` that tells every agent which machines exist. |
| `netbird` | Installs NetBird and joins with a setup key on machines that are not connected yet. Connected peers are not touched. |

Each pyinfra run happens per host, so the plan is exact for each machine.

## Quick start

```bash
uv sync
uv run flotilla scan-keys              # pin host keys into the state
uv run flotilla plan ssh               # dry run
uv run flotilla apply ssh dev          # one host, or a group name
uv run flotilla diff omp dev           # file diff, secrets masked
uv run flotilla exec 'uptime' work     # run a command on a group
uv run flotilla serve --host <netbird-ip> --port 8790
```

The web UI asks for a password. It reads `FLOTILLA_PASSWORD`, or creates `~/.config/flotilla/password` on first start.

Other settings:

- `FLOTILLA_CONTROLLER`: host name that runs flotilla (default: `controller` in `hosts.yaml`). The controller manages itself locally.
- `FLOTILLA_CATALOG_HOST`: host whose `omp models` catalog drives autocomplete (default: the controller).

## Security notes

- Bind the server to the NetBird address only. Anyone with the password can run commands on every host.
- The controller holds the fleet SSH key and can read the vault. Treat it like the most sensitive machine in the fleet.
- Host keys are pinned on first use over NetBird, which already authenticates the peers. Rebuilt machines need `flotilla scan-keys` again.
