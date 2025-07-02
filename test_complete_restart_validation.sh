#!/bin/bash

echo "=== WebRTC VPN Complete Restart Validation Test ==="
echo "This script helps validate that complete connection failures trigger full restarts"
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}Building binaries...${NC}"
go build -o bin/offerer cmd/offerer/main.go
go build -o bin/answerer cmd/answerer/main.go

if [ $? -ne 0 ]; then
    echo -e "${RED}Build failed!${NC}"
    exit 1
fi

echo -e "${GREEN}Build successful!${NC}"
echo ""

echo -e "${YELLOW}Test Instructions:${NC}"
echo "1. Run the offerer in one terminal: sudo ./bin/offerer -room testroom"
echo "2. Run the answerer in another terminal: sudo ./bin/answerer -room testroom"
echo "3. Wait for connection to establish (you'll see keepalive pings)"
echo "4. Kill the answerer (Ctrl+C) and restart it immediately"
echo "5. Observe that the offerer detects the restart and triggers new negotiation"
echo "6. If connection fails completely, you should see:"
echo "   - 'Connection failed - triggering complete restart' (offerer)"
echo "   - 'WebRTC state: failed - triggering complete restart' (answerer)"
echo "   - '*** COMPLETE CONNECTION FAILURE - Full PeerConnection restart ***'"
echo ""

echo -e "${BLUE}What to Look For:${NC}"
echo "✅ Complete restart logs when connection fails"
echo "✅ New PeerConnection, TAP interface, and DataChannel creation"
echo "✅ Successful reconnection after restart"
echo "✅ No signaling conflicts or rapid offer processing"
echo ""

echo -e "${GREEN}Ready to test! Start the programs as instructed above.${NC}"
