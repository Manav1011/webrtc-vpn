#!/bin/bash

cd "$(dirname "$0")"
/usr/local/go/bin/go build -o bin/offerer cmd/offerer/main.go
/usr/local/go/bin/go build -o bin/answerer cmd/answerer/main.go

# Check if role argument is provided
if [ -z "$1" ]; then
    echo "Usage: $0 <offerer|answerer> <room_id>"
    exit 1
fi

# Check if room_id argument is provided
if [ -z "$2" ]; then
    echo "Usage: $0 <offerer|answerer> <room_id>"
    exit 1
fi

ROLE=$1
ROOM_ID=$2

# Run the appropriate binary with sudo
if [ "$ROLE" = "offerer" ]; then
    sudo ./bin/offerer -room "$ROOM_ID"
elif [ "$ROLE" = "answerer" ]; then
    sudo ./bin/answerer -room "$ROOM_ID"
else
    echo "Invalid role. Use 'offerer' or 'answerer'"
    exit 1
fi 