{ lib, buildEnv, python312, python312Packages, writeShellScriptBin, tsumgi }:

let
  # Keep the PostgreSQL dependency in Control/Installer, not the Worker.
  # The Python console script installed by the base package has its own
  # interpreter closure. Calling that script from python.withPackages does
  # not automatically make the additional psycopg package importable.
  pythonEnv = python312.withPackages (_: [
    tsumgi
    python312Packages.psycopg
    python312Packages.psycopg-pool
  ]);

  # Bind each executable to the interpreter that actually contains psycopg.
  # Higher priority is intentional: replace the base console wrappers.
  control = lib.hiPrio (writeShellScriptBin "tsumgi-control" ''
    exec ${pythonEnv}/bin/python -m tsumgi.control.daemon "$@"
  '');
  migrate = lib.hiPrio (writeShellScriptBin "tsumgi-migrate" ''
    exec ${pythonEnv}/bin/python -m tsumgi.control.migrate "$@"
  '');
in
buildEnv {
  name = "tsumgi-control-cli-env";
  paths = [ control migrate pythonEnv ];
  ignoreCollisions = true;
  meta = {
    description = "TSUMGI Control and migration entrypoints with role-local PostgreSQL";
    license = lib.licenses.bsd3;
    platforms = lib.platforms.linux;
  };
}
