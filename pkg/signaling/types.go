package signaling

import "github.com/pion/webrtc/v3"

// Message represents a message exchanged through the signaling server
type Message struct {
	Type      string                   `json:"type"`
	Target    string                   `json:"target,omitempty"`
	Role      string                   `json:"role,omitempty"`
	Room      string                   `json:"room,omitempty"`
	SDP       string                   `json:"sdp,omitempty"`
	Candidate *webrtc.ICECandidateInit `json:"candidate,omitempty"`
}
