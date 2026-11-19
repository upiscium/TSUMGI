{ lib, python312Packages }:

let
  # The pinned nixpkgs FastAPI package currently pulls its own upstream
  # inline-snapshot test dependency, whose test suite is broken independently
  # of TSUMGI. TSUMGI consumes FastAPI as a runtime dependency only,
  # so strip FastAPI's check-only inputs rather than overriding inline-snapshot
  # globally.
  fastapiRuntime = python312Packages.fastapi.overridePythonAttrs (_: {
    doCheck = false;
    nativeCheckInputs = [ ];
  });
in
python312Packages.buildPythonPackage {
  pname = "tsumgi";
  version = "0.2.1";
  pyproject = true;

  src = lib.cleanSourceWith {
    src = ../.;
    filter = path: type:
      let
        base = baseNameOf path;
      in
      !(base == ".git" || base == ".worktrees" || base == "__pycache__");
  };

  build-system = [ python312Packages.hatchling ];

  dependencies = with python312Packages; [
    fastapiRuntime
    httpx
    pydantic
    uvicorn
  ];

  pythonImportsCheck = [
    "tsumgi"
    "tsumgi.control"
  ];

  doCheck = false;

  meta = {
    description = "TSUMGI deployment-agnostic compute fabric for heterogeneous compute nodes";
    homepage = "https://github.com/upiscium/TSUMGI";
    license = lib.licenses.bsd3;
    platforms = lib.platforms.linux;
  };
}
