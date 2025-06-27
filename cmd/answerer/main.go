package main

import (
	"crypto/tls"
	"flag"
	"fmt"
	"io"
	"log"
	"net/url"
	"os"
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

	for {
		if err := run(*roomID); err != nil {
			log.Printf("Error in run: %v. Reconnecting in 3 seconds...", err)
			time.Sleep(3 * time.Second)
			continue
		}
	}
}

func run(roomID string) error {
	// Create WebSocket connection
	u := url.URL{Scheme: "wss", Host: "webrtc-vpn.mnv-dev.site"}
	log.Printf("Connecting to %s", u.String())

	dialer := websocket.Dialer{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true},
	}

	c, _, err := dialer.Dial(u.String(), nil)
	if err != nil {
		return err
	}
	defer c.Close()

	// Register as answerer
	registerMsg := signaling.Message{
		Type: "register",
		Role: "answerer",
		Room: roomID,
	}
	if err := c.WriteJSON(registerMsg); err != nil {
		return err
	}

	// Create a new RTCPeerConnection
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

	// Open the existing TAP interface
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

	// Handle connection state changes
	reconnectChan := make(chan struct{})
	var closeOnce sync.Once
	var connectionFailed bool
	peerConnection.OnConnectionStateChange(func(s webrtc.PeerConnectionState) {
		log.Printf("Connection state changed to: %s\n", s.String())
		switch s {
		case webrtc.PeerConnectionStateConnected:
			log.Println("WebRTC state: connected (ICE completed, DTLS connected)")
		case webrtc.PeerConnectionStateDisconnected, webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed:
			log.Printf("WebRTC state: %s (ICE disconnected/failed/closed)\n", s.String())
			closeOnce.Do(func() {
				close(reconnectChan)
				connectionFailed = true
			})
		}
	})

	// Handle ICE connection state changes
	peerConnection.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		log.Printf("ICE connection state changed to: %s\n", state.String())
	})

	// Handle ICE candidates
	peerConnection.OnICECandidate(func(ice *webrtc.ICECandidate) {
		if ice != nil {
			iceJSON := ice.ToJSON()
			candidateMsg := signaling.Message{
				Type:      "candidate",
				Target:    "offerer",
				Candidate: &iceJSON,
			}
			if err := c.WriteJSON(candidateMsg); err != nil {
				log.Printf("Error sending ICE candidate: %v", err)
			}
		}
	})

	// Handle data channel
	peerConnection.OnDataChannel(func(d *webrtc.DataChannel) {
		log.Printf("New DataChannel %s %d\n", d.Label(), d.ID())

		d.OnOpen(func() {
			log.Printf("Data channel '%s' opened\n", d.Label())

			// Start keepalive mechanism
			go func() {
				ticker := time.NewTicker(10 * time.Second)
				defer ticker.Stop()

				for {
					select {
					case <-ticker.C:
						if d.ReadyState() == webrtc.DataChannelStateOpen {
							if err := d.Send([]byte("ping")); err != nil {
								log.Printf("Error sending keepalive: %v", err)
								return
							}
							log.Println("Sent keepalive ping")
						}
					case <-reconnectChan:
						return
					}
				}
			}()

			// Start reading from TAP interface
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

		// Handle incoming data
		d.OnMessage(func(msg webrtc.DataChannelMessage) {
			if string(msg.Data) == "ping" {
				log.Println("Received keepalive ping")
				return
			}
			if _, err := tap.Write(msg.Data); err != nil {
				log.Printf("Error writing to TAP: %v", err)
			}
		})
	})

	// Handle incoming messages
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
			if err := c.WriteJSON(answerMsg); err != nil {
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
