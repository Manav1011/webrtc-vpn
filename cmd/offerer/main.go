package main

import (
	"crypto/tls"
	"flag"
	"fmt"
	"io"
	"log"
	"net/url"
	"os"
	"os/exec"
	"sync"
	"time"
	"webrtc-vpn-go/pkg/signaling"

	"github.com/gorilla/websocket"
	"github.com/pion/webrtc/v3"
	"github.com/songgao/water"
)

func main() {
	roomID := flag.String("room", "", "Room ID for signaling")
	flag.Parse()

	if *roomID == "" {
		log.Fatal("Room ID is required")
	}

	u := url.URL{Scheme: "wss", Host: "webrtc-vpn-go.mnv-dev.site", Path: "/ws"}
	log.Printf("Connecting to %s", u.String())

	dialer := websocket.Dialer{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
	}

	c, _, err := dialer.Dial(u.String(), nil)
	if err != nil {
		log.Fatalf("Failed to connect to signaling server: %v", err)
	}
	defer c.Close()

	var wsWriteMu sync.Mutex

	registerMsg := signaling.Message{
		Type: "register",
		Role: "offerer",
		Room: *roomID,
	}
	wsWriteMu.Lock()
	if err := c.WriteJSON(registerMsg); err != nil {
		wsWriteMu.Unlock()
		log.Fatalf("Failed to register: %v", err)
	}
	wsWriteMu.Unlock()

	for {
		// Wait for ready message from server before attempting connection
		var msg signaling.Message
		if err := c.ReadJSON(&msg); err != nil {
			log.Printf("Error reading from websocket: %v", err)
			return
		}
		if msg.Type == "ready" {
			log.Println("Received ready message, starting WebRTC connection...")
			err := runWithSignaling(c, &wsWriteMu)
			if err != nil {
				log.Printf("PeerConnection reset, waiting for answerer to reconnect...")
				continue
			}
		} else {
			log.Printf("Waiting for ready message, got: %s", msg.Type)
		}
	}
}

