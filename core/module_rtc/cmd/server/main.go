package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gorilla/mux"
	"github.com/penguintech/waddlebot/module_rtc/internal/api"
	"github.com/penguintech/waddlebot/module_rtc/internal/config"
	"github.com/penguintech/waddlebot/module_rtc/internal/services"
)

func main() {
	cfg := config.LoadConfig()

	log.Printf("Starting %s v%s", cfg.ModuleName, cfg.ModuleVersion)

	if cfg.LiveKitAPIKey == "" || cfg.LiveKitAPISecret == "" {
		log.Println("WARNING: LiveKit API credentials not configured")
	}

	roomService := services.NewRoomService(cfg.LiveKitHost, cfg.LiveKitAPIKey, cfg.LiveKitAPISecret)
	featuresService := services.NewCallFeaturesService(roomService)

	handlers := api.NewHandlers(roomService, featuresService)

	r := mux.NewRouter()

	r.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		if _, err := fmt.Fprintf(w, `{"status":"healthy","module":"%s","version":"%s","timestamp":"%s"}`,
			cfg.ModuleName, cfg.ModuleVersion, time.Now().UTC().Format(time.RFC3339)); err != nil {
			log.Printf("health response write failed: %v", err)
		}
	}).Methods("GET")

	handlers.RegisterRoutes(r)

	srv := &http.Server{
		Addr:         fmt.Sprintf(":%d", cfg.ModulePort),
		Handler:      r,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 15 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	go func() {
		log.Printf("HTTP server starting on port %d", cfg.ModulePort)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("HTTP server failed: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	log.Println("Shutting down server...")

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("Server forced to shutdown: %v", err)
	}

	log.Println("Server stopped")
}
