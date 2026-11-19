import ipaddress
from typing import Sequence
from fastapi import Request


class ClientAddressError(ValueError):
    """
    Raised when client IP resolution fails due to malformed headers,
    untrusted proxy topology, spoofing attempts, or configuration errors.
    """
    pass


# Security Bounds
MAX_FORWARDED_HOPS = 20
MAX_FORWARDED_HEADER_LENGTH = 2048


def get_client_ip(
    request: Request,
    trusted_proxy_cidrs: Sequence[str] | None = None,
) -> str:
    """
    Resolves the authoritative client IP address securely.

    Trust Model:
    1. Evaluates the direct TCP peer.
    2. If the peer is untrusted, all forwarding headers are strictly ignored.
    3. If the peer is trusted, parses X-Forwarded-For from right-to-left.
    4. Rejects duplicate X-Forwarded-For headers to prevent HTTP desync/smuggling.
    5. Rejects over-length or excessively chained headers.
    6. Returns the first untrusted IP in the chain (canonicalized).
    """
    # 1. Configuration Validation (Strict Type Checking)
    trusted_networks = []
    if trusted_proxy_cidrs is not None:
        if not isinstance(trusted_proxy_cidrs, (list, tuple)):
            raise ClientAddressError("Trusted proxy configuration must be a sequence.")
        for cidr in trusted_proxy_cidrs:
            if not isinstance(cidr, str):
                raise ClientAddressError("Trusted proxy CIDR entries must be strings.")
            try:
                # Parse strictly once. Do not repeat network parsing inside hop iteration.
                trusted_networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                raise ClientAddressError("Malformed trusted proxy CIDR configuration.")

    # 2. Direct Peer Validation
    try:
        # Starlette converts the scope["client"] tuple into an Address namedtuple.
        # Malformed tuple structures can raise ValueError, TypeError, AttributeError, or IndexError.
        client_conn = request.client
    except (AttributeError, IndexError, TypeError, ValueError):
        raise ClientAddressError("Malformed ASGI client scope.")

    if client_conn is None:
        raise ClientAddressError("Direct TCP peer is unavailable.")

    host = getattr(client_conn, "host", None)
    if not isinstance(host, str) or not host:
        raise ClientAddressError("Direct TCP peer address is malformed or missing.")

    try:
        direct_ip = ipaddress.ip_address(host)
        canonical_direct_ip = str(direct_ip)
    except ValueError:
        raise ClientAddressError("Direct peer address format is invalid.")

    # 3. Direct Peer Trust Evaluation
    is_direct_trusted = False
    for network in trusted_networks:
        if direct_ip in network:
            is_direct_trusted = True
            break

    if not is_direct_trusted:
        # Untrusted peer: Strict termination, ignore all forwarding headers.
        return canonical_direct_ip

    # 4. Raw ASGI Header Validation (Duplicate & Structure Defense)
    try:
        raw_headers = request.scope.get("headers", [])
        if not isinstance(raw_headers, list):
            raise ClientAddressError("Malformed ASGI headers scope.")

        xff_values = []
        for header in raw_headers:
            if not isinstance(header, (list, tuple)) or len(header) != 2:
                raise ClientAddressError("Malformed ASGI header pair.")

            name, value = header
            if not isinstance(name, bytes) or not isinstance(value, bytes):
                raise ClientAddressError("Malformed ASGI header data types.")

            if name.lower() == b"x-forwarded-for":
                xff_values.append(value)
    except (AttributeError, TypeError):
        raise ClientAddressError("Malformed ASGI header structure.")

    if len(xff_values) == 0:
        return canonical_direct_ip
    if len(xff_values) > 1:
        raise ClientAddressError("Duplicate forwarding headers detected.")

    xff_raw_bytes = xff_values[0]

    # 5. Forwarded Header Parsing
    if len(xff_raw_bytes) > MAX_FORWARDED_HEADER_LENGTH:
        raise ClientAddressError("Forwarding header exceeds length limits.")

    try:
        xff_str = xff_raw_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise ClientAddressError("Forwarding header contains invalid encoding.")

    if not xff_str.strip():
        raise ClientAddressError("Empty forwarding header detected.")

    raw_ips = xff_str.split(",")
    if len(raw_ips) > MAX_FORWARDED_HOPS:
        raise ClientAddressError("Forwarding chain exceeds allowed hops.")

    parsed_ips = []
    for raw_ip in raw_ips:
        ip_str = raw_ip.strip()
        if not ip_str:
            raise ClientAddressError("Empty element detected in forwarding chain.")
        try:
            parsed_ips.append(ipaddress.ip_address(ip_str))
        except ValueError:
            raise ClientAddressError("Malformed IP address in forwarding chain.")

    # 6. Trust Traversal (Right-to-Left)
    for parsed_ip in reversed(parsed_ips):
        ip_is_trusted = False
        for network in trusted_networks:
            if parsed_ip in network:
                ip_is_trusted = True
                break

        if not ip_is_trusted:
            return str(parsed_ip)

    # 7. All-Trusted Chain Fallback
    return canonical_direct_ip
