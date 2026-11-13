"""LAN helpers for the demo network mode: local addresses and a self-signed HTTPS certificate.

Phones only allow camera access and notifications on secure origins, so LAN mode serves HTTPS.
The certificate is self-signed for this computer's LAN addresses; each phone must accept the
warning once. Keys stay under the ignored data directory and are never committed.
"""

import datetime
import ipaddress
import socket
from pathlib import Path


def lan_addresses():
    """Best-effort list of this machine's IPv4 LAN addresses (no packets are sent)."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))  # UDP connect only selects a route
            found.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    result = []
    for address in found:
        ip = ipaddress.ip_address(address)
        if ip.is_loopback or ip.is_link_local or address in result:
            continue
        result.append(address)
    return result


def ensure_certificate(directory, addresses):
    """Return (cert_path, key_path), regenerating when the LAN addresses are not covered."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / "sentrycare-lan.crt", directory / "sentrycare-lan.key"
    wanted = sorted(set(addresses) | {"127.0.0.1"})
    if cert_path.is_file() and key_path.is_file():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            covered = {str(ip) for ip in sans.get_values_for_type(x509.IPAddress)}
            if set(wanted) <= covered and cert.not_valid_after_utc > datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7):
                return cert_path, key_path
        except Exception:
            pass
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SentryCare local demo")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(a)) for a in wanted]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path
