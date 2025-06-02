import asyncio
import json
import websockets
import logging
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCDataChannel, RTCIceCandidate, RTCIceServer,RTCConfiguration
import time
import tuntap
import ipaddress
import ssl
import subprocess

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[        
        logging.StreamHandler(),
        logging.FileHandler('offerer.log', mode='a')
    ]
)
logger = logging.getLogger('offerer')

def parse_candidate(candidate_str):
    # Parse the candidate string into an RTCIceCandidate object
    # This is a placeholder implementation, adjust as necessary
    return RTCIceCandidate(candidate_str)

async def wait_for_signaling_server(url, retry_interval=30):
    import ssl
    ssl_context = ssl._create_unverified_context()
    browser_headers = {
        "Origin": "https://webrtc-vpn.mnv-dev.site",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        # Replace with your actual cf_clearance value
        "Cookie": "cf_clearance=z31K.46Cp3n5IpXHNiPBGuxUysFtT0O0kOosh9PJ6.A-1748719877-1.2.1.1-Lr.jHqZ9FnkoIwKy81b569PLjeTCtHme_erqTTIa.VslXwXs623NhWz3Wem7n5J3hD7vS5.NDIJt8gkXbUMNMdEFbVWThgxU5tcF1syJHL98JDyklZNkuxGZJuGIErIRyDAHYJ2zoWjpkKUNr5sFBGY2t.kFVRL3IGvHzyjRyAWINxZhVolhDbdrrWAugmKiqDD9UidsrQFA9JtfMIMrQcSdc.qf1R73UZuhjDxHWrp3ixtXZZO9culhnfBOgApwy5AIDyeqh8GVODOOPhHua9OQMhGvIkHUOxscDVkfRNd_o6n5qrxztAgR6ysiexwqeqyvVri2dluDh2l7m1faLs8GG8g7JmyCC2hwZ8R2XLFYQ0qyBlCH._BzJNJa6pfL"
    }
    while True:
        try:
            async with websockets.connect(
                url,                
                ssl=ssl_context
            ) as websocket:
                logger.info("Signaling server is online.")
                return
        except Exception as e:
            print(e)
            logger.warning(f"Signaling server not available, retrying in {retry_interval} seconds...")
            await asyncio.sleep(retry_interval)

def is_private_ip(ip):
    try:
        ip_obj = ipaddress.ip_address(ip)
        return ip_obj.is_private or ip_obj.is_loopback
    except ValueError:
        return False

def cleanup_tun_interface(name):
    """Clean up existing TUN interface if it exists"""
    try:
        # Check if interface exists
        result = subprocess.run(["ip", "link", "show", name], capture_output=True)
        if result.returncode == 0:
            logger.info(f"Found existing {name} interface, cleaning up...")
            # Bring interface down
            subprocess.run(["ip", "link", "set", name, "down"], check=True)
            # Delete interface
            subprocess.run(["ip", "link", "delete", name], check=True)
            logger.info(f"Successfully cleaned up {name}")
    except subprocess.CalledProcessError as e:
        logger.debug(f"No cleanup needed for {name}: {e}")
    except Exception as e:
        logger.error(f"Error during cleanup of {name}: {e}")

