# TSUMGI

TSUMGI is a deployment-agnostic compute fabric for scheduling capability-based
workloads across heterogeneous compute nodes: a Control plane with Gateways, and
Workers that run on GPU hosts. It is GPU-first, but its contracts are not tied to
LLMs, a particular accelerator generation, Proxmox, or a one-GPU-per-worker topology.

The current stable release is [v0.3.0](https://github.com/upiscium/TSUMGI/releases/tag/v0.3.0).
The source and issue tracker are on [GitHub](https://github.com/upiscium/TSUMGI).

```{toctree}
:maxdepth: 2
:caption: Get started

getting-started
installation
nix
deployment
deployment-profiles
naming
host-migration
```

```{toctree}
:maxdepth: 2
:caption: Operate

admin-console
setup-tui
runtime-setup
setup-filesystem-safety
operator-rollout-gates
worker-liveness
worker-offline-recovery
borrowable-worker
hardware-e2e
```

```{toctree}
:maxdepth: 2
:caption: Concepts and contracts

architecture
control-plane
protocol-v1
worker-contract
executor-contract
host-contract
runtime-supervision
runtime-compatibility
runtime-providers
```

```{toctree}
:maxdepth: 2
:caption: Runtimes

runtime-llama-cpp
runtime-strata
runtime-freetoken
runtime-ollama
runtime-vllm
runtime-exllamav3
qwen38-upstream-llama-cpp
dual-resident-embedding-decision
```

```{toctree}
:maxdepth: 2
:caption: Acceptance and release gates

release-readiness
runtime-deployment-acceptance
decision-d1-3b-acceptance
decision-d1-3b-gpu-deployment
decision-shadow-acceptance
opencode-chat-acceptance
opencode-independent-review
audio-artifact-mvi-acceptance
coding-pilot
release-gate-2026-10-14
rc-qualification-v0.1.0-rc.1
ga-qualification-v0.1.0
```

```{toctree}
:maxdepth: 1
:caption: Release notes
:glob:
:reversed:

releases/*
```

```{toctree}
:maxdepth: 1
:caption: Design records
:glob:

design/*
```

```{toctree}
:maxdepth: 1
:caption: Decisions and validation
:glob:

adr/*
validation/*
```
