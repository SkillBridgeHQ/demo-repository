import pytest
from fastapi import Request
from app.core.client_ip import get_client_ip, ClientAddressError, MAX_FORWARDED_HOPS, MAX_FORWARDED_HEADER_LENGTH

def build_mock_request(client_host, raw_headers: list[tuple[bytes, bytes]] = None) -> Request:
    """
    Constructs a Request object with native Starlette ASGI scope format.
    client_host can be a string, or an int to trigger malformed type exceptions.
    """
    scope = {
        "type": "http",
        "client": (client_host, 12345) if client_host is not None else None,
        "headers": raw_headers or []
    }
    return Request(scope)

# ==========================================
# Original Preservation Tests (15 Functions)
# ==========================================

def test_direct_client_ip_no_proxy():
    req = build_mock_request("203.0.113.5", [])
    assert get_client_ip(req, ["10.0.0.0/8"]) == "203.0.113.5"

def test_untrusted_peer_spoofing_protection():
    req = build_mock_request("203.0.113.5", [(b"x-forwarded-for", b"1.1.1.1")])
    assert get_client_ip(req, ["10.0.0.0/8"]) == "203.0.113.5"

def test_trusted_proxy_forwarding():
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", b"198.51.100.10")])
    assert get_client_ip(req, ["10.0.0.0/8"]) == "198.51.100.10"

def test_multiple_proxy_hops():
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", b"203.0.113.99, 10.0.0.1")])
    assert get_client_ip(req, ["10.0.0.0/8"]) == "203.0.113.99"

def test_trusted_proxy_without_forwarding_header():
    req = build_mock_request("10.0.0.5", [(b"host", b"test")])
    assert get_client_ip(req, ["10.0.0.0/8"]) == "10.0.0.5"

def test_all_trusted_chain_fallback():
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", b"10.0.0.2, 10.0.0.1")])
    assert get_client_ip(req, ["10.0.0.0/8"]) == "10.0.0.5"

def test_invalid_forwarded_ip():
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", b"not-an-ip")])
    with pytest.raises(ClientAddressError, match="Malformed IP address"):
        get_client_ip(req, ["10.0.0.0/8"])

def test_empty_forwarded_chain_elements():
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", b"1.1.1.1, , 2.2.2.2")])
    with pytest.raises(ClientAddressError, match="Empty element detected"):
        get_client_ip(req, ["10.0.0.0/8"])

def test_invalid_proxy_addresses():
    req = build_mock_request("invalid-peer-ip")
    with pytest.raises(ClientAddressError, match="Direct peer address format is invalid"):
        get_client_ip(req)

def test_missing_direct_peer():
    req = build_mock_request(None)
    with pytest.raises(ClientAddressError, match="Direct TCP peer is unavailable"):
        get_client_ip(req)

def test_ipv6_canonicalization():
    req = build_mock_request("fd00::1", [(b"x-forwarded-for", b"2001:0db8:0000:0000:0000:0000:0000:0001")])
    ip = get_client_ip(req, ["fd00::/8"])
    assert ip == "2001:db8::1"

def test_invalid_trusted_proxy_configuration():
    req = build_mock_request("10.0.0.5")
    with pytest.raises(ClientAddressError, match="Malformed trusted proxy CIDR"):
        get_client_ip(req, ["invalid-cidr"])

def test_malformed_ip_in_forwarded_chain():
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", b"203.0.113.10, invalid-proxy")])
    with pytest.raises(ClientAddressError, match="Malformed IP address in forwarding chain"):
        get_client_ip(req, ["10.0.0.0/8"])

def test_bad_direct_peer_host_type():
    req = build_mock_request(12345)
    with pytest.raises(ClientAddressError, match="Direct TCP peer address is malformed or missing"):
        get_client_ip(req)

def test_malformed_asgi_client_tuple():
    """Specific regression scenario: invalid tuple length fails safely instead of throwing IndexError."""
    scope = {"type": "http", "client": ("10.0.0.5",), "headers": []}
    req = Request(scope)
    with pytest.raises(ClientAddressError, match="Malformed ASGI client scope"):
        get_client_ip(req, ["10.0.0.0/8"])


# ==========================================
# New Hardening Tests (14 Functions)
# ==========================================

