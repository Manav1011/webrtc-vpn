import asyncio
import json
import websockets
import logging
import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[        
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('signaling_server')

# Store only one pair of peers
peers = {
    'offerer': None,
    'answerer': None
}
pending_offer = None
pending_candidates = {
    'offerer': [],
    'answerer': []
}

async def notify_peers_ready():
    # Notify both peers when both are connected
    if peers['offerer'] and peers['answerer']:
        try:
            await peers['offerer'].send(json.dumps({"type": "ready"}))
        except Exception:
            pass
        try:
            await peers['answerer'].send(json.dumps({"type": "ready"}))
        except Exception:
            pass

async def signaling(websocket, path):
    global pending_offer
    global pending_candidates
    client_id = str(id(websocket))[-6:]
    client_role = None
    logger.info(f"New connection from client: {client_id}")
    
    try:
        async for message in websocket:
            data = json.loads(message)
            logger.info(f"Received message from {client_id}: {data['type']}")
            
            if data['type'] == 'register':
                role = data['role']
                
                # Check if role is already taken
                if peers[role] is not None:
                    logger.warning(f"Rejecting client {client_id}: {role} already registered")
                    await websocket.close()
                    return
                
                # Register the peer
                peers[role] = websocket
                client_role = role
                logger.info(f"Client {client_id} registered as {role}")
                
                # Notify both peers if both are present
                await notify_peers_ready()
                # Do not send pending offer/candidates yet
                
            elif data['type'] == 'offer':
                if peers['answerer']:
                    logger.info("Forwarding offer to answerer")
                    await peers['answerer'].send(message)
                else:
                    logger.info("Storing offer until answerer connects")
                    pending_offer = message
                    
            elif data['type'] == 'answer':
                if peers['offerer']:
                    logger.info("Forwarding answer to offerer")
                    await peers['offerer'].send(message)
                else:
                    logger.warning("Offerer not connected, cannot forward answer")
                    
            elif data['type'] == 'candidate':
                target_role = 'answerer' if client_role == 'offerer' else 'offerer'
                if peers[target_role]:
                    logger.info(f"Forwarding ICE candidate to {target_role}")
                    await peers[target_role].send(message)
                else:
                    logger.info(f"Storing ICE candidate for {target_role}")
                    pending_candidates[target_role].append(data)
            
    except Exception as e:
        logger.error(f"Error handling client {client_id}: {str(e)}")
    finally:
        if client_role:
            peers[client_role] = None
            logger.info(f"Client {client_id} ({client_role}) disconnected")
            if client_role == 'offerer':
                pending_offer = None
                pending_candidates['offerer'] = []

async def main():
    logger.info("Starting 1-to-1 signaling server on ws://localhost:8080")
    async with websockets.serve(signaling, "localhost", 8080):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())