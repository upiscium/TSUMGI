from __future__ import annotations

import json
from pathlib import Path

import pytest

from tsumgi.gateway.decision import (
    DECISION_CATALOG_SCHEMA,
    DECISION_OPERATION_SCHEMA,
    DecisionGatewayError,
    DecisionProfileCatalog,
)
from tsumgi.serving import (
    LogicalServingProfile,
    ServingContract,
    ServingDeploymentDeclaration,
)
from tsumgi.setup.repin import (
    OLD_SCHEMA,
    RepinError,
    main,
    repin_decision_catalog,
    repin_runtime_manifest,
    repin_serving_declaration,
)

from test_decision_gateway_contracts import values

OLD_PKG = "/nix/store/" + "a" * 32 + "-astrumweaver-d1-decision-cuda-sm61-runtime"
NEW_PKG = "/nix/store/" + "b" * 32 + "-tsumgi-d1-decision-cuda-sm61-runtime"


def legacy_parts():
    deployment, semantics, contract, logical, profile = values()
    old_contract = ServingContract(
        deployment_revision=contract.deployment_revision,
        capability=contract.capability,
        operation_schema=OLD_SCHEMA,
        validation_evidence_sha256=contract.validation_evidence_sha256,
        features=contract.features,
        limits=contract.limits,
        semantic_revision=contract.semantic_revision,
    )
    old_logical = LogicalServingProfile(
        profile_id=logical.profile_id,
        deployment_revision=logical.deployment_revision,
        serving_contract_revision=old_contract.revision,
        capability=logical.capability,
        operation_schema=OLD_SCHEMA,
        required_features=logical.required_features,
        limits={},
    )
    return deployment, semantics, old_contract, old_logical, contract


def legacy_catalog() -> dict:
    deployment, semantics, contract, logical, _ = legacy_parts()
    return {
        "schema_version": DECISION_CATALOG_SCHEMA,
        "profiles": [{
            "adapter_id": "llama-cpp-system-one-v1",
            "request_timeout_seconds": 10,
            "max_attempts": 2,
            "deployment": deployment.to_dict(),
            "contract": contract.to_dict(),
            "profile": logical.to_dict(),
            "semantics": semantics.to_dict(),
        }],
    }


def legacy_declaration() -> dict:
    deployment, _, contract, _, _ = legacy_parts()
    embedding = ServingContract(
        deployment_revision=deployment.revision,
        capability="text.embed",
        operation_schema="openai-embeddings-v1",
        validation_evidence_sha256="sha256:" + "7" * 64,
        features=frozenset({"vector-float"}),
        limits={"input_bytes": 4096},
        semantic_revision="sha256:" + "8" * 64,
    )
    return {
        "schema_version": "serving-deployment-v1",
        "deployment": deployment.to_dict(),
        "contracts": [
            {"serving_contract_revision": embedding.revision, "contract": embedding.to_dict()},
            {"serving_contract_revision": contract.revision, "contract": contract.to_dict()},
        ],
    }


def test_the_legacy_catalog_is_refused_by_the_new_code():
    with pytest.raises(DecisionGatewayError):
        DecisionProfileCatalog.from_dict(legacy_catalog())
    # a legacy declaration still parses (the Worker would advertise the old schema),
    # which is why Control and altair must be re-pinned together
    declaration = ServingDeploymentDeclaration.from_dict(legacy_declaration())
    assert declaration.contracts[-1].operation_schema == OLD_SCHEMA


def test_catalog_repin_loads_and_changes_only_the_decision_binding():
    old = legacy_catalog()
    new, changes = repin_decision_catalog(old)
    catalog = DecisionProfileCatalog.from_dict(new)  # accepted by the real loader
    profile = catalog.get("decision-local-v1")
    assert profile.resolved.contract.operation_schema == DECISION_OPERATION_SCHEMA
    (old_rev, new_rev), = changes
    assert old_rev != new_rev and new["profiles"][0]["profile"]["serving_contract_revision"] == new_rev
    # everything else is untouched
    for key in ("deployment", "semantics", "adapter_id", "max_attempts", "request_timeout_seconds"):
        assert new["profiles"][0][key] == old["profiles"][0][key]
    assert old["profiles"][0]["contract"]["operation_schema"] == OLD_SCHEMA  # input not mutated


