"""Private SSH command worker; no network service or password configuration.

Remote conversion remains bounded if the SSH caller disappears. Each request
owns a disposable Docker container, profile, and private source directory.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

IMAGE = "sha256:6a2cfcf932ad67e7f0445d50b87e446e0b5c9710053eee27324d54d06201cc70"
EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
FILTERS = {"pdf:writer_pdf_Export", "pdf:calc_pdf_Export", "pdf:impress_pdf_Export", "xlsx:Calc MS Excel 2007 XML"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--font-inventory", action="store_true")
    parser.add_argument("--extension")
    parser.add_argument("--filter")
    args = parser.parse_args()
    name = "ov-office-" + uuid.uuid4().hex
    base = ["docker", "run", "--rm", "--name", name, "--user", f"{os.getuid()}:{os.getgid()}", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "4g", "--cpus", "2", "--pids-limit", "128",
            "--ulimit", "cpu=180:180", "--ulimit", "fsize=2147483648:2147483648",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g", "-e", "MAX_CONCURRENCY=2",
            "-e", "OMP_NUM_THREADS=2", "-e", "HOME=/tmp"]
    directory = None
    try:
        if args.version or args.font_inventory:
            binary, options = ("/usr/bin/fc-list", ["--format", "%{family}\n"]) if args.font_inventory else ("/usr/bin/soffice", ["--version"])
            completed = subprocess.run([*base, "--entrypoint", binary, IMAGE, *options], capture_output=True, timeout=12)
        else:
            if args.extension not in EXTENSIONS or not args.filter or args.filter.split(":{")[0] not in FILTERS:
                raise ValueError("Unsupported extension/export filter")
            directory = Path(tempfile.mkdtemp(prefix="transport_", dir=Path(__file__).parent))
            directory.chmod(0o700)
            source = directory / ("input" + args.extension)
            with source.open("wb") as target:
                total = 0
                while block := sys.stdin.buffer.read(1024 * 1024):
                    total += len(block)
                    if total > 512 * 1024 * 1024:
                        raise ValueError("Input exceeds transport limit")
                    target.write(block)
            source.chmod(0o400)
            output = directory / "output"
            output.mkdir(mode=0o700)
            output.chmod(0o700)
            user = output / "profile" / "user"
            user.mkdir(parents=True)
            user.parent.chmod(0o700)
            user.chmod(0o700)
            (user / "registrymodifications.xcu").write_text(
                '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
                '<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                '</item></oor:items>')
            completed = subprocess.run([*base, "-v", f"{source}:/input/source{args.extension}:ro",
                "-v", f"{output}:/output:rw", "--entrypoint", "/usr/bin/soffice", IMAGE,
                "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nofirststartwizard",
                "--nologo", "-env:UserInstallation=file:///output/profile", "--convert-to", args.filter,
                "--outdir", "/output", f"/input/source{args.extension}"], capture_output=True, timeout=180)
            if completed.returncode:
                sys.stderr.buffer.write(completed.stderr)
                return completed.returncode
            result = output / ("source.xlsx" if args.filter.startswith("xlsx:") else "source.pdf")
            if not result.is_file() or result.is_symlink() or result.stat().st_size > 2 * 1024**3:
                raise ValueError("Conversion produced no valid bounded output")
            with result.open("rb") as handle:
                shutil.copyfileobj(handle, sys.stdout.buffer)
            return 0
        sys.stdout.buffer.write(completed.stdout)
        sys.stderr.buffer.write(completed.stderr)
        return completed.returncode
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if directory:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
