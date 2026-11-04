"""Start a fresh loopback-only synthetic PostgreSQL cluster and always stop it.

Set POS_TEST_PG_BIN to a locally installed PostgreSQL bin directory. No production
configuration, models, transport, storage credentials or printers are used.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import psycopg2


def main():
    root = Path(__file__).resolve().parents[1]
    binaries = Path(os.environ["POS_TEST_PG_BIN"]).resolve(strict=True)
    artifact = root / ".test-results" / ("postgres-" + str(time.time_ns()))
    artifact.mkdir(parents=True, exist_ok=False)
    data = artifact / "data"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    hidden = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    def command(args, timeout=30):
        # A daemon can inherit Windows pipe handles after pg_ctl exits. A regular
        # log file avoids waiting for EOF from the long-lived server process.
        with (artifact / (args[0] + ".log")).open("ab") as output:
            result = subprocess.run([str(binaries / args[0]), *args[1:]], cwd=root,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                timeout=timeout, creationflags=hidden)
        if result.returncode:
            raise RuntimeError(f"Local PostgreSQL command failed: {args[0]}")
        return result
    started = False
    result = {"scope": "fresh_loopback_synthetic_postgres", "port": port}
    try:
        command(["initdb.exe" if os.name == "nt" else "initdb", "-D", str(data), "-U", "api_tools_test",
                 "-A", "trust", "--no-locale", "-E", "UTF8"])
        command(["pg_ctl.exe" if os.name == "nt" else "pg_ctl", "-D", str(data), "-l", str(artifact / "server.log"),
                 "-w", "-t", "15", "-o", f"-h 127.0.0.1 -p {port} -F", "start"])
        started = True
        db = psycopg2.connect(host="127.0.0.1", port=port, user="api_tools_test", dbname="postgres", connect_timeout=10)
        try:
            db.autocommit = True
            with db.cursor() as cursor:
                cursor.execute("CREATE DATABASE escalar_agent_isolated")
        finally:
            db.close()
        url = f"postgresql+psycopg2://api_tools_test@127.0.0.1:{port}/escalar_agent_isolated"
        env = {k: v for k, v in os.environ.items() if not any(x in k for x in ("SUPABASE", "DATABASE", "OPENAI", "ANTHROPIC", "QZ_TRAY"))}
        env.update(ESCALAR_ENV_FILE_DISABLED="true", API_TOOLS_LOCAL_PG_TEST="true",
            POS_TEST_ASGI_POSTGRES_URL=url, POS_TEST_POSTGRES_URL=url, AGENT_USAGE_PG_TEST_ALLOWED="true")
        with (artifact / "pytest.log").open("wb") as output:
            checked = subprocess.run([sys.executable, "-m", "pytest", "-q", "--disable-warnings", "--maxfail=3",
                "tests/test_agent_tools_contract.py", "tests/test_agent_tools_acceptance.py", "tests/test_agent_tool_metadata.py",
                "tests/test_agent_checkout.py", "tests/test_agent_order_fulfillment.py",
                "tests/test_agent_printing.py", "tests/test_agent_checkout_postgres.py", "tests/test_agent_model_usage_postgres.py",
                "--junitxml=" + str(artifact / "pytest.xml")], cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT,
                timeout=240, creationflags=hidden)
        result["tests_exit_code"] = checked.returncode
        print((artifact / "pytest.log").read_text(encoding="utf-8", errors="replace")[-6000:])
    finally:
        if started or (data / "postmaster.pid").exists():
            command(["pg_ctl.exe" if os.name == "nt" else "pg_ctl", "-D", str(data), "-m", "fast", "-w", "-t", "15", "stop"])
            result["server_stopped"] = True
        (artifact / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({**result, "artifacts": str(artifact)}))
    return result.get("tests_exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
