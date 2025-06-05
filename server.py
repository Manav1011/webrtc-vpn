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

# Store multiple peer groups by room/session ID
rooms = {}

async def notify_peers_ready(room):
    # Notify both peers when both are connected in a room
    if room['offerer'] and room['answerer']:
        try:
            await room['offerer'].send(json.dumps({"type": "ready"}))
        except Exception:
            pass
        try:
            await room['answerer'].send(json.dumps({"type": "ready"}))
        except Exception:
            pass

def get_room(room_id):
    if room_id not in rooms:
        rooms[room_id] = {
            'offerer': None,
            'answerer': None,
            'pending_offer': None,
            'pending_candidates': {
                'offerer': [],
                'answerer': []
            }
        }
    return rooms[room_id]

async def signaling(websocket, path='/'):
    client_id = str(id(websocket))[-6:]
    client_role = None
    room_id = None
    logger.info(f"New connection from client: {client_id}")
    try:
        async for message in websocket:
            data = json.loads(message)
            logger.info(f"Received message from {client_id}: {data['type']}")

            if data['type'] == 'register':
                role = data['role']
                room_id = str(data.get('room', 'default'))
                room = get_room(room_id)
                # Check if role is already taken in this room
                if room[role] is not None:
                    logger.warning(f"Rejecting client {client_id}: {role} already registered in room {room_id}")
                    await websocket.close()
                    return
                # Register the peer
                room[role] = websocket
                client_role = role
                logger.info(f"Client {client_id} registered as {role} in room {room_id}")
                # Notify both peers if both are present
                await notify_peers_ready(room)
            elif data['type'] == 'offer':
                room = get_room(room_id)
                if room['answerer']:
                    logger.info(f"Forwarding offer to answerer in room {room_id}")
                    await room['answerer'].send(message)
                else:
                    logger.info(f"Storing offer until answerer connects in room {room_id}")
                    room['pending_offer'] = message
            elif data['type'] == 'answer':
                room = get_room(room_id)
                if room['offerer']:
                    logger.info(f"Forwarding answer to offerer in room {room_id}")
                    await room['offerer'].send(message)
                else:
                    logger.warning(f"Offerer not connected in room {room_id}, cannot forward answer")
            elif data['type'] == 'candidate':
                room = get_room(room_id)
                target_role = 'answerer' if client_role == 'offerer' else 'offerer'
                if room[target_role]:
                    logger.info(f"Forwarding ICE candidate to {target_role} in room {room_id}")
                    await room[target_role].send(message)
                else:
                    logger.info(f"Storing ICE candidate for {target_role} in room {room_id}")
                    room['pending_candidates'][target_role].append(data)
    except Exception as e:
        logger.error(f"Error handling client {client_id}: {str(e)}")
    finally:
        if client_role and room_id:
            room = get_room(room_id)
            room[client_role] = None
            logger.info(f"Client {client_id} ({client_role}) disconnected from room {room_id}")
            if client_role == 'offerer':
                room['pending_offer'] = None
                room['pending_candidates']['offerer'] = []

async def main():
    logger.info("Starting 1-to-1 signaling server on ws://localhost:8080")
    async with websockets.serve(signaling, "localhost", 8080):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())