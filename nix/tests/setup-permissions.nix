{ pkgs, integration }:

let
  fakeNvidia = pkgs.writeShellScriptBin "nvidia-smi" ''
    case "$*" in
      *"--query-gpu=uuid,minor_number"*)
        echo "Unknown field: minor_number" >&2
        exit 2
        ;;
      *"--query-gpu=uuid"*) printf '%s\n' GPU-permission-test ;;
    esac
    exit 0
  '';
  fakePython = pkgs.writeShellScriptBin "python3" ''
    exec ${pkgs.python312}/bin/python3 "$@"
  '';
  fakeDaemon = pkgs.writeShellScript "tsumgi-permission-test-daemon" ''
    exec ${pkgs.coreutils}/bin/sleep 60
  '';
  fakeDaemonPath = "/etc/tsumgi-permission-test-daemon";
  baseNode = { ... }: {
    system.stateVersion = "26.05";
    virtualisation.memorySize = 1024;
    users.mutableUsers = true;
    environment.systemPackages = [
      integration fakeNvidia fakePython pkgs.bash pkgs.coreutils pkgs.diffutils pkgs.gawk
      pkgs.gnugrep pkgs.gnused pkgs.shadow pkgs.systemd pkgs.util-linux
    ];
    environment.etc."tsumgi-permission-test-daemon".source = fakeDaemon;
  };
  customIdentityNode = { ... }: {
    imports = [ baseNode ];
    # Both same-name groups exist without users: this exercises useradd's
    # --gid branch for both custom roles. The default nodes exercise --user-group.
    users.groups."custom-control" = { };
    users.groups."custom-worker" = { };
  };
  negativeFixturesNode = { ... }: {
    imports = [ baseNode ];
    users.groups."bad-primary" = { };
    users.groups."bad-primary-source" = { };
    users.users."bad-primary" = {
      isSystemUser = true; group = "bad-primary-source";
      home = "/var/empty"; createHome = false;
    };
    users.groups."cross-control" = { };
    users.groups."cross-worker" = { };
    users.users."cross-control" = {
      isSystemUser = true; group = "cross-control";
      home = "/var/empty"; createHome = false;
    };
    users.users."cross-worker" = {
      isSystemUser = true; group = "cross-worker";
      home = "/var/empty"; createHome = false;
    };
  };
in
pkgs.testers.runNixOSTest {
  name = "tsumgi-setup-permissions";
  nodes = {
    controlFirst = baseNode;
    workerFirst = baseNode;
    customIdentity = customIdentityNode;
    negativeFixtures = negativeFixturesNode;
  };
  testScript = ''
    start_all()

    bash = "${pkgs.bash}/bin/bash"
    cat = "${pkgs.coreutils}/bin/cat"
    chmod = "${pkgs.coreutils}/bin/chmod"
    chown = "${pkgs.coreutils}/bin/chown"
    cp = "${pkgs.coreutils}/bin/cp"
    cmp = "${pkgs.diffutils}/bin/cmp"
    cut = "${pkgs.coreutils}/bin/cut"
    grep = "${pkgs.gnugrep}/bin/grep"
    id = "${pkgs.coreutils}/bin/id"
    ls = "${pkgs.coreutils}/bin/ls"
    mkdir = "${pkgs.coreutils}/bin/mkdir"
    mknod = "${pkgs.coreutils}/bin/mknod"
    rm = "${pkgs.coreutils}/bin/rm"
    runuser = "${pkgs.util-linux}/bin/runuser"
    sha = "${pkgs.coreutils}/bin/sha256sum"
    stat = "${pkgs.coreutils}/bin/stat"
    systemd_run = "${pkgs.systemd}/bin/systemd-run"
    tr = "${pkgs.coreutils}/bin/tr"

    control_bin = "${integration}/bin/tsumgi-setup-control-plane"
    worker_bin = "${integration}/bin/tsumgi-setup-gpu-worker"
    fake_daemon_path = "${fakeDaemonPath}"
    control_files = ["/etc/tsumgi/control.toml", "/etc/tsumgi/control.env"]
    worker_files = [
        "/etc/tsumgi/worker.toml", "/etc/tsumgi/worker.env",
        "/etc/tsumgi/gpu-uuids", "/etc/tsumgi/gpu-device-map",
        "/etc/tsumgi/runtime-deployment.json",
    ]
    fake_gpu_proc = "/run/tsumgi-fake-nvidia-gpus"
    fake_gpu_devices = "/run/tsumgi-fake-proc-devices"
    all_files = control_files + worker_files
    control_unit = "/etc/systemd/system/tsumgi-control.service"
    worker_unit = "/etc/systemd/system/tsumgi-worker.service"
    isolation_unit = "/etc/systemd/system/tsumgi-worker-gpu-isolation-preflight.service"
    isolation_dropin = "/etc/systemd/system/tsumgi-worker.service.d/10-gpu-isolation.conf"
    dropin_override = "/etc/systemd/system/tsumgi-worker.service.d/override.conf"
    control_state = "/var/lib/tsumgi-control"
    worker_state = "/var/lib/tsumgi"
    snapshot_paths = [
        "/etc/tsumgi", control_state, worker_state, control_unit, worker_unit,
        isolation_unit, isolation_dropin, dropin_override,
    ] + all_files
    sentinel_paths = snapshot_paths + ["/etc/tsumgi/unchanged"]

    def control_cmd(user):
        return (f"{control_bin} --config /tmp/control.toml --environment-file /tmp/control.env "
                f"--executable {fake_daemon_path} --root / --user {user}")

    def worker_cmd(user):
        return (f"TSUMGI_GPU_PROC_ROOT={fake_gpu_proc} "
                f"TSUMGI_GPU_PROC_DEVICES={fake_gpu_devices} {worker_bin} "
                "--config /tmp/worker.toml --environment-file /tmp/worker.env "
                f"--runtime-manifest /tmp/runtime-deployment.json --executable {fake_daemon_path} "
                f"--root / --user {user} --gpu-isolation on "
                "--gpu-device GPU-permission-test=/dev/nvidia0")

    def run_as(user, command):
        return f"{runuser} -u {user} -- {bash} -ceu '{command}'"

    def deny_read(machine, user, path):
        machine.succeed(f"if {runuser} -u {user} -- {cat} {path} >/dev/null 2>&1; then exit 1; fi")

    def deny_write(machine, user, path):
        command = f"printf denied >> {path}"
        machine.succeed(f"if {run_as(user, command)}; then exit 1; fi")

    def prepare(machine):
        machine.wait_for_unit("multi-user.target")
        # NixOS links this directory into the store; generic systemd installs
        # need a writable copy for the setup scripts' existing-file checks.
        machine.succeed(
            f"{mkdir} /run/generic-systemd && {cp} -a "
            "/etc/systemd/system/. /run/generic-systemd/ && "
            "mount --bind /run/generic-systemd /etc/systemd/system"
        )
        machine.succeed(
            f"{rm} -f /dev/nvidia0 && "
            f"{mknod} -m 0660 /dev/nvidia0 c 195 0 && test -c /dev/nvidia0"
        )
        machine.succeed(
            f"{mkdir} -p {fake_gpu_proc}/gpu0 && "
            f"{cat} >{fake_gpu_proc}/gpu0/information <<'EOF'\n"
            "GPU UUID : GPU-permission-test\nDevice Minor : 0\nEOF\n"
        )
        machine.succeed(
            f"{cat} >{fake_gpu_devices} <<'EOF'\n"
            "Character devices:\n 195 nvidia-frontend\nBlock devices:\nEOF\n"
        )
        machine.succeed(
            f"{cat} >/tmp/control.toml <<'EOF'\n[control]\nlisten = \"127.0.0.1:9000\"\nEOF\n"
            f"{cat} >/tmp/control.env <<'EOF'\nTSUMGI_TEST=control\nEOF\n"
            f"{cat} >/tmp/worker.toml <<'EOF'\n[worker]\nid = \"worker-permission-test\"\n"
            "class = \"gpu-single\"\ngpu_uuids = [\"GPU-permission-test\"]\nEOF\n"
            f"{cat} >/tmp/worker.env <<'EOF'\nTSUMGI_TEST=worker\nEOF\n"
            f"{cat} >/tmp/runtime-deployment.json <<'EOF'\n"
            "{\"schema_version\":\"v1\",\"provider_id\":\"test\"}\nEOF\n"
        )

    def install(machine, role, user):
        machine.succeed(control_cmd(user) if role == "control" else worker_cmd(user))

    def stat_is(machine, path, value):
        machine.succeed(f"test \"$({stat} -c '%U:%G %a' {path})\" = '{value}'")

    def unit_is(machine, path, user, state):
        for key, value in [("User", user), ("Group", user),
                           ("SupplementaryGroups", "tsumgi-config"),
                           ("StateDirectory", state)]:
            machine.succeed(f"{grep} -Fx -- '{key}={value}' {path}")

    def role_layout(machine, role, user):
        files, state, unit, state_name = (
            (control_files, control_state, control_unit, "tsumgi-control")
            if role == "control" else
            (worker_files, worker_state, worker_unit, "tsumgi")
        )
        stat_is(machine, "/etc/tsumgi", "root:tsumgi-config 710")
        stat_is(machine, state, f"{user}:{user} 750")
        for path in files:
            stat_is(machine, path, f"root:{user} 640")
        machine.succeed(f"test \"$(getent passwd {user} | {cut} -d: -f6)\" = '{state}'")
        machine.succeed(f"{id} -nG {user} | {tr} ' ' '\\n' | {grep} -Fx -- tsumgi-config")
        unit_is(machine, unit, user, state_name)

    def assert_all(machine, control_user, worker_user):
        role_layout(machine, "control", control_user)
        role_layout(machine, "worker", worker_user)
        machine.succeed(f"{grep} -Fx -- 'GPU-permission-test=/dev/nvidia0' /etc/tsumgi/gpu-device-map")
        machine.succeed("test -f /usr/local/libexec/tsumgi/gpu_mapping.py")
        machine.succeed(
            f"! {grep} -F -- '/nix/store/' /usr/local/libexec/tsumgi/gpu-device-map"
        )
        machine.succeed(
            f"{grep} -F -- '{fake_daemon_path} --config /etc/tsumgi/worker.toml "
            "--runtime-manifest /etc/tsumgi/runtime-deployment.json' "
            f"{worker_unit} && {grep} -F -- 'DevicePolicy=closed' {isolation_dropin} && "
            f"{grep} -F -- 'DeviceAllow=/dev/nvidia0 rw' {isolation_dropin} && "
            f"{grep} -F -- 'Environment=CUDA_VISIBLE_DEVICES=GPU-permission-test' {isolation_dropin}"
        )
        machine.succeed(
            "systemctl daemon-reload && systemctl cat tsumgi-control.service >/dev/null && "
            "systemctl cat tsumgi-worker.service >/dev/null && "
            "systemctl cat tsumgi-worker-gpu-isolation-preflight.service >/dev/null"
        )
        machine.succeed(
            "systemctl show --no-pager --all "
            "--property=DevicePolicy,DeviceAllow,Environment,EnvironmentFiles,UnsetEnvironment,ExecStartPre,Requires,After "
            "tsumgi-worker.service > /tmp/worker-effective-properties"
        )
        effective_properties = machine.succeed(
            f"{cat} /tmp/worker-effective-properties"
        )
        for property in [
            "DevicePolicy=closed",
            "DeviceAllow=/dev/nvidia0 rw",
            "CUDA_VISIBLE_DEVICES=GPU-permission-test",
            "LD_LIBRARY_PATH=/etc/tsumgi/runtime/nvidia-driver",
            "EnvironmentFiles=/etc/tsumgi/worker.env",
            "UnsetEnvironment=",
            "gpu-preflight",
            "/etc/tsumgi/gpu-uuids",
            "ignore_errors=no",
        ]:
            machine.succeed(
                f"{grep} -F -- '{property}' /tmp/worker-effective-properties"
            )
        for property_name in ["Requires", "After"]:
            effective_value = next(
                line.split("=", 1)[1]
                for line in effective_properties.splitlines()
                if line.startswith(f"{property_name}=")
            )
            assert "tsumgi-worker-gpu-isolation-preflight.service" in effective_value

    def verify_isolation_lifecycle(machine):
        worker_dropin = (
            "/etc/systemd/system/tsumgi-worker.service.d/"
            "90-test-runtime-path.conf"
        )
        preflight_dropin_dir = (
            "/etc/systemd/system/"
            "tsumgi-worker-gpu-isolation-preflight.service.d"
        )
        preflight_dropin = f"{preflight_dropin_dir}/90-test-runtime-path.conf"
        path_value = ":".join([
            "${pkgs.bash}/bin",
            "${pkgs.coreutils}/bin",
            "${pkgs.diffutils}/bin",
            "${fakeNvidia}/bin",
            "${fakePython}/bin",
            "${pkgs.gawk}/bin",
        ])
        machine.succeed(
            f"{mkdir} -p /etc/systemd/system/tsumgi-worker.service.d "
            f"{preflight_dropin_dir}; "
            f"{cat} >{worker_dropin} <<'EOF'\n[Service]\n"
            f"Environment=PATH={path_value}\nEOF\n"
            f"{cat} >{preflight_dropin} <<'EOF'\n[Service]\n"
            f"Environment=PATH={path_value}\n"
            f"Environment=TSUMGI_GPU_PROC_ROOT={fake_gpu_proc}\n"
            f"Environment=TSUMGI_GPU_PROC_DEVICES={fake_gpu_devices}\nEOF\n"
            "systemctl daemon-reload"
        )
        machine.succeed("systemctl start tsumgi-worker.service")
        machine.succeed("systemctl is-active --quiet tsumgi-worker.service")
        machine.succeed(
            "test \"$(systemctl is-active tsumgi-worker-gpu-isolation-preflight.service || true)\" = inactive"
        )
        reject(
            machine,
            "active-worker-isolation-reinstall",
            worker_cmd("tsumgi"),
            needle="Worker service is active; stop it before applying GPU isolation",
        )
        machine.succeed("systemctl is-active --quiet tsumgi-worker.service")
        machine.succeed("systemctl stop tsumgi-worker.service")
        machine.succeed(
            f"{rm} -f {worker_dropin} {preflight_dropin}; "
            f"{rm} -rf {preflight_dropin_dir}; systemctl daemon-reload"
        )

    def assert_only(machine, role, user):
        role_layout(machine, role, user)
        own = control_files if role == "control" else worker_files
        peer = worker_files if role == "control" else control_files
        state = worker_state if role == "control" else control_state
        unit = worker_unit if role == "control" else control_unit
        machine.succeed(run_as(user, f"test -x /etc/tsumgi; cd /etc/tsumgi; "
                                    f"if {ls} >/dev/null 2>&1; then exit 1; fi; "
                                    f"{cat} {' '.join(own)} >/dev/null"))
        machine.succeed(" && ".join([f"test ! -e {path}" for path in peer + [state, unit]]))

    def assert_access(machine, control_user, worker_user):
        for user, own, peer in [
            (control_user, control_files, worker_files),
            (worker_user, worker_files, control_files),
        ]:
            machine.succeed(run_as(user, f"test -x /etc/tsumgi; cd /etc/tsumgi; "
                                        f"if {ls} >/dev/null 2>&1; then exit 1; fi; "
                                        f"{cat} {' '.join(own)} >/dev/null"))
            for path in peer:
                deny_read(machine, user, path)
            for path in own + peer:
                deny_read(machine, "nobody", path)
        for owner, other, state in [
            (control_user, worker_user, control_state),
            (worker_user, control_user, worker_state),
        ]:
            marker = f"{state}/permission-write-check"
            machine.succeed(run_as(owner, f"printf owner > {marker}; cat {marker} >/dev/null; printf owner >> {marker}"))
            deny_read(machine, other, marker)
            deny_write(machine, other, marker)
            deny_read(machine, "nobody", marker)

    def service_cat(user, path):
        return (f"{systemd_run} --wait --pipe --uid {user} --gid {user} "
                f"-p SupplementaryGroups=tsumgi-config {cat} {path}")

    def assert_systemd_access(machine, control_user, worker_user):
        for user, own, peer in [
            (control_user, control_files, worker_files),
            (worker_user, worker_files, control_files),
        ]:
            for path in own:
                machine.succeed(f"{service_cat(user, path)} >/dev/null")
            for path in peer:
                machine.succeed(f"if {service_cat(user, path)} >/dev/null 2>&1; then exit 1; fi")

    def damage(machine):
        machine.succeed(
            f"{chown} root:root /etc/tsumgi {control_state} {worker_state} {' '.join(all_files)} && "
            f"{chmod} 0777 /etc/tsumgi && {chmod} 0700 {control_state} {worker_state} && "
            f"{chmod} 0600 {' '.join(all_files)}"
        )

    def repair_both_orders(machine):
        for order in [("control", "worker"), ("worker", "control")]:
            damage(machine)
            for role in order:
                install(machine, role, "tsumgi-control" if role == "control" else "tsumgi")
            assert_all(machine, "tsumgi-control", "tsumgi")

    def exercise_order(machine, first):
        prepare(machine)
        machine.succeed("! getent passwd tsumgi-control && ! getent group tsumgi-control && "
                        "! getent passwd tsumgi && ! getent group tsumgi")
        install(machine, first, "tsumgi-control" if first == "control" else "tsumgi")
        assert_only(machine, first, "tsumgi-control" if first == "control" else "tsumgi")
        second = "worker" if first == "control" else "control"
        install(machine, second, "tsumgi" if second == "worker" else "tsumgi-control")
        assert_all(machine, "tsumgi-control", "tsumgi")
        verify_isolation_lifecycle(machine)
        repair_both_orders(machine)
        assert_access(machine, "tsumgi-control", "tsumgi")
        assert_systemd_access(machine, "tsumgi-control", "tsumgi")

    def clear(machine):
        machine.succeed(
            f"{rm} -rf /etc/tsumgi {control_state} {worker_state} /usr/local/libexec/tsumgi; "
            f"{rm} -f {control_unit} {worker_unit} {isolation_unit}; "
            f"{rm} -rf /etc/systemd/system/tsumgi-worker.service.d"
        )

    def snapshot(machine, name, paths=snapshot_paths, users=[], groups=[]):
        records = []
        for path in paths:
            records.append(
                f"if test -e {path}; then {stat} -c 'path=%n type=%F owner=%U:%G mode=%a size=%s' {path}; "
                f"if test -f {path}; then {sha} {path}; fi; else printf 'absent %s\\n' {path}; fi"
            )
        for user in users:
            records.append(f"printf 'passwd:{user} '; getent passwd {user} 2>/dev/null || true")
            records.append(f"printf 'groups:{user} '; {id} -nG {user} 2>/dev/null || true")
        for group in groups:
            records.append(f"printf 'group:{group} '; getent group {group} 2>/dev/null || true")
        machine.succeed("{ " + "; ".join(records) + f"; }} > /tmp/{name}")

    def check_snapshot(machine, name, paths=snapshot_paths, users=[], groups=[]):
        snapshot(machine, name + ".after", paths, users, groups)
        machine.succeed(f"{cmp} -s /tmp/{name} /tmp/{name}.after")

    def reject(machine, name, command, users=[], needle=None, paths=snapshot_paths, groups=[]):
        snapshot(machine, name, paths, users, groups)
        machine.succeed(f"if {command} >/tmp/{name}.err 2>&1; then exit 1; fi")
        if needle:
            machine.succeed(f"{grep} -F -- '{needle}' /tmp/{name}.err")
        check_snapshot(machine, name, paths, users, groups)

    def reject_numeric(machine, name, role, user, kind, users, groups):
        command = control_cmd(user) if role == "control" else worker_cmd(user)
        reject(machine, name, command, users=users,
               needle=f"numeric {kind} alias", groups=groups)

    def remove_fixture_alias(machine, user=None, group=None):
        if user:
            machine.succeed(f"userdel {user}")
        if group:
            # This test-only alias intentionally shares a primary GID. Remove
            # its named entry, retaining the real role group with that GID.
            machine.succeed(f"groupdel --force {group}")

    exercise_order(controlFirst, "control")
    exercise_order(workerFirst, "worker")

    # Model an older shared-identity installation, then the explicit stopped
    # operator migration in deployment.md. No implicit unit or state rewrite.
    controlFirst.succeed(
        f"systemctl stop tsumgi-control.service tsumgi-worker.service; "
        f"cp {control_unit} /tmp/reviewed-control.unit; cp {worker_unit} /tmp/reviewed-worker.unit; "
        f"sed -i -e '/^SupplementaryGroups=/d' -e 's/^User=.*/User=tsumgi/' "
        f"-e 's/^Group=.*/Group=tsumgi/' -e 's/^StateDirectory=.*/StateDirectory=tsumgi/' {control_unit}; "
        f"sed -i '/^SupplementaryGroups=/d' {worker_unit}; "
        f"{chown} root:tsumgi {' '.join(control_files)}"
    )
    reject(controlFirst, "legacy-control-retry", control_cmd("tsumgi-control"),
           users=["tsumgi-control", "tsumgi"], needle="legacy/shared")
    reject(controlFirst, "legacy-worker-retry", worker_cmd("tsumgi"),
           users=["tsumgi-control", "tsumgi"], needle="legacy/shared")
    controlFirst.succeed(
        f"{chown} root:root {' '.join(control_files)}; {chmod} 0600 {' '.join(control_files)}; "
        f"cp /tmp/reviewed-control.unit {control_unit}; cp /tmp/reviewed-worker.unit {worker_unit}"
    )
    snapshot(controlFirst, "worker-state-preserved", paths=[worker_state, worker_state + "/permission-write-check"])
    install(controlFirst, "control", "tsumgi-control")
    check_snapshot(controlFirst, "worker-state-preserved", paths=[worker_state, worker_state + "/permission-write-check"])
    install(controlFirst, "worker", "tsumgi")
    assert_all(controlFirst, "tsumgi-control", "tsumgi")
    assert_access(controlFirst, "tsumgi-control", "tsumgi")
    assert_systemd_access(controlFirst, "tsumgi-control", "tsumgi")

    prepare(customIdentity)
    customIdentity.succeed(
        "getent group custom-control && getent group custom-worker && "
        "! getent passwd custom-control && ! getent passwd custom-worker"
    )
    customIdentity.succeed("getent group custom-control > /tmp/pre-control-group; getent group custom-worker > /tmp/pre-worker-group")
    install(customIdentity, "control", "custom-control")
    install(customIdentity, "worker", "custom-worker")
    customIdentity.succeed("getent group custom-control > /tmp/post-control-group; getent group custom-worker > /tmp/post-worker-group; "
                           f"{cmp} /tmp/pre-control-group /tmp/post-control-group; {cmp} /tmp/pre-worker-group /tmp/post-worker-group")
    assert_all(customIdentity, "custom-control", "custom-worker")
    install(customIdentity, "control", "custom-control")
    install(customIdentity, "worker", "custom-worker")
    assert_all(customIdentity, "custom-control", "custom-worker")
    assert_access(customIdentity, "custom-control", "custom-worker")
    assert_systemd_access(customIdentity, "custom-control", "custom-worker")

    clear(customIdentity)
    install(customIdentity, "control", "same-role")
    reject(customIdentity, "same-account", worker_cmd("same-role"), users=["same-role"], needle="legacy/shared")

    prepare(negativeFixtures)
    clear(negativeFixtures)
    reject(negativeFixtures, "wrong-primary", control_cmd("bad-primary"),
           users=["bad-primary", "tsumgi-control"])

    clear(negativeFixtures)
    install(negativeFixtures, "worker", "cross-worker")
    negativeFixtures.succeed("gpasswd --add cross-control cross-worker")
    reject(negativeFixtures, "supplementary-control", control_cmd("cross-control"),
           users=["cross-control", "cross-worker"])
    negativeFixtures.succeed("gpasswd --delete cross-control cross-worker")

    # Reverse direction: Worker is a member of the Control private group.
    clear(negativeFixtures)
    install(negativeFixtures, "control", "cross-control")
    negativeFixtures.succeed("gpasswd --add cross-worker cross-control")
    reject(negativeFixtures, "supplementary-worker", worker_cmd("cross-worker"),
           users=["cross-control", "cross-worker"])
    negativeFixtures.succeed("gpasswd --delete cross-worker cross-control")

    clear(negativeFixtures)
    negativeFixtures.succeed(
        f"{mkdir} -p /etc/tsumgi {worker_state}; printf sentinel > /etc/tsumgi/unchanged; "
        f"{chmod} 0700 /etc/tsumgi; {chmod} 0701 {worker_state}"
    )
    reject(negativeFixtures, "legacy-state", control_cmd("tsumgi-control"),
           users=["tsumgi-control"], needle="legacy/shared", paths=sentinel_paths)

    clear(negativeFixtures)
    negativeFixtures.succeed(
        f"{mkdir} -p /etc/tsumgi /etc/systemd/system {control_state}; "
        f"printf sentinel > /etc/tsumgi/unchanged; "
        f"printf '[Service]\\nUser=tsumgi\\nGroup=tsumgi\\nStateDirectory=tsumgi\\n' > {control_unit}; "
        f"{chmod} 0700 /etc/tsumgi {control_state}; {chmod} 0600 {control_unit}"
    )
    reject(negativeFixtures, "legacy-unit", control_cmd("tsumgi-control"),
           users=["tsumgi-control"], needle="legacy/shared", paths=sentinel_paths)

    clear(negativeFixtures)
    negativeFixtures.succeed(
        f"{mkdir} -p /etc/tsumgi /etc/systemd/system; "
        f"printf '[Service]\\nUser=tsumgi-control\\nGroup=tsumgi-control\\n' > {control_unit}"
    )
    reject(negativeFixtures, "malformed-unit", control_cmd("tsumgi-control"),
           users=["tsumgi-control"])

    clear(negativeFixtures)
    negativeFixtures.succeed(
        f"{mkdir} -p /etc/tsumgi /etc/systemd/system; "
        f"printf '[Service]\\nUser=tsumgi-control\\nGroup=other-service\\nStateDirectory=tsumgi-control\\n' > {control_unit}"
    )
    reject(negativeFixtures, "group-mismatch", control_cmd("tsumgi-control"),
           users=["tsumgi-control"])

    clear(negativeFixtures)
    negativeFixtures.succeed(
        f"{mkdir} -p /etc/tsumgi /etc/systemd/system/tsumgi-worker.service.d; "
        f"printf '[Service]\\nSupplementaryGroups=other-service\\n' > {dropin_override}"
    )
    reject(negativeFixtures, "dropin-override", control_cmd("tsumgi-control"),
           users=["tsumgi-control"])

    for index, role in enumerate(["control", "worker"], 1):
        clear(negativeFixtures)
        user = f"numeric-{role}-uid"
        alias = f"{user}-alias"
        negativeFixtures.succeed(f"groupadd --gid {41000 + index} {user}; "
                                 f"useradd --uid {41100 + index} --gid {user} --no-create-home --home-dir /var/empty {user}; "
                                 f"useradd --non-unique --uid {41100 + index} --gid nogroup --no-create-home --home-dir /var/empty {alias}")
        reject_numeric(negativeFixtures, f"own-{role}-uid-alias", role, user, "UID",
                       users=[user, alias], groups=[user, "tsumgi-config"])
        remove_fixture_alias(negativeFixtures, user=alias)

    for index, role in enumerate(["control", "worker"], 1):
        clear(negativeFixtures)
        user = f"numeric-{role}-gid"
        alias = f"{user}-alias"
        negativeFixtures.succeed(f"groupadd --gid {42000 + index} {user}; "
                                 f"groupadd --non-unique --gid {42000 + index} {alias}; "
                                 f"useradd --uid {42100 + index} --gid {user} --no-create-home --home-dir /var/empty {user}; "
                                 f"gpasswd --add nobody {alias}; test -z \"$(getent group {user} | {cut} -d: -f4)\"")
        reject_numeric(negativeFixtures, f"own-{role}-gid-alias", role, user, "GID",
                       users=[user], groups=[user, alias, "tsumgi-config"])
        remove_fixture_alias(negativeFixtures, group=alias)

    for index, role in enumerate(["control", "worker"], 1):
        clear(negativeFixtures)
        user = f"numeric-{role}-absent"
        alias = f"{user}-alias"
        negativeFixtures.succeed(f"groupadd --gid {43000 + index} {user}; "
                                 f"groupadd --non-unique --gid {43000 + index} {alias}; "
                                 f"gpasswd --add nobody {alias}; test -z \"$(getent group {user} | {cut} -d: -f4)\"; "
                                 f"! getent passwd {user}")
        reject_numeric(negativeFixtures, f"absent-{role}-gid-alias", role, user, "GID",
                       users=[user], groups=[user, alias, "tsumgi-config"])
        remove_fixture_alias(negativeFixtures, group=alias)

    clear(negativeFixtures)
    config_gid = negativeFixtures.succeed(
        f"getent group tsumgi-config | {cut} -d: -f3"
    ).strip()
    config_alias = "numeric-config-gid-alias"
    negativeFixtures.succeed(f"groupadd --non-unique --gid {config_gid} {config_alias}; "
                             f"gpasswd --add nobody {config_alias}")
    for role in ["control", "worker"]:
        clear(negativeFixtures)
        user = f"numeric-config-{role}"
        reject_numeric(negativeFixtures, f"shared-config-{role}-gid-alias", role, user, "GID",
                       users=[user], groups=["tsumgi-config", config_alias])
    remove_fixture_alias(negativeFixtures, group=config_alias)

    # Exercise the installed peer path as well as the requested role path.
    for index, kind in enumerate(["UID", "GID"], 1):
        clear(negativeFixtures)
        peer = f"numeric-peer-{kind.lower()}-worker"
        user = f"numeric-peer-{kind.lower()}-control"
        alias_group = f"numeric-peer-{kind.lower()}-alias"
        install(negativeFixtures, "worker", peer)
        if kind == "UID":
            alias = f"{peer}-alias"
            peer_uid = negativeFixtures.succeed(f"getent passwd {peer} | {cut} -d: -f3").strip()
            negativeFixtures.succeed(f"groupadd --gid {44000 + index} {alias_group}; "
                                     f"useradd --non-unique --uid {peer_uid} --gid {alias_group} --no-create-home --home-dir /var/empty {alias}")
            alias_users = [peer, user, alias]
        else:
            peer_gid = negativeFixtures.succeed(f"getent group {peer} | {cut} -d: -f3").strip()
            negativeFixtures.succeed(f"groupadd --non-unique --gid {peer_gid} {alias_group}; "
                                     f"gpasswd --add nobody {alias_group}")
            alias_users = [peer, user]
        reject_numeric(negativeFixtures, f"peer-{kind.lower()}-alias", "control", user, kind,
                       users=alias_users,
                       groups=["tsumgi-config", peer, alias_group])
        if kind == "UID":
            remove_fixture_alias(negativeFixtures, user=alias, group=alias_group)
        else:
            remove_fixture_alias(negativeFixtures, group=alias_group)
  '';
}
