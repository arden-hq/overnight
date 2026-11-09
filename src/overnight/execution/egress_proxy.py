import argparse
import asyncio
import ipaddress
import json
import socket
import sys

READY = "egress proxy ready"
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


async def serve(port: int, hosts: frozenset[str]) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: _serve(hosts, reader, writer),
        host="0.0.0.0",
        port=port,
        limit=HEADER_LIMIT,
    )
    print(READY, flush=True)
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--allow", action="append", default=[])
    options = parser.parse_args()
    asyncio.run(serve(options.port, frozenset(host.lower() for host in options.allow)))


if __name__ == "__main__":
    main()
