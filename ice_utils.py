from aiortc import RTCIceCandidate

def parse_candidate(candidate_str: str) -> RTCIceCandidate:
    parts = candidate_str.split()
    # Expected minimal parts count is 8 (foundation, component, protocol, priority, ip, port, 'typ', type)
    if len(parts) < 8:
        raise ValueError("Invalid candidate string format")

    foundation = parts[0].replace("candidate:", "")
    component = int(parts[1])
    protocol = parts[2].lower()
    priority = int(parts[3])
    ip = parts[4]
    port = int(parts[5])
    # 'typ' should be parts[6], type is parts[7]
    if parts[6] != "typ":
        raise ValueError("Invalid candidate string format: missing 'typ'")

    cand_type = parts[7]

    # Optional fields follow after index 7
    related_address = None
    related_port = None
    tcp_type = None

    # Parse optional fields key-value pairs
    opt_idx = 8
    while opt_idx + 1 < len(parts):
        key = parts[opt_idx]
        value = parts[opt_idx + 1]
        if key == "raddr":
            related_address = value
        elif key == "rport":
            related_port = int(value)
        elif key == "tcptype":
            tcp_type = value
        opt_idx += 2

    return RTCIceCandidate(
        component=component,
        foundation=foundation,
        ip=ip,
        port=port,
        priority=priority,
        protocol=protocol,
        type=cand_type,
        relatedAddress=related_address,
        relatedPort=related_port,
        tcpType=tcp_type,
    )
