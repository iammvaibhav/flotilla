# Onboarding a machine

This guide is written for a person or an agent. Do the steps in order. Every step can be repeated; each `flotilla plan` shows what would change before you apply it.

The controller is the machine that runs flotilla. Run all `flotilla` commands there.

## 0. Before you start

You need:

- A user account on the new machine with SSH access from the controller, or console access to add a key.
- Passwordless `sudo` only for step 1 (NetBird install). Nothing else needs root.
- The machine's group (for example `personal` or `work`). The group decides git identity and omp models.

## 1. Join the NetBird network

The fleet talks over NetBird. Use a **setup key** so no browser login is needed.

- Long-lived machine: a one-off or reusable setup key.
- Disposable machine that is rebuilt often: a **reusable, ephemeral** setup key. NetBird deletes an ephemeral peer that has been offline for 10 minutes, so the rebuilt machine can take the same name again.

Keep the key in the vault, never in a file in a repository. On the new machine:

```bash
curl -fsSL https://pkgs.netbird.io/install.sh | sh
sudo netbird up --setup-key "$NB_SETUP_KEY"
netbird status        # note the NetBird IPv4 address and FQDN
```

Use the NetBird **IPv4** address as the host address. Some networks cannot resolve NetBird DNS names, and some clients resolve names to the IPv6 address first.

## 2. Let the controller in

Add the fleet public key (the `ssh.pubkey` value in the flotilla vars) to `~/.ssh/authorized_keys` of the new user. Use cloud-init, the provider console, or an existing SSH key:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo "<fleet public key>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

## 3. Add the machine to the inventory

In the web UI: **Onboard → Add to inventory**. Or edit `hosts.yaml` in the state directory:

```yaml
hosts:
  new-box:
    address: 100.x.y.z          # NetBird IPv4
    user: ubuntu
    platform: linux-x64         # linux-x64 | linux-arm64 | darwin-arm64
    groups: [personal]
    names: [new-box.netbird.cloud]
```

Then pin its SSH host key. Over NetBird the peer is already authenticated, so this first-use pin is safe:

```bash
flotilla scan-keys
```

## 4. Apply the components

Apply them in this order. Read each plan first.

```bash
flotilla plan ssh new-box        && flotilla apply ssh new-box
flotilla plan toolchain new-box  && flotilla apply toolchain new-box
flotilla plan github new-box     && flotilla apply github new-box
flotilla plan omp new-box        && flotilla apply omp new-box
```

| Component | What it does |
|---|---|
| `ssh` | Fleet key, `~/.ssh/config.d/flotilla.conf` (short names for every host), pinned host keys, `Include` at the top of `~/.ssh/config`. |
| `toolchain` | uv and pnpm, with a 7-day release cooldown (`exclude-newer`, `minimumReleaseAge`). Optional Node through `pnpm runtime`. |
| `github` | `gh` (newest release older than 7 days), `gh` login, git name and email for the host's group. |
| `omp` | omp binary if missing, layered `config.yml` / `models.yml` / `account-routing.yml`, `agents/`, `rules/`, and the fleet `AGENTS.md`. |

Then apply `ssh` to every host, so the other hosts learn the new machine's name and host key:

```bash
flotilla apply ssh
```

## 5. Paseo

Paseo is built once per platform on a builder host and copied to the others. To add the machine, add it to the Paseo deploy inventory on the build host and run the Paseo deploy there. The Paseo repository documents the inventory format and the deploy command.

## 6. Check

- **Hosts → Check SSH mesh**: every cell is ✓, including `github`.
- **Components → Plan every component**: every cell for the new machine says "in sync".

## Problems

- `Host key verification failed`: the machine was rebuilt. Run `flotilla scan-keys`, then `flotilla apply ssh`.
- `Authentication error` from pyinfra while `ssh` works: the host allows few authentication attempts. flotilla only offers the fleet key; check that it is in `authorized_keys`.
- `fleet key conflict`: the machine already has a different `~/.ssh/id_ed25519`. flotilla never overwrites it. Move the old key away by hand if it is not needed.
- pnpm install fails while editing `.bashrc`: some terminal hooks write non-UTF-8 bytes into `.bashrc`. The binary still installs; flotilla links it into `~/.local/bin`.
