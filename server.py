import json
import logging
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[        
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('signaling_server')

app = FastAPI()

# Optionally allow CORS for testing (remove or restrict in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store multiple peer groups by room/session ID
rooms = {}

async def notify_peers_ready(room):
    if room['offerer'] and room['answerer']:
        try:
            await room['offerer'].send_json({"type": "ready"})
        except Exception:
            pass
        try:
            await room['answerer'].send_json({"type": "ready"})
        except Exception:
            pass
        # Deliver pending offer if present
        if room['pending_offer']:
            try:
                await room['answerer'].send_text(room['pending_offer'])
                logger.info(f"Delivered pending offer to answerer in room.")
                room['pending_offer'] = None
            except Exception as e:
                logger.warning(f"Failed to deliver pending offer: {e}")
        # Deliver pending candidates if present
        for role in ['offerer', 'answerer']:
            for candidate in room['pending_candidates'][role]:
                try:
                    if room[role]:
                        await room[role].send_json(candidate)
                        logger.info(f"Delivered pending candidate to {role} in room.")
                except Exception as e:
                    logger.warning(f"Failed to deliver pending candidate to {role}: {e}")
            room['pending_candidates'][role] = []

async def notify_peer_down(room, role_left):
    """Send a peer_down event to the remaining peer when its counterpart goes offline."""
    other = 'answerer' if role_left == 'offerer' else 'offerer'
    if room.get(other):
        try:
            await room[other].send_json({"type": "peer_down"})
            logger.info(f"Sent peer_down to {other} in room.")
        except Exception as e:
            logger.warning(f"Failed to send peer_down to {other}: {e}")

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

@app.get("/")
async def root():
    return JSONResponse(content={"message": "WebSocket server running"})

@app.api_route("/health", methods=["GET", "HEAD"])
async def health_check():
    return JSONResponse(content={"status": "ok"})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_id = str(id(websocket))[-6:]
    client_role = None
    room_id = None
    logger.info(f"New connection from client: {client_id}")
    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            logger.info(f"Received message from {client_id} (room={room_id}, role={client_role}): {data['type']}")

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
                # Notify both peers if both are present and deliver pending
                await notify_peers_ready(room)
            elif data['type'] == 'disconnect':
                # Explicit disconnect message from client
                if client_role and room_id:
                    room = get_room(room_id)
                    # Notify the remaining peer before cleaning up
                    await notify_peer_down(room, client_role)
                    room[client_role] = None
                    logger.info(f"Client {client_id} ({client_role}) sent disconnect for room {room_id}")
                    # Clear all room state immediately
                    if room_id in rooms:
                        logger.info(f"Explicit disconnect: Cleaning up room {room_id}")
                        del rooms[room_id]
                await websocket.close()
                return
            elif data['type'] == 'offer':
                room = get_room(room_id)
                if room['answerer']:
                    logger.info(f"Forwarding offer to answerer in room {room_id}")
                    await room['answerer'].send_text(message)
                else:
                    logger.info(f"Storing offer until answerer connects in room {room_id}")
                    room['pending_offer'] = message
            elif data['type'] == 'answer':
                room = get_room(room_id)
                if room['offerer']:
                    logger.info(f"Forwarding answer to offerer in room {room_id}")
                    await room['offerer'].send_text(message)
                else:
                    logger.warning(f"Offerer not connected in room {room_id}, cannot forward answer")
            elif data['type'] == 'candidate':
                room = get_room(room_id)
                target_role = 'answerer' if client_role == 'offerer' else 'offerer'
                if room[target_role]:
                    logger.info(f"Forwarding ICE candidate to {target_role} in room {room_id}")
                    await room[target_role].send_json(data)
                else:
                    logger.info(f"Storing ICE candidate for {target_role} in room {room_id}")
                    room['pending_candidates'][target_role].append(data)
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error from client {client_id}: {e}")
    except WebSocketDisconnect as e:
        logger.info(f"Connection closed for client {client_id}: {e}")
    except Exception as e:
        logger.error(f"Error handling client {client_id}: {str(e)}")
    finally:
        if client_role and room_id:
            room = get_room(room_id)
            # Notify the other peer that this one is gone
            await notify_peer_down(room, client_role)
            room[client_role] = None
            logger.info(f"Client {client_id} ({client_role}) disconnected from room {room_id}")
            if client_role == 'offerer':
                room['pending_offer'] = None
                room['pending_candidates']['offerer'] = []
            # Clean up room if both peers are gone
            if not room['offerer'] and not room['answerer']:
                logger.info(f"Cleaning up empty room {room_id}")
                del rooms[room_id]

if __name__ == "__main__":
    host = os.environ.get('SIGNALING_HOST', '0.0.0.0')
    port = int(os.environ.get('SIGNALING_PORT', '9090'))
    logger.info(f"Starting FastAPI signaling server on ws://{host}:{port}")
    uvicorn.run("server:app", host=host, port=port, reload=True)