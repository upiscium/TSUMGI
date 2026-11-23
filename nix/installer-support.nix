{ lib, buildEnv, stdenv, stdenvNoCC, callPackage, python312, python312Packages, tsumgi, integration, runtimeProfileSupport }:

let
  pythonEnv = python312.withPackages (_: [
    tsumgi
    python312Packages.psycopg
    python312Packages.psycopg-pool
  ]);
  controlCli = callPackage ./control-cli-env.nix { inherit tsumgi; };

  migrations = stdenvNoCC.mkDerivation {
    pname = "tsumgi-installer-assets";
    version = "0.3.0pre2";
    src = ../.;
    dontBuild = true;
    installPhase = ''
      mkdir -p "$out/share/tsumgi/migrations"
      cp migrations/*.sql "$out/share/tsumgi/migrations/"
    '';
  };
  base = buildEnv {
    name = "tsumgi-installer-support-base-0.3.0pre2";
    paths = [
      controlCli
      migrations
      integration
      runtimeProfileSupport
    ];
  };
in
stdenvNoCC.mkDerivation {
  name = "tsumgi-installer-support-0.3.0pre2";
  dontUnpack = true;
  installPhase = ''
    mkdir -p "$out"
    cp -a ${base}/. "$out/"
    chmod u+w "$out/bin"
    rm -f "$out/bin/tsumgi-setup-tui"
    cat > "$TMPDIR/tsumgi-setup-tui.c" <<EOF
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *python_tui = "${pythonEnv}/bin/tsumgi-setup-tui";

static int set_invocation_path(const char *argv0, char *invocation, size_t size) {
  if (strchr(argv0, '/') != NULL) {
    if (snprintf(invocation, size, "%s", argv0) >= (int)size) {
      fputs("tsumgi-setup-tui: invocation path is too long\\n", stderr);
      return 1;
    }
    return 0;
  }

  const char *raw_path = getenv("PATH");
  if (raw_path == NULL) {
    fputs("tsumgi-setup-tui: cannot resolve bare invocation without PATH\\n", stderr);
    return 1;
  }
  char *path_copy = strdup(raw_path);
  if (path_copy == NULL) {
    fputs("tsumgi-setup-tui: cannot allocate PATH copy\\n", stderr);
    return 1;
  }

  char *save = NULL;
  for (char *directory = strtok_r(path_copy, ":", &save);
       directory != NULL;
       directory = strtok_r(NULL, ":", &save)) {
    if (*directory == '\\0') {
      directory = ".";
    }
    char candidate[PATH_MAX];
    if (snprintf(candidate, sizeof(candidate), "%s/%s", directory, argv0)
          >= (int)sizeof(candidate)) {
      continue;
    }
    struct stat metadata;
    if (stat(candidate, &metadata) == 0 && S_ISREG(metadata.st_mode) &&
        access(candidate, X_OK) == 0) {
      int result = snprintf(invocation, size, "%s", candidate) >= (int)size;
      free(path_copy);
      if (result != 0) {
        fputs("tsumgi-setup-tui: invocation path is too long\\n", stderr);
        return 1;
      }
      return 0;
    }
  }
  free(path_copy);
  fputs("tsumgi-setup-tui: cannot resolve bare invocation through PATH\\n", stderr);
  return 1;
}

int main(int argc, char **argv) {
  char invocation[PATH_MAX];
  char cwd[PATH_MAX];
  char *separator;
  char **forwarded;

  if (argv[0][0] != '/' && strchr(argv[0], '/') != NULL &&
      getcwd(cwd, sizeof(cwd)) == NULL) {
    fputs("tsumgi-setup-tui: cannot determine invocation directory\\n", stderr);
    return 1;
  }
  if (argv[0][0] != '/' && strchr(argv[0], '/') != NULL) {
    char relative[PATH_MAX];
    if (snprintf(relative, sizeof(relative), "%s/%s", cwd, argv[0]) >=
        (int)sizeof(relative)) {
      fputs("tsumgi-setup-tui: invocation path is too long\\n", stderr);
      return 1;
    }
    if (set_invocation_path(relative, invocation, sizeof(invocation)) != 0) {
      return 1;
    }
  } else if (set_invocation_path(argv[0], invocation, sizeof(invocation)) != 0) {
    return 1;
  }

  separator = strrchr(invocation, '/');
  if (separator == NULL) {
    fputs("tsumgi-setup-tui: invocation has no directory\\n", stderr);
    return 1;
  }
  *separator = '\\0';

  forwarded = calloc((size_t)argc + 3, sizeof(*forwarded));
  if (forwarded == NULL) {
    fputs("tsumgi-setup-tui: cannot allocate argument vector\\n", stderr);
    return 1;
  }
  forwarded[0] = (char *)python_tui;
  for (int index = 1; index < argc; ++index) {
    forwarded[index] = argv[index];
  }
  forwarded[argc] = "--packaged-tool-dir";
  forwarded[argc + 1] = invocation;
  forwarded[argc + 2] = NULL;
  execv(python_tui, forwarded);
  fprintf(stderr, "tsumgi-setup-tui: cannot execute packaged TUI: %s\\n", strerror(errno));
  free(forwarded);
  return 1;
}
EOF
    ${stdenv.cc}/bin/cc -O2 -Wall -Wextra \
      -o "$out/bin/tsumgi-setup-tui" "$TMPDIR/tsumgi-setup-tui.c"
  '';
  meta = {
    description = "TSUMGI first-run installer and Control/Worker bootstrap closure";
    license = lib.licenses.bsd3;
    platforms = lib.platforms.linux;
  };
}
