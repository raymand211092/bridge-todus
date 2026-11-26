package connector

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/networkid"
)

// ProfileEntry describe el perfil que el bridge debe usar para un usuario
// remoto (normalmente un DM o un bot). Se lee de profiles.json en caliente,
// así que se puede editar sin recompilar ni reiniciar el puente.
type ProfileEntry struct {
	Name      string `json:"name"`
	AvatarURL string `json:"avatar_url"`
	IsBot     bool   `json:"is_bot"`
	AutoLoad  bool   `json:"auto_load"`
}

type profilesFile struct {
	Profiles map[string]ProfileEntry `json:"profiles"`
}

var (
	profilesMu      sync.RWMutex
	profilesMap     = map[string]ProfileEntry{}
	profilesPath    = ""
	profilesModTime time.Time
	avatarHTTP      = &http.Client{Timeout: 90 * time.Second}

	avatarCacheMu sync.Mutex
	avatarCache   = map[string][]byte{}
)

func init() {
	profilesPath = os.Getenv("TODUS_PROFILES_FILE")
	if profilesPath == "" {
		profilesPath = "/opt/bridge-todus/profiles.json"
	}
	loadProfiles()
}

// normalizeProfileID deja un JID/teléfono en la forma que usa el bridge como
// ID de portal y de ghost: sin @dominio, sin + y en minúsculas.
func normalizeProfileID(id string) string {
	s := strings.TrimSpace(strings.ToLower(id))
	s = strings.TrimPrefix(s, "+")
	if i := strings.Index(s, "@"); i > 0 {
		s = s[:i]
	}
	return s
}

// loadProfiles relee profiles.json si cambió. Se llama antes de cada consulta,
// de modo que basta con editar el archivo para aplicar cambios.
func loadProfiles() {
	info, err := os.Stat(profilesPath)
	if err != nil {
		return
	}
	profilesMu.RLock()
	unchanged := info.ModTime().Equal(profilesModTime)
	profilesMu.RUnlock()
	if unchanged {
		return
	}

	raw, err := os.ReadFile(profilesPath)
	if err != nil {
		return
	}
	var file profilesFile
	if err := json.Unmarshal(raw, &file); err != nil {
		return
	}
	out := make(map[string]ProfileEntry, len(file.Profiles))
	for id, entry := range file.Profiles {
		key := normalizeProfileID(id)
		if key != "" {
			out[key] = entry
		}
	}

	profilesMu.Lock()
	profilesMap = out
	profilesModTime = info.ModTime()
	profilesMu.Unlock()
}

func currentProfiles() map[string]ProfileEntry {
	loadProfiles()
	profilesMu.RLock()
	defer profilesMu.RUnlock()
	out := make(map[string]ProfileEntry, len(profilesMap))
	for k, v := range profilesMap {
		out[k] = v
	}
	return out
}

func lookupProfile(id string) (ProfileEntry, bool) {
	loadProfiles()
	key := normalizeProfileID(id)
	if key == "" {
		return ProfileEntry{}, false
	}
	profilesMu.RLock()
	defer profilesMu.RUnlock()
	entry, ok := profilesMap[key]
	return entry, ok
}

// fetchAvatar descarga un avatar de ToDus. El S3 de ToDus es lento y a veces
// falla la primera petición, así que se reintenta y se cachea en memoria.
func fetchAvatar(ctx context.Context, url string) ([]byte, error) {
	avatarCacheMu.Lock()
	data, cached := avatarCache[url]
	avatarCacheMu.Unlock()
	if cached {
		return data, nil
	}

	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(time.Duration(attempt) * time.Second):
			}
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		resp, err := avatarHTTP.Do(req)
		if err != nil {
			lastErr = err
			continue
		}
		if resp.StatusCode != http.StatusOK {
			lastErr = fmt.Errorf("avatar HTTP %d para %s", resp.StatusCode, url)
			resp.Body.Close()
			continue
		}
		data, err = io.ReadAll(io.LimitReader(resp.Body, 12<<20))
		resp.Body.Close()
		if err != nil {
			lastErr = err
			continue
		}
		avatarCacheMu.Lock()
		avatarCache[url] = data
		avatarCacheMu.Unlock()
		return data, nil
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("no se pudo descargar %s", url)
	}
	return nil, lastErr
}

// avatarForURL construye un Avatar de bridgev2 que descarga la imagen de
// ToDus cuando el puente la necesita (el bridge la sube a MXC y la cachea).
func avatarForURL(rawURL string) *bridgev2.Avatar {
	url := strings.TrimSpace(rawURL)
	if !strings.HasPrefix(url, "http://") && !strings.HasPrefix(url, "https://") {
		return nil
	}
	return &bridgev2.Avatar{
		ID:  networkid.AvatarID(url),
		Get: func(ctx context.Context) ([]byte, error) { return fetchAvatar(ctx, url) },
	}
}
