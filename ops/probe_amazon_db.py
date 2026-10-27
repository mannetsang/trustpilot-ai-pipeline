#!/usr/bin/env python3
"""Work out what database is behind AMAZON_RDS_HOST, and whether we can reach it.

The host we were given is an EC2 public DNS name, not an RDS endpoint, so the
engine and port are unknown. This opens a socket to each of the usual database
ports and, where the wire protocol allows it without a login, says which engine
answered. Nothing here needs a database driver installed.

Run it from a machine whose IP the instance's security group admits:

    python ops/probe_amazon_db.py
"""
import os
import socket
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from lib.secrets import get_secret  # noqa: E402

# port -> the engine that normally listens there
CANDIDATES = [
    (5432, "PostgreSQL"),
    (3306, "MySQL / MariaDB"),
    (1433, "SQL Server"),
    (27017, "MongoDB"),
    (6379, "Redis"),
    (5433, "PostgreSQL (alt)"),
    (3307, "MySQL (alt)"),
]

TIMEOUT = 6


def identify_mysql(sock):
    """MySQL greets on connect: [3-byte len][seq][protocol][NUL-terminated version]."""
    head = sock.recv(5)
    if len(head) < 5 or head[4] != 10:
        return None
    rest = sock.recv(256)
    version, _, _ = rest.partition(b"\x00")
    return version.decode("ascii", "replace")


def identify_postgres(sock):
    """Postgres answers an SSLRequest with a single byte, S (yes) or N (no)."""
    sock.sendall(struct.pack("!ii", 8, 80877103))
    reply = sock.recv(1)
    if reply == b"S":
        return "TLS supported"
    if reply == b"N":
        return "plaintext only"
    return None


def probe(host, port, engine):
    try:
        sock = socket.create_connection((host, port), TIMEOUT)
    except socket.timeout:
        return "filtered", "no response (security group, most likely)"
    except OSError as exc:
        return "closed", exc.strerror or str(exc)

    with sock:
        sock.settimeout(TIMEOUT)
        try:
            if port in (3306, 3307):
                detail = identify_mysql(sock)
                if detail:
                    return "open", f"MySQL/MariaDB server {detail}"
            elif port in (5432, 5433):
                detail = identify_postgres(sock)
                if detail:
                    return "open", f"PostgreSQL, {detail}"
        except OSError as exc:
            return "open", f"connected, handshake failed: {exc}"
        return "open", f"connected, assumed {engine}"


def main():
    host = get_secret("AMAZON_RDS_HOST", env_var="AMAZON_RDS_HOST")
    print(f"probing {host}\n")

    reachable = []
    for port, engine in CANDIDATES:
        state, detail = probe(host, port, engine)
        print(f"  {port:>5}  {state:<9} {detail}")
        if state == "open":
            reachable.append((port, detail))

    if not reachable:
        print("\nNothing answered. Either the security group does not admit this"
              "\nmachine's IP, or the database is not on a public port.")
        return 1

    print("\nReachable:")
    for port, detail in reachable:
        print(f"  {port}: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
