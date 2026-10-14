"""Restart the Lumo proxy (:8790) and LiteLLM (:4000) so they run the files of a new release (#119).

session-restart.py calls restart_services() after the user chose "wait for all" or "interrupt all"
and the other sessions were restarted. It can also run by hand:

    python service_restart.py [--dry-run]

Per service, in the launcher's order (proxy, then LiteLLM): find the PIDs listening on its port
(netstat on Windows and Linux, lsof on macOS), stop them (taskkill on Windows, kill on POSIX),
start it detached the way claude-lumo.sh does, then wait for its health check. A step that fails
prints the manual command and the rest carries on; a service whose port is still taken is not
started, so a stale health check cannot pass for it. --dry-run prints the plan and changes nothing
(it only lists the listeners).

Every side effect goes through injectable runners (run, spawn, healthy, sleep, clock), so tests
never touch real processes or ports.
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.request

PROXY_PORT, LITELLM_PORT = 8790, 4000
PROXY_HEALTH = "http://127.0.0.1:8790/v1/health"
LITELLM_HEALTH = "http://127.0.0.1:4000/health/liveliness"
STOP_TIMEOUT, PROXY_TIMEOUT, LITELLM_TIMEOUT = 10, 30, 45  # seconds; LiteLLM's 45 is the launcher's


def install_dir():
    return os.path.dirname(os.path.abspath(__file__))


def state_dir():
    return os.environ.get("CLAUDE_LUMO_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".claude-lumo")


def platform_name():
    """'windows', 'darwin' or 'linux' (any other POSIX)."""
    if os.name == "nt":
        return "windows"
    return "darwin" if sys.platform == "darwin" else "linux"


def services(install, state, plat):
    """The two services as claude-lumo.sh starts them, proxy first."""
    proxy_dir = os.path.join(install, "proxy")
    venv = os.path.join(state, "litellm-venv")
    litellm = (os.path.join(venv, "Scripts", "litellm.exe") if plat == "windows"
               else os.path.join(venv, "bin", "litellm"))
    log = os.path.join(state, "litellm.log")
    key_file = os.path.join(state, "master.key")
    yaml = os.path.join(proxy_dir, "litellm.yaml")
    return [
        {"name": "the Lumo proxy", "port": PROXY_PORT, "health": PROXY_HEALTH, "timeout": PROXY_TIMEOUT,
         "argv": ["node", "--no-warnings", "proxy.mjs"], "cwd": proxy_dir, "key_file": None, "log": None,
         "manual": f'cd "{proxy_dir}" && nohup node --no-warnings proxy.mjs >/dev/null 2>&1 &'},
        {"name": "LiteLLM", "port": LITELLM_PORT, "health": LITELLM_HEALTH, "timeout": LITELLM_TIMEOUT,
         "argv": [litellm, "--config", yaml, "--host", "127.0.0.1", "--port", str(LITELLM_PORT)],
         "cwd": None, "key_file": key_file, "log": log,
         "manual": (f'LITELLM_MASTER_KEY="$(cat "{key_file}")" PYTHONIOENCODING=utf-8 nohup "{litellm}" '
                    f'--config "{yaml}" --host 127.0.0.1 --port {LITELLM_PORT} > "{log}" 2>&1 &')},
    ]


def listeners_command(port, plat):
    if plat == "windows":
        return ["netstat", "-ano"]
    if plat == "darwin":
        return ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]
    return ["netstat", "-tlnp"]


def _port_of(address):
    return address.rsplit(":", 1)[-1] if ":" in address else ""


def parse_listeners(output, port, plat):
    """PIDs listening on `port` in the listeners command's output, sorted, without duplicates.

    Windows `netstat -ano`: `TCP 127.0.0.1:8790 0.0.0.0:0 LISTENING 1234`. The state word is
    localized, so a listener is recognized by its foreign address ending in :0 instead.
    Linux `netstat -tlnp`: `tcp 0 0 127.0.0.1:8790 0.0.0.0:* LISTEN 1234/node`; the PID shows
    as '-' for another user's process, which yields nothing. macOS `lsof -t`: one PID per line.
    """
    pids = set()
    for line in (output or "").splitlines():
        t = line.split()
        if plat == "darwin":
            if len(t) == 1 and t[0].isdigit():
                pids.add(int(t[0]))
            continue
        if not t or not t[0].lower().startswith("tcp"):
            continue
        if plat == "windows":
            if len(t) != 5 or not t[2].endswith(":0"):
                continue
            local, pid = t[1], t[4]
        else:
            if len(t) < 7:
                continue
            local, pid = t[3], t[-1].split("/", 1)[0]
        if _port_of(local) == str(port) and pid.isdigit() and int(pid) > 0:
            pids.add(int(pid))
    return sorted(pids)


def stop_command(pid, plat):
    return ["taskkill", "/PID", str(pid), "/F"] if plat == "windows" else ["kill", str(pid)]


def run_command(argv):
    """(returncode, stdout) of a short command; (None, '') when it cannot run."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def http_healthy(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - any failure means "not healthy yet"
        return False


