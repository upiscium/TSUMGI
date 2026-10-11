{ pkgs, control, installer }:

# The actual installed Nix console entrypoint (not an unrelated
# python.withPackages interpreter) must be able to import psycopg.
# Supply intentionally invalid libpq conninfo, so no database/network
# access is required during sandboxed derivation execution.
pkgs.runCommand "astrumweaver-postgres-cli-closure" { } ''
  set -eu
  for migrate in \
    ${control}/bin/astrumweaver-migrate \
    ${installer}/bin/astrumweaver-migrate; do
    if ASTRUMWEAVER_DATABASE_URL=not-a-valid-dsn "$migrate" \
        > "$TMPDIR/migrate.out" 2>&1; then
      echo "invalid conninfo unexpectedly succeeded" >&2
      exit 1
    fi
    if grep -Fq "psycopg is required" "$TMPDIR/migrate.out"; then
      echo "installed migration CLI lacks psycopg dependency" >&2
      exit 1
    fi
    if ! grep -Eq "ProgrammingError|missing.*after" "$TMPDIR/migrate.out"; then
      echo "migration CLI did not reach psycopg conninfo validation" >&2
      cat "$TMPDIR/migrate.out" >&2
      exit 1
    fi
  done

  mkdir -p "$out"
''
