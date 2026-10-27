package api

import (
	"encoding/json"
	"log"
	"net/http"

	"github.com/gorilla/mux"
	"github.com/penguintech/waddlebot/module_rtc/internal/services"
)

type Handlers struct {
	roomService     *services.RoomService
	featuresService *services.CallFeaturesService
}

func NewHandlers(roomService *services.RoomService, featuresService *services.CallFeaturesService) *Handlers {
	return &Handlers{
		roomService:     roomService,
		featuresService: featuresService,
	}
}

func (h *Handlers) RegisterRoutes(r *mux.Router) {
	api := r.PathPrefix("/api/v1").Subrouter()

	api.HandleFunc("/rooms", h.CreateRoom).Methods("POST")
	api.HandleFunc("/rooms/{roomName}", h.GetRoom).Methods("GET")
	api.HandleFunc("/rooms/{roomName}", h.DeleteRoom).Methods("DELETE")
	api.HandleFunc("/rooms/{roomName}/join", h.JoinRoom).Methods("POST")
	api.HandleFunc("/rooms/{roomName}/leave", h.LeaveRoom).Methods("POST")
	api.HandleFunc("/rooms/{roomName}/participants", h.ListParticipants).Methods("GET")

	api.HandleFunc("/rooms/{roomName}/raise-hand", h.RaiseHand).Methods("POST")
	api.HandleFunc("/rooms/{roomName}/lower-hand", h.LowerHand).Methods("POST")
	api.HandleFunc("/rooms/{roomName}/raised-hands", h.GetRaisedHands).Methods("GET")
	api.HandleFunc("/rooms/{roomName}/acknowledge-hand/{userId}", h.AcknowledgeHand).Methods("POST")

	api.HandleFunc("/rooms/{roomName}/mute/{userId}", h.MuteParticipant).Methods("POST")
	api.HandleFunc("/rooms/{roomName}/unmute/{userId}", h.UnmuteParticipant).Methods("POST")
	api.HandleFunc("/rooms/{roomName}/mute-all", h.MuteAll).Methods("POST")
	api.HandleFunc("/rooms/{roomName}/kick/{userId}", h.KickParticipant).Methods("POST")

	api.HandleFunc("/rooms/{roomName}/lock", h.LockRoom).Methods("POST")
	api.HandleFunc("/rooms/{roomName}/unlock", h.UnlockRoom).Methods("POST")
}

type CreateRoomRequest struct {
	CommunityID     int    `json:"community_id"`
	RoomName        string `json:"room_name"`
	MaxParticipants uint32 `json:"max_participants"`
}

type JoinRoomRequest struct {
	UserID   string `json:"user_id"`
	UserName string `json:"user_name"`
	Role     string `json:"role"`
}

type RaiseHandRequest struct {
	UserID   string `json:"user_id"`
	UserName string `json:"user_name"`
}

type ModeratorRequest struct {
	ModeratorID string `json:"moderator_id"`
}

