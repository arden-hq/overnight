import argparse
import asyncio
import ipaddress
import json
import socket
import sys

READY = "egress proxy ready"
# Training only: the proxy's own port that passes a connection, unread, to one port of the host
# (`--policy-port`), where the trainer serves the policy under training on loopback.
POLICY_LISTEN_PORT = 3129
POLICY_HOST = "host.docker.internal"
HEADER_LIMIT = 16 * 1024
HEADER_SECONDS = 10.0
CONNECT_SECONDS = 15.0
PORT = 443


def allowed(host: str, hosts: frozenset[str]) -> bool:
    host = host.lower().rstrip(".")
    return host in hosts or any(entry.startswith(".") and host.endswith(entry) for entry in hosts)


def public(address: str) -> bool:
    return ipaddress.ip_address(address).is_global


LOGGED_CHARACTERS = 200


def _log(**fields: object) -> None:
    capped = {key: str(value)[:LOGGED_CHARACTERS] for key, value in fields.items()}
    print(json.dumps(capped), file=sys.stderr, flush=True)


async def _public_address(host: str) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        found = await loop.getaddrinfo(host, PORT, type=socket.SOCK_STREAM)
    except socket.gaierror, UnicodeError:
        return None
    addresses = [str(entry[4][0]) for entry in found]
    if not addresses or not all(public(address) for address in addresses):
        return None
    return addresses[0]


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def _refuse(writer: asyncio.StreamWriter, status: str, **fields: object) -> None:
    _log(decision="refused", status=status, **fields)
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    writer.close()


async def _serve(
    hosts: frozenset[str], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_SECONDS)
    except asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError:
        writer.close()
        return
    method, _, rest = head.split(b"\r\n", 1)[0].decode("latin-1").partition(" ")
    target = rest.split(" ", 1)[0]
    host, _, port = target.rpartition(":")
    if method != "CONNECT" or port != str(PORT):
        await _refuse(writer, "405 Method Not Allowed", method=method, target=target)
        return
    if not allowed(host, hosts):
        await _refuse(writer, "403 Forbidden", host=host, reason="not allowed")
        return
    address = await _public_address(host)
    if address is None:
        await _refuse(writer, "403 Forbidden", host=host, reason="no public address")
        return
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(address, PORT), CONNECT_SECONDS
        )
    except (OSError, TimeoutError) as error:
        await _refuse(writer, "502 Bad Gateway", host=host, reason=type(error).__name__)
        return
    _log(decision="allowed", host=host, address=address)
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))


async def _forward(
    target: tuple[str, int], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    host, port = target
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), CONNECT_SECONDS
        )
    except (OSError, TimeoutError) as error:
        _log(decision="refused", policy=f"{host}:{port}", reason=type(error).__name__)
        writer.close()
        return
    _log(decision="allowed", policy=f"{host}:{port}")
    await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))


async def serve(port: int, hosts: frozenset[str], policy: tuple[str, int] | None = None) -> None:
    """Serve CONNECT to the allowed hosts on `port`. With `policy`, also pass every connection to
    `POLICY_LISTEN_PORT` through to that one host and port, and nowhere else."""
    server = await asyncio.start_server(
        lambda reader, writer: _serve(hosts, reader, writer),
        host="0.0.0.0",
        port=port,
        limit=HEADER_LIMIT,
    )
    servers = [server]
    if policy is not None:
        servers.append(
            await asyncio.start_server(
                lambda reader, writer: _forward(policy, reader, writer),
                host="0.0.0.0",
                port=POLICY_LISTEN_PORT,
            )
        )
    print(READY, flush=True)
    await asyncio.gather(*(server.serve_forever() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--policy-port", type=int)
    options = parser.parse_args()
    policy = None if options.policy_port is None else (POLICY_HOST, options.policy_port)
    asyncio.run(serve(options.port, frozenset(host.lower() for host in options.allow), policy))


if __name__ == "__main__":
    main()
