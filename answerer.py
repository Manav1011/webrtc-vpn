import asyncio
import json
import websockets
import logging
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCIceServer, RTCConfiguration
import time
import tuntap
import ipaddress
import subprocess
import sys


# Add DEBUG logging
logging.basicConfig(
    level=logging.INFO,  # Changed from INFO to DEBUG
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[        
        logging.StreamHandler(),
        logging.FileHandler('answerer.log', mode='a')
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

def tun_start(tap, channel):
    logger.info("Starting TUN interface and relay")
    max_retries = 3
    retry_count = 0
    
    # Always use string for interface name
    if isinstance(tap.name, bytes):
        tap_name_str = tap.name.decode()
    else:
        tap_name_str = tap.name

    while retry_count < max_retries:
        try:
            tap.open()
            break
        except OSError as e:
            if e.errno == 16:  # Device or resource busy
                retry_count += 1
                logger.warning(f"TUN device busy, attempt {retry_count}/{max_retries}")
                cleanup_tun_interface(tap_name_str)
                time.sleep(1)
                if retry_count == max_retries:
                    raise RuntimeError(f"Failed to open TUN device after {max_retries} attempts")
            else:
                raise
        except Exception as e:
            logger.error(f"Failed to open TAP device {tap_name_str}: {e}")
            raise

    def handle_incoming_data(data):
        try:
            if data == b"ping":
                logger.debug("Received keepalive ping on data channel")
                return
            try:
                result = tap.fd.write(data)
                return result
            except Exception as e:
                logger.error(f"Error writing to TUN interface: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Exception in handle_incoming_data: {e}", exc_info=True)
    channel.on("message")(handle_incoming_data)

    def tun_reader():
        try:
            if not tap.fd or tap.fd.closed:
                logger.error("TUN interface is closed")
                return
            try:
                data = tap.fd.read(tap.mtu)
                if data:
                    channel.send(data)
            except Exception as e:
                logger.error(f"Error reading from TUN interface: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Exception in tun_reader: {e}", exc_info=True)
            
    loop = asyncio.get_event_loop()
    loop.add_reader(tap.fd, tun_reader)
    tap.up()
    # Set IP address after interface is up
    try:
        subprocess.run(["ip", "address", "add", "172.16.0.2/24", "dev", tap_name_str], check=True)
        logger.info(f"Assigned 172.16.0.2/24 to {tap_name_str}")
        # Set MTU to 1300 for the answerer side
        subprocess.run(["ip", "link", "set", "dev", tap_name_str, "mtu", "1300"], check=True)
        logger.info(f"Set MTU 1300 for {tap_name_str}")
    except Exception as e:
        logger.error(f"Failed to assign IP or set MTU to {tap_name_str}: {e}")
    
    return tap.fd  # Return the file descriptor for cleanup

async def run(room_id):
    tap_name = room_id
    signaling_url = "wss://webrtc-tunnel.mnv.rocks/ws"
    while True:
        tap = None
        reader_handle = None
        pc = None
        channel_ref = {"channel": None}  # Using a dict to store mutable reference
        
        await wait_for_signaling_server(signaling_url)
        try:
            # Add cleanup before creating TUN interface
            cleanup_tun_interface(tap_name)
            
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
            tap = tuntap.Tun(name=tap_name)
            reconnect_event = asyncio.Event()

            ready = asyncio.Event()
            # Add data channel close/error handlers and keepalive ping
            @pc.on("datachannel")
            def on_datachannel(channel):
                nonlocal ready, reader_handle
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
                        nonlocal reader_handle
                        reader_handle = tun_start(tap, channel)
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
                    # Add keepalive ping every 10 seconds
                    async def keepalive_ping():
                        while True:
                            try:
                                if channel.readyState == "open":
                                    channel.send(b"ping")
                                    logger.debug("Sent keepalive ping on data channel")
                                await asyncio.sleep(10)
                            except Exception as e:
                                logger.error(f"Keepalive ping error: {e}")
                                break
                    asyncio.create_task(keepalive_ping())

                    @channel.on("close")
                    def on_close():
                        logger.warning("Data channel closed!")
                    @channel.on("error")
                    def on_error(e):
                        logger.error(f"Data channel error: {e}")

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
                    await ws.send(json.dumps({"type": "register", "role": "answerer", "room": room_id}))

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
    if len(sys.argv) < 2:
        print("Usage: python offerer.py <room_id>")
        sys.exit(1)

    room_id = str(sys.argv[1])
    if len(room_id) > 15:
        print("Error: room_id must not exceed 15 characters (required for Linux interface naming).")
        sys.exit(1)
    try:
        asyncio.run(run(room_id))
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application error: {str(e)}")
    finally:
        logger.info("Cleaning up before exit...")
        cleanup_tun_interface(room_id)