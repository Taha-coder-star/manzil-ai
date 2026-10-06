"""Privately switch a migrated server from its admin URL to manzil_app.

Run as root from the server checkout after migration 002 has applied. The only
credential output is the updated private .env file; nothing is printed or put
on a process command line. The administrator URL stays in a protected file.
"""

import json
import os
import secrets
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

PRIVATE_DIR = Path("/home/ubuntu/.config/manzil")
ADMIN_ENV = PRIVATE_DIR / "migrate.env"
BACKUP_ENV = PRIVATE_DIR / "pre-task22.env"
RUNTIME_ENV = Path(".env")

ROLE_PROVISION = """
import json
import os
import sys

import psycopg
from psycopg import sql

try:
    password, runtime_url = json.loads(sys.stdin.read())
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5) as admin:
        applied = admin.execute(
            "SELECT count(*) FROM schema_migration WHERE name = %s",
            ('002_tenant_rls.sql',),
        ).fetchone()[0]
        if applied != 1:
            raise RuntimeError('migration missing')
        verifier = admin.pgconn.encrypt_password(
            password.encode('utf-8'), b'manzil_app', b'scram-sha-256'
        )
        admin.execute(
            sql.SQL('ALTER ROLE manzil_app PASSWORD {}').format(
                sql.Literal(verifier.decode('ascii'))
            )
        )
    with psycopg.connect(runtime_url, connect_timeout=5) as runtime:
        identity = runtime.execute('''
            SELECT session_user, current_user, rolsuper, rolbypassrls,
                   rolcreatedb, rolcreaterole
            FROM pg_roles WHERE rolname = current_user
        ''').fetchone()
        if identity != ('manzil_app', 'manzil_app', False, False, False, False):
            raise RuntimeError('unsafe runtime identity')
        if runtime.execute('SELECT current_agency_id()').fetchone() != (None,):
            raise RuntimeError('tenant context already set')
        if runtime.execute('SELECT count(*) FROM agency').fetchone() != (0,):
            raise RuntimeError('missing context exposed agency rows')
    print('Restricted login and missing-context checks passed.')
except Exception as exc:
    print('Role activation failed:', type(exc).__name__)
    raise SystemExit(1) from None
"""


def url_from_line(lines):
    matches = [line for line in lines if line.startswith("DATABASE_URL=")]
    if len(matches) != 1:
        raise ValueError("expected one DATABASE_URL line")
    original = matches[0]
    value = original[len("DATABASE_URL=") :].strip()
    quote_char = value[0] if value and value[0] in ("'", '"') else ""
    if quote_char:
        if not value.endswith(quote_char):
            raise ValueError("invalid DATABASE_URL quoting")
        value = value[1:-1]
    if not value or any(char.isspace() for char in value):
        raise ValueError("invalid DATABASE_URL value")
    return original, value, quote_char


def runtime_url(admin_url, password):
    parts = urlsplit(admin_url)
    if parts.scheme not in ("postgresql", "postgres") or not parts.netloc:
        raise ValueError("invalid administrator URL")
    _, separator, host = parts.netloc.rpartition("@")
    if not separator or not host:
        raise ValueError("administrator URL must include a user and host")
    userinfo = "manzil_app:" + quote(password, safe="")
    return urlunsplit(parts._replace(netloc=userinfo + "@" + host))


def main():
    if os.geteuid() != 0:
        raise PermissionError("run with sudo")
    if not RUNTIME_ENV.is_file() or RUNTIME_ENV.is_symlink():
        raise ValueError("runtime .env must be a regular file")
    if not ADMIN_ENV.is_file() or ADMIN_ENV.is_symlink():
        raise ValueError("private migration environment file is missing")
    if stat.S_IMODE(ADMIN_ENV.stat().st_mode) != 0o600:
        raise PermissionError("private migration environment file must be mode 0600")
    if stat.S_IMODE(PRIVATE_DIR.stat().st_mode) != 0o700:
        raise PermissionError("private directory must be mode 0700")

    contents = RUNTIME_ENV.read_text(encoding="utf-8")
    lines = contents.splitlines(keepends=True)
    old_line, admin_url, quote_char = url_from_line(lines)
    _, stored_admin_url, _ = url_from_line(
        ADMIN_ENV.read_text(encoding="utf-8").splitlines(keepends=True)
    )
    if admin_url != stored_admin_url:
        raise ValueError("runtime URL no longer matches saved administrator URL")

    if BACKUP_ENV.exists():
        if BACKUP_ENV.read_text(encoding="utf-8") != contents:
            raise ValueError("existing private backup differs from current .env")
    else:
        backup_fd = os.open(BACKUP_ENV, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(backup_fd, "w", encoding="utf-8") as backup:
            backup.write(contents)
            backup.flush()
            os.fsync(backup.fileno())

    password = secrets.token_urlsafe(48)
    new_url = runtime_url(admin_url, password)
    child = subprocess.run(
        [
            "docker", "run", "--rm", "-i", "--env-file", str(ADMIN_ENV),
            "--entrypoint", "python", "manzil-ai:latest", "-c", ROLE_PROVISION,
        ],
        input=json.dumps([password, new_url]),
        text=True,
        capture_output=True,
        check=False,
    )
    if child.returncode:
        # Child output and Docker errors can contain connection details: never echo.
        raise RuntimeError("restricted login setup failed; inspect exception type privately")

    replacement = f"DATABASE_URL={quote_char}{new_url}{quote_char}"
    if old_line.endswith("\r\n"):
        replacement += "\r\n"
    elif old_line.endswith("\n"):
        replacement += "\n"
    updated = "".join(replacement if line == old_line else line for line in lines)
    file_stat = RUNTIME_ENV.stat()
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".env.task22-", dir=RUNTIME_ENV.parent)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as temporary:
            os.fchmod(temporary.fileno(), 0o600)
            os.fchown(temporary.fileno(), file_stat.st_uid, file_stat.st_gid)
            temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(tmp_name, RUNTIME_ENV)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    print("Restricted login verified; private runtime environment updated.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as exc:
        print("Activation stopped:", type(exc).__name__)
        raise SystemExit(1) from None
