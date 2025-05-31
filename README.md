# aiortc WebRTC VPN Tunnel

This project demonstrates a robust point-to-point VPN tunnel over WebRTC using Python's `aiortc` and TAP devices. It includes a signaling server and two peer scripts (offerer and answerer) that relay TAP device traffic over a single WebRTC data channel, mimicking a VPN tunnel.

## Features

- **Single Data Channel**: Uses only one data channel (`vpntap`) for VPN traffic.
- **TAP Device Relay**: Relays TAP device traffic over WebRTC, with logic adapted from `vpn.py`.
- **Automatic IP Assignment**: Assigns static IPs to TAP interfaces after setup.
- **MTU Configuration**: Sets MTU to 1300 on both peers for optimal VPN performance.
- **Robust Reconnection**: Handles reconnection and signaling failures gracefully.
- **Cloudflare Tunnel Support**: Connects to a remote signaling server behind a Cloudflare tunnel, with browser-like headers and dynamic `cf_clearance` cookie support.

## Files

- `server.py`: Simple 1-to-1 WebSocket signaling server.
- `offerer.py`: Initiates the WebRTC connection, creates the data channel, and relays TAP traffic.
- `answerer.py`: Responds to the offer, joins the data channel, and relays TAP traffic.
- `tuntap.py`: TAP device abstraction.
- `vpn.py`: Original reference VPN relay logic.
- `ice_utils.py`: ICE candidate utilities (if needed).

## Requirements

- Python 3.8+
- Linux (with TAP device support)
- [aiortc](https://github.com/aiortc/aiortc)
- [websockets](https://websockets.readthedocs.io/)
- [playwright](https://playwright.dev/python/) (optional, for dynamic Cloudflare cookie fetching)

Install dependencies:

```sh
pip install aiortc websockets
# For dynamic Cloudflare cookie fetching (optional):
pip install playwright
playwright install
```

## Usage

### 1. Start the Signaling Server

On a public or private server (can be behind Cloudflare Tunnel):

```sh
python server.py
```

### 2. Start the Answerer (Peer 2)

On one machine:

```sh
sudo python answerer.py
```

- Creates TAP device `revpn-answer`.
- Assigns IP `172.16.0.2/24`.
- Sets MTU to 1300.

### 3. Start the Offerer (Peer 1)

On another machine:

```sh
sudo python offerer.py
```

- Creates TAP device `revpn-offer`.
- Assigns IP `172.16.0.1/24`.
- Sets MTU to 1300.

### 4. Cloudflare Tunnel/Access

If using a Cloudflare tunnel for the signaling server:

- The Python clients must send browser-like headers and a valid `cf_clearance` cookie.
- You can automate fetching the latest `cf_clearance` cookie using Playwright or Selenium (see code comments).

## Notes

- **Root Privileges**: Both peers require `sudo` to create and configure TAP devices.
- **MTU**: Both peers set MTU to 1300 for compatibility with WebRTC overhead.
- **Firewall**: Ensure UDP and WebSocket ports are open as needed.
- **Cloudflare**: If you get HTTP 403 errors, check your Cloudflare Access/Zero Trust settings and ensure your client is sending the correct headers and cookies.

## Troubleshooting

- **403 Forbidden**: Make sure your `cf_clearance` cookie is up to date and your headers mimic a browser.
- **TAP Device Errors**: Ensure you have permissions and the `tuntap` kernel module is loaded.
- **Connection Drops**: The scripts will automatically attempt to reconnect.

## Credits

- Based on [aiortc](https://github.com/aiortc/aiortc) and inspired by classic VPN-over-WebRTC demos.
