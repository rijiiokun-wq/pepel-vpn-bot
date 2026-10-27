"""Publish the approved primary entry before its DDG fallback endpoints."""
from urllib.parse import parse_qs, quote, unquote, urlsplit

SOURCE = "5owmkn.whitechannel-x1-cdn.ru"
ADDITIONAL = ("5owmkn.edge-assets.ru", "v3zipd.edge-assets.ru")
EXPECTED_PATH = "/api/v1/0b623738cecedb35/sync/"


def _append_clearway_ddg_hosts(payload: str, host_name: str, tier: str | None) -> str:
    """Run after access/tier filtering; this does not issue or mutate users."""
    _ = tier
    if host_name != "MainPanel" or not payload:
        return payload
    lines = payload.splitlines()
    known = set()
    for line in lines:
        try:
            parsed = urlsplit(line.strip())
            known.add((parsed.scheme, parsed.username, parsed.hostname, parsed.port))
        except ValueError:
            pass
    additions = []
    published = {line.strip() for line in lines}
    preferred = []
    for line in lines:
        try:
            parsed = urlsplit(line.strip())
            query = parse_qs(parsed.query, keep_blank_values=True)
            if not (
                parsed.scheme == "vless" and parsed.hostname == SOURCE and parsed.port == 443
                and parsed.username and parsed.password is None
                and unquote(parsed.fragment) == "WHITELIST2"
                and query.get("security") == ["tls"] and query.get("type") == ["xhttp"]
                and query.get("mode") == ["packet-up"] and query.get("path") == [EXPECTED_PATH]
                and query.get("host") == [SOURCE] and query.get("sni") == [SOURCE]
            ):
                continue
        except ValueError:
            continue
        preferred.append(line.strip())
        replacements = []
        for hostname in ADDITIONAL:
            identity = ("vless", parsed.username, hostname, 443)
            query_parts = []
            for part in parsed.query.split("&"):
                key, separator, _value = part.partition("=")
                if unquote(key) in {"host", "sni"}:
                    part = key + separator + quote(hostname, safe="")
                query_parts.append(part)
            # Keep UUID, percent-encoded extra, path, TLS/fp/ALPN and all unknown
            # query fields exactly as published by the working primary profile.
            clone = parsed._replace(
                netloc=parsed.netloc.rsplit("@", 1)[0] + "@" + hostname + ":443",
                query="&".join(query_parts),
                fragment="WHITELIST3" if hostname == ADDITIONAL[0] else "WHITELIST4",
            ).geturl()
            replacements.append(clone)
            if identity not in known:
                additions.append(clone)
                published.add(clone)
                known.add(identity)
        # Promote only exact clones; do not override an existing endpoint with
        # conflicting parameters merely because its UUID/domain matches.
        preferred.extend(clone for clone in replacements if clone in published)
    if not preferred:
        return payload
    rank = {line: index for index, line in enumerate(dict.fromkeys(preferred))}
    chunks = payload.splitlines(keepends=True) + additions
    prioritized = sorted(
        ((rank[line.strip()], index, line) for index, line in enumerate(chunks)
         if line.strip() in rank),
        key=lambda item: (item[0], item[1]),
    )
    ordered = [line for _, _, line in prioritized] + [
        line for line in chunks if line.strip() not in rank
    ]
    # Keep each existing URI and ordinary-node relative order unchanged.
    result = ""
    for line in ordered:
        if result and not result.endswith(("\n", "\r")):
            result += "\n"
        result += line
    return _append_direct_hosts(result, DIRECT_ENTRIES)


# Direct Entry1 aliases: publish only routes whose exact profiles passed 10 MB.
DIRECT_ENTRIES = ({'address': '5owmkn.d2.nordtile.ru', 'label': 'WHITELIST5'}, {'address': 'v3zipd.d2.nordtile.ru', 'label': 'WHITELIST6'})


def _append_direct_hosts(payload, entries):
    """Only append domain-clones of an eligible current primary Entry1."""
    if not payload or not entries:
        return payload
    known = set()
    sources = []
    for line in payload.splitlines():
        try:
            source = urlsplit(line.strip())
            q = parse_qs(source.query, keep_blank_values=True)
            if source.scheme == 'vless' and source.username and q.get('path'):
                known.add((source.username, source.hostname, source.port, q['path'][0]))
            if (source.scheme == 'vless' and source.hostname == SOURCE and source.port == 443
                    and source.username and source.password is None
                    and unquote(source.fragment) == 'WHITELIST2'
                    and q.get('security') == ['tls'] and q.get('type') == ['xhttp']
                    and q.get('mode') == ['packet-up'] and q.get('path') == [EXPECTED_PATH]
                    and q.get('host') == [SOURCE] and q.get('sni') == [SOURCE]):
                sources.append(source)
        except ValueError:
            continue
    additions = []
    for entry in entries:
        hostname, label = entry['address'], entry['label']
        assert hostname in ('5owmkn.d2.nordtile.ru', 'v3zipd.d2.nordtile.ru')
        assert label in ('WHITELIST5', 'WHITELIST6')
        for source in sources:
            identity = (source.username, hostname, 443, EXPECTED_PATH)
            if identity in known:
                # Preserve any existing conflicting endpoint. Readback must
                # detect a mismatch; this renderer must never overwrite it.
                continue
            parts = []
            for part in source.query.split('&'):
                key, separator, _value = part.partition('=')
                if unquote(key) in ('host', 'sni'):
                    part = key + separator + quote(hostname, safe='')
                parts.append(part)
            clone = source._replace(
                netloc=source.netloc.rsplit('@', 1)[0] + '@' + hostname + ':443',
                query='&'.join(parts), fragment=quote(label, safe='')).geturl()
            additions.append(clone)
            known.add(identity)
    if not additions:
        return payload
    separator = '' if payload.endswith(('\n', '\r')) else '\n'
    return payload + separator + '\n'.join(additions) + '\n'


# Presentation is applied after eligibility and alias generation.
DISPLAY_HOSTS = {
    "5owmkn.d2.nordtile.ru": (0, "5"),
    "v3zipd.d2.nordtile.ru": (1, "6"),
    SOURCE: (2, "2"),
    ADDITIONAL[0]: (3, "3"),
    ADDITIONAL[1]: (4, "4"),
}
DISPLAY_PREFIX = "ОБХОД БЕЛЫХ СПИСКОВ "


def _format_whitelist_hosts(payload: str) -> str:
    rows = []
    matched = False
    for line in payload.splitlines(keepends=True):
        rank = len(DISPLAY_HOSTS)
        raw = line.rstrip("\r\n")
        ending = line[len(raw):]
        try:
            parsed = urlsplit(raw)
            display = DISPLAY_HOSTS.get(parsed.hostname)
            if parsed.scheme == "vless" and display:
                prefix, marker, fragment = raw.partition("#")
                label, separator, description = fragment.partition("?serverDescription=")
                number = display[1]
                display_number = "1" if parsed.hostname == "5owmkn.d2.nordtile.ru" else number
                query = parse_qs(parsed.query, keep_blank_values=True)
                if (marker and unquote(label) in ("WHITELIST" + number, DISPLAY_PREFIX + number,
                                                  DISPLAY_PREFIX + display_number)
                        and parsed.port == 443 and query.get("path") == [EXPECTED_PATH]):
                    rank = display[0]
                    line = prefix + "#" + quote(DISPLAY_PREFIX + display_number, safe="")
                    line += separator + description + ending
                    matched = True
        except ValueError:
            pass
        rows.append((rank, line))
    if not matched:
        return payload
    result = ""
    for _rank, line in sorted(rows, key=lambda item: item[0]):
        if result and not result.endswith(("\n", "\r")):
            result += "\n"
        result += line
    return result


def append_clearway_ddg_hosts(payload: str, host_name: str, tier: str | None) -> str:
    """Publish eligible aliases with user-facing names and Direct entries first."""
    result = _append_clearway_ddg_hosts(payload, host_name, tier)
    if host_name != "MainPanel":
        return result
    return _format_whitelist_hosts(result)
