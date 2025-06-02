import asyncio
import json
import websockets
import logging
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCIceServer, RTCConfiguration
import time
import tuntap
import ipaddress
import subprocess


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[        
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('answerer')

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

async def wait_for_signaling_server(url, retry_interval=30):
    while True:
        try:
            async with websockets.connect(url):
                logger.info("Signaling server is online.")
                return
        except Exception:
            logger.warning(f"Signaling server not available, retrying in {retry_interval} seconds...")
            await asyncio.sleep(retry_interval)

async def run():
    signaling_url = "wss://webrtc-vpn.mnv-dev.site"
    while True:
        await wait_for_signaling_server(signaling_url)
        try:
            # Add cleanup before creating TUN interface
            cleanup_tun_interface("revpn-answer")
            
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
            channel_ref = {"channel": None}
            ping_task = None
            reconnect_event = asyncio.Event()
            tap = tuntap.Tun(name="revpn-answer")

            def is_private_ip(ip):
                try:
                    ip_obj = ipaddress.ip_address(ip)
                    return ip_obj.is_private or ip_obj.is_loopback
                except ValueError:
                    return False

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
                    subprocess.run(["ip", "address", "add", "172.16.0.2/24", "dev", tap.name], check=True)
                    logger.info(f"Assigned 172.16.0.2/24 to {tap.name}")
                    # Set MTU to 1300 for the answerer side
                    subprocess.run(["ip", "link", "set", "dev", tap.name, "mtu", "1300"], check=True)
                    logger.info(f"Set MTU 1300 for {tap.name}")
                except Exception as e:
                    logger.error(f"Failed to assign IP or set MTU to {tap.name}: {e}")

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

            ready = asyncio.Event()
            @pc.on("datachannel")
            def on_datachannel(channel):
                nonlocal ready
                channel_ref["channel"] = channel
                logger.info(f"Data channel {channel.label} established")
                if channel.label == "vpntap":
                    # VPN relay logic: wait for ICE, then start TUN
                    async def check_ready():
                        logger.info("Waiting for ICE to connect...")
                        await wait_for_ice_connected(pc)
                        logger.info("ICE connected.")
                        ready.set()
                    async def start_tun_when_ready():
                        await ready.wait()
                        logger.info("Starting TUN interface")
                        tun_start(tap, channel)
                    if channel.readyState == "open":
                        logger.info("Data channel is already open, waiting for ICE connection")
                        asyncio.create_task(check_ready())
                        asyncio.create_task(start_tun_when_ready())
                    else:
                        @channel.on("open")
                        def on_open():
                            logger.info("Data channel is open, waiting for ICE connection")
                            asyncio.create_task(check_ready())
                        asyncio.create_task(start_tun_when_ready())

            @pc.on("connectionstatechange")
            def on_connectionstatechange():
                logger.info(f"Connection state changed to: {pc.connectionState}")
                if pc.connectionState == "new":
                    logger.info("WebRTC state: new (connection created, ICE not started)")
                elif pc.connectionState == "checking":
                    logger.info("WebRTC state: checking (gathering ICE candidates and checking connectivity)")
                elif pc.connectionState == "connected":
                    logger.info("WebRTC state: connected (ICE completed, DTLS connected)")
                    logger.info("P2P connection established, closing websocket connection.")
                    if ws:
                        asyncio.create_task(ws.close())
                elif pc.connectionState == "disconnected":
                    logger.warning("WebRTC state: disconnected (ICE disconnected, will attempt to reconnect)")
                    if ping_task:
                        ping_task.cancel()
                    asyncio.create_task(pc.close())
                    logger.info("Restarting connection loop due to P2P connection loss.")
                    reconnect_event.set()
                elif pc.connectionState == "failed":
                    logger.error("WebRTC state: failed (ICE connection failed, will attempt to reconnect)")
                    if ping_task:
                        ping_task.cancel()
                    asyncio.create_task(pc.close())
                    logger.info("Restarting connection loop due to P2P connection loss.")
                    reconnect_event.set()
                elif pc.connectionState == "closed":
                    logger.info("WebRTC state: closed (connection closed)")
                    if ping_task:
                        ping_task.cancel()
                    asyncio.create_task(pc.close())
                    logger.info("Restarting connection loop due to P2P connection loss.")
                    reconnect_event.set()

            @pc.on("icecandidate")
            async def on_icecandidate(candidate):
                if candidate:
                    logger.info(f"Generated ICE candidate: {candidate}")
                    if ws:
                        await ws.send(json.dumps({
                            "type": "candidate",
                            "target": "offerer",
                            "candidate": candidate.candidate,
                            "sdpMid": candidate.sdpMid,
                            "sdpMLineIndex": candidate.sdpMLineIndex,
                        }))

            ws = None
            try:
                logger.info("Connecting to signaling server...")
                async with websockets.connect(signaling_url) as websocket:
                    ws = websocket
                    logger.info("Registering as answerer")
                    await ws.send(json.dumps({"type": "register", "role": "answerer"}))

                    while True:
                        message = await ws.recv()
                        data = json.loads(message)
                        logger.info(f"Received message type: {data['type']}")
                        if data["type"] == "ready":
                            logger.info("Both peers are online. Waiting for offer.")
                            break
                    async for message in ws:
                        data = json.loads(message)
                        logger.info(f"Received message type: {data['type']}")
                        if data["type"] == "offer":
                            logger.info("Processing offer from peer")
                            offer = RTCSessionDescription(sdp=data["sdp"], type="offer")
                            await pc.setRemoteDescription(offer)
                            logger.info("Set remote description")
                            logger.info("Creating answer")
                            answer = await pc.createAnswer()
                            await pc.setLocalDescription(answer)
                            logger.info("Set local description")
                            logger.info("Sending answer to offerer")
                            await ws.send(json.dumps({
                                "type": "answer",
                                "target": "offerer",
                                "sdp": pc.localDescription.sdp,
                            }))
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
    tap_name = "revpn-answer"
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application error: {str(e)}")
    finally:
        logger.info("Cleaning up before exit...")
        cleanup_tun_interface(tap_name)