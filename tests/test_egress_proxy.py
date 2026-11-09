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
