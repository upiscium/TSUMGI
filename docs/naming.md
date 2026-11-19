# Naming: TSUMGI

**TSUMGI（紡ぎ）** is the project, the repository and, from v0.3.0, the whole runtime. It replaced **AstrumWeaver** in two steps ([issue #120](https://github.com/upiscium/TSUMGI/issues/120)): the project and documentation in October 2026 (Phase A), and the runtime and wire names in v0.3.0 (Phase B, plan in [design/rename-phase-b.md](design/rename-phase-b.md)).

## Names

| Surface | Name |
| --- | --- |
| GitHub project and docs | `upiscium/TSUMGI` |
| Python distribution and import namespace | `tsumgi` |
| Installed command prefix | `tsumgi-*` |
| Nix flake package output | `.#tsumgi` |
| NixOS options | `services.tsumgi.control`, `services.tsumgi.worker` |
| systemd units | `tsumgi-control.service`, `tsumgi-worker.service` |
| Service users and groups | `tsumgi`, `tsumgi-control`, `tsumgi-config` |
| Environment variables | `TSUMGI_*` |
| Configuration and state paths | `/etc/tsumgi`, `/var/lib/tsumgi`, `/var/lib/tsumgi-control` |
| Managed runtime Nix profiles | `/nix/var/nix/profiles/tsumgi-*` |

The rename is a **breaking change**. The code reads only the new names. A host that
still uses the old names is moved by the host migration tool (stage 4 of the plan),
not by hand: do not rename units, move directories or change ownership manually.
Historical release notes under `docs/releases/` keep the old names on purpose.

> Status: the repository source uses the new names, including the client-visible
> wire names (`x_tsumgi`, `x_tsumgi_input_type`, `x_tsumgi_embedding_space_id`, the
> `tsumgi.decision*` and `tsumgi.embedding_space` object types, `tsumgi-decisions-v1`,
> `tsumgi_revision`). No production host changes until the cutover in the plan.

## Source checkouts and references

The canonical GitHub remote is:

```sh
git remote set-url origin https://github.com/upiscium/TSUMGI.git
git remote -v
```

Run this **in each chosen local checkout**, after verifying which checkout is being updated. GitHub redirects the old repository URL, but redirects are not a substitute for updating hard-coded CI integrations, submodules, deployment automation, webhooks, references or flake inputs.

For Nix flake commands, use `github:upiscium/TSUMGI`. Existing pinned `github:upiscium/AstrumWeaver` references keep working through the redirect until their owners migrate them; do not rewrite locks or trigger fleet-wide rebuilds as part of renaming.
