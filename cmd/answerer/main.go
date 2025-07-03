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

	dialer := websocket.Dialer{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}
	c, _, err := dialer.Dial(u.String(), nil)
	if err != nil {
		return nil, nil, err
	}

	wsMu := &sync.Mutex{}
	reg := signaling.Message{Type: "register", Role: role, Room: room}
	wsMu.Lock()
	err = c.WriteJSON(reg)
	wsMu.Unlock()
	if err != nil {
		c.Close()
		return nil, nil, err
	}
	return c, wsMu, nil
}

func main() {
	roomID := flag.String("room", "", "Room ID for signaling")
	flag.Parse()
	if *roomID == "" {
		log.Fatal("Room ID is required")
	}

	for {
		conn, wsMu, err := connectWebSocket("answerer", *roomID)
		if err != nil {
			log.Printf("Failed to connect/register to signaling server: %v. Retrying in 5s...", err)
			time.Sleep(5 * time.Second)
			continue
		}

		if err := runWithSignaling(conn, wsMu); err != nil {
			log.Printf("Error in run: %v", err)
		}
		conn.Close()
		log.Println("Re-establishing signaling connection in 3s...")
		time.Sleep(3 * time.Second)
	}
}

func runWithSignaling(c *websocket.Conn, wsWriteMu *sync.Mutex) error {
	var needsDisconnect bool // Track if we need to send disconnect on exit

	// Helper function to send disconnect message
	sendDisconnect := func() {
		if !needsDisconnect {
			return // Don't run twice
		}
		// Keep the signaling WebSocket alive so the offerer can send a fresh offer when it
		// reconnects.  Simply clear the flag and log the event instead of notifying the
		// server with a special "disconnect" packet (which would close the socket).
		log.Println("Local PeerConnection closed; keeping signaling WebSocket open for reconnection")
		needsDisconnect = false
	}

	defer sendDisconnect() // Will be called on both normal exit and panic

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
	defer peerConnection.Close()

	tunConfig := water.Config{
		DeviceType: water.TAP,
		PlatformSpecificParams: water.PlatformSpecificParams{
			Name:    "revpn-answer_",
			Persist: true,
		},
	}

	log.Println("Opening existing TAP interface 'revpn-answer_...")
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

	var currentDataChannel *webrtc.DataChannel
	var connectionFailed bool

	// Channel to signal keep-alive goroutine to stop. We recreate it each time the
	// PeerConnection transitions back to Connected so the monitor can restart
	// cleanly after a temporary ICE outage.
	var (
		stopKeepalive     chan struct{}
		stopKeepaliveOnce sync.Once
	)

	createKeepalive := func() {
		stopKeepalive = make(chan struct{})
		stopKeepaliveOnce = sync.Once{}
	}
	createKeepalive()
	defer stopKeepaliveOnce.Do(func() { close(stopKeepalive) })

	// Variables for connection monitoring
	var (
		lastPingMu   sync.Mutex
		lastPingTime = time.Now()
	)

	// Function to monitor data channel state
	startKeepalive := func(d *webrtc.DataChannel, stopCh <-chan struct{}) {
		log.Println("Starting connection monitor")
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-ticker.C:
				if d.ReadyState() == webrtc.DataChannelStateOpen {
					lastPingMu.Lock()
					timeSinceLastPing := time.Since(lastPingTime)
					lastPingMu.Unlock()

					log.Printf("DataChannel state: %s, Time since last ping: %v", d.ReadyState(), timeSinceLastPing)
					// We no longer close the connection on missing ping; rely on ICE state
					// and signaling presence to detect failures. The log remains useful.
				}
			case <-stopCh:
				log.Println("Stopping connection monitor")
				return
			}
		}
	}

	peerConnection.OnConnectionStateChange(func(s webrtc.PeerConnectionState) {
		log.Printf("Connection state changed to: %s\n", s.String())
		switch s {
		case webrtc.PeerConnectionStateConnected:
			log.Println("WebRTC state: connected (ICE completed, DTLS connected)")
			needsDisconnect = true // We'll need to send disconnect if this connection ends
			// Reset ping timer so the new monitor has a full grace period.
			lastPingMu.Lock()
			lastPingTime = time.Now()
			lastPingMu.Unlock()
			// Recreate keep-alive control channel so a fresh monitor can run after a reconnect.
			createKeepalive()
			if currentDataChannel != nil && currentDataChannel.ReadyState() == webrtc.DataChannelStateOpen {
				go startKeepalive(currentDataChannel, stopKeepalive)
			}
		case webrtc.PeerConnectionStateDisconnected, webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed:
			log.Printf("WebRTC state: %s (ICE disconnected/failed/closed)\n", s.String())
			stopKeepaliveOnce.Do(func() { close(stopKeepalive) }) // Stop keepalive routine (once)
		}
	})
	peerConnection.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		log.Printf("ICE connection state changed to: %s\n", state.String())
		// Only log and let the offerer handle ICE restart. Do not send offers from the answerer!
	})
	peerConnection.OnICECandidate(func(ice *webrtc.ICECandidate) {
		if ice != nil {
			iceJSON := ice.ToJSON()
			candidateMsg := signaling.Message{
				Type:      "candidate",
				Target:    "offerer",
				Candidate: &iceJSON,
			}
			wsWriteMu.Lock()
			if err := c.WriteJSON(candidateMsg); err != nil {
				log.Printf("Error sending ICE candidate: %v", err)
			}
			wsWriteMu.Unlock()
		}
	})
	peerConnection.OnDataChannel(func(d *webrtc.DataChannel) {
		log.Printf("New DataChannel %s %d\n", d.Label(), d.ID())
		currentDataChannel = d
		d.OnOpen(func() {
			log.Printf("Data channel '%s' opened\n", d.Label())
			lastPingMu.Lock()
			lastPingTime = time.Now()
			lastPingMu.Unlock()

			go startKeepalive(d, stopKeepalive)

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
					if err := d.Send(buffer[:n]); err != nil {
						log.Printf("Error sending data: %v", err)
						return
					}
				}
			}()
		})
		d.OnMessage(func(msg webrtc.DataChannelMessage) {
			if string(msg.Data) == "ping" {
				lastPingMu.Lock()
				lastPingTime = time.Now() // Update last ping time
				lastPingMu.Unlock()
				log.Println("Received keepalive ping, sending pong")
				if err := d.Send([]byte("pong")); err != nil {
					log.Printf("Error sending pong: %v", err)
				}
				return
			}
			if _, err := tap.Write(msg.Data); err != nil {
				log.Printf("Error writing to TAP: %v", err)
			}
		})
	})

	// No change needed here: answerer is always ready to accept a new offer after restart
	for {
		if connectionFailed {
			return fmt.Errorf("connection failed, triggering reconnect")
		}
		var msg signaling.Message
		if err := c.ReadJSON(&msg); err != nil {
			return err
		}
		switch msg.Type {
		case "offer":
			offer := webrtc.SessionDescription{
				Type: webrtc.SDPTypeOffer,
				SDP:  msg.SDP,
			}
			if err := peerConnection.SetRemoteDescription(offer); err != nil {
				return err
			}
			answer, err := peerConnection.CreateAnswer(nil)
			if err != nil {
				return err
			}
			if err = peerConnection.SetLocalDescription(answer); err != nil {
				return err
			}
			answerMsg := signaling.Message{
				Type:   "answer",
				Target: "offerer",
				SDP:    answer.SDP,
			}
			wsWriteMu.Lock()
			if err := c.WriteJSON(answerMsg); err != nil {
				wsWriteMu.Unlock()
				return err
			}
			wsWriteMu.Unlock()
		case "candidate":
			if msg.Candidate != nil {
				if err := peerConnection.AddICECandidate(*msg.Candidate); err != nil {
					log.Printf("Error adding ICE candidate: %v", err)
				}
			}
		case "peer_down":
			log.Println("Signaling: remote peer went offline – resetting connection")
			peerConnection.Close()
			connectionFailed = true
			stopKeepaliveOnce.Do(func() { close(stopKeepalive) })
			continue
		}
	}

}
