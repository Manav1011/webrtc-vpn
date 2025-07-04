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

func connectWebSocket(role, room string) (*websocket.Conn, *sync.Mutex, error) {
	u := url.URL{Scheme: "wss", Host: "webrtc-vpn-go.mnv-dev.site", Path: "/ws"}
	log.Printf("Connecting to %s", u.String())

	dialer := websocket.Dialer{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
	}

	c, _, err := dialer.Dial(u.String(), nil)
	if err != nil {
		return nil, nil, err
	}

	wsWriteMu := &sync.Mutex{}
	reg := signaling.Message{Type: "register", Role: role, Room: room}
	wsWriteMu.Lock()
	err = c.WriteJSON(reg)
	wsWriteMu.Unlock()
	if err != nil {
		c.Close()
		return nil, nil, err
	}
	return c, wsWriteMu, nil
}

func main() {
	roomID := flag.String("room", "", "Room ID for signaling")
	flag.Parse()
	if *roomID == "" {
		log.Fatal("Room ID is required")
	}

	for {
		conn, wsMu, err := connectWebSocket("offerer", *roomID)
		if err != nil {
			log.Printf("Failed to connect/register to signaling server: %v. Retrying in 5s...", err)
			time.Sleep(5 * time.Second)
			continue
		}

		if err := runWithSignaling(conn, wsMu); err != nil {
			log.Printf("runWithSignaling ended: %v", err)
		}

		// ensure socket closed before next attempt
		conn.Close()
		log.Println("Re-establishing signaling connection in 3s...")
		time.Sleep(3 * time.Second)
	}
}

