"""Exercise the shipped Compose against an isolated HTTPS controller on Linux."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_integration import FakeState, TestServer  # noqa: E402


def command(args, *, check=True):
    return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def wait_for(predicate, detail):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(f"Timed out: {detail}")


def publish_feed(state, blocked, ip="192.0.2.1", *, stale=False):
    stamp = "2000-01-01T00:00:00Z" if stale else datetime.now(timezone.utc).isoformat()
    state.status_payload = {
        "isBlocked": blocked,
        "lastUpdate": stamp,
        "data": [
            {
                "ip": ip,
                "description": "Cloudflare",
                "isp": "TEST",
                "stateChanges": [{"timestamp": stamp, "state": blocked}],
            }
        ],
    }


def main():
    image = sys.argv[1] if len(sys.argv) > 1 else "stopliga:test"
    state = FakeState(
        {"isBlocked": True},
        ["192.0.2.1"],
        {
            "_id": "route-1",
            "name": "StopLiga",
            "enabled": False,
            "network_id": "vpn-1",
            "target_devices": [{"client_mac": "aa:bb:cc:dd:ee:01", "type": "CLIENT"}],
            "ip_addresses": [{"ip_or_subnet": "192.0.2.1", "ip_version": "IPv4"}],
        },
    )
    publish_feed(state, True)
    report = {"image": image, "checks": []}
    with (
        tempfile.TemporaryDirectory(prefix="stopliga-compose-") as temporary,
        TestServer(state, https=True) as unifi,
        TestServer(state, https=False) as feed,
    ):
        project = Path(temporary)
        data = project / "data"
        data.mkdir(mode=0o777)
        data.chmod(0o777)  # Only this disposable CI directory; application files must still be private.
        (project / "config").mkdir()
        (project / "override.yml").write_text("services:\n  stopliga:\n    network_mode: host\n")
        env_file = project / ".env"
        env_file.write_text(
            "\n".join(
                [
                    f"STOPLIGA_IMAGE={image}",
                    "STOPLIGA_RUN_MODE=loop",
                    "STOPLIGA_SYNC_INTERVAL_SECONDS=1",
                    "UNIFI_HOST=127.0.0.1",
                    f"UNIFI_PORT={unifi.base_url.rsplit(':', 1)[1]}",
                    "UNIFI_API_KEY=test-api-key",
                    "UNIFI_VERIFY_TLS=false",
                    "STOPLIGA_FEED_VERIFY_TLS=false",
                    f"STOPLIGA_STATUS_URL={feed.base_url}/feed/status.json",
                    "STOPLIGA_REQUEST_TIMEOUT=2",
                    "STOPLIGA_RETRIES=1",
                ]
            )
            + "\n"
        )
        compose = [
            "docker",
            "compose",
            "--project-directory",
            str(project),
            "-f",
            str(ROOT / "docker-compose.yml"),
            "-f",
            str(project / "override.yml"),
        ]
        container = ""

        def exec_python(code, *, check=True):
            return command(["docker", "exec", container, "/opt/venv/bin/python", "-c", code], check=check)

        def persisted():
            response = exec_python(
                "from pathlib import Path; p=Path('/data/state.json'); print(p.read_text() if p.exists() else '{}')",
                check=False,
            )
            return json.loads(response.stdout) if response.returncode == 0 else {}

        try:
            command(compose + ["up", "-d"])
            container = command(compose + ["ps", "-q"]).stdout.strip()
            assert container, "Compose did not start a container"
            wait_for(
                lambda: state.route["enabled"] and persisted().get("status") == "success", "initial route activation"
            )
            info = json.loads(command(["docker", "inspect", container]).stdout)[0]
            assert info["Config"]["User"] == "10001:10001"
            assert info["HostConfig"]["ReadonlyRootfs"] is True
            assert "ALL" in info["HostConfig"]["CapDrop"]
            assert info["HostConfig"]["LogConfig"]["Config"]["max-size"] == "5m"
            assert (
                command(["docker", "exec", container, "/opt/venv/bin/python", "-m", "stopliga.healthcheck"]).returncode
                == 0
            )
            identity = exec_python(
                "import os,stat,json; s=os.stat('/data/state.json'); print(json.dumps([os.getuid(),s.st_uid,stat.S_IMODE(s.st_mode)]))"
            ).stdout
            assert json.loads(identity) == [10001, 10001, 0o600]
            report["checks"].append("compose_nonroot_cap_drop_readonly_volume_and_health")

            def writes():
                return sum(path.endswith("/trafficroutes/route-1") for path, body in state.request_bodies)

            before = writes()
            time.sleep(3)
            assert writes() == before, "Idempotent loops rewrote the route"
            report["checks"].append("idempotent_loop")
            publish_feed(state, True, "192.0.2.2")
            wait_for(lambda: state.route["ip_addresses"][0]["ip_or_subnet"] == "192.0.2.2", "destination replacement")
            publish_feed(state, False, "192.0.2.2")
            wait_for(lambda: not state.route["enabled"], "route disable")
            assert state.route["ip_addresses"][0]["ip_or_subnet"] == "192.0.2.2"
            report["checks"].append("activation_destination_change_disable_and_preserve_ips")
            publish_feed(state, True, stale=True)
            wait_for(lambda: persisted().get("status") == "error", "stale feed health failure")
            assert not state.route["enabled"]
            assert (
                command(
                    ["docker", "exec", container, "/opt/venv/bin/python", "-m", "stopliga.healthcheck"], check=False
                ).returncode
                == 1
            )
            publish_feed(state, True, "192.0.2.2")
            wait_for(lambda: state.route["enabled"] and persisted().get("status") == "success", "fresh feed recovery")
            report["checks"].append("stale_feed_preserves_route_and_recovers")
            report["resources"] = {
                "memory": command(
                    ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container]
                ).stdout.strip(),
                "expanded_image_bytes": int(
                    command(["docker", "image", "inspect", "--format", "{{.Size}}", image]).stdout.strip()
                ),
                "python_and_alpine": exec_python(
                    "import sys,pathlib,json; print(json.dumps([sys.version.split()[0],pathlib.Path('/etc/alpine-release').read_text().strip()]))"
                ).stdout.strip(),
            }
            command(compose + ["stop", "-t", "10"])
            # A standalone dry-run must leave all existing persistent files untouched.
            probe = [
                "docker",
                "run",
                "--rm",
                "--user",
                "10001:10001",
                "--entrypoint",
                "/opt/venv/bin/python",
                "-v",
                f"{data}:/data:ro",
                image,
                "-c",
                "import pathlib,hashlib,json; print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/data').iterdir()}))",
            ]
            before_files = command(probe).stdout
            command(compose + ["run", "--rm", "--no-deps", "stopliga", "--once", "--dry-run"])
            # Lock acquisition is operational; dry-run must not rewrite state or guard.
            before_hashes, after_hashes = json.loads(before_files), json.loads(command(probe).stdout)
            before_hashes.pop("stopliga.lock", None)
            after_hashes.pop("stopliga.lock", None)
            assert before_hashes == after_hashes
            report["checks"].append("dry_run_preserves_applied_state")
            # The current weekday is excluded, independently of CI wall-clock time.
            tomorrow = (datetime.now(timezone.utc).weekday() + 1) % 7
            day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][tomorrow]
            with env_file.open("a") as handle:
                handle.write(f"STOPLIGA_ACTIVE_DAYS={day}\nSTOPLIGA_ACTIVE_TIMEZONE=UTC\n")
            command(compose + ["up", "-d", "--force-recreate"])
            container = command(compose + ["ps", "-q"]).stdout.strip()
            wait_for(
                lambda: not state.route["enabled"] and persisted().get("status") == "success",
                "scheduled disable after restart",
            )
            report["checks"].append("schedule_close_and_restart")
            command(compose + ["stop", "-t", "10"])
            code = command(["docker", "inspect", "--format", "{{.State.ExitCode}}", container]).stdout.strip()
            assert code == "0", f"Shutdown exit code {code}"
            report["checks"].append("graceful_sigterm")
        except BaseException:
            print(command(compose + ["logs", "--no-color", "--tail", "100"], check=False).stdout, file=sys.stderr)
            raise
        finally:
            command(compose + ["down", "--volumes", "--remove-orphans"], check=False)
    print(json.dumps(report, indent=2))
    Path("container-smoke.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