def test_non_string_cidr_rejected():
    req = build_mock_request("10.0.0.5")
    with pytest.raises(ClientAddressError, match="CIDR entries must be strings"):
        get_client_ip(req, [100008])

def test_string_cidr_rejected():
    req = build_mock_request("10.0.0.5")
    with pytest.raises(ClientAddressError, match="must be a sequence"):
        get_client_ip(req, "10.0.0.0/8")

def test_empty_string_cidr_rejected():
    req = build_mock_request("10.0.0.5")
    with pytest.raises(ClientAddressError, match="must be a sequence"):
        get_client_ip(req, "")

def test_duplicate_xff_trusted_peer_rejected():
    headers = [
        (b"X-Forwarded-For", b"1.1.1.1"),
        (b"x-forwarded-for", b"2.2.2.2")
    ]
    req = build_mock_request("10.0.0.5", headers)
    with pytest.raises(ClientAddressError, match="Duplicate forwarding headers"):
        get_client_ip(req, ["10.0.0.0/8"])

def test_duplicate_xff_untrusted_peer_ignored():
    headers = [
        (b"X-Forwarded-For", b"1.1.1.1"),
        (b"x-forwarded-for", b"2.2.2.2")
    ]
    req = build_mock_request("203.0.113.5", headers)
    assert get_client_ip(req, ["10.0.0.0/8"]) == "203.0.113.5"

def test_malformed_asgi_headers():
    req = Request({"type": "http", "client": ("10.0.0.5", 12345), "headers": "bad"})
    with pytest.raises(ClientAddressError, match="Malformed ASGI headers scope"):
        get_client_ip(req, ["10.0.0.0/8"])

def test_malformed_asgi_header_tuple():
    req = Request({"type": "http", "client": ("10.0.0.5", 12345), "headers": [("bad",)]})
    with pytest.raises(ClientAddressError, match="Malformed ASGI header pair"):
        get_client_ip(req, ["10.0.0.0/8"])

def test_exactly_2048_bytes_allowed():
    ips = [f"1.1.1.{i}" for i in range(1, 20)] + ["8.8.8.8"]
    base_val = b", ".join(ip.encode("ascii") for ip in ips)
    padding = 2048 - len(base_val)
    val = base_val.replace(b", ", b", " + b" " * padding, 1)

    assert len(val) == 2048
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", val)])

    assert get_client_ip(req, ["10.0.0.0/8"]) == "8.8.8.8"

def test_2049_bytes_rejected():
    ips = [f"1.1.1.{i}" for i in range(1, 20)] + ["8.8.8.8"]
    base_val = b", ".join(ip.encode("ascii") for ip in ips)
    padding = 2049 - len(base_val)
    val = base_val.replace(b", ", b", " + b" " * padding, 1)

    assert len(val) == 2049
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", val)])
    with pytest.raises(ClientAddressError, match="Forwarding header exceeds length"):
        get_client_ip(req, ["10.0.0.0/8"])

def test_exactly_max_hops_allowed():
    chain = ", ".join([f"198.51.100.{i}" for i in range(1, MAX_FORWARDED_HOPS + 1)])
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", chain.encode("ascii"))])
    assert get_client_ip(req, ["10.0.0.0/8"]) == "198.51.100.20"

def test_more_than_max_hops_rejected():
    chain = ", ".join([f"198.51.100.{i}" for i in range(1, MAX_FORWARDED_HOPS + 2)])
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", chain.encode("ascii"))])
    with pytest.raises(ClientAddressError, match="Forwarding chain exceeds allowed hops"):
        get_client_ip(req, ["10.0.0.0/8"])

def test_explicitly_empty_xff_rejected():
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", b"   ")])
    with pytest.raises(ClientAddressError, match="Empty forwarding header"):
        get_client_ip(req, ["10.0.0.0/8"])

def test_none_trusted_cidrs_is_handled():
    req = build_mock_request("203.0.113.5", [(b"x-forwarded-for", b"1.1.1.1")])
    assert get_client_ip(req, None) == "203.0.113.5"

def test_all_trusted_chain_is_handled_with_fallback():
    # Additional logic validation on fallback explicitly scoped to hardening constraints
    req = build_mock_request("10.0.0.5", [(b"x-forwarded-for", b"10.0.0.2, 10.0.0.1")])
    assert get_client_ip(req, ["10.0.0.0/8"]) == "10.0.0.5"