def spawn_detached(argv, cwd=None, env=None, log=None):
    """Start argv detached from this process, like the launcher's `nohup ... &`."""
    out = open(log, "w", encoding="utf-8") if log else subprocess.DEVNULL
    try:
        kw = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL, "stdout": out,
              "stderr": subprocess.STDOUT if log else subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try:  # outlive the terminal's job object when it allows that, else start without
                subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:
                subprocess.Popen(argv, creationflags=flags, **kw)
        else:
            subprocess.Popen(argv, start_new_session=True, **kw)
    finally:
        if log:
            out.close()


def wait_for(pred, timeout, sleep, clock, step=1):
    deadline = clock() + timeout
    while True:
        if pred():
            return True
        if clock() >= deadline:
            return False
        sleep(step)


def find_listeners(port, plat, run):
    """PIDs listening on port, or None when the listeners command could not run."""
    rc, out = run(listeners_command(port, plat))
    if rc is None or (rc != 0 and plat != "darwin"):  # lsof exits 1 when nothing listens
        return None
    return parse_listeners(out, port, plat)


def restart_services(install=None, state=None, plat=None, run=run_command, spawn=spawn_detached,
                     healthy=http_healthy, sleep=time.sleep, clock=time.monotonic, out=print,
                     dry_run=False):
    """Restart the proxy, then LiteLLM. True when both answer their health checks again."""
    install, state, plat = install or install_dir(), state or state_dir(), plat or platform_name()
    all_ok = True
    for svc in services(install, state, plat):
        name, port = svc["name"], svc["port"]
        manual = f"start it by hand with: {svc['manual']} (or launch claude-lumo, which starts it)"
        pids = find_listeners(port, plat, run)
        if dry_run:
            if pids:
                stops = "; ".join(" ".join(stop_command(p, plat)) for p in pids)
                out(f"  [dry run] would stop {name} on port {port}: {stops}")
            else:
                out(f"  [dry run] found no process listening on port {port} for {name}")
            out(f"  [dry run] would start {name}: {' '.join(svc['argv'])}"
                + (f" (in {svc['cwd']})" if svc["cwd"] else "")
                + f", then wait up to {svc['timeout']}s for {svc['health']}")
            continue

        if pids is None:
            out(f"  Could not list the processes on port {port} ({' '.join(listeners_command(port, plat))} "
                f"failed); {name} is left as it is. Stop the process on port {port}, then {manual}")
            all_ok = False
            continue
        if not pids and healthy(svc["health"]):
            out(f"  {name} answers on port {port} but its PID is not visible; it is left as it is. "
                f"Stop it, then {manual}")
            all_ok = False
            continue
        for pid in pids:
            rc, _ = run(stop_command(pid, plat))
            if rc != 0:
                out(f"  Could not stop PID {pid} ({name}); run: {' '.join(stop_command(pid, plat))}")
        if pids and not wait_for(lambda: not find_listeners(port, plat, run), STOP_TIMEOUT, sleep, clock):
            out(f"  Port {port} is still in use, so {name} was not restarted. Stop PID "
                f"{', '.join(map(str, pids))}, then {manual}")
            all_ok = False
            continue

        env = None
        if svc["key_file"]:
            try:
                with open(svc["key_file"], encoding="utf-8") as f:
                    key = f.read().strip()
            except OSError:
                out(f"  Could not read {svc['key_file']}, so {name} was not started; {manual}")
                all_ok = False
                continue
            env = dict(os.environ, LITELLM_MASTER_KEY=key, PYTHONIOENCODING="utf-8")
        try:
            spawn(svc["argv"], cwd=svc["cwd"], env=env, log=svc["log"])
        except OSError as e:
            out(f"  Could not start {name} ({e}); {manual}")
            all_ok = False
            continue
        if wait_for(lambda: healthy(svc["health"]), svc["timeout"], sleep, clock):
            out(f"  Restarted {name} (port {port}).")
        else:
            where = f"; see {svc['log']}" if svc["log"] else ""
            out(f"  {name} did not answer {svc['health']} within {svc['timeout']}s{where}. "
                f"If it is not running, {manual}")
            all_ok = False
    return all_ok


def main(argv=None):
    parser = argparse.ArgumentParser(description="Restart the Lumo proxy and LiteLLM.")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and change nothing")
    args = parser.parse_args(argv)
    print("Restarting the proxy and LiteLLM:" if not args.dry_run else "Plan for the proxy and LiteLLM:")
    return 0 if restart_services(dry_run=args.dry_run) or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
