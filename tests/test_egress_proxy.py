from overnight.execution.egress_proxy import allowed, public


def test_only_listed_hosts_and_dotted_subdomains_are_allowed():
    hosts = frozenset({"openrouter.ai", ".example.com"})
    assert allowed("openrouter.ai", hosts) and allowed("OpenRouter.ai.", hosts)
    assert allowed("api.example.com", hosts)
    assert not allowed("evilopenrouter.ai", hosts)
    assert not allowed("api.openrouter.ai", hosts)
    assert not allowed("evilexample.com", hosts)


def test_only_public_addresses_are_reachable():
    assert public("104.18.2.1")
    for inside in ("127.0.0.1", "192.168.65.254", "10.0.0.1", "172.17.0.1", "169.254.1.1", "::1"):
        assert not public(inside)


def test_an_unencodable_name_is_refused_not_crashed():
    import asyncio

    from overnight.execution.egress_proxy import _public_address

    assert asyncio.run(_public_address("a" * 64 + ".github.com")) is None


def test_the_policy_port_reaches_one_host_port_and_nothing_else(monkeypatch):
    """Training only: the proxy's policy port passes bytes to the one configured port, while its
    CONNECT port keeps refusing the host."""
    import asyncio
    import socket

    from overnight.execution import egress_proxy

    def free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    async def scenario() -> tuple[bytes, bytes]:
        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write(b"policy:" + await reader.readline())
            await writer.drain()
            writer.close()

        policy = await asyncio.start_server(echo, "127.0.0.1", 0)
        policy_port = policy.sockets[0].getsockname()[1]
        proxy_port, listen_port = free_port(), free_port()
        # The container's fixed policy port may be taken on the host; any free one serves here.
        monkeypatch.setattr(egress_proxy, "POLICY_LISTEN_PORT", listen_port)
        proxy = asyncio.create_task(
            egress_proxy.serve(proxy_port, frozenset(), ("127.0.0.1", policy_port))
        )
        await asyncio.sleep(0.2)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
            writer.write(b"POST /v1/chat/completions\n")
            forwarded = await reader.read()
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
            refused = await reader.read()
            return forwarded, refused
        finally:
            proxy.cancel()
            policy.close()

    forwarded, refused = asyncio.run(scenario())
    assert forwarded == b"policy:POST /v1/chat/completions\n"
    assert refused.startswith(b"HTTP/1.1 403")
