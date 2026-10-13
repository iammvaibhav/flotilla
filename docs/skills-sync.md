# Skills sync (design, not implemented)

Status: design only. The rollout is parked until 2026-10-16. Nothing in flotilla writes skills yet.

## Goal

Every host gets the omp skills its layers ask for, with the same layering as the rest of flotilla: fleet defaults (`all`), then the host's groups (`personal`, `work`), then the host. The most specific layer wins, and each layer can add, replace or remove a skill.

## State layout

Skill sources live once, in a library in the state repo:

```
files/skills/<name>/SKILL.md          one directory per skill (any extra files too)
files/skills-groups/<group>/<name>/   optional group-specific version of a skill
files/skills-hosts/<host>/<name>/     optional host-specific version of a skill
```

Which skills a host gets is a set in the vars, kept as a map like `paseo_models.visible`:

```yaml
# vars/all.yaml
skills:
  omp:                       # -> ~/.omp/agent/skills/<name>/
    shared-chrome: true
  agents:                    # -> ~/.agents/skills/<name>/
    aside-vault: true

# vars/groups/personal.yaml
skills:
  omp:
    llm-gateway: true        # add for one group

# vars/hosts/blrofc3.yaml
skills:
  omp:
    shared-chrome: false     # remove one inherited from a lower layer
```

- `true` installs the skill; `false` deletes it from the host; `null` drops the key from the merged vars (as everywhere in flotilla), so the skill is no longer managed and is left as it is on the host.
- The skill's files come from the most specific source that has the skill: `skills-hosts/<host>/`, then `skills-groups/<group>/` (in the host's group order, last wins), then `files/skills/`. A layer replaces a skill directory as a whole; files inside one skill are not merged.

## Behaviour on a host

- A selected skill directory is written exactly as rendered; files in it that the state does not have are removed (the directory is owned).
- A skill set to `false` is deleted.
- Skills that the vars do not mention are left alone, so hand-installed skills and omp's `~/.omp/agent/managed-skills` are never touched. This is the difference from `agents/` and `rules/`, which flotilla owns completely.
- Secrets inside skills are aside:// references, resolved on the controller like other files, and written with mode 600.
- `flotilla plan skills` lists `add`, `update` and `remove` per skill; `flotilla diff skills <host>` shows file diffs.

## Fleet today (2026-10-13)

| Skill | Root | Hosts | Copies |
|---|---|---|---|
| shared-chrome | `~/.omp/agent/skills` | all six | identical |
| use_gpu | `~/.omp/agent/skills` | dev, macbook, blrofc3 | identical |
| llm-gateway | `~/.omp/agent/skills` | dev, macbook, blrofc3 | three different versions |
| aside-vault | `~/.agents/skills` | all six | identical |

## Rollout

1. Copy shared-chrome, use_gpu and aside-vault into `files/skills/` and select them in `vars/all.yaml` (use_gpu: decide whether it belongs on every host or only the three where it is today).
2. Pick the canonical llm-gateway (or per-group versions in `files/skills-groups/`).
3. Implement the `skills` component, plan against all hosts, and apply only when the plan shows no unexpected removals.
