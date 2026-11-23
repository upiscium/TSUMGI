"""Re-pin the decision files for the v0.3.0 rename (``astrumweaver-decisions-v1`` -> ``tsumgi-decisions-v1``).

A serving contract's revision hashes its ``operation_schema``, so the new name
gives altair's decision contract a new revision. Three files carry it and are
written, never edited in place:

* altair's serving declaration (the decision contract and its revision);
* Control's decision catalog (the contract, the profile and the profile's
  ``serving_contract_revision``);
* optionally altair's runtime manifest, when the runtime package changed name
  (the launcher must be ``<package>/bin/tsumgi-llama-server``).

Both outputs are loaded back through the same classes the Worker and Control
use, so a file that would be refused at start-up is refused here. Chat and
embedding contracts do not contain the name and are left byte-for-byte alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..gateway.decision import DECISION_OPERATION_SCHEMA, DecisionProfileCatalog
from ..runtime.deployment import RuntimeDeploymentSpec
from ..serving import ServingContract, ServingDeploymentDeclaration

OLD_SCHEMA = "astrumweaver-decisions-v1"
OLD_LAUNCHER = "astrumweaver-llama-server"
NEW_LAUNCHER = "tsumgi-llama-server"


class RepinError(RuntimeError):
    pass


def _dump(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _new_contract(contract: Mapping[str, Any]) -> ServingContract:
    data = dict(contract)
    data["operation_schema"] = DECISION_OPERATION_SCHEMA
    return ServingContract(**data)


def repin_serving_declaration(document: Mapping[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Returns the new declaration and ``(old, new)`` contract revisions."""
    out = json.loads(json.dumps(document))
    changes: list[tuple[str, str]] = []
    for item in out.get("contracts", []):
        contract = item.get("contract", {})
        if contract.get("operation_schema") != OLD_SCHEMA:
            continue
        new = _new_contract(contract)
        changes.append((item.get("serving_contract_revision", ""), new.revision))
        item["contract"] = new.to_dict()
        item["serving_contract_revision"] = new.revision
    if not changes:
        raise RepinError(f"the serving declaration has no contract with operation_schema {OLD_SCHEMA}")
    ServingDeploymentDeclaration.from_dict(out)  # refused here as it would be at Worker start-up
    return out, changes


def repin_decision_catalog(document: Mapping[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    out = json.loads(json.dumps(document))
    changes: list[tuple[str, str]] = []
    for entry in out.get("profiles", []):
        contract = entry.get("contract", {})
        if contract.get("operation_schema") != OLD_SCHEMA:
            continue
        new = _new_contract(contract)
        profile = entry["profile"]
        changes.append((profile.get("serving_contract_revision", ""), new.revision))
        entry["contract"] = new.to_dict()
        profile["operation_schema"] = DECISION_OPERATION_SCHEMA
        profile["serving_contract_revision"] = new.revision
    if not changes:
        raise RepinError(f"the decision catalog has no profile with operation_schema {OLD_SCHEMA}")
    DecisionProfileCatalog.from_dict(out)
    return out, changes


def repin_runtime_manifest(document: Mapping[str, Any], old_package: str, new_package: str) -> dict[str, Any]:
    """Point a runtime manifest at the new runtime package and its renamed launcher."""

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            if value == old_package or value.startswith(old_package + "/"):
                value = new_package + value[len(old_package):]
            if value.startswith(new_package + "/bin/") and value.endswith("/" + OLD_LAUNCHER):
                value = value[: -len(OLD_LAUNCHER)] + NEW_LAUNCHER
            return value
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    out = walk(json.loads(json.dumps(document)))
    if out == document:
        raise RepinError(f"the runtime manifest does not mention {old_package}")
    RuntimeDeploymentSpec.from_dict(out)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tsumgi-repin-decision", description=__doc__.split("\n\n")[0])
    parser.add_argument("--out-dir", required=True, help="where the new files are written (never the input directory)")
    parser.add_argument("--serving", help="altair's serving-deployment.json")
    parser.add_argument("--catalog", help="Control's decision catalog")
    parser.add_argument("--runtime-manifest", help="altair's runtime-deployment.json")
    parser.add_argument("--runtime-old", help="the old runtime package store path (with --runtime-manifest)")
    parser.add_argument("--runtime-new", help="the new runtime package store path (with --runtime-manifest)")
    args = parser.parse_args(argv)
    if not (args.serving or args.catalog or args.runtime_manifest):
        parser.error("give at least one of --serving, --catalog, --runtime-manifest")
    if bool(args.runtime_manifest) != bool(args.runtime_old) or bool(args.runtime_old) != bool(args.runtime_new):
        parser.error("--runtime-manifest needs --runtime-old and --runtime-new")
    out_dir = Path(args.out_dir).resolve()
    jobs: list[tuple[Path, str]] = []
    try:
        for source, kind in ((args.serving, "serving"), (args.catalog, "catalog"), (args.runtime_manifest, "runtime")):
            if not source:
                continue
            path = Path(source).resolve()
            if path.parent == out_dir:
                raise RepinError(f"{path.name}: the output directory must not be the input directory")
            document = json.loads(path.read_text(encoding="utf-8"))
            if kind == "serving":
                new, changes = repin_serving_declaration(document)
            elif kind == "catalog":
                new, changes = repin_decision_catalog(document)
            else:
                new, changes = repin_runtime_manifest(document, args.runtime_old, args.runtime_new), []
            jobs.append((out_dir / path.name, _dump(new)))
            for old_rev, new_rev in changes:
                print(f"{path.name}: serving contract revision {old_rev} -> {new_rev}")
            if kind == "runtime":
                print(f"{path.name}: runtime package {args.runtime_old} -> {args.runtime_new}")
    except (RepinError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"tsumgi-repin-decision: {exc}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    for target, text in jobs:
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