func (h *Handlers) CreateRoom(w http.ResponseWriter, r *http.Request) {
	var req CreateRoomRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonError(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	if req.MaxParticipants == 0 {
		req.MaxParticipants = 100
	}

	room, err := h.roomService.CreateRoom(r.Context(), req.CommunityID, req.RoomName, req.MaxParticipants)
	if err != nil {
		log.Printf("Failed to create room: %v", err)
		jsonError(w, "Failed to create room", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, room, http.StatusCreated)
}

func (h *Handlers) GetRoom(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	room, err := h.roomService.GetRoomInfo(r.Context(), roomName)
	if err != nil {
		jsonError(w, "Room not found", http.StatusNotFound)
		return
	}

	jsonResponse(w, room, http.StatusOK)
}

func (h *Handlers) DeleteRoom(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	if err := h.roomService.DeleteRoom(r.Context(), roomName); err != nil {
		jsonError(w, "Failed to delete room", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func (h *Handlers) JoinRoom(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	if h.featuresService.IsRoomLocked(r.Context(), roomName) {
		jsonError(w, "Room is locked", http.StatusForbidden)
		return
	}

	var req JoinRoomRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonError(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	if req.Role == "" {
		req.Role = "viewer"
	}

	token, err := h.roomService.JoinRoom(r.Context(), roomName, req.UserID, req.UserName, req.Role)
	if err != nil {
		log.Printf("Failed to join room: %v", err)
		jsonError(w, "Failed to join room", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, token, http.StatusOK)
}

func (h *Handlers) LeaveRoom(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	var req struct {
		UserID string `json:"user_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonError(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	if err := h.featuresService.LowerHand(r.Context(), roomName, req.UserID); err != nil {
		log.Printf("Failed to lower hand before leaving room: %v", err)
	}

	if err := h.roomService.LeaveRoom(r.Context(), roomName, req.UserID); err != nil {
		log.Printf("Failed to leave room: %v", err)
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func (h *Handlers) ListParticipants(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	participants, err := h.roomService.ListParticipants(r.Context(), roomName)
	if err != nil {
		jsonError(w, "Failed to list participants", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]interface{}{
		"participants": participants,
		"count":        len(participants),
	}, http.StatusOK)
}

func (h *Handlers) RaiseHand(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	var req RaiseHandRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonError(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	if err := h.featuresService.RaiseHand(r.Context(), roomName, req.UserID, req.UserName); err != nil {
		jsonError(w, "Failed to raise hand", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func (h *Handlers) LowerHand(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	var req struct {
		UserID string `json:"user_id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonError(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	if err := h.featuresService.LowerHand(r.Context(), roomName, req.UserID); err != nil {
		jsonError(w, "Failed to lower hand", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func (h *Handlers) GetRaisedHands(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	hands, err := h.featuresService.GetRaisedHands(r.Context(), roomName)
	if err != nil {
		jsonError(w, "Failed to get raised hands", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]interface{}{
		"raised_hands": hands,
		"count":        len(hands),
	}, http.StatusOK)
}

func (h *Handlers) AcknowledgeHand(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	roomName := vars["roomName"]
	userID := vars["userId"]

	var req ModeratorRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		jsonError(w, "Invalid request body", http.StatusBadRequest)
		return
	}

	if err := h.featuresService.AcknowledgeHand(r.Context(), roomName, userID, req.ModeratorID); err != nil {
		jsonError(w, "Failed to acknowledge hand", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func (h *Handlers) MuteParticipant(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	roomName := vars["roomName"]
	userID := vars["userId"]

	var req ModeratorRequest
	// Body is optional for moderator actions; an empty/invalid body leaves req zero-valued.
	_ = json.NewDecoder(r.Body).Decode(&req)

	if err := h.featuresService.MuteParticipant(r.Context(), roomName, userID, req.ModeratorID); err != nil {
		jsonError(w, "Failed to mute participant", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func (h *Handlers) UnmuteParticipant(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	roomName := vars["roomName"]
	userID := vars["userId"]

	var req ModeratorRequest
	// Body is optional for moderator actions; an empty/invalid body leaves req zero-valued.
	_ = json.NewDecoder(r.Body).Decode(&req)

	if err := h.featuresService.UnmuteParticipant(r.Context(), roomName, userID, req.ModeratorID); err != nil {
		jsonError(w, "Failed to unmute participant", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func (h *Handlers) MuteAll(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	var req ModeratorRequest
	// Body is optional for moderator actions; an empty/invalid body leaves req zero-valued.
	_ = json.NewDecoder(r.Body).Decode(&req)

	if err := h.featuresService.MuteAll(r.Context(), roomName, req.ModeratorID); err != nil {
		jsonError(w, "Failed to mute all", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func (h *Handlers) KickParticipant(w http.ResponseWriter, r *http.Request) {
	vars := mux.Vars(r)
	roomName := vars["roomName"]
	userID := vars["userId"]

	var req struct {
		AdminID string `json:"admin_id"`
	}
	// Body is optional; an empty/invalid body leaves req zero-valued.
	_ = json.NewDecoder(r.Body).Decode(&req)

	if err := h.featuresService.KickParticipant(r.Context(), roomName, userID, req.AdminID); err != nil {
		jsonError(w, "Failed to kick participant", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func (h *Handlers) LockRoom(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	var req struct {
		AdminID string `json:"admin_id"`
	}
	// Body is optional; an empty/invalid body leaves req zero-valued.
	_ = json.NewDecoder(r.Body).Decode(&req)

	if err := h.featuresService.LockRoom(r.Context(), roomName, req.AdminID); err != nil {
		jsonError(w, "Failed to lock room", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func (h *Handlers) UnlockRoom(w http.ResponseWriter, r *http.Request) {
	roomName := mux.Vars(r)["roomName"]

	var req struct {
		AdminID string `json:"admin_id"`
	}
	// Body is optional; an empty/invalid body leaves req zero-valued.
	_ = json.NewDecoder(r.Body).Decode(&req)

	if err := h.featuresService.UnlockRoom(r.Context(), roomName, req.AdminID); err != nil {
		jsonError(w, "Failed to unlock room", http.StatusInternalServerError)
		return
	}

	jsonResponse(w, map[string]bool{"success": true}, http.StatusOK)
}

func jsonResponse(w http.ResponseWriter, data interface{}, status int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(data); err != nil {
		log.Printf("Failed to write JSON response: %v", err)
	}
}

func jsonError(w http.ResponseWriter, message string, status int) {
	jsonResponse(w, map[string]string{"error": message}, status)
}
