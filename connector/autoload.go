package connector

import (
	"context"
	"time"

	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/networkid"
	"maunium.net/go/mautrix/bridgev2/simplevent"
)

// AutoLoadRemotes hace dos cosas:
//   - Crea los portales Matrix para TODOS los grupos de la cuenta de ToDus
//     (portal `group:<id>`) usando ChatResync con CreatePortal, reusando
//     GetChatInfo para poblar nombre/tema/miembros.
//   - Crea los portales Matrix para TODOS los canales suscritos (portal
//     `channel:<id>`) igual que los grupos.
//
// Se invoca desde Connect, asi el puente arranca con todos los chats ya
// visibles en Beeper (los grupos y canales), no solo con los DMs de los que
// haya recibido mensajes.
func (nc *MyNetworkClient) AutoLoadRemotes(ctx context.Context) {
	if nc.todus == nil || nc.login == nil {
		nc.log.Warn().Msg("AutoLoadRemotes: todus client o login nil, no se hace nada")
		return
	}

	ncs := func(ctx context.Context, receiver networkid.UserLoginID, portalID networkid.PortalID, kind string) {
		evt := &simplevent.ChatResync{
			EventMeta: simplevent.EventMeta{
				Type:         bridgev2.RemoteEventChatResync,
				PortalKey:    networkid.PortalKey{ID: portalID, Receiver: receiver},
				Sender:       bridgev2.EventSender{Sender: networkid.UserID(nc.login.UserMXID), IsFromMe: true},
				CreatePortal: true,
				Timestamp:    time.Now(),
			},
			GetChatInfoFunc: func(ctx context.Context, portal *bridgev2.Portal) (*bridgev2.ChatInfo, error) {
				return nc.GetChatInfo(ctx, portal)
			},
		}
		if res := nc.bridge.QueueRemoteEvent(nc.login, evt); !res.Success && !res.Queued {
			nc.log.Warn().Str("kind", kind).Str("portal", string(portalID)).
				Msg("AutoLoadRemotes: no se pudo encolar el ChatResync")
		}
	}

	if groups, err := nc.todus.Groups(ctx); err == nil {
		nc.log.Info().Int("groups", len(groups)).Msg("AutoLoadRemotes: creando portales para grupos")
		for _, g := range groups {
			if g.ID == "" {
				continue
			}
			ncs(ctx, nc.login.ID, networkid.PortalID(groupPortalPrefix+g.ID), "group")
		}
	} else {
		nc.log.Warn().Err(err).Msg("AutoLoadRemotes: no se pudieron listar grupos")
	}

	if channels, err := nc.todus.Channels(ctx); err == nil {
		nc.log.Info().Int("channels", len(channels)).Msg("AutoLoadRemotes: creando portales para canales")
		for _, ch := range channels {
			if ch.ID == "" {
				continue
			}
			ncs(ctx, nc.login.ID, networkid.PortalID(channelPortalPrefix+ch.ID), "channel")
		}
	} else {
		nc.log.Warn().Err(err).Msg("AutoLoadRemotes: no se pudieron listar canales")
	}

	// DMs y bots declarados en profiles.json con auto_load: true. ToDus no
	// expone un roster consultable, así que esta es la única forma de crear
	// sus salas antes de que llegue el primer mensaje.
	loaded := map[string]bool{}
	profiles := currentProfiles()
	for id, profile := range profiles {
		if !profile.AutoLoad {
			continue
		}
		portalID := normalizeProfileID(id)
		if portalID == "" || loaded[portalID] {
			continue
		}
		loaded[portalID] = true
		nc.log.Info().Str("portal", portalID).Str("name", profile.Name).
			Msg("AutoLoadRemotes: creando portal para perfil configurado")
		ncs(ctx, nc.login.ID, networkid.PortalID(portalID), "profile")
	}
}
