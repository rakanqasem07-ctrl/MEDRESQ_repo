"""One-click launcher for the MEDRESQ demo (standard library only).

First run: creates a private .venv, installs requirements (needs internet once), starts the app,
opens the browser. Next runs start in a few seconds. Stop with Ctrl+C.
Options: --port N, --phone (also reachable from a phone on the same Wi-Fi), --no-browser,
--no-venv (use the current Python, for testing).
"""
import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser

def say(msg):
    """print that never crashes on a Windows console with a limited code page"""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode(), flush=True)


HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
STAMP = os.path.join(VENV, ".medresq_installed")


def venv_python():
    sub = "Scripts" if os.name == "nt" else "bin"
    return os.path.join(VENV, sub, "python.exe" if os.name == "nt" else "python")


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))     # no packet is sent; it only picks the active network card
            return s.getsockname()[0]
    except Exception:
        return None


def free_port(start):
    for p in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise SystemExit("No free port found near %d" % start)


def ensure_env():
    if os.path.exists(STAMP):
        return venv_python()
    say("First run: preparing the app (needs internet, about 1 to 3 minutes)...")
    if not os.path.exists(venv_python()):
        venv.EnvBuilder(with_pip=True).create(VENV)
    py = venv_python()
    cmd = [py, "-m", "pip", "install", "--disable-pip-version-check", "-r",
           os.path.join(HERE, "requirements.txt")]
    if subprocess.call(cmd) != 0:
        raise SystemExit("Install failed. Check your internet connection and run again.")
    open(STAMP, "w").write("ok")
    return py


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--phone", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-venv", action="store_true")
    ap.add_argument("--easy", action="store_true", help="shared password 'demo' for all accounts (demo only)")
    a = ap.parse_args()
    py = sys.executable if a.no_venv else ensure_env()
    port = free_port(a.port)
    env = dict(os.environ, PORT=str(port))
    if a.easy:
        env["MEDRESQ_EASY_PASSWORDS"] = "1"
    if a.phone:
        env["HOST"] = "0.0.0.0"
    proc = subprocess.Popen([py, os.path.join(HERE, "app.py")], cwd=HERE, env=env)
    url = "http://127.0.0.1:%d/" % port
    for _ in range(120):                       # wait up to 60 s for the server
        if proc.poll() is not None:
            raise SystemExit("The app stopped unexpectedly.")
        try:
            urllib.request.urlopen(url + "api/accounts", timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    say("")
    say("MEDRESQ is running at %s   (press Ctrl+C to stop)" % url)
    say("Keep this window open while you use the app.")
    if a.phone:
        ip = lan_ip()
        say("")
        if ip:
            say("ON YOUR PHONE (same Wi-Fi as this computer) open:   http://%s:%d/" % (ip, port))
        else:
            say("Could not find the Wi-Fi address. Run ipconfig and use the IPv4 address with :%d" % port)
        say("If Windows asks about the firewall, choose Allow on private networks.")
        say("Use trusted Wi-Fi only: the demo password is the same for everyone.")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


if __name__ == "__main__":
    main()
