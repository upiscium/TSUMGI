{ lib, buildEnv, python312, tsumgi, integration }:

let
  pythonEnv = python312.withPackages (_: [ tsumgi ]);
in
buildEnv {
  name = "tsumgi-worker-support-0.4.0pre1";
  paths = [
    pythonEnv
    integration
  ];
  meta = {
    description = "TSUMGI Worker runtime support closure";
    license = lib.licenses.bsd3;
    platforms = lib.platforms.linux;
  };
}
