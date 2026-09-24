#!/usr/bin/env python3
"""Install the already-authorized per-user, half-hour publisher."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'com.wellsleep.codex-task-dashboard'


def proxy_environment(environ):
    """Carry local desktop proxy settings into launchd without storing credentials."""
    result = {}
    for name in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
        value = environ.get(name)
        if value:
            parsed = urlparse(value)
            if parsed.hostname in ('localhost','127.0.0.1','::1') and not parsed.username and not parsed.password:
                result[name] = value
    if result:
        for name in ('NO_PROXY','no_proxy'):
            if environ.get(name):
                result[name] = environ[name]
    return result


def main():
    local = ROOT / '.local'
    local.mkdir(exist_ok=True)
    dest = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {'Label':LABEL, 'ProgramArguments':[sys.executable, str(ROOT / 'scripts/publish.py')],
               'WorkingDirectory':str(ROOT), 'StartInterval':1800, 'RunAtLoad':True,
               'ProcessType':'Background', 'LowPriorityIO':True,
               'StandardOutPath':str(local / 'launchd.stdout.log'),
               'StandardErrorPath':str(local / 'launchd.stderr.log'),
               'EnvironmentVariables':{'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin', 'HOME':str(Path.home()), **proxy_environment(os.environ)}}
    if dest.exists() and plistlib.loads(dest.read_bytes()).get('Label') != LABEL:
        raise RuntimeError('Refusing to replace another launch agent')
    dest.write_bytes(plistlib.dumps(payload))
    domain = 'gui/' + str(os.getuid())
    subprocess.run(['launchctl','bootout',domain + '/' + LABEL],capture_output=True)
    subprocess.run(['launchctl','bootstrap',domain,str(dest)],check=True)
    subprocess.run(['launchctl','print',domain + '/' + LABEL],check=True)


if __name__ == '__main__':
    main()