def tun_start(tap, channel):
    logger.info("Starting TUN interface and relay")
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            tap.open()
            break
        except OSError as e:
            if e.errno == 16:  # Device or resource busy
                retry_count += 1
                logger.warning(f"TUN device busy, attempt {retry_count}/{max_retries}")
                cleanup_tun_interface(tap.name)
                time.sleep(1)
                if retry_count == max_retries:
                    raise RuntimeError(f"Failed to open TUN device after {max_retries} attempts")
            else:
                raise
        except Exception as e:
            logger.error(f"Failed to open TAP device {tap.name}: {e}")
            raise

    def handle_incoming_data(data):
        try:
            logger.info(f"Received {len(data)} bytes from WebRTC channel")
            result = tap.fd.write(data)
            logger.info(f"Wrote {len(data)} bytes to TUN interface")
            return result
        except Exception as e:
            logger.error(f"Error writing to TUN: {e}")
    channel.on("message")(handle_incoming_data)

    def tun_reader():
        try:
            if not tap.fd or tap.fd.closed:
                logger.error("TUN interface is closed")
                return
            data = tap.fd.read(tap.mtu)
            if data:
                logger.info(f"Sending {len(data)} bytes through WebRTC channel")
                channel.send(data)
        except Exception as e:
            logger.error(f"Error reading from TUN: {e}")
            
    loop = asyncio.get_event_loop()
    loop.add_reader(tap.fd, tun_reader)
    tap.up()
    # Set IP address after interface is up
    try:
        subprocess.run(["ip", "address", "add", "172.16.0.1/24", "dev", tap.name], check=True)
        logger.info(f"Assigned 172.16.0.1/24 to {tap.name}")
        # Set MTU to 1300 for the offerer side
        subprocess.run(["ip", "link", "set", "dev", tap.name, "mtu", "1300"], check=True)
        logger.info(f"Set MTU 1300 for {tap.name}")
    except Exception as e:
        logger.error(f"Failed to assign IP or set MTU to {tap.name}: {e}")
    
    return tap.fd  # Return the file descriptor for cleanup

async def wait_for_ice_connected(pc):
    connected = asyncio.Event()
    def check_state():
        logger.info(f"ICE connection state changed to {pc.iceConnectionState}")
        if pc.iceConnectionState in ("connected", "completed"):
            connected.set()
        elif pc.iceConnectionState == "failed":
            logger.error("ICE connection failed")
            connected.set()
    pc.on("iceconnectionstatechange", check_state)
    check_state()
    await connected.wait()

