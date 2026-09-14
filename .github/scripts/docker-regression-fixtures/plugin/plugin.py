"""Exercise dependencies and tools inside the real application process."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shlex
import sqlite3
import ssl
import sys

from fastapi import APIRouter
import pyfiglet
import xxhash

from qwenpaw.agents.tools.file_io import edit_file, read_file, write_file
from qwenpaw.agents.tools.shell import execute_shell_command


WORKSPACE = Path("/app/working/workspaces/default")
MARKER = WORKSPACE / "docker-artifact-marker.md"
DATABASE = WORKSPACE / "docker-artifact-fixture.sqlite3"
TEXT = "Docker verified artifact\n中文文件读写正常\n"


def chunk_text(chunk):
    return "\n".join(getattr(block, "text", "") for block in chunk.content)


class RegressionPlugin:
    def register(self, api):
        router = APIRouter()

        @router.get("/probe")
        async def probe():
            assert pyfiglet.figlet_format("OK")
            assert xxhash.xxh64(b"xxhash").hexdigest() == "32dd38952c4bc720"
            return {
                "pid": os.getpid(),
                "python": sys.version.split()[0],
                "executable": sys.executable,
                "openssl": ssl.OPENSSL_VERSION,
                "dependencies": {
                    name: importlib.metadata.version(name)
                    for name in ("pyfiglet", "xxhash")
                },
                "dependency_paths": [pyfiglet.__file__, xxhash.__file__],
                "marker_sha256": (
                    hashlib.sha256(MARKER.read_bytes()).hexdigest()
                    if MARKER.exists()
                    else None
                ),
            }

        @router.post("/tools")
        async def tools():
            await write_file(str(MARKER), TEXT.replace("verified", "initial"))
            await edit_file(str(MARKER), "initial", "verified")
            assert "中文文件读写正常" in chunk_text(await read_file(str(MARKER)))
            assert MARKER.read_text(encoding="utf-8") == TEXT
            with sqlite3.connect(DATABASE) as database:
                database.execute(
                    "create table if not exists smoke (value text)"
                )
                database.execute("delete from smoke")
                database.execute("insert into smoke values (?)", (TEXT,))
            code = (
                "import sys,ssl,xxhash,pyfiglet; "
                "print(sys.executable,ssl.OPENSSL_VERSION); "
                "assert xxhash.xxh64(b'xxhash').hexdigest() == "
                "'32dd38952c4bc720'; assert pyfiglet.figlet_format('OK')"
            )
            shell = chunk_text(
                await execute_shell_command(
                    "python -c " + shlex.quote(code), timeout=30
                )
            )
            assert "/app/venv/bin/python" in shell, shell
            assert "OpenSSL " in shell, shell
            return {"passed": True, "shell": shell}

        api.register_http_router(router, prefix="/docker-artifact-regression")


plugin = RegressionPlugin()
