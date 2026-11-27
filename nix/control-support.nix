{ lib, buildEnv, stdenvNoCC, callPackage, tsumgi, integration }:

let
  controlCli = callPackage ./control-cli-env.nix { inherit tsumgi; };

  migrations = stdenvNoCC.mkDerivation {
    pname = "tsumgi-control-assets";
    version = "0.3.0";
    src = ../.;
    dontBuild = true;
    installPhase = ''
      mkdir -p "$out/share/tsumgi/migrations"
      cp migrations/*.sql "$out/share/tsumgi/migrations/"
    '';
  };
in
buildEnv {
  name = "tsumgi-control-support-0.3.0";
  paths = [
    controlCli
    migrations
    integration
  ];
  meta = {
    description = "TSUMGI Control Plane runtime support closure";
    license = lib.licenses.bsd3;
    platforms = lib.platforms.linux;
  };
}
