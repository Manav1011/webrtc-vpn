import argparse
import asyncio
import ipaddress
import logging

import tuntap
from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription
from aiortc import RTCConfiguration, RTCIceServer
from aiortc.contrib.signaling import BYE, add_signaling_arguments, create_signaling

logger = logging.getLogger("vpn")

def channel_log(channel, t, message):
    print(f"channel({channel.label}) {t} {message}")

def is_private_ip(ip):
    try:
        ip_obj = ipaddress.ip_address(ip)
        return ip_obj.is_private or ip_obj.is_loopback
    except ValueError:
        return False

async def consume_signaling(pc, signaling):
    while True:
        obj = await signaling.receive()

        if isinstance(obj, RTCSessionDescription):
            await pc.setRemoteDescription(obj)

            if obj.type == "offer":
                await pc.setLocalDescription(await pc.createAnswer())
                await signaling.send(pc.localDescription)

        elif isinstance(obj, RTCIceCandidate):
            await pc.addIceCandidate(obj)

        elif obj is BYE:
            print("Exiting")
            break

async def send_ice_candidates(pc, signaling):
    @pc.on("icecandidate")
    async def on_icecandidate(candidate):
        if candidate:
            ip = candidate.address
            if is_private_ip(ip):
                await signaling.send(candidate)

def tun_start(tap, channel):
    print("Starting TUN interface and relay")
    try:
        tap.open()
    except Exception as e:
        print(f"Failed to open TAP device {tap.name}: {e}")
        raise

    # relay channel -> tap (INCOMING DATA)
    def handle_incoming_data(data):
        try:
            print(f"Received {len(data)} bytes from WebRTC channel")
            result = tap.fd.write(data)
            print(f"Wrote {len(data)} bytes to TUN interface")
            return result
        except Exception as e:
            print(f"Error writing to TUN: {e}")
    
    channel.on("message")(handle_incoming_data)

    # relay tap -> channel (OUTGOING DATA)
    def tun_reader():
        try:
            data = tap.fd.read(tap.mtu)
            if data:
                print(f"Sending {len(data)} bytes through WebRTC channel")
                channel.send(data)
        except Exception as e:
            print(f"Error reading from TUN: {e}")

    loop = asyncio.get_event_loop()
    loop.add_reader(tap.fd, tun_reader)

    tap.up()

async def wait_for_ice_connected(pc):
    connected = asyncio.Event()

    def check_state():
        print(f"ICE connection state changed to {pc.iceConnectionState}")
        if pc.iceConnectionState in ("connected", "completed"):
            connected.set()
        elif pc.iceConnectionState == "failed":
            print("ICE connection failed")
            connected.set()

    pc.on("iceconnectionstatechange", check_state)
    check_state()
    await connected.wait()

async def run_answer(pc, signaling, tap):
    await signaling.connect()
    await send_ice_candidates(pc, signaling)

    ready = asyncio.Event()

    @pc.on("datachannel")
    def on_datachannel(channel):
        channel_log(channel, "-", "created by remote party")
        print(channel.label)
        if channel.label == "vpntap":
            
            async def check_ready():
                print("Waiting for ICE to connect...")
                await wait_for_ice_connected(pc)
                print("ICE connected.")
                ready.set()

            async def start_tun_when_ready():
                await ready.wait()
                print("Starting TUN interface")
                tun_start(tap, channel)

            # Check if channel is already open
            if channel.readyState == "open":
                print("Data channel is already open, waiting for ICE connection")
                asyncio.ensure_future(check_ready())
                asyncio.ensure_future(start_tun_when_ready())
            else:
                # Set up the open handler for when it opens later
                @channel.on("open")
                def on_open():
                    print("Data channel is open, waiting for ICE connection")
                    asyncio.ensure_future(check_ready())
                
                asyncio.ensure_future(start_tun_when_ready())

    await consume_signaling(pc, signaling)

    await consume_signaling(pc, signaling)

async def run_offer(pc, signaling, tap):
    await signaling.connect()
    await send_ice_candidates(pc, signaling)

    channel = pc.createDataChannel("vpntap")
    channel_log(channel, "-", "created by local party")

    ready = asyncio.Event()

    @channel.on("open")
    def on_open():
        print("Data channel is open, waiting for ICE connection")
        asyncio.ensure_future(check_ready())

    async def check_ready():
        print("Waiting for ICE to connect...")
        await wait_for_ice_connected(pc)
        print("ICE connected.")
        ready.set()

    async def start_tun_when_ready():
        await ready.wait()
        print("Starting TUN interface")
        tun_start(tap, channel)

    asyncio.ensure_future(start_tun_when_ready())

    await pc.setLocalDescription(await pc.createOffer())
    await signaling.send(pc.localDescription)

    await consume_signaling(pc, signaling)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VPN over data channel")
    parser.add_argument("role", choices=["offer", "answer"])
    parser.add_argument("--verbose", "-v", action="count")
    add_signaling_arguments(parser)
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.CRITICAL + 1)  # Silence all logs

    tap = tuntap.Tun(name="revpn-%s" % args.role)
    signaling = create_signaling(args)

    configuration = RTCConfiguration(
        iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
    )
    pc = RTCPeerConnection(configuration)

    if args.role == "offer":
        coro = run_offer(pc, signaling, tap)
    else:
        coro = run_answer(pc, signaling, tap)

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(coro)
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(pc.close())
        loop.run_until_complete(signaling.close())
        tap.close()