async def run():
    signaling_url = "wss://webrtc-vpn.mnv-dev.site"
    ssl_context = ssl._create_unverified_context()
    browser_headers = {
        "Origin": "https://webrtc-vpn.mnv-dev.site",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        # Replace with your actual cf_clearance value
        "Cookie": "cf_clearance=z31K.46Cp3n5IpXHNiPBGuxUysFtT0O0kOosh9PJ6.A-1748719877-1.2.1.1-Lr.jHqZ9FnkoIwKy81b569PLjeTCtHme_erqTTIa.VslXwXs623NhWz3Wem7n5J3hD7vS5.NDIJt8gkXbUMNMdEFbVWThgxU5tcF1syJHL98JDyklZNkuxGZJuGIErIRyDAHYJ2zoWjpkKUNr5sFBGY2t.kFVRL3IGvHzyjRyAWINxZhVolhDbdrrWAugmKiqDD9UidsrQFA9JtfMIMrQcSdc.qf1R73UZuhjDxHWrp3ixtXZZO9culhnfBOgApwy5AIDyeqh8GVODOOPhHua9OQMhGvIkHUOxscDVkfRNd_o6n5qrxztAgR6ysiexwqeqyvVri2dluDh2l7m1faLs8GG8g7JmyCC2hwZ8R2XLFYQ0qyBlCH._BzJNJa6pfL"
    }
    while True:
        tap = None
        reader_handle = None
        pc = None
        
        await wait_for_signaling_server(signaling_url)
        try:
            # Add cleanup before creating TUN interface
            cleanup_tun_interface("revpn-offer")
            
            logger.info("Creating RTCPeerConnection with STUN server")
            stun_servers = [
                RTCIceServer(urls="stun:stun.l.google.com:19302"),
                RTCIceServer(urls="stun:stun1.l.google.com:19302"),
                RTCIceServer(urls="stun:stun2.l.google.com:19302"),
                RTCIceServer(urls="stun:stun3.l.google.com:19302"),
                RTCIceServer(urls="stun:stun4.l.google.com:19302"),
            ]
            config = RTCConfiguration(iceServers=stun_servers)
            pc = RTCPeerConnection(configuration=config)
            tap = tuntap.Tun(name="revpn-offer")
            reconnect_event = asyncio.Event()

            # Create the data channel for VPN traffic (like vpn.py)
            channel = pc.createDataChannel("vpntap")
            logger.info("Created data channel 'vpntap'")

            ready = asyncio.Event()
            @channel.on("open")
            def on_open():
                nonlocal ready, reader_handle
                logger.info("Data channel is open, waiting for ICE connection")
                async def check_ready():
                    logger.info("Waiting for ICE to connect...")
                    await wait_for_ice_connected(pc)
                    logger.info("ICE connected.")
                    ready.set()
                async def start_tun_when_ready():
                    await ready.wait()
                    logger.info("Starting TUN interface")
                    nonlocal reader_handle
                    reader_handle = tun_start(tap, channel)
                asyncio.create_task(check_ready())
                asyncio.create_task(start_tun_when_ready())

            @pc.on("connectionstatechange")
            def on_connectionstatechange():
                nonlocal tap, reader_handle
                logger.info(f"Connection state changed to: {pc.connectionState}")
                if pc.connectionState == "connected":
                    logger.info("WebRTC state: connected (ICE completed, DTLS connected)")
                    logger.info("P2P connection established, closing websocket connection.")
                    if ws:
                        asyncio.create_task(ws.close())
                elif pc.connectionState in ("disconnected", "failed", "closed"):
                    logger.warning("WebRTC state: lost (ICE disconnected/failed/closed), will attempt to reconnect")
                    # Clean up TUN interface reader
                    if reader_handle:
                        loop = asyncio.get_event_loop()
                        loop.remove_reader(tap.fd)
                        reader_handle = None
                    # Clean up TUN interface
                    if tap:
                        try:
                            tap.close()
                        except Exception as e:
                            logger.error(f"Error closing TUN interface: {e}")
                    asyncio.create_task(pc.close())
                    reconnect_event.set()

            @pc.on("icecandidate")
            async def on_icecandidate(candidate):
                if candidate:
                    logger.info(f"Generated ICE candidate: {candidate}")
                    if ws:
                        await ws.send(json.dumps({
                            "type": "candidate",
                            "target": "answerer",
                            "candidate": candidate.candidate,
                            "sdpMid": candidate.sdpMid,
                            "sdpMLineIndex": candidate.sdpMLineIndex,
                        }))

            ws = None
            try:
                logger.info("Connecting to signaling server...")
                async with websockets.connect(
                    signaling_url,
                    extra_headers=browser_headers,
                    ssl=ssl_context
                ) as websocket:
                    ws = websocket
                    logger.info("Registering as offerer")
                    await ws.send(json.dumps({"type": "register", "role": "offerer"}))

                    while True:
                        message = await ws.recv()
                        data = json.loads(message)
                        logger.info(f"Received message type: {data['type']}")
                        if data["type"] == "ready":
                            logger.info("Both peers are online. Proceeding to create offer.")
                            break
                    logger.info("Creating offer")
                    offer = await pc.createOffer()
                    await pc.setLocalDescription(offer)
                    logger.info("Set local description")

                    logger.info("Sending offer to answerer")
                    await ws.send(json.dumps({
                        "type": "offer",
                        "target": "answerer",
                        "sdp": pc.localDescription.sdp,
                    }))

                    async for message in ws:
                        data = json.loads(message)
                        logger.info(f"Received message type: {data['type']}")
                        if data["type"] == "answer":
                            logger.info("Processing answer from peer")
                            answer = RTCSessionDescription(sdp=data["sdp"], type="answer")
                            await pc.setRemoteDescription(answer)
                            logger.info("Set remote description")
                        elif data["type"] == "candidate":
                            logger.info("Processing ICE candidate from peer")
                            print(data['candidate'])
                            logger.info("Added ICE candidate")

                # Wait for reconnect event
                await reconnect_event.wait()
            except Exception as e:
                logger.error(f"Error occurred: {str(e)}. Reconnecting in 3 seconds...")
                await asyncio.sleep(3)
                continue
        except Exception as e:
            logger.error(f"Error occurred: {str(e)}")
            raise
        finally:
            ws = None

if __name__ == "__main__":
    tap_name = "revpn-offer"
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application error: {str(e)}")
    finally:
        logger.info("Cleaning up before exit...")
        cleanup_tun_interface(tap_name)