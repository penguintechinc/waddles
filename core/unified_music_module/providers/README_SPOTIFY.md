# Spotify Provider Implementation

A comprehensive implementation of the `BaseMusicProvider` interface for Spotify Web API.

## Overview

The `SpotifyProvider` class implements all methods from `BaseMusicProvider` and adds Spotify-specific features including:

- **OAuth2 Authentication**: Full authorization code flow with automatic token refresh
- **Playback Control**: Play, pause, resume, skip, seek, volume control
- **Search**: Search for tracks with detailed metadata
- **Device Management**: List and control playback devices
- **Playlist Support**: Access user playlists and playlist tracks
- **Queue Management**: Add tracks to the playback queue
- **URL Parsing**: Extract track IDs from Spotify URLs and URIs

## File Location

```
/home/penguin/code/WaddleBot/core/unified_music_module/providers/spotify_provider.py
```

## Dependencies

- **httpx**: Async HTTP client for API requests
- Python 3.10+

Install with:
```bash
pip install httpx>=0.25.0
```

## Environment Variables

Required environment variables:

```bash
export SPOTIFY_CLIENT_ID="your_spotify_client_id"
export SPOTIFY_CLIENT_SECRET="your_spotify_client_secret"
export SPOTIFY_REDIRECT_URI="http://localhost:8888/callback"
```

### Getting Spotify Credentials

1. Visit [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create a new application
3. Copy your Client ID and Client Secret
4. Add your redirect URI in the app settings

## Features Implemented

### Base Interface Methods

All methods from `BaseMusicProvider` are implemented:

- `search(query, limit)` - Search for tracks
- `get_track(track_id)` - Get track by ID
- `play(track_id)` - Start playing a track
- `pause()` - Pause playback
- `resume()` - Resume playback
- `skip()` - Skip to next track
- `get_now_playing()` - Get current track
- `is_authenticated()` - Check auth status
- `authenticate(credentials)` - Authenticate with Spotify
- `health_check()` - Check service health

### Spotify-Specific Methods

Additional methods beyond the base interface:

- `get_devices()` - List available playback devices
- `set_device(device_id)` - Set active playback device
- `get_playlists(limit)` - Get user's playlists
- `get_playlist_tracks(playlist_id, limit)` - Get tracks from a playlist
- `add_to_queue(track_id)` - Add track to queue
- `get_playback_state()` - Get full playback state
- `set_volume(volume_percent)` - Set playback volume
- `seek(position_ms)` - Seek to position in track

### Authentication Methods

- `get_auth_url(state)` - Generate OAuth2 authorization URL
- `exchange_code(code)` - Exchange authorization code for tokens
- `refresh_access_token()` - Refresh the access token

### Utility Methods

- `parse_track_url(url)` - Static method to extract track ID from URLs
- `close()` - Close HTTP client connection

## OAuth2 Flow

The provider implements the OAuth2 authorization code flow:

1. **Get Authorization URL**:
   ```python
   auth_url = spotify.get_auth_url(state="random_state")
   # Redirect user to auth_url
   ```

2. **Exchange Code for Tokens**:
   ```python
   # After user authorizes and is redirected back with code
   await spotify.authenticate({"code": "authorization_code"})
   ```

3. **Automatic Token Refresh**:
   - Tokens are automatically refreshed when they expire
   - Refresh occurs 5 minutes before expiration
   - 401 responses trigger automatic token refresh and retry

## URL Parsing

The provider can extract track IDs from various URL formats:

```python
# Standard URL
SpotifyProvider.parse_track_url("https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp")
# Returns: "3n3Ppam7vgaVa1iaRUc9Lp"

# URL with query parameters
SpotifyProvider.parse_track_url("https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp?si=abc123")
# Returns: "3n3Ppam7vgaVa1iaRUc9Lp"

# Spotify URI
SpotifyProvider.parse_track_url("spotify:track:3n3Ppam7vgaVa1iaRUc9Lp")
# Returns: "3n3Ppam7vgaVa1iaRUc9Lp"
```

## Error Handling

Two custom exception types:

- `SpotifyAuthError`: Raised for authentication failures
- `SpotifyAPIError`: Raised for API request failures

Example:
```python
from providers import SpotifyAuthError, SpotifyAPIError

try:
    await spotify.play("track_id")
except SpotifyAuthError as e:
    print(f"Auth error: {e}")
except SpotifyAPIError as e:
    print(f"API error: {e}")
```

## OAuth Scopes

The provider requests these scopes:

- `user-read-playback-state` - Read playback state
- `user-modify-playback-state` - Control playback
- `user-read-currently-playing` - Read currently playing track
- `playlist-read-private` - Read private playlists
- `playlist-read-collaborative` - Read collaborative playlists
- `user-library-read` - Read user's library

## Data Model

The provider returns `MusicTrack` objects with the following fields:

- `track_id`: Spotify track ID
- `name`: Track name
- `artist`: Artist name(s)
- `album`: Album name
- `album_art_url`: URL to album artwork
- `duration_ms`: Track duration in milliseconds
- `provider`: "spotify"
- `uri`: Spotify URI (spotify:track:xxx)
- `metadata`: Dict with additional data (popularity, explicit, external_urls)

## Implementation Details

### HTTP Client

Uses `httpx.AsyncClient` for all HTTP requests:
- 30-second timeout
- Automatic retries on 401 (with token refresh)
- Proper error handling and logging

### Token Management

Tokens are stored in instance variables:
- `access_token`: Current access token
- `refresh_token`: Refresh token for getting new access tokens
- `token_expires_at`: Datetime when token expires

The provider automatically:
- Refreshes tokens 5 minutes before expiration
- Handles 401 responses by refreshing and retrying
- Stores new refresh tokens if provided (token rotation)

### Device Management

The provider supports multiple playback devices:
- `active_device_id` stores the currently selected device
- If set, all playback commands target this device
- Can be changed with `set_device()`

## Testing

A test file is included at:
```
/home/penguin/code/WaddleBot/core/unified_music_module/providers/test_spotify_provider.py
```

Run tests:
```bash
cd /home/penguin/code/WaddleBot/core/unified_music_module/providers
python3 test_spotify_provider.py
```

Tests include:
- URL parsing
- Auth URL generation
- Track data conversion
- Basic initialization

## Usage Examples

See `SPOTIFY_USAGE.md` for comprehensive usage examples including:
- Authentication flow
- Search and playback
- Device management
- Playlist operations
- Complete working examples

## API Endpoints Used

The provider interacts with these Spotify Web API endpoints:

- `POST /api/token` - Token exchange and refresh
- `GET /search` - Search for tracks
- `GET /tracks/{id}` - Get track info
- `GET /me/player/currently-playing` - Get current track
- `PUT /me/player/play` - Start/resume playback
- `PUT /me/player/pause` - Pause playback
- `POST /me/player/next` - Skip track
- `GET /me/player/devices` - List devices
- `PUT /me/player` - Transfer playback
- `GET /me/playlists` - Get playlists
- `GET /playlists/{id}/tracks` - Get playlist tracks
- `POST /me/player/queue` - Add to queue
- `PUT /me/player/volume` - Set volume
- `PUT /me/player/seek` - Seek position
- `GET /me/player` - Get playback state
- `GET /me` - Get user profile (health check)

## Logging

The provider uses Python's logging module:

```python
import logging
logging.basicConfig(level=logging.INFO)
```

Logs include:
- Token refresh events
- API errors
- Authentication failures
- Request failures

### Log and error-message safety

Spotify error bodies and `str(httpx.HTTPStatusError)` echo request data (search text, auth
code, refresh token, device ids), so neither is ever logged or placed in a raised error.
Failures are described by `providers/_log_safety.py:describe_error` -- exception **type**,
HTTP **status** and Spotify's machine-readable **reason** enum (accepted only when it is a
plain identifier, e.g. `invalid_grant`, `PREMIUM_REQUIRED`):

```
Spotify API error: HTTPStatusError status=403 reason=PREMIUM_REQUIRED
```

Raised `SpotifyAPIError` / `SpotifyAuthError` carry the same text and are raised
`from None` so a caller's `exc_info` cannot re-print the chained httpx URL. Importing the
provider also pins the `httpx` logger to WARNING (its INFO request line contains the full
URL) and installs a query-stripping filter as a backstop. Regression tests:
`providers/test_spotify_provider_log_redaction.py`.

## Future Enhancements

Potential additions:
- Album search
- Artist search
- Saved tracks management
- Playlist creation/modification
- Shuffle and repeat controls
- Transfer playback between devices
- Recently played tracks
- Recommendations
- Audio features analysis

## License

Part of the WaddleBot project.
