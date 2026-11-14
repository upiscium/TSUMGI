# Strata Runtime Provider

[Strata](https://github.com/Niko1221/Strata) (MIT) serves large MoE models from the GPU, host RAM and,
for models larger than RAM, the SSD. TSUMGI binds it as provider `strata` with the chat adapter
`strata-chat-v1`.

## Scope

- one Worker-owned `strata serve` process per Worker, on a loopback port
- OpenAI-compatible `llm.chat` (Strata has no `/v1/completions`), with streaming and structured tools
- local GGUF MoE models that Strata has prepared on the host
- Linux x86_64; NVIDIA RTX 20 or newer runs Strata's CUDA 13 engine, and GTX 10 (Pascal, sm_61) runs its
  experimental CUDA 12 engine

## How it runs

Strata separates preparation from serving:

1. **Setup (once per host).** `strata launch` downloads the engine for the GPU and the model, converts the
   model into Strata's pack, and writes a *run config* (`strata-<model>.json`). The run config holds the
   engine argv (`--native <gguf> --max-context N ...`), the tokenizer directory, the model name and the
   CUDA flavor. Setup must also set `"fit_max_tokens": false` in it (see [Limits](#limits)).
2. **Serving (the Worker).** The provider starts
   `strata serve --profile <run config> --host 127.0.0.1 --port P --parallel N --ready-file F
   --shutdown-timeout S --output jsonl` in its own session. It never downloads or prepares anything.

The run config is plain JSON, so the deployment identity pins it. Before a Worker starts, the provider
checks the run config against the deployment and refuses on any mismatch. It checks:

| Run config | Must equal |
| --- | --- |
| `model_name` | `served_model_name` |
| `--native` model path | the demand's `model_ref` |
| `--max-context` | `context_tokens` |
| `fit_max_tokens` | `false` |

## Provider settings

| Setting | Meaning |
| --- | --- |
| `executable` | absolute path of the Strata Terminal entry point, `.../bin/strata` |
| `run_config` | absolute path of the prepared run config |
| `home`, `data_dir` | `HOME` and `STRATA_DATA_DIR` of the Strata install. Engines, packs and logs live there, so the Worker user must be able to write them |
| `ready_dir` | private directory for the ready file. The default is the Worker unit's `RuntimeDirectory`, `/run/astrumweaver-worker` |
| `base_url` | `http://127.0.0.1:<port>` only (default port 8080); Strata binds IPv4 loopback |
| `served_model_name` | the run config's `model_name` |
| `context_tokens` | the run config's `--max-context` |
| `parallel` | concurrent requests, 1 to 8 (default 1) |
| `api_key_file` | optional key file, read once per runtime. It is passed to Strata as `STRATA_API_KEY`, never on argv, and sent as a Bearer token |
| `shutdown_timeout_seconds` | drain time Strata gives running requests on stop, 0 to 300 (default 30) |
| `enable_thinking`, `tools_enabled` | same meaning as for FreeToken |

## Readiness and stop

`strata serve` writes the ready file (mode 0600, created with `O_EXCL`) once the API is bound and the model
is loaded. The provider treats the runtime as ready only when all of these hold:

- the file's `pid` is the owned process or a member of its process group
- `model_state` and `api_state` are both `ready`
- `/v1/models` lists the served model name

The provider deletes a stale ready file before each start, because Strata refuses to start over one.

Stop sends SIGTERM to the process group, so Strata drains requests for `shutdown_timeout_seconds`. After
`shutdown_timeout_seconds` plus 15 s it sends SIGKILL, and it then reaps any engine child that is still
alive. This is the same process-group handling as FreeToken.

## Limits

Strata checks `prompt + max_tokens <= --max-context` itself. With `fit_max_tokens: false` it rejects an
oversized request with HTTP 400 ("request exceeds the context window") instead of silently shortening
`max_tokens`. The executor maps that rejection to `context_length_exceeded`, so it needs no pre-flight
token count. Strata's only token counter is the Anthropic-format `/v1/messages/count_tokens`.

Strata has no separate input-only limit. A Strata serving contract therefore declares
`input_tokens == total_tokens == context_tokens`, and the Worker refuses to start on any other contract
(`validate_serving_contract`). The Gateway still caps `max_tokens` at `output_tokens` before a request
reaches the Worker.

Other Strata errors are mapped as follows. An `error` event inside an SSE stream keeps its meaning:
`invalid_request_error` counts as 400, `model_busy` as 409, anything else as 500.

| Strata response | Mapped to |
| --- | --- |
| 408, 409 (model busy), 429 | retryable `provider_busy` |
| 401, 403, 404 (key or route mismatch, a deployment fault) | `provider_error`, not retryable |
| any other 4xx | `invalid_request` |
| 5xx, or Strata unreachable | retryable `provider_error` |
| a response body that is not valid JSON | `invalid_provider_response` |

Cancelling a Job closes the HTTP request. Strata may still finish that answer, so with `parallel: 1` the
next request can briefly get `provider_busy`.

## Models

Qwen3.6-35B-A3B needs Unsloth's GGUF **with the MTP layer** (`unsloth/Qwen3.6-35B-A3B-MTP-GGUF`). The
plain GGUF fails to load with `missing qwen35moe.nextn_predict_layers`. Strata's setup downloads the right
file at a pinned revision.

Measured on a GTX 1080 Ti 11GB with 32GB RAM (Strata 0.2.13, engine 0.1.59 cuda12, Qwen3.6 UD-Q4_K_XL,
thinking off):

- decode: 32 to 38 tokens/s
- a 10,000-token prompt: 11 s
- tool calls, tool results and streaming worked

For comparison, FreeToken on the same GPU at 12K decodes 22 to 25 tokens/s and reads the same prompt in
25 s.

## Host setup

Install Strata at a pinned release, prepare the model once as root, then hand the Strata home to the
Worker user:

```sh
R=/opt/astrumweaver/strata
mkdir -p $R && cd $R
curl -fsSLO https://github.com/Niko1221/Strata/releases/download/v0.2.13/install.sh
echo "d47754af1f74c95bf763e0b8d484232443466decd1a22d0be3208760a2f53822  install.sh" | sha256sum -c
export HOME=$R/home STRATA_DATA_DIR=$R/data STRATA_UPDATE_FEED=https://github.com/Niko1221/Strata/releases/download/v0.2.13
STRATA_FEED=$STRATA_UPDATE_FEED STRATA_BIN_DIR=$R/bin sh install.sh   # checks the signed feed
# Downloads the engine and the model, prepares the pack and writes the run config.
$R/bin/strata launch --family qwen36 --model ud-q4_k_xl --context 12288 --vision off --parallel 1 \
  --tune skip --access local --port 8080 --no-api-key --yes --no-tui
$R/bin/strata launch --stop
```

Copy `$R/home/.local/share/strata/install/strata-<model>.json` to a root-owned path. Set
`"fit_max_tokens": false` in the copy and keep `--max-context` equal to the contract's `total_tokens`.
Then point `run_config` at the copy and run `chown -R astrumweaver: $R/home`. The model files, packs and
the Terminal stay root-owned and world-readable.

`strata launch --no-tui` hands the server to a background owner. A transient systemd unit that runs it
therefore kills the server on exit. The Worker always runs `strata serve` in the foreground instead.

## Worker unit

Strata needs no extra socket families: it ran under the stock `RestrictAddressFamilies=AF_UNIX AF_INET
AF_INET6`, unlike FreeToken, which also needs `AF_NETLINK`. Under `ProtectSystem=strict` the Worker needs
one extra writable path, the Strata home, which holds the engine log, state and caches:

```ini
[Service]
ReadWritePaths=/opt/astrumweaver/strata/home
# first start of a large model; Strata reads every expert once
TimeoutStartSec=900
```

Lab E2E on altair: loopback Control, the Worker as `astrumweaver` under these unit settings, and
Qwen3.6 UD-Q4_K_XL at 12K.

| Check | Result |
| --- | --- |
| Gateway chat | passed |
| streaming | passed |
| tool call and tool result | passed |
| 10,000-token prompt | 11 s |
| over-context request | `context_length_exceeded` |
| over-context stream | SSE error with `context_length_exceeded` |
| Worker stop | the Strata processes and GPU memory were released |
| HumanEval+ | 150/164 (FreeToken with the same GGUF quantization: 151) |
