import os
import runpy
import subprocess
import sys
from pathlib import Path


directory = Path(__file__).resolve().parent
os.chdir(directory)
# pythonw has no console streams; Uvicorn still needs valid file-like objects.
sys.stdout = (directory / "server.out.log").open("a", encoding="utf-8", buffering=1)
sys.stderr = (directory / "server.err.log").open("a", encoding="utf-8", buffering=1)
if sys.argv[1:] == ["--watchdog"]:
    result = subprocess.run(
        [
            str(Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(directory / "start-proxy.ps1"),
        ],
        stdout=sys.stdout,
        stderr=sys.stderr,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    sys.exit(result.returncode)
sys.argv = [str(directory / "cp4cc.py"), *sys.argv[1:]]
runpy.run_path(sys.argv[0], run_name="__main__")