func runWithSignaling(c *websocket.Conn, wsWriteMu *sync.Mutex) error {
	var (
		hasConnected     bool // Track if we ever established a connection
		needsDisconnect  bool // Track if we need to send disconnect on exit
		connectionFailed bool
	)

	// glare flag and restart management
	var (
		makingOffer       bool
		restartInProgress bool // Prevent multiple simultaneous restart attempts
	)

	// Helper function to send disconnect message
	sendDisconnect := func() {
		if !needsDisconnect {
			return // Don't run twice
		}
		// We used to notify the signaling server with a special "disconnect" message, but that
		// causes the server to close our WebSocket which breaks the automatic reconnection loop.
		// Instead we simply mark the flag as processed and keep the signaling socket open so we
		// can wait for the next "ready" message and restart the WebRTC handshake without having
		// to recreate the WebSocket.
		log.Println("Local PeerConnection closed; keeping signaling WebSocket open for reconnection")
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

	se := webrtc.SettingEngine{}
	se.SetICETimeouts(10*time.Second, 15*time.Second, 2*time.Second) // disconnected, failed, keepalive ticks
	api := webrtc.NewAPI(webrtc.WithSettingEngine(se))
	peerConnection, err := api.NewPeerConnection(config)
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

	// defer registering OnClose until triggerFatal is defined

	reconnectChan := make(chan struct{})
	var closeOnce sync.Once

	// notify answerer once per failure
	var notifyOnce sync.Once
	sendPeerDown := func() {
		notifyOnce.Do(func() {
			pd := signaling.Message{Type: "peer_down", Target: "answerer"}
			wsWriteMu.Lock()
			_ = c.WriteJSON(pd)
			wsWriteMu.Unlock()
			log.Println("Sent peer_down to answerer")
		})
	}

	// Placeholder: restartICE defined later, after triggerFatal
	var restartICE func()

	// Helper to trigger a connection reset and return from runWithSignaling
	triggerFatal := func(reason string) {
		log.Printf("PeerConnection reset needed: %s", reason)
		closeOnce.Do(func() {
			if hasConnected {
				needsDisconnect = true // Mark that we need to send disconnect on exit
			}
			close(reconnectChan)
			connectionFailed = true
			// keep signalling WebSocket alive; just notify peer
			sendPeerDown()
		})
	}

	// Reset the notifyOnce when a new WebSocket/PC is up
	resetNotifiers := func() {
		notifyOnce = sync.Once{}
	}

	// restartICE performs an ICE restart and signals a fresh offer to the answerer.
	restartICE = func() {
		// Prevent multiple simultaneous restart attempts
		if restartInProgress || connectionFailed {
			log.Println("ICE restart already in progress or connection failed, skipping")
			return
		}

		// Check if PeerConnection is in a valid state for restart
		signalingState := peerConnection.SignalingState()
		if signalingState != webrtc.SignalingStateStable {
			log.Printf("Cannot restart ICE in signaling state %s, triggering full reset", signalingState)
			triggerFatal("Invalid signaling state for ICE restart")
			return
		}

		restartInProgress = true
		defer func() { restartInProgress = false }()

		retry := 0

		retryOffer := func() bool {
			log.Println("Attempting ICE restart (CreateOffer with ICERestart)")
			makingOffer = true
			offer, err := peerConnection.CreateOffer(&webrtc.OfferOptions{ICERestart: true})
			if err != nil {
				log.Printf("CreateOffer (restart) failed: %v", err)
				makingOffer = false
				return false
			}
			if err = peerConnection.SetLocalDescription(offer); err != nil {
				log.Printf("SetLocalDescription (restart) failed: %v", err)
				makingOffer = false
				return false
			}
			wsWriteMu.Lock()
			err = c.WriteJSON(signaling.Message{Type: "offer", Target: "answerer", SDP: offer.SDP})
			wsWriteMu.Unlock()
			if err != nil {
				log.Printf("Failed to send restart offer: %v", err)
				makingOffer = false
				return false
			}
			log.Println("ICE restart offer sent")
			makingOffer = false
			return true
		}

		if !retryOffer() {
			triggerFatal("Restart offer send failed")
			return
		}

		for retry < 2 && !connectionFailed {
			time.Sleep(8 * time.Second)

			// Check if connection was reset during sleep
			if connectionFailed {
				log.Println("Connection was reset during ICE restart, stopping retries")
				return
			}

			if peerConnection.ConnectionState() == webrtc.PeerConnectionStateConnected {
				log.Println("ICE restart succeeded")
				return
			}
			retry++
			log.Printf("ICE still not connected after retry %d, resending offer", retry)
			if !retryOffer() {
				break
			}
		}

		// give up
		if !connectionFailed {
			log.Println("ICE restart retries exhausted")
			triggerFatal("ICE restart retries exhausted")
		}
	}

	// Now that triggerFatal is defined, register the OnClose handler
	dataChannel.OnClose(func() {
		log.Println("Data channel closed, triggering reset")
		triggerFatal("Data channel closed")
	})

	var lastPongTime time.Time
	peerConnection.OnConnectionStateChange(func(s webrtc.PeerConnectionState) {
		log.Printf("Connection state: %s", s.String())
		switch s {
		case webrtc.PeerConnectionStateConnected:
			log.Println("WebRTC connected")
			hasConnected = true
			needsDisconnect = true // We'll need to send disconnect if this connection ends
			resetNotifiers()
			// Reset pong timer when connection is established/restored
			lastPongTime = time.Now()
		case webrtc.PeerConnectionStateDisconnected:
			log.Println("WebRTC disconnected – initiating ICE restart")
			go restartICE()
			// Start a short timer; if still disconnected, fall back to full reset
			go func() {
				time.Sleep(4 * time.Second)
				if peerConnection.ConnectionState() == webrtc.PeerConnectionStateDisconnected && !connectionFailed {
					log.Println("Connection still disconnected after restart, triggering reset")
					triggerFatal("Connection recovery timeout")
				}
			}()
		case webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed:
			log.Printf("WebRTC state: %s", s.String())
			triggerFatal("Connection state: " + s.String())
		}
	})
	peerConnection.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		log.Printf("ICE state: %s", state.String())
		switch state {
		case webrtc.ICEConnectionStateDisconnected:
			// Let the connection state handler manage ICE restart to avoid race conditions
			log.Println("ICE disconnected – connection state handler will manage restart")
		case webrtc.ICEConnectionStateFailed:
			log.Println("ICE failed, triggering reset")
			triggerFatal("ICE connection failed")
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
	dataChannel.OnOpen(func() {
		log.Println("Data channel opened")
		lastPongTime = time.Now() // Initialize on connection
		go func() {
			ticker := time.NewTicker(5 * time.Second) // Less frequent keepalive for performance
			defer ticker.Stop()
			for {
				select {
				case <-ticker.C:
					if dataChannel.ReadyState() == webrtc.DataChannelStateOpen {
						// Skip timeout check if the PC is not yet in Connected state (during ICE restart)
						if peerConnection.ConnectionState() != webrtc.PeerConnectionStateConnected {
							continue
						}
						// Allow a longer grace period (30s) for better stability
						if time.Since(lastPongTime) > 30*time.Second {
							log.Println("Connection timeout detected, triggering reset")
							triggerFatal("Pong timeout")
							return
						}
						// Only log if approaching timeout (last 10 seconds)
						timeSinceLastPong := time.Since(lastPongTime)
						if timeSinceLastPong > 20*time.Second {
							log.Printf("Connection health check: %v since last response", timeSinceLastPong)
						}
						if err := dataChannel.Send([]byte("ping")); err != nil {
							log.Printf("Keepalive failed: %v", err)
							triggerFatal("Keepalive failed")
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
			// Silent ping response for performance
			if err := dataChannel.Send([]byte("pong")); err != nil {
				log.Printf("Error sending pong: %v", err)
			}
			return
		}
		if string(msg.Data) == "pong" {
			// Silent pong reception, just update timer
			lastPongTime = time.Now()
			return
		}
		if _, err := tap.Write(msg.Data); err != nil {
			log.Printf("Error writing to TAP: %v", err)
		}
	})

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

	// Start a goroutine to monitor connectionFailed and close WebSocket if needed
	go func() {
		ticker := time.NewTicker(1 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			if connectionFailed {
				log.Println("Connection failed detected, closing WebSocket to break read loop")
				c.Close()
				return
			}
		}
	}()

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
		case "peer_down":
			log.Println("Signaling: remote peer went offline – resetting connection")
			triggerFatal("peer_down from signaling server")
			continue
		case "offer":
			if makingOffer || peerConnection.SignalingState() != webrtc.SignalingStateStable {
				log.Println("Impolite offerer received glare offer – ignoring")
				continue
			}
			// If stable (should not happen in normal flow), treat as reset from server
			log.Println("Unexpected remote offer while stable; rolling over to restart")
			// Accept it politely to recover
			if err := peerConnection.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: msg.SDP}); err != nil {
				log.Printf("SetRemote unexpected offer failed: %v", err)
				continue
			}
			answer, err := peerConnection.CreateAnswer(nil)
			if err != nil {
				log.Printf("CreateAnswer to unexpected offer failed: %v", err)
				continue
			}
			if err = peerConnection.SetLocalDescription(answer); err != nil {
				log.Printf("SetLocalDescription unexpected answer failed: %v", err)
				continue
			}
			wsWriteMu.Lock()
			_ = c.WriteJSON(signaling.Message{Type: "answer", Target: "answerer", SDP: answer.SDP})
			wsWriteMu.Unlock()
			continue
		}
	}
}
