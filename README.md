# flotilla

Manage a small fleet of machines on a NetBird network from one place: SSH between every host, GitHub access, toolchains, and config files (omp config, skills, Paseo settings) kept as a base plus small per-group and per-host overrides. Changes are planned first (dry run with diffs) and then applied over SSH. Nothing is installed on the managed machines.

- **Hosts**: live status of every machine (versions, GitHub login, NetBird, disk, load) and a click-to-run command drawer.
- **Config**: the state directory exactly as it is on the controller, in a file tree and a code editor: create, edit and delete any file (a save that would break a bundle or `hosts.yaml` is refused). A bundle's overview lists its files with a label for every group or host that overrides one; click a name for base, a label for that override. A group or host file has **Diff vs base**: what it removes and adds, unsaved edits included (merged files are compared after merging; a host file as that host gets it). **Apply…** shows the diff for every host, then applies.
- **Components**: plan or apply the installers per host or group: `ssh`, `github`, `toolchain`, `omp-binary`, `netbird`, `paseo-deploy`, `docker`, `browser`.

Agents use it through the CLI and the `flotilla` skill (in the `skills` bundle, so every host has it).

## How it works

The desired state lives in a **private state directory** (default `~/.config/flotilla/state`, override with `FLOTILLA_STATE`). It is not part of this repository and is versioned in its own git repo; every change through the web UI or the CLI is a commit. If that repo has an `origin` remote (on the fleet: the private `iammvaibhav/flotilla-state`), each commit is also pushed, in the background.

```
hosts.yaml                  machines: address, user, platform, groups
bundles/<name>/             files placed on hosts
  bundle.yaml               destination and merge rules
  README.md                 notes for people and agents (not copied)
  base/...                  files for every host
  groups/<group>/...        only what differs for one group
  hosts/<host>/...          only what differs for one host
settings/                   Flotilla's own settings, same layers (not copied)
known_hosts                 pinned SSH host keys (flotilla scan-keys)
```

Layers apply in order: `base`, the host's groups (in the order the host lists them), the host. The most specific layer wins.

- YAML and JSON files (`merge: keys` in `bundle.yaml`) merge key by key. An override file holds only what differs:

  ```yaml
  modelRoles.default: anthropic/claude-opus-5-5:high   # one key, any depth
  disabledProviders+: [grok-build]                     # add to a list
  disabledProviders-: [ollama]                         # remove from a list
  retry.fallbackChains!: {default: [cursor/x]}         # replace, no merge
  auth.broker: ~                                       # delete the key
  ```

  Dots split a key into a path only at the top level of an override file, so a nested key with a dot in it stays literal. `+`, `-` and `!` work at any depth.
- Other files are replaced whole by the most specific layer that has them.
- A file that only one layer has is written exactly as it is (comments kept).
- `name.j2` is a Jinja template rendered per host into `name` (`me` is the host, `hosts` the managed hosts). `name.json.yml` is YAML, with comments, written as `name.json`.
- `_layer.yml` in a layer: `skip: [item]` does not place an item there and leaves it alone on the host; `delete: [item]` removes it from those hosts.

`bundle.yaml`:

```yaml
description: omp settings, models, agents, rules and the fleet AGENTS.md
dest: ~/.omp/agent          # directory on every host
unit: file                  # or directory: one folder = one item (skills)
files:                      # first match wins; the default is whole
  - {match: "*.yml", merge: keys}
  - {match: config.json, merge: keys, own_keys: [agents.providers]}
owned: [agents, rules]      # files there that the bundle lacks are deleted
after_apply: paseo daemon reload --home ~/.paseo   # on hosts that changed
```

`own_keys` makes Flotilla set only those keys of a host file that another program also writes (Paseo's `config.json`). To manage a new set of files, add a folder with a `bundle.yaml`; no code change.

Bundles today: `omp` (`~/.omp/agent`: settings, models, account routing, agents, rules, and the fleet `AGENTS.md`), `skills` (`~/.agents/skills`), `paseo` (agent providers, visible models and memory in `~/.paseo/config.json`).

### Sync

Flotilla compiles each bundle per host on the controller, reads the host's files over SSH, and lists what would change (plan). Apply writes only those files, atomically, then runs `after_apply`. Nothing changes on a host until someone applies. After every change, and every 30 minutes, the web app re-plans every bundle in the background; the file tree marks bundles that are not applied on every host.

### Hosts

A host's `address` is its NetBird IP, or its NetBird DNS name for a machine whose IP changes when it is rebuilt; the controller resolves the name when it needs the IP. A host that cannot resolve NetBird names sets `ssh.netbird_dns: false` in its settings; its SSH config then gets IPs plus a `HostKeyAlias`.

`managed: false` marks a machine Flotilla does not manage: it keeps an SSH name and a pinned key on the other hosts, and no component or bundle ever runs there.

### Secrets

State files never hold secret values. They hold references that the `aside-vault` CLI resolves on the controller when it renders files:

```yaml
apiKey: aside://you@example.com/<item-id>            # password field
token:  aside://you@example.com/<item-id>#notes      # another field
```

Saving a file with a literal secret (a key named like `apiKey`, `token`, `secret`, `password`) is refused. Diffs and logs mask secret values, including literal ones already on a host. Files that hold a secret are written with mode `600`.

### Components

| Component | What it manages |
|---|---|
| `ssh` | Fleet key `~/.ssh/id_ed25519` (never overwrites a different key), `~/.ssh/config.d/flotilla.conf` with short names for every host, pinned host keys, an `Include` at the top of `~/.ssh/config`, and removal of retired keys (`ssh.remove_keyfiles`, `ssh.remove_pubkeys`). |
| `toolchain` | uv and pnpm with release cooldowns (`exclude-newer`, `minimumReleaseAge`), links in `~/.local/bin`, Node through `pnpm runtime` on hosts with no node (nvm hosts keep theirs), and the pinned agent-browser native binary in `~/.local/bin`. |
| `docker` | Docker Engine (get.docker.com, passwordless sudo) and the user in the `docker` group, on Linux hosts with `docker.enabled`. |
| `browser` | Shared browser on hosts with `browser.enabled`: `lscr.io/linuxserver/chromium` (Chrome + Selkies live view) and a CDP proxy in `~/shared-chrome`, bound to the host's NetBird IP, no login or token. Leaves the profile in `~/shared-chrome/config` alone. A config change recreates the container, which restarts Chrome. The `*.chrome.thevm.space` names and index page are prod's Caddy, not flotilla. |
| `github` | `gh` (newest release older than 7 days), `gh` login from a vault token, git name and email per group. |
| `omp-binary` | omp installed if missing; old npm-global copies removed (`omp.remove_npm_global`). |
| `netbird` | Installs NetBird and joins with a setup key on machines that are not connected yet. Connected peers are not touched. |
| `paseo-deploy` | Adds missing hosts to the Paseo deploy inventory as targets of the builder for their platform, updates the address of a host that moved and the host's `services` from the layered `paseo.services` setting (`omp: true` adds, `false` removes, unlisted services are left alone), then runs the Paseo deploy for those hosts only (`--hosts`). |

Components run with pyinfra, one process per host.

## Quick start

```bash
uv sync
uv run flotilla scan-keys                                  # pin host keys into the state
uv run flotilla bundles                                    # list the bundles
uv run flotilla show omp config.yml --host dev --explain   # what dev gets, and from which layer
uv run flotilla override omp config.yml modelRoles.default anthropic/claude-opus-5-5:high --group work
uv run flotilla skip skills use_gpu --host prod            # --delete, --clear
uv run flotilla plan omp                                   # dry run (bundle or component)
uv run flotilla diff omp dev                               # file diff, secrets masked
uv run flotilla apply omp work                             # one host, or a group name
uv run flotilla exec 'uptime' work                         # run a command on a group
uv run flotilla serve --host <netbird-ip> --port 8790
```

The web UI asks for a password. It reads `FLOTILLA_PASSWORD`, or creates `~/.config/flotilla/password` on first start. On the fleet the controller serves it at `https://flotilla.thevm.space`: a public DNS-only record pointing at the controller's NetBird IP, and Caddy on the controller (DNS-01 certificate) that accepts NetBird source addresses only and proxies to the NetBird-bound server.

Other settings:

- `FLOTILLA_CONTROLLER`: host name that runs flotilla (default: `controller` in `hosts.yaml`). The controller manages itself locally.
- `FLOTILLA_CATALOG_HOST`: host whose `omp models` catalog drives autocomplete (default: the controller).
- `FLOTILLA_NO_PLANNER=1`: do not re-plan bundles in the background.

## Security notes

- Bind the server to the NetBird address only. Anyone with the password can run commands on every host.
- The controller holds the fleet SSH key and can read the vault. Treat it like the most sensitive machine in the fleet.
- Host keys are pinned on first use over NetBird, which already authenticates the peers. Rebuilt machines need `flotilla scan-keys` again.
