package connector

import (
	"context"
	"fmt"
	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/networkid"
	"maunium.net/go/mautrix/bridgev2/simplevent"
	"maunium.net/go/mautrix/event"
	"net/http"
	"strings"
)

// remoteMedia extrae la URL, el nombre y el tipo de Matrix de un adjunto ToDus.
func remoteMedia(m TodusMessage) (string, string, event.MessageType) {
	switch {
	case m.VideoURL != "" || m.VideoName != "":
		url := m.VideoURL
		if url == "" {
			url = m.URL
		}
		name := m.VideoName
		if name == "" {
			name = "video.mp4"
		}
		return url, name, event.MsgVideo
	case m.VoiceURL != "":
		return m.VoiceURL, "nota-de-voz.ogg", event.MsgAudio
	case m.GifURL != "":
		return m.GifURL, "animacion.gif", event.MsgImage
	case m.URL != "":
		name := m.FileName
		if name == "" {
			name = "archivo"
		}
		if m.ImageWidth > 0 || m.ImageHeight > 0 || m.ImageThumbnail != "" ||
			strings.HasPrefix(strings.ToLower(name), "image") ||
			strings.HasSuffix(strings.ToLower(name), ".jpg") ||
			strings.HasSuffix(strings.ToLower(name), ".jpeg") ||
			strings.HasSuffix(strings.ToLower(name), ".png") ||
			strings.HasSuffix(strings.ToLower(name), ".webp") {
			return m.URL, name, event.MsgImage
		}
		return m.URL, name, event.MsgFile
	}
	return "", "", event.MsgText
}

// processIncomingMessage convierte un mensaje de ToDus en un evento de Matrix.
func (nc *MyNetworkClient) processIncomingMessage(m TodusMessage) {
	myPhone := normalizePhone(nc.phone)
	fromPhone := normalizePhone(extractPhone(m.From))
	toPhone := normalizePhone(extractPhone(m.To))

	mediaURL, _, _ := remoteMedia(m)
	if m.Body == "" && mediaURL == "" {
		nc.log.Debug().Str("type", m.Type).Msg("Mensaje sin body ni adjunto, ignorado")
		return
	}

	isGroup := m.IsGroup || m.GroupID != "" || strings.Contains(m.From, "@muclight.im.todus.cu")
	isChannel := m.IsChannel || m.ChannelID != "" || strings.Contains(m.From, "@ch")

	var (
		portalID    networkid.PortalID
		senderPhone string
		isFromMe    bool
	)

	if isGroup {
		// El id de grupo se usa EXACTAMENTE como llega (sin quitar guiones):
		// AutoLoadRemotes y SendGroupMessage usan el UUID crudo; si aquí se
		// "normaliza" (normalizePhone borra guiones), se crea un SEGUNDO portal
		// y los mensajes recibidos caen en una sala distinta a la de los
		// mensajes enviados.
		groupID := bareRemoteID(m.GroupID)
		if groupID == "" {
			groupID = bareRemoteID(groupIDFromJID(m.From))
		}
		if groupID == "" {
			nc.log.Warn().Str("from", m.From).Msg("Mensaje de grupo sin group_id, ignorado")
			return
		}
		senderPhone = normalizePhone(m.SenderPhone)
		if senderPhone == "" {
			senderPhone = normalizePhone(groupSenderFromJID(m.From))
		}
		if senderPhone == "" {
			senderPhone = fromPhone
		}
		isFromMe = senderPhone == myPhone
		portalID = networkid.PortalID(groupPortalPrefix + groupID)
	} else if isChannel {
		channelID := bareRemoteID(m.ChannelID)
		if channelID == "" {
			channelID = bareRemoteID(m.From)
		}
		if channelID == "" || channelID == "ch" {
			nc.log.Warn().Str("from", m.From).Msg("Mensaje de canal sin channel_id, ignorado")
			return
		}
		senderPhone = m.PubBy
		if senderPhone == "" {
			senderPhone = normalizePhone(m.SenderPhone)
		}
		if senderPhone == "" {
			senderPhone = normalizePhone(groupSenderFromJID(m.From))
		}
		if senderPhone == "" {
			senderPhone = fromPhone
		}
		isFromMe = senderPhone == myPhone
		if isFromMe {
			// Es publicacion propia: ya la entregamos por Matrix, no la reenviamos.
			nc.log.Debug().Str("channel_id", channelID).Str("pub_by", senderPhone).Msg("Publicacion propia descartada")
			return
		}
		portalID = networkid.PortalID(channelPortalPrefix + channelID)
	} else {
		if fromPhone == "" {
			nc.log.Warn().Str("from", m.From).Msg("No se pudo extraer teléfono del remitente, ignorando")
			return
		}
		otherPhone := fromPhone
		if fromPhone == myPhone {
			isFromMe = true
			otherPhone = toPhone
		}
		if otherPhone == "" || otherPhone == myPhone {
			otherPhone = myPhone
		}
		senderPhone = fromPhone
		portalID = networkid.PortalID(otherPhone)
	}

	if senderPhone == "" {
		senderPhone = myPhone
	}

	nc.log.Info().
		Str("from", fromPhone).
		Str("to", toPhone).
		Str("sender", senderPhone).
		Bool("group", isGroup).
		Bool("channel", isChannel).
		Bool("from_me", isFromMe).
		Str("portal_id", string(portalID)).
		Str("body", m.Body).
		Bool("offline", m.OfflineTS != "").
		Str("offline_ts", m.OfflineTS).
		Msg("Encolando mensaje para Matrix")

	var replyTo *networkid.MessageOptionalPartID
	if m.ReplyTo != "" {
		replyTo = &networkid.MessageOptionalPartID{
			MessageID: networkid.MessageID("todus-" + m.ReplyTo),
		}
	}

	// Hora original si el mensaje viene de la cola offline de ToDus.
	eventTS := offlineEventTimestamp(m)

	evt := &simplevent.Message[string]{
		EventMeta: simplevent.EventMeta{
			Type:      bridgev2.RemoteEventMessage,
			PortalKey: networkid.PortalKey{ID: portalID, Receiver: nc.login.ID},
			Sender: bridgev2.EventSender{
				Sender:   networkid.UserID(senderPhone),
				IsFromMe: isFromMe,
			},
			CreatePortal: true,
			Timestamp:    eventTS,
		},
		Data: m.Body,
		ID:   networkid.MessageID(fmt.Sprintf("todus-%s", m.ID)),
		ConvertMessageFunc: func(ctx context.Context, portal *bridgev2.Portal, intent bridgev2.MatrixAPI, data string) (*bridgev2.ConvertedMessage, error) {
			mediaURL, fileName, msgType := remoteMedia(m)
			if mediaURL == "" {
				return &bridgev2.ConvertedMessage{
					ReplyTo: replyTo,
					Parts: []*bridgev2.ConvertedMessagePart{{
						Type: event.EventMessage,
						Content: &event.MessageEventContent{
							MsgType: event.MsgText,
							Body:    data,
						},
					}},
				}, nil
			}

			mediaData, err := nc.todus.DownloadMedia(ctx, mediaURL)
			if err != nil {
				return nil, fmt.Errorf("%w: %w", bridgev2.ErrMediaDownloadFailed, err)
			}
			mime := http.DetectContentType(mediaData)
			mxc, encrypted, err := intent.UploadMedia(ctx, portal.MXID, mediaData, fileName, mime)
			if err != nil {
				return nil, fmt.Errorf("%w: %w", bridgev2.ErrMediaReuploadFailed, err)
			}

			body := fileName
			if data != "" {
				body = data
			}
			content := &event.MessageEventContent{
				MsgType:  msgType,
				Body:     body,
				FileName: fileName,
				URL:      mxc,
				File:     encrypted,
				Info: &event.FileInfo{
					MimeType: mime,
					Size:     len(mediaData),
				},
			}
			if m.ImageWidth > 0 {
				content.Info.Width = m.ImageWidth
			}
			if m.ImageHeight > 0 {
				content.Info.Height = m.ImageHeight
			}
			if m.VideoDuration > 0 {
				content.Info.Duration = m.VideoDuration
			}
			if m.VoiceDuration > 0 {
				content.Info.Duration = m.VoiceDuration
			}
			return &bridgev2.ConvertedMessage{
				ReplyTo: replyTo,
				Parts: []*bridgev2.ConvertedMessagePart{{
					Type:    event.EventMessage,
					Content: content,
				}},
			}, nil
		},
	}

	nc.bridge.QueueRemoteEvent(nc.login, evt)
}

