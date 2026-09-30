"""The machine's load and free memory, read before the supervisor opens a seat (REQ-6).

`read()` -> {load1, cpus, avail_mb, errors}: a figure it cannot read is None, with {what: error} in `errors`
(what: load, cpus, memory). Available memory: macOS `vm_stat` free + inactive + speculative pages; Linux
/proc/meminfo MemAvailable. The readers are arguments, for tests.
"""
import os
import re
import subprocess
import sys


def parse_vm_stat(text) -> float:
    """MB in free + inactive + speculative pages; the first line gives the page size."""
    size = int(re.search(r"page size of (\d+) bytes", text).group(1))
    pages = {m[1]: int(m[2]) for m in re.finditer(r"^Pages (\w+):\s+(\d+)\.?$", text, re.M)}
    return sum(pages[k] for k in ("free", "inactive", "speculative")) * size / 2 ** 20


def parse_meminfo(text) -> float:
    return int(re.search(r"^MemAvailable:\s+(\d+) kB$", text, re.M).group(1)) / 1024


def avail_mb() -> float:
    if sys.platform == "darwin":
        r = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=10, check=True)
        return parse_vm_stat(r.stdout)
    with open("/proc/meminfo", encoding="ascii") as f:
        return parse_meminfo(f.read())


def read(*, loadavg=os.getloadavg, cpu_count=os.cpu_count, avail=avail_mb) -> dict:
    out = {"load1": None, "cpus": None, "avail_mb": None, "errors": {}}
    for what, key, fn in (("load", "load1", lambda: loadavg()[0]), ("cpus", "cpus", cpu_count),
                          ("memory", "avail_mb", avail)):
        try:
            if (v := fn()) is None:
                raise ValueError("unknown")
            out[key] = v
        except (OSError, ValueError, TypeError, AttributeError, IndexError, KeyError,
                subprocess.SubprocessError) as e:
            out["errors"][what] = f"{type(e).__name__}: {e}"
    return out
