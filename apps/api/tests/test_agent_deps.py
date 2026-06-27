"""test_agent_deps.py — code-editing v1, Step 1 (dependency pipeline).

Heavy on the GATEKEEPER (the install-RCE control): only allowlisted, pinned, hashed
packages pass; everything else is rejected fail-closed. Plus the wheels-only/hashed/
mirror install command, the two-allowlist egress, and the build orchestration.
"""
from __future__ import annotations

import pytest

from services import agent_deps as ad
from services.agent_deps import (
    DepsError, build_agent_image, build_egress, build_requirements_lock,
    pip_install_command, resolve_install_set, run_egress, validate_requirements,
)

LOCK = {
    "httpx": {"0.28.1": ["sha256:aaa"]},
    "requests": {"2.34.2": ["sha256:bbb"]},
    "idna": {"3.18": ["sha256:ccc"]},
}


def _codes(errors):
    return {(e["code"]) for e in errors}


# ── gatekeeper: accept ──────────────────────────────────────────────────────────

def test_valid_pin_accepted():
    pins, errors = validate_requirements("requests==2.34.2", LOCK)
    assert errors == []
    assert pins == [("requests", "2.34.2", ["sha256:bbb"])]


def test_comments_and_blanks_ignored():
    pins, errors = validate_requirements("# deps\n\nhttpx==0.28.1\n", LOCK)
    assert errors == [] and pins[0][0] == "httpx"


def test_name_normalization():
    pins, errors = validate_requirements("Requests==2.34.2", LOCK)
    assert errors == [] and pins[0][0] == "requests"


# ── gatekeeper: reject (the product) ────────────────────────────────────────────

def test_unpinned_rejected():
    assert "unpinned" in _codes(validate_requirements("httpx", LOCK)[1])


@pytest.mark.parametrize("line", ["httpx>=0.28", "httpx~=0.28", "httpx==0.28,<1", "httpx==0.28.1 ; python_version>'3'"])
def test_ranges_and_markers_rejected(line):
    assert validate_requirements(line, LOCK)[1]  # any error


def test_unknown_package_rejected():
    assert "not_allowed" in _codes(validate_requirements("evil-pkg==1.0", LOCK)[1])


def test_disallowed_version_rejected():
    assert "version_not_allowed" in _codes(validate_requirements("httpx==9.9.9", LOCK)[1])


def test_duplicate_rejected():
    assert "duplicate" in _codes(validate_requirements("httpx==0.28.1\nhttpx==0.28.1", LOCK)[1])


@pytest.mark.parametrize("line", ["-e .", "-r other.txt", "git+https://x/y.git",
                                  "https://files/x.whl", "pkg @ https://x/y.whl"])
def test_unsupported_lines_rejected(line):
    assert "unsupported" in _codes(validate_requirements(line, LOCK)[1])


def test_too_many_deps_rejected():
    body = "\n".join(f"p{i}==1.0" for i in range(21))
    assert "too_many" in _codes(validate_requirements(body, LOCK)[1])


# ── #2: parser can't be bypassed by requirements-file trickery ──────────────────

def test_recursive_flag_rejected_and_no_partial_build():
    # -r mixed with a valid line: rejected, and resolve returns [] (nothing built)
    pins, errors = validate_requirements("requests==2.34.2\n-r /etc/secrets.txt", LOCK)
    assert "unsupported" in _codes(errors)
    iset, errs = resolve_install_set("requests==2.34.2\n-r /etc/secrets.txt")
    assert iset == [] and errs            # any error ⇒ no install set, no build


@pytest.mark.parametrize("line", ["requests ==2.34.2", "requests== 2.34.2",
                                  "requests == 2.34.2", " requests==2.34.2 "])
def test_whitespace_around_operator_failclosed(line):
    # pip tolerates whitespace; we reject (fail-closed) so it can't dodge the allowlist
    pins, errors = validate_requirements(line, LOCK)
    if line.strip() == "requests==2.34.2":      # the fully-trimmed one is valid
        assert errors == []
    else:
        assert errors                            # internal whitespace ⇒ rejected


def test_separator_and_case_normalization_pep503():
    # casing + separators normalize to ONE key, so the allowlist can't be dodged
    lock = {"python-dateutil": {"2.9.0.post0": ["sha256:x"]}}
    for variant in ["python-dateutil==2.9.0.post0", "Python_Dateutil==2.9.0.post0",
                    "python.dateutil==2.9.0.post0", "PYTHON__DATEUTIL==2.9.0.post0"]:
        pins, errors = validate_requirements(variant, lock)
        assert errors == [] and pins[0][0] == "python-dateutil", variant


def test_build_rejects_recursive_flag_before_sandbox():
    captured = {}
    with pytest.raises(DepsError):
        build_agent_image("a", 1, {}, "-r secrets.txt", mirror_url="m", mirror_host="m",
                          sandbox_factory=_factory_for(_FakeSandbox(), captured))
    assert captured == {}                        # never spun a sandbox


# ── resolve_install_set: base closure + user, conflicts ─────────────────────────

def test_base_deps_always_included():
    install_set, errors = resolve_install_set("requests==2.34.2")   # real lockfile
    assert errors == []
    names = {n for n, _, _ in install_set}
    assert {"wayforth-sdk", "httpx", "anyio"} <= names   # base closure baked
    assert "requests" in names                            # user dep added


def test_base_dep_version_conflict_rejected():
    # httpx is a base dep pinned to 0.28.1; overriding it is a conflict
    install_set, errors = resolve_install_set("httpx==0.27.0")
    assert "version_not_allowed" in _codes(errors) or "base_dep_conflict" in _codes(errors)


def test_user_errors_propagate():
    install_set, errors = resolve_install_set("evil==1.0")
    assert install_set == [] and "not_allowed" in _codes(errors)


# ── install command + lock file ─────────────────────────────────────────────────

def test_requirements_lock_has_hashes():
    body = build_requirements_lock([("requests", "2.34.2", ["sha256:bbb"])])
    assert "requests==2.34.2 --hash=sha256:bbb" in body


def test_pip_command_is_wheels_only_hashed_mirror_pinned():
    cmd = pip_install_command("https://mirror.internal/simple")
    assert "--only-binary=:all:" in cmd
    assert "--require-hashes" in cmd
    assert "--no-deps" in cmd
    assert "--index-url https://mirror.internal/simple" in cmd


# ── two-allowlist egress separation ─────────────────────────────────────────────

def test_egress_two_allowlists_are_separate():
    b = build_egress("mirror.internal")
    r = run_egress("gateway.wayforth.io")
    assert b == {"deny_out": ["0.0.0.0/0"], "allow_out": ["mirror.internal"]}
    assert r == {"deny_out": ["0.0.0.0/0"], "allow_out": ["gateway.wayforth.io"]}
    # build can't reach gateway; run can't reach mirror
    assert "gateway.wayforth.io" not in b["allow_out"]
    assert "mirror.internal" not in r["allow_out"]


# ── build orchestration (fake sandbox) ──────────────────────────────────────────

class _Res:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr


class _Snap:
    snapshot_id = "snap-abc"


class _FakeSandbox:
    def __init__(self, install_exit=0):
        self.written, self.ran, self.killed = {}, [], False
        self.snapshot_name = None
        self._install_exit = install_exit
        self.files = self
        self.commands = self

    def write(self, path, content):
        self.written[path] = content

    def run(self, cmd, timeout=None):
        self.ran.append(cmd)
        if "du -sm" in cmd:
            return _Res(0, "15")          # 15 MB, under cap
        return _Res(self._install_exit, "ok", "boom" if self._install_exit else "")

    def create_snapshot(self, name=None):
        self.snapshot_name = name
        return _Snap()

    def kill(self):
        self.killed = True


def _factory_for(sbx, captured):
    def factory(egress, timeout_s):
        captured["egress"] = egress
        captured["timeout"] = timeout_s
        return sbx
    return factory


def test_build_orchestration_happy_path():
    sbx, captured = _FakeSandbox(), {}
    ref = build_agent_image("agent1", 3, {"agent.py": "print(1)"}, "requests==2.34.2",
                            mirror_url="https://mirror.internal/simple",
                            mirror_host="mirror.internal",
                            sandbox_factory=_factory_for(sbx, captured))
    assert ref == "snap-abc"
    # build egress = mirror-only (the §0 separation, enforced at build)
    assert captured["egress"] == {"deny_out": ["0.0.0.0/0"], "allow_out": ["mirror.internal"]}
    # files + lock written; install ran wheels-only; snapshot named per version; killed
    assert sbx.written["/home/user/agent.py"] == "print(1)"
    assert "--hash=sha256:" in sbx.written["/home/user/requirements.lock"]
    assert any("--only-binary=:all:" in c for c in sbx.ran)
    assert sbx.snapshot_name == "agent-agent1-v3"
    assert sbx.killed is True


def test_build_rejects_invalid_requirements_before_sandbox():
    captured = {}
    with pytest.raises(DepsError):
        build_agent_image("a", 1, {}, "evil==1.0", mirror_url="m", mirror_host="m",
                          sandbox_factory=_factory_for(_FakeSandbox(), captured))
    assert captured == {}   # never even spun a build sandbox


def test_build_install_failure_raises_and_kills():
    sbx = _FakeSandbox(install_exit=1)
    with pytest.raises(DepsError) as e:
        build_agent_image("a", 1, {"agent.py": "x"}, "requests==2.34.2",
                          mirror_url="m", mirror_host="m",
                          sandbox_factory=_factory_for(sbx, {}))
    assert "install_failed" in {er["code"] for er in e.value.errors}
    assert sbx.killed is True   # always cleaned up


def test_build_rejects_path_traversal():
    sbx = _FakeSandbox()
    with pytest.raises(DepsError) as e:
        build_agent_image("a", 1, {"../../etc/passwd": "x"}, "requests==2.34.2",
                          mirror_url="m", mirror_host="m",
                          sandbox_factory=_factory_for(sbx, {}))
    assert "bad_path" in {er["code"] for er in e.value.errors}


def test_lockfile_loads_and_has_base_deps():
    lock = ad.load_lockfile()
    for name, version in ad.BASE_DEPS:
        assert ad._norm(name) in lock and version in lock[ad._norm(name)]
        # every entry is hashed
        assert all(h.startswith("sha256:") for h in lock[ad._norm(name)][version])