// groupIDFromJID saca el id de "grupo@muclight.im.todus.cu[/remitente@im.todus.cu]".
func groupIDFromJID(jid string) string {
	if !strings.Contains(jid, "@muclight.im.todus.cu") {
		return ""
	}
	return jid[:strings.Index(jid, "@")]
}

// groupSenderFromJID saca el teléfono del remitente de un JID de grupo.
func groupSenderFromJID(jid string) string {
	parts := strings.SplitN(jid, "/", 2)
	if len(parts) != 2 {
		return ""
	}
	return extractPhone(parts[1])
}

// bareRemoteID recorta un id remoto de grupo/canal a su parte desnuda:
// quita dominio y recurso del JID ("id@dominio/recurso" -> "id").
// A diferencia de normalizePhone, NO borra guiones: los group_id de ToDus
// son UUIDs con guiones y el portal debe coincidir con el que crea
// AutoLoadRemotes y con el que usa SendGroupMessage.
func bareRemoteID(id string) string {
	id = strings.TrimSpace(id)
	if i := strings.IndexAny(id, "@/"); i >= 0 {
		id = id[:i]
	}
	return id
}

// normalizePhone deja el teléfono en el formato de los JIDs de ToDus:
// sin "+", espacios, guiones ni paréntesis.
func normalizePhone(p string) string {
	p = strings.TrimSpace(p)
	p = strings.TrimPrefix(p, "+")
	return strings.NewReplacer(" ", "", "-", "", "(", "", ")", "").Replace(p)
}

// extractPhone saca el número de un JID tipo "5353598561@im.todus.cu/recurso".
func extractPhone(jid string) string {
	if jid == "" {
		return ""
	}
	if i := strings.Index(jid, "@"); i >= 0 {
		return jid[:i]
	}
	return jid
}
