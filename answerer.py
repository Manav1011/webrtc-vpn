import asyncio
import json
import websockets
import logging
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, RTCIceServer, RTCConfiguration
import time
import tuntap
import ipaddress
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
    logger.info("Starting TAP interface and relay")
    try:
        tap.open()
    except Exception as e:
        logger.error(f"Failed to open TAP device {tap.name}: {e}")
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
                logger.error(f"Error writing to TAP interface: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Exception in handle_incoming_data: {e}", exc_info=True)
    channel.on("message")(handle_incoming_data)

    def tap_reader():
        try:
            if not tap.fd or tap.fd.closed:
                logger.error("TAP interface is closed")
                return
            try:
                data = tap.fd.read(tap.mtu)
                if data:
                    channel.send(data)
            except Exception as e:
                logger.error(f"Error reading from TAP interface: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Exception in tap_reader: {e}", exc_info=True)
    loop = asyncio.get_event_loop()
    loop.add_reader(tap.fd, tap_reader)
    tap.up()
    return tap.fd

async def run():
    if len(sys.argv) < 2:
        print("Usage: python answerer.py <room_id>")
        sys.exit(1)
    room_id = str(sys.argv[1])
    signaling_url = "wss://webrtc-vpn.mnv-dev.site"
    while True:
        tap = None
        reader_handle = None
        pc = None
        channel_ref = {"channel": None}
        await wait_for_signaling_server(signaling_url)
        try:
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
            tap = tuntap.Tun(name="revpn-answer")
            reconnect_event = asyncio.Event()
            ready = asyncio.Event()
            @pc.on("datachannel")
            def on_datachannel(channel):
                nonlocal ready, reader_handle
                channel_ref["channel"] = channel
                logger.info(f"Data channel {channel.label} established")
                if channel.label == "vpntap":
                    async def check_ready():
                        logger.info("Waiting for ICE to connect...")
                        await wait_for_ice_connected(pc)
                        logger.info("ICE connected.")
                        ready.set()
                    async def start_tap_when_ready():
                        await ready.wait()
                        logger.info("Starting TAP interface")
                        nonlocal reader_handle
                        reader_handle = tun_start(tap, channel)
                    if channel.readyState == "open":
                        logger.info("Data channel is already open, waiting for ICE connection")
                        asyncio.create_task(check_ready())
                        asyncio.create_task(start_tap_when_ready())
                    else:
                        @channel.on("open")
                        def on_open():
                            logger.info("Data channel is open, waiting for ICE connection")
                            asyncio.create_task(check_ready())
                            asyncio.create_task(start_tap_when_ready())
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
                    # Clean up TAP interface reader
                    if reader_handle:
                        loop = asyncio.get_event_loop()
                        loop.remove_reader(tap.fd)
                        reader_handle = None
                    # Clean up TAP interface
                    if tap:
                        try:
                            tap.close()
                        except Exception as e:
                            logger.error(f"Error closing TAP interface: {e}")
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
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Application stopped by user")
    except Exception as e:
        logger.error(f"Application error: {str(e)}")