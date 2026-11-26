package connector

import (
	"context"
	"strings"
	"sync"
	"time"

	"github.com/rs/zerolog"
	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/networkid"
	"maunium.net/go/mautrix/bridgev2/status"
)

var _ bridgev2.NetworkAPI = (*MyNetworkClient)(nil)

type MyNetworkClient struct {
	log       zerolog.Logger
	bridge    *bridgev2.Bridge
	login     *bridgev2.UserLogin
	connector *MyConnector

	// Cliente HTTP al sidecar
	todus     *TodusHTTPClient
	phone     string
	longToken string

	// Control del polling
	stopPoll chan struct{}
	pollOnce sync.Once
}

// Connect asegura la sesión en el sidecar y arranca el bucle de polling.
func (nc *MyNetworkClient) Connect(ctx context.Context) {
	nc.log.Info().Str("phone", nc.phone).Msg("MyNetworkClient Connect called")

	if nc.todus == nil {
		nc.log.Error().Msg("todus client nil; revisa LoadUserLogin")
		return
	}

	// El canal se crea antes de intentar el login para que el reintento en
	// segundo plano pueda detenerse con Disconnect().
	nc.stopPoll = make(chan struct{})
	go nc.pollLoop()

	// Asegurar que el sidecar tiene la sesión activa (por si se reinició)
	if err := nc.todus.EnsureLogin(ctx); err != nil {
		nc.log.Err(err).Msg("EnsureLogin falló; se reintentará en segundo plano")
		nc.login.BridgeState.Send(status.BridgeState{
			StateEvent: status.StateTransientDisconnect,
			Error:      "todus-sidecar-unavailable",
			Message:    err.Error(),
		})
		// Antes, si el sidecar aún no había arrancado, el puente se quedaba
		// "desconectado" para siempre (p. ej. "login: HTTP 500") hasta que
		// alguien lo reiniciaba a mano. Ahora reintentamos indefinidamente.
		go nc.retryLoginUntilConnected()
	} else {
		nc.markConnected(ctx)
	}
}

// markConnected publica el estado CONNECTED y arranca la carga de portales.
func (nc *MyNetworkClient) markConnected(ctx context.Context) {
	nc.log.Info().Msg("Sesión ToDus confirmada en el sidecar")
	// bridgev2 no publica CONNECTED por su cuenta: sin esto, Beeper
	// muestra el puente como RUNNING pero sin cuenta remota asociada.
	nc.login.BridgeState.Send(status.BridgeState{
		StateEvent: status.StateConnected,
	})
	// Cargar automaticamente los portales de Matrix para todos los grupos y
	// canales de la cuenta. En background para no bloquear el polling.
	go nc.AutoLoadRemotes(ctx)
}

// retryLoginUntilConnected reintenta restaurar la sesión del sidecar con
// backoff exponencial (5s → 5min) hasta lograrlo o hasta Disconnect().
func (nc *MyNetworkClient) retryLoginUntilConnected() {
	const maxBackoff = 5 * time.Minute
	backoff := 5 * time.Second
	for {
		select {
		case <-nc.stopPoll:
			return
		case <-time.After(backoff):
		}

		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		err := nc.todus.EnsureLogin(ctx)
		cancel()
		if err == nil {
			nc.log.Info().Msg("Sidecar disponible de nuevo; sesión ToDus restaurada")
			nc.markConnected(context.Background())
			return
		}

		nc.log.Warn().Err(err).
			Dur("espera", backoff).
			Msg("Reintento de login fallido")
		if backoff < maxBackoff {
			backoff *= 2
			if backoff > maxBackoff {
				backoff = maxBackoff
			}
		}
	}
}

// Disconnect detiene el bucle de polling.
func (nc *MyNetworkClient) Disconnect() {
	nc.log.Info().Msg("MyNetworkClient Disconnect called")
	nc.pollOnce.Do(func() {
		if nc.stopPoll != nil {
			close(nc.stopPoll)
		}
	})
}

// LogoutRemote es un no-op; el sidecar mantiene la sesión en memoria.
func (nc *MyNetworkClient) LogoutRemote(ctx context.Context) {
	nc.log.Info().Msg("MyNetworkClient LogoutRemote called")
}

func (nc *MyNetworkClient) IsThisUser(ctx context.Context, userID networkid.UserID) bool {
	return strings.EqualFold(string(userID), normalizePhone(nc.phone))
}

func (nc *MyNetworkClient) IsLoggedIn() bool {
	return nc.phone != "" && nc.longToken != ""
}

// pollLoop consulta /poll cada 2 segundos y procesa los mensajes entrantes.
func (nc *MyNetworkClient) pollLoop() {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	nc.log.Info().Msg("pollLoop iniciado (cada 2s)")
	// Tras 5 fallos seguidos (~10 s) avisamos a Beeper; al recuperar,
	// volvemos a CONNECTED. Evita quedarse "desconectado" tras una caída.
	fails := 0
	reportedDown := false
	for {
		select {
		case <-nc.stopPoll:
			nc.log.Info().Msg("pollLoop detenido")
			return
		case <-ticker.C:
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			res, err := nc.todus.Poll(ctx)
			cancel()
			if err != nil {
				fails++
				nc.log.Warn().Err(err).Msg("poll falló (se reintentará)")
				if fails >= 5 && !reportedDown {
					reportedDown = true
					nc.login.BridgeState.Send(status.BridgeState{
						StateEvent: status.StateTransientDisconnect,
						Error:      "todus-poll-unavailable",
						Message:    err.Error(),
					})
				}
				continue
			}
			if reportedDown {
				reportedDown = false
				nc.login.BridgeState.Send(status.BridgeState{
					StateEvent: status.StateConnected,
				})
			}
			fails = 0
			for _, m := range res.Messages {
				nc.processIncomingMessage(m)
			}
			if len(res.Receipts) > 0 {
				nc.processReceipts(res.Receipts)
			}
		}
	}
}