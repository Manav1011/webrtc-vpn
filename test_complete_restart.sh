#!/bin/bash

# Test script to validate complete restart behavior
# This script helps monitor the logs for complete restart events

echo "=== WebRTC VPN Complete Restart Test ==="
echo "This script will monitor for complete restart events in the logs"
echo ""

ROOM_ID="test-complete-restart-$(date +%s)"
echo "Using room ID: $ROOM_ID"
echo ""

# Function to monitor logs for complete restart events
monitor_logs() {
    local role=$1
    local logfile=$2
    
    echo "Monitoring $role logs for complete restart events..."
    tail -f "$logfile" | while read line; do
        if [[ "$line" == *"COMPLETE CONNECTION FAILURE"* ]]; then
            echo "🔴 [$role] COMPLETE RESTART DETECTED: $line"
        elif [[ "$line" == *"connection failed completely"* ]]; then
            echo "🔴 [$role] FULL RESTART TRIGGERED: $line"
        elif [[ "$line" == *"Connection state changed to: failed"* ]]; then
            echo "⚠️  [$role] CONNECTION FAILED: $line"
        elif [[ "$line" == *"Connection state changed to: closed"* ]]; then
            echo "⚠️  [$role] CONNECTION CLOSED: $line"
        elif [[ "$line" == *"Connection state changed to: connected"* ]]; then
            echo "✅ [$role] CONNECTION RESTORED: $line"
        elif [[ "$line" == *"Data channel opened"* ]]; then
            echo "✅ [$role] DATA CHANNEL READY: $line"
        fi
    done
}

echo "Starting offerer and answerer..."
echo "Press Ctrl+C to stop both processes"

# Start both processes in background
./bin/offerer -room "$ROOM_ID" > offerer_restart_test.log 2>&1 &
OFFERER_PID=$!

./bin/answerer -room "$ROOM_ID" > answerer_restart_test.log 2>&1 &
ANSWERER_PID=$!

# Wait a moment for startup
sleep 2

echo "Offerer PID: $OFFERER_PID"
echo "Answerer PID: $ANSWERER_PID"
echo ""

# Monitor both logs
monitor_logs "OFFERER" "offerer_restart_test.log" &
MONITOR1_PID=$!

monitor_logs "ANSWERER" "answerer_restart_test.log" &
MONITOR2_PID=$!

echo "Monitoring started. Test scenarios:"
echo "1. Kill answerer process: kill $ANSWERER_PID"
echo "2. Kill offerer process: kill $OFFERER_PID"
echo "3. Restart answerer: kill $ANSWERER_PID && ./bin/answerer -room \"$ROOM_ID\" > answerer_restart_test.log 2>&1 &"
echo ""

# Wait for user interrupt
trap 'echo "Stopping all processes..."; kill $OFFERER_PID $ANSWERER_PID $MONITOR1_PID $MONITOR2_PID 2>/dev/null; exit' INT

wait
