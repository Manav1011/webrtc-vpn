# How `offerer.py` and `answerer.py` Work Together

This document explains how `offerer.py` and `answerer.py` collaborate to establish a VPN tunnel using WebRTC.

---

## 1. Signaling Server Connection

- Both scripts connect to the same WebSocket signaling server:  
    `wss://webrtc-vpn.mnv-dev.site`
- `offerer.py` registers as the **offerer**.
- `answerer.py` registers as the **answerer**.
- Each waits for both peers to be online (signaled by a `"ready"` message).

---

## 2. WebRTC Peer Connection Setup

- Both create an `RTCPeerConnection` with the same STUN servers for NAT traversal.
- `offerer.py` creates a data channel named `"vpntap"` for VPN traffic.
- `answerer.py` waits for the data channel to be created and receives it.

---

## 3. Offer/Answer Exchange (Signaling)

- `offerer.py` creates an SDP offer and sends it to the answerer via the signaling server.
- `answerer.py` receives the offer, sets it as the remote description, creates an SDP answer, and sends it back.
- `offerer.py` receives the answer and sets it as the remote description.

---

## 4. ICE Candidate Exchange

- Both peers gather ICE candidates (network info for connectivity).
- Each sends its ICE candidates to the other via the signaling server.
- Each peer adds received ICE candidates to its connection.

---

## 5. Data Channel Establishment

- Once ICE negotiation is complete and the data channel is open:
    - Both scripts set up event handlers for the `"vpntap"` data channel.

---

## 6. TUN Interface Setup

- Each script creates a TUN interface (`revpn-offer` and `revpn-answer`).
- Each assigns a private IP address and sets the MTU.
- Each sets up a loop to:
    - Read from the TUN device and send data over the data channel.
    - Write received data from the data channel to the TUN device.

---

## 7. Data Sharing (VPN Traffic)

- When a packet arrives at the TUN interface (from the OS network stack), it is read and sent over the WebRTC data channel to the peer.
- When a packet is received from the data channel, it is written to the local TUN interface.
- This creates a point-to-point VPN tunnel between the two machines, with all traffic routed through the WebRTC data channel.

---

## 8. Connection Management & Cleanup

- Both scripts monitor connection state.
- On disconnect or failure, they clean up the TUN interface and try to reconnect.

---

## Summary

- The two scripts use a signaling server to exchange connection info and ICE candidates.
- They establish a WebRTC data channel for direct peer-to-peer communication.
- Each creates a TUN interface and relays packets between the TUN device and the data channel.
- All VPN traffic is shared between the two scripts via the WebRTC data channel, creating a virtual network link.

---