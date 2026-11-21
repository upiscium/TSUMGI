# #180 / #216: explicit Style-Bert-VITS2 Worker for the JVNV Japanese F1 voice

This is a **default-off**, pre-provisioned Linux x86-64 CPU Worker candidate.
It is a second, independent `audio.synthesize` provider, not a VOICEVOX voice.
The public alias is `jvnv-f1-neutral` (speaker `0`, style `0` / `Neutral`).
Credit: `litagin / JVNV (CC BY-SA 4.0)`. No automatic model download, setup
action, service installation, synthesis fallback, or default catalog.

## Operator inputs

Use the normal `[runtime].manifest` and `[serving].manifest` settings. The
Worker must be non-root, dedicated to `audio.synthesize`, one slot, no GPU,
with at least 4096 MiB of total host RAM, a loopback Control URL and a loopback
health listener. Select `provider_id: "style-bert-vits2"` and the exact demand:

```json
{"model":{"model_ref":"jvnv-F1-jp","model_format":"safetensors","topology":"dense"},
 "residency_policy":"cpu_gpu_hybrid","gpu_topology":"none"}
```

The `provider_config` fields are:

* `enabled`: literal `true`. Omission does not opt in.
* `model_directory`: one canonical directory named `jvnv-F1-jp` containing
  only the reviewed safetensors, `config.json` and `style_vectors.npy`.
  Its sibling `bert/` must contain the pinned safetensors and the four
  reviewed config/tokenizer files; do not put another weight format there.
* `interpreter_path`: the reviewed **copied** CPython 3.11.14 venv `bin/python`
  (not a symlink). `dependency_lock_path` points to the reviewed hashed lock.
  Runtime requirements include CPU torch `2.3.1+cpu`, Style-Bert-VITS2 2.7.0
  at `d8148f3090ee5038ca7b4e4b327116c64467f952` and `pyopenjtalk-dict`
  `0.3.4.dev2`. A matching lockfile is not proof of all installed package bytes.
  This first implementation accepts only the interpreter and lock byte hashes
  in `jvnv_assets.py`, not any installation reporting those versions. It
  supplies neither a general installer nor a portable wheel lock.
* `dictionary_directory`, `dictionary_sha256`: the already provisioned Open
  JTalk system dictionary and the **separately reviewed** digest of its entire
  bounded file inventory. `jvnv_assets.dictionary_inventory_sha256(path)`
  computes the digest without starting a runtime. A missing or changed
  dictionary must not be silently installed on first use: the reviewed
  `pyopenjtalk-dict` CPython 3.11 Linux x86-64 wheel does not itself contain
  `sys.dic`. Do not accept a
  newly computed digest without reviewing the dictionary's provenance.
* `private_home`: separate, pre-provisioned, **empty** `0700` working/HOME/temp
  directory owned by the Worker. Rotate it explicitly when it contains state;
  this provider does not empty directories. No Job text is put in argv or env.
* `rights_review_sha256` and `validation_evidence_sha256`: separate operator
  review records. Use actual approved records, not test placeholders. File
  ownership and a checksum do not grant usage or redistribution rights.

Voice artifacts: [litagin/style_bert_vits2_jvnv, pinned snapshot](https://huggingface.co/litagin/style_bert_vits2_jvnv/tree/205830ca1d49e666ddfbf2a755f0108e9cade4dd).
Encoder: [ku-nlp/deberta-v2-large-japanese-char-wwm, pinned snapshot](https://huggingface.co/ku-nlp/deberta-v2-large-japanese-char-wwm/tree/547b0e8b044fba3f9b84d0ab9f990440bd130c8b).
The checks do not replace operator review of software, voice, dictionary or
encoder licenses. No weights or runtime dependencies are distributed here.

Generate the expected serving document from the trusted TSUMGI package using
`StyleBertManifestConfig(**provider_config).serving_declaration().to_dict()`,
then review it against the intended Gateway catalog. The execution identity
includes the native/dependency pins, dictionary inventory, rights receipt and
code bytes, but not temporary directories, credentials or host-local paths.
The old one-shot trial manifests are not interchangeable with this declaration.

## Readiness and evidence boundary

Starting an enabled native Worker performs **one real readiness synthesis**
of the fixed phrase `音声の準備を確認します。`, not a durable Job.
The pinned child receives a fixed bootstrap frame and
returns the entire bounded 44.1 kHz mono PCM16 probe over its private pipe.
The parent checks the canonical header, sample count, non-silence and SHA-256,
then discards the probe. A metadata-only READY, bad probe or changed asset
does not permit registration. Job inference uses a separate frame and makes a
new inference call. A failed instance is not restarted on another provider.

Keep the installed interpreter, dependencies, dictionary, assets and their
ancestor directories protected from concurrent modification. Hash and inode
checks are not an atomic guarantee about a native library's in-memory state.
HF offline flags and a child-side guard against Python socket/HTTP fallbacks
are not an OS-level network sandbox; restrict egress in the actual service.

The tests use tiny synthetic files, stdlib subprocesses and an in-process
Control API. They cover receipt of probe bytes before registration, rejection
and reaping on failures (including cancellation during spawn), and a renewed
lease on the same attempt/epoch. With JVNV and VOICEVOX in the same speech
catalog but only a JVNV Worker online, a VOICEVOX request gets 503 and no Job;
a JVNV request returns the Job WAV, not the probe. The separate artifact GET
must match the Gateway bytes. These tests do **not** qualify native quality/latency,
PostgreSQL deployment, a shared three-provider installation or release. Any
native trial must budget the readiness synthesis separately from Job inference.
