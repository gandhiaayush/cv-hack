"""Python code-execution tool for quantitative constraints (power budgets, derating, thermal).

NOT a security sandbox: runs in a subprocess with a timeout and isolated mode. Fine for a hackathon."""
import subprocess
import sys

SPEC = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": "Run Python to compute quantitative constraints (power budget, voltage margins, "
        "derating, thermal rise, cost totals). Use print() for results. Stdlib only, 10s limit.",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
}


def run_python(code: str) -> str:
    try:
        p = subprocess.run(
            [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        return "ERROR: timed out after 10s"
    out = (p.stdout + p.stderr).strip()
    return out[:4000] or "(no output)"
