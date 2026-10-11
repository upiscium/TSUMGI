{ lib, buildEnv, stdenvNoCC, callPackage, astrumweaver, integration }:

let
  controlCli = callPackage ./control-cli-env.nix { inherit astrumweaver; };

  migrations = stdenvNoCC.mkDerivation {
    pname = "astrumweaver-control-assets";
    version = "0.1.0-dev";
    src = ../.;
    dontBuild = true;
    installPhase = ''
      mkdir -p "$out/share/astrumweaver/migrations"
      cp migrations/*.sql "$out/share/astrumweaver/migrations/"
    '';
  };
in
buildEnv {
  name = "astrumweaver-control-support-0.1.0-dev";
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
