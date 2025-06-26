package main

import "github.com/pion/webrtc/v3"

// SignalingMessage represents a message exchanged through the signaling server
type SignalingMessage struct {
	Type      string                   `json:"type"`
	Target    string                   `json:"target,omitempty"`
	Role      string                   `json:"role,omitempty"`
	Room      string                   `json:"room,omitempty"`
	SDP       string                   `json:"sdp,omitempty"`
	Candidate *webrtc.ICECandidateInit `json:"candidate,omitempty"`
}
