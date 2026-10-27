package services

import (
	"context"
	"sync"
	"time"
)

type RaisedHand struct {
	UserID         string    `json:"user_id"`
	UserName       string    `json:"user_name"`
	RaisedAt       time.Time `json:"raised_at"`
	AcknowledgedAt *time.Time `json:"acknowledged_at,omitempty"`
	AcknowledgedBy string    `json:"acknowledged_by,omitempty"`
}

type CallFeaturesService struct {
	roomService  *RoomService
	raisedHands  map[string][]*RaisedHand // roomName -> hands
	lockedRooms  map[string]bool
	mu           sync.RWMutex
}

func NewCallFeaturesService(roomService *RoomService) *CallFeaturesService {
	return &CallFeaturesService{
		roomService:  roomService,
		raisedHands:  make(map[string][]*RaisedHand),
		lockedRooms:  make(map[string]bool),
	}
}

func (s *CallFeaturesService) RaiseHand(ctx context.Context, roomName, userID, userName string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	hands := s.raisedHands[roomName]
	for _, h := range hands {
		if h.UserID == userID {
			return nil // Already raised
		}
	}

	s.raisedHands[roomName] = append(hands, &RaisedHand{
		UserID:   userID,
		UserName: userName,
		RaisedAt: time.Now(),
	})

	return nil
}

func (s *CallFeaturesService) LowerHand(ctx context.Context, roomName, userID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	hands := s.raisedHands[roomName]
	for i, h := range hands {
		if h.UserID == userID {
			s.raisedHands[roomName] = append(hands[:i], hands[i+1:]...)
			return nil
		}
	}

	return nil
}

func (s *CallFeaturesService) AcknowledgeHand(ctx context.Context, roomName, userID, moderatorID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	hands := s.raisedHands[roomName]
	for _, h := range hands {
		if h.UserID == userID {
			now := time.Now()
			h.AcknowledgedAt = &now
			h.AcknowledgedBy = moderatorID
			return nil
		}
	}

	return nil
}

func (s *CallFeaturesService) GetRaisedHands(ctx context.Context, roomName string) ([]*RaisedHand, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	hands := s.raisedHands[roomName]
	if hands == nil {
		return []*RaisedHand{}, nil
	}

	result := make([]*RaisedHand, len(hands))
	copy(result, hands)
	return result, nil
}

func (s *CallFeaturesService) ClearRaisedHands(ctx context.Context, roomName string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	delete(s.raisedHands, roomName)
	return nil
}

func (s *CallFeaturesService) MuteParticipant(ctx context.Context, roomName, userID, moderatorID string) error {
	return s.roomService.MuteParticipant(ctx, roomName, userID, true)
}

func (s *CallFeaturesService) UnmuteParticipant(ctx context.Context, roomName, userID, moderatorID string) error {
	return s.roomService.MuteParticipant(ctx, roomName, userID, false)
}

func (s *CallFeaturesService) MuteAll(ctx context.Context, roomName, moderatorID string) error {
	participants, err := s.roomService.ListParticipants(ctx, roomName)
	if err != nil {
		return err
	}

	for _, p := range participants {
		if p.Identity != moderatorID {
			_ = s.roomService.MuteParticipant(ctx, roomName, p.Identity, true)
		}
	}

	return nil
}

func (s *CallFeaturesService) KickParticipant(ctx context.Context, roomName, userID, adminID string) error {
	// Best effort: a failure to lower the hand must not block the kick.
	_ = s.LowerHand(ctx, roomName, userID)
	return s.roomService.KickParticipant(ctx, roomName, userID)
}

func (s *CallFeaturesService) LockRoom(ctx context.Context, roomName, adminID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.lockedRooms[roomName] = true
	return nil
}

func (s *CallFeaturesService) UnlockRoom(ctx context.Context, roomName, adminID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	delete(s.lockedRooms, roomName)
	return nil
}

func (s *CallFeaturesService) IsRoomLocked(ctx context.Context, roomName string) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()

	return s.lockedRooms[roomName]
}
