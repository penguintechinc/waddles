package services

import (
	"context"
	"fmt"
	"time"

	"github.com/livekit/protocol/auth"
	"github.com/livekit/protocol/livekit"
	lksdk "github.com/livekit/server-sdk-go/v2"
)

type RoomService struct {
	client    *lksdk.RoomServiceClient
	apiKey    string
	apiSecret string
	host      string
}

type RoomInfo struct {
	RoomID       string    `json:"room_id"`
	RoomName     string    `json:"room_name"`
	CommunityID  int       `json:"community_id"`
	Participants int       `json:"participants"`
	CreatedAt    time.Time `json:"created_at"`
	IsLocked     bool      `json:"is_locked"`
}

type ParticipantInfo struct {
	UserID   string `json:"user_id"`
	Identity string `json:"identity"`
	Role     string `json:"role"`
	JoinedAt int64  `json:"joined_at"`
	IsMuted  bool   `json:"is_muted"`
}

type JoinToken struct {
	Token    string `json:"token"`
	RoomName string `json:"room_name"`
	Identity string `json:"identity"`
}

func NewRoomService(host, apiKey, apiSecret string) *RoomService {
	client := lksdk.NewRoomServiceClient(host, apiKey, apiSecret)
	return &RoomService{
		client:    client,
		apiKey:    apiKey,
		apiSecret: apiSecret,
		host:      host,
	}
}

func (s *RoomService) CreateRoom(ctx context.Context, communityID int, roomName string, maxParticipants uint32) (*RoomInfo, error) {
	fullRoomName := fmt.Sprintf("community_%d_%s", communityID, roomName)

	room, err := s.client.CreateRoom(ctx, &livekit.CreateRoomRequest{
		Name:            fullRoomName,
		MaxParticipants: maxParticipants,
		EmptyTimeout:    300,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to create room: %w", err)
	}

	return &RoomInfo{
		RoomID:       room.Sid,
		RoomName:     room.Name,
		CommunityID:  communityID,
		Participants: 0,
		CreatedAt:    time.Now(),
		IsLocked:     false,
	}, nil
}

func (s *RoomService) JoinRoom(ctx context.Context, roomName, userID, userName, role string) (*JoinToken, error) {
	at := auth.NewAccessToken(s.apiKey, s.apiSecret)

	canPublish := role == "host" || role == "moderator" || role == "speaker"
	canSubscribe := true
	canPublishData := role == "host" || role == "moderator"

	grant := &auth.VideoGrant{
		RoomJoin:       true,
		Room:           roomName,
		CanPublish:     &canPublish,
		CanSubscribe:   &canSubscribe,
		CanPublishData: &canPublishData,
	}

	at.SetVideoGrant(grant).
		SetIdentity(userID).
		SetName(userName).
		SetValidFor(24 * time.Hour).
		SetMetadata(fmt.Sprintf(`{"role":"%s"}`, role))

	token, err := at.ToJWT()
	if err != nil {
		return nil, fmt.Errorf("failed to generate token: %w", err)
	}

	return &JoinToken{
		Token:    token,
		RoomName: roomName,
		Identity: userID,
	}, nil
}

func (s *RoomService) LeaveRoom(ctx context.Context, roomName, userID string) error {
	_, err := s.client.RemoveParticipant(ctx, &livekit.RoomParticipantIdentity{
		Room:     roomName,
		Identity: userID,
	})
	return err
}

func (s *RoomService) ListParticipants(ctx context.Context, roomName string) ([]*ParticipantInfo, error) {
	resp, err := s.client.ListParticipants(ctx, &livekit.ListParticipantsRequest{
		Room: roomName,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to list participants: %w", err)
	}

	participants := make([]*ParticipantInfo, 0, len(resp.Participants))
	for _, p := range resp.Participants {
		participants = append(participants, &ParticipantInfo{
			UserID:   p.Sid,
			Identity: p.Identity,
			Role:     "viewer",
			JoinedAt: p.JoinedAt,
			IsMuted:  !p.Permission.CanPublish,
		})
	}

	return participants, nil
}

func (s *RoomService) GetRoomInfo(ctx context.Context, roomName string) (*RoomInfo, error) {
	rooms, err := s.client.ListRooms(ctx, &livekit.ListRoomsRequest{
		Names: []string{roomName},
	})
	if err != nil {
		return nil, fmt.Errorf("failed to get room info: %w", err)
	}

	if len(rooms.Rooms) == 0 {
		return nil, fmt.Errorf("room not found")
	}

	room := rooms.Rooms[0]
	return &RoomInfo{
		RoomID:       room.Sid,
		RoomName:     room.Name,
		Participants: int(room.NumParticipants),
		CreatedAt:    time.Unix(room.CreationTime, 0),
	}, nil
}

func (s *RoomService) DeleteRoom(ctx context.Context, roomName string) error {
	_, err := s.client.DeleteRoom(ctx, &livekit.DeleteRoomRequest{
		Room: roomName,
	})
	return err
}

func (s *RoomService) MuteParticipant(ctx context.Context, roomName, userID string, muted bool) error {
	_, err := s.client.UpdateParticipant(ctx, &livekit.UpdateParticipantRequest{
		Room:     roomName,
		Identity: userID,
		Permission: &livekit.ParticipantPermission{
			CanPublish:   !muted,
			CanSubscribe: true,
		},
	})
	return err
}

func (s *RoomService) KickParticipant(ctx context.Context, roomName, userID string) error {
	_, err := s.client.RemoveParticipant(ctx, &livekit.RoomParticipantIdentity{
		Room:     roomName,
		Identity: userID,
	})
	return err
}