def test_declaration_repin_touches_only_the_decision_contract():
    old = legacy_declaration()
    new, changes = repin_serving_declaration(old)
    ServingDeploymentDeclaration.from_dict(new)
    assert new["contracts"][0] == old["contracts"][0]  # the embedding contract is byte-for-byte the same
    assert new["contracts"][1]["contract"]["operation_schema"] == DECISION_OPERATION_SCHEMA
    assert len(changes) == 1
    # catalog and declaration agree on the new revision
    _, catalog_changes = repin_decision_catalog(legacy_catalog())
    assert changes[0][1] == catalog_changes[0][1]


def test_already_repinned_files_are_refused():
    new, _ = repin_decision_catalog(legacy_catalog())
    with pytest.raises(RepinError, match="no profile"):
        repin_decision_catalog(new)
    declaration, _ = repin_serving_declaration(legacy_declaration())
    with pytest.raises(RepinError, match="no contract"):
        repin_serving_declaration(declaration)


def test_runtime_manifest_repin_walks_strings_only(monkeypatch):
    document = {
        "provider_id": "llama-cpp-dual",
        "extra": [OLD_PKG, OLD_PKG + "/bin/astrumweaver-llama-server", "unrelated", {"p": OLD_PKG + "/lib/x"}],
    }
    monkeypatch.setattr("tsumgi.setup.repin.RuntimeDeploymentSpec.from_dict", classmethod(lambda cls, d: None))
    out = repin_runtime_manifest(document, OLD_PKG, NEW_PKG)
    assert out["extra"] == [NEW_PKG, NEW_PKG + "/bin/tsumgi-llama-server", "unrelated", {"p": NEW_PKG + "/lib/x"}]
    with pytest.raises(RepinError, match="does not mention"):
        repin_runtime_manifest({"provider_id": "x"}, OLD_PKG, NEW_PKG)


def test_cli_writes_new_files_and_leaves_the_inputs(tmp_path: Path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    (src / "serving-deployment.json").write_text(json.dumps(legacy_declaration()))
    (src / "decision.json").write_text(json.dumps(legacy_catalog()))
    before = {p.name: p.read_text() for p in src.iterdir()}
    out = tmp_path / "out"
    code = main(["--out-dir", str(out), "--serving", str(src / "serving-deployment.json"), "--catalog", str(src / "decision.json")])
    text = capsys.readouterr().out
    assert code == 0 and "serving contract revision" in text
    assert {p.name: p.read_text() for p in src.iterdir()} == before
    assert (out / "serving-deployment.json").is_file() and (out / "decision.json").is_file()
    assert OLD_SCHEMA not in (out / "decision.json").read_text()
    DecisionProfileCatalog.from_dict(json.loads((out / "decision.json").read_text()))


def test_cli_refuses_the_input_directory_bad_arguments_and_unrepinnable_files(tmp_path: Path, capsys):
    f = tmp_path / "decision.json"
    f.write_text(json.dumps(legacy_catalog()))
    assert main(["--out-dir", str(tmp_path), "--catalog", str(f)]) == 2
    assert "must not be the input directory" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--out-dir", str(tmp_path / "o")])
    with pytest.raises(SystemExit):
        main(["--out-dir", str(tmp_path / "o"), "--runtime-manifest", str(f)])
    new, _ = repin_decision_catalog(legacy_catalog())
    done = tmp_path / "done.json"
    done.write_text(json.dumps(new))
    assert main(["--out-dir", str(tmp_path / "o"), "--catalog", str(done)]) == 2
    assert not (tmp_path / "o").exists()  # nothing is written when anything fails