func runWithSignaling(c *websocket.Conn, wsWriteMu *sync.Mutex) error {
	var (
		hasConnected    bool // Track if we ever established a connection
		needsDisconnect bool // Track if we need to send disconnect on exit
	)

	// Helper function to send disconnect message
	sendDisconnect := func() {
		if !needsDisconnect {
			return // Don't send duplicate disconnect messages
		}
		msg := signaling.Message{
			Type: "disconnect",
		}
		wsWriteMu.Lock()
		if err := c.WriteJSON(msg); err != nil {
			log.Printf("Error sending disconnect message: %v", err)
		}
		wsWriteMu.Unlock()
		log.Println("Sent disconnect message to signaling server")
		needsDisconnect = false
	}

	config := webrtc.Configuration{
		ICEServers: []webrtc.ICEServer{
			{URLs: []string{"stun:stun.l.google.com:19302"}},
			{URLs: []string{"stun:stun1.l.google.com:19302"}},
			{URLs: []string{"stun:stun2.l.google.com:19302"}},
			{URLs: []string{"stun:stun3.l.google.com:19302"}},
			{URLs: []string{"stun:stun4.l.google.com:19302"}},
		},
	}

	peerConnection, err := webrtc.NewPeerConnection(config)
	if err != nil {
		return err
	}
	defer func() {
		peerConnection.Close()
		// Only send disconnect if we had established a connection
		if hasConnected {
			sendDisconnect()
		}
	}()

	tunConfig := water.Config{
		DeviceType: water.TAP,
		PlatformSpecificParams: water.PlatformSpecificParams{
			Name:    "revpn-offer_",
			Persist: true,
		},
	}

	log.Println("Opening existing TAP interface 'revpn-offer_'...")
	tap, err := water.New(tunConfig)
	if err != nil {
		log.Printf("Failed to open TAP interface: %v\n", err)
		if os.IsPermission(err) {
			log.Println("Permission denied. Make sure you have the right permissions to access the TAP interface")
		}
		return err
	}
	defer tap.Close()

	log.Printf("Successfully opened TAP interface: %s\n", tap.Name())

	cmd := exec.Command("ip", "link", "set", "dev", tap.Name(), "mtu", "1300")
	if err := cmd.Run(); err != nil {
		log.Printf("Failed to set MTU 1300 for %s: %v", tap.Name(), err)
	} else {
		log.Printf("Set MTU 1300 for %s", tap.Name())
	}

	dataChannel, err := peerConnection.CreateDataChannel("vpntap", nil)
	if err != nil {
		return err
	}

	reconnectChan := make(chan struct{})
	var closeOnce sync.Once
	var connectionFailed bool

	// Helper to trigger a connection reset and return from runWithSignaling
	triggerFatal := func(reason string) {
		log.Printf("PeerConnection reset needed: %s", reason)
		closeOnce.Do(func() {
			if hasConnected {
				needsDisconnect = true // Mark that we need to send disconnect on exit
			}
			close(reconnectChan)
			connectionFailed = true
		})
	}

	peerConnection.OnConnectionStateChange(func(s webrtc.PeerConnectionState) {
		log.Printf("Connection state changed to: %s\n", s.String())
		switch s {
		case webrtc.PeerConnectionStateConnected:
			log.Println("WebRTC state: connected (ICE completed, DTLS connected)")
			hasConnected = true
			needsDisconnect = true // We'll need to send disconnect if this connection ends
		case webrtc.PeerConnectionStateDisconnected:
			log.Printf("WebRTC state: disconnected (waiting for recovery)")
			// Start a timer - if still disconnected after delay, trigger reset
			go func() {
				time.Sleep(10 * time.Second)
				if peerConnection.ConnectionState() == webrtc.PeerConnectionStateDisconnected {
					log.Println("Connection still disconnected after delay, triggering reset")
					triggerFatal("Connection recovery timeout")
				}
			}()
		case webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed:
			log.Printf("WebRTC state: %s (ICE failed/closed)\n", s.String())
			triggerFatal("Connection state: " + s.String())
		}
	})
	peerConnection.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		log.Printf("ICE connection state changed to: %s\n", state.String())
		if state == webrtc.ICEConnectionStateFailed {
			log.Println("ICE connection failed, triggering reset")
			triggerFatal("ICE connection failed")
		} else if state == webrtc.ICEConnectionStateDisconnected {
			log.Println("ICE connection disconnected, waiting for recovery")
			// Let OnConnectionStateChange handle the timeout
		}
	})
	peerConnection.OnICECandidate(func(ice *webrtc.ICECandidate) {
		if ice != nil {
			iceJSON := ice.ToJSON()
			candidateMsg := signaling.Message{
				Type:      "candidate",
				Target:    "answerer",
				Candidate: &iceJSON,
			}
			wsWriteMu.Lock()
			if err := c.WriteJSON(candidateMsg); err != nil {
				log.Printf("Error sending ICE candidate: %v", err)
			}
			wsWriteMu.Unlock()
		}
	})
	var lastPongTime time.Time
	dataChannel.OnOpen(func() {
		log.Println("Data channel opened")
		lastPongTime = time.Now() // Initialize on connection
		go func() {
			ticker := time.NewTicker(2 * time.Second) // More frequent keepalive
			defer ticker.Stop()
			for {
				select {
				case <-ticker.C:
					if dataChannel.ReadyState() == webrtc.DataChannelStateOpen {
						log.Printf("DataChannel state: %s, Time since last pong: %v", dataChannel.ReadyState(), time.Since(lastPongTime))
						if err := dataChannel.Send([]byte("ping")); err != nil {
							log.Printf("Error sending keepalive: %v", err)
							triggerFatal("Keepalive failed")
							return
						}
						// If we haven't received a pong in 10 seconds, trigger reconnect
						if time.Since(lastPongTime) > 10*time.Second {
							log.Printf("No pong received in %v, triggering reconnect", time.Since(lastPongTime))
							triggerFatal("No pong received")
							return
						}
					}
				case <-reconnectChan:
					return
				}
			}
		}()
		go func() {
			buffer := make([]byte, 1500)
			for {
				n, err := tap.Read(buffer)
				if err != nil {
					if err != io.EOF {
						log.Printf("Error reading from TAP: %v", err)
					}
					return
				}
				if err := dataChannel.Send(buffer[:n]); err != nil {
					log.Printf("Error sending data: %v", err)
					return
				}
			}
		}()
	})
	dataChannel.OnMessage(func(msg webrtc.DataChannelMessage) {
		if string(msg.Data) == "ping" {
			log.Println("Received keepalive ping, sending pong")
			if err := dataChannel.Send([]byte("pong")); err != nil {
				log.Printf("Error sending pong: %v", err)
			}
			return
		}
		if string(msg.Data) == "pong" {
			log.Println("Received keepalive pong")
			lastPongTime = time.Now()
			return
		}
		if _, err := tap.Write(msg.Data); err != nil {
			log.Printf("Error writing to TAP: %v", err)
		}
	})

	// Wait for ready message
	for {
		if connectionFailed {
			return fmt.Errorf("connection failed, triggering reconnect")
		}
		var msg signaling.Message
		if err := c.ReadJSON(&msg); err != nil {
			return err
		}
		log.Printf("Received message type: %s", msg.Type)
		if msg.Type == "ready" {
			log.Println("Both peers are online. Creating offer...")
			break
		}
	}

	// Add a short delay to allow answerer to reset before sending offer
	time.Sleep(500 * time.Millisecond)

	offer, err := peerConnection.CreateOffer(nil)
	if err != nil {
		triggerFatal("CreateOffer initial failed")
		return err
	}
	if err = peerConnection.SetLocalDescription(offer); err != nil {
		triggerFatal("SetLocalDescription initial failed")
		return err
	}
	offerMsg := signaling.Message{
		Type:   "offer",
		Target: "answerer",
		SDP:    offer.SDP,
	}
	wsWriteMu.Lock()
	if err := c.WriteJSON(offerMsg); err != nil {
		wsWriteMu.Unlock()
		triggerFatal("WriteJSON initial offer failed")
		return err
	}
	wsWriteMu.Unlock()

	for {
		if connectionFailed {
			return fmt.Errorf("connection failed, triggering reconnect")
		}
		var msg signaling.Message
		if err := c.ReadJSON(&msg); err != nil {
			return err
		}
		switch msg.Type {
		case "answer":
			answer := webrtc.SessionDescription{
				Type: webrtc.SDPTypeAnswer,
				SDP:  msg.SDP,
			}
			if err := peerConnection.SetRemoteDescription(answer); err != nil {
				triggerFatal("SetRemoteDescription answer failed")
				return err
			}
		case "candidate":
			if msg.Candidate != nil {
				if err := peerConnection.AddICECandidate(*msg.Candidate); err != nil {
					log.Printf("Error adding ICE candidate: %v", err)
				}
			}
		}
	}
}
