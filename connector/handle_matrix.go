package connector

import (
	"context"
	"encoding/base64"
	"fmt"
	"strings"

	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/database"
	"maunium.net/go/mautrix/bridgev2/networkid"
	"maunium.net/go/mautrix/event"
)

// HandleMatrixMessage maneja los mensajes entrantes desde Matrix.
func (nc *MyNetworkClient) HandleMatrixMessage(ctx context.Context, msg *bridgev2.MatrixMessage) (*bridgev2.MatrixMessageResponse, error) {
	log := nc.log.With().
		Str("portal_id", string(msg.Portal.ID)).
		Str("sender_mxid", string(msg.Event.Sender)).
		Str("event_id", string(msg.Event.ID)).
		Logger()
	ctx = log.WithContext(ctx)

	content := msg.Content
	if content == nil {
		content = msg.Event.Content.AsMessage()
	}
	if content == nil {
		return nil, fmt.Errorf("evento no es un mensaje")
	}
	if content.MsgType.IsMedia() {
		return nc.handleMatrixMedia(ctx, msg, content)
	}
	body := strings.TrimSpace(content.Body)
	if body == "" {
		return nil, fmt.Errorf("mensaje vacío")
	}

	portalID := string(msg.Portal.ID)

	// ¿El mensaje de Matrix responde a otro? bridgev2 ya resolvió el evento
	// citado a la fila de la base; su ID de red es "todus-<id original>".
	replyToID := ""
	if msg.ReplyTo != nil {
		replyToID = strings.TrimPrefix(string(msg.ReplyTo.ID), "todus-")
		if replyToID != "" {
			log.Info().Str("reply_to", replyToID).Msg("Mensaje de Matrix es una respuesta")
		}
	}

	var (
		msgID string
		err   error
	)

	switch {
	case strings.HasPrefix(portalID, groupPortalPrefix):
		groupID := strings.TrimPrefix(portalID, groupPortalPrefix)
		if groupID == "" {
			return nil, fmt.Errorf("portal de grupo sin id")
		}
		log.Info().Str("group_id", groupID).Str("body", body).Msg("Enviando mensaje a grupo de ToDus")
		msgID, err = nc.todus.SendGroupMessage(ctx, groupID, body, replyToID)
	case strings.HasPrefix(portalID, channelPortalPrefix):
		channelID := strings.TrimPrefix(portalID, channelPortalPrefix)
		if channelID == "" {
			return nil, fmt.Errorf("portal de canal sin id")
		}
		log.Info().Str("channel_id", channelID).Str("body", body).Msg("Enviando mensaje a canal de ToDus")
		msgID, err = nc.todus.SendChannelMessage(ctx, channelID, body)
	default:
		if portalID == "" {
			return nil, fmt.Errorf("portal sin teléfono destinatario")
		}
		log.Info().Str("to", portalID).Str("body", body).Msg("Enviando mensaje a ToDus")
		msgID, err = nc.todus.SendMessage(ctx, portalID, body, replyToID)
	}

	if err != nil {
		log.Err(err).Msg("Falló el envío a ToDus")
		return nil, fmt.Errorf("send to todus: %w", err)
	}

	log.Info().Str("todus_msg_id", msgID).Msg("Mensaje enviado a ToDus")

	return &bridgev2.MatrixMessageResponse{
		DB: &database.Message{
			ID: networkid.MessageID(fmt.Sprintf("todus-%s", msgID)),
		},
	}, nil
}

// GetUserInfo devuelve la info del ghost para un teléfono o JID de ToDus.
// El nombre y el avatar se toman de profiles.json (bots/contactos configurados)
// y, si no están ahí, de las tarjetas <contact> aprendidas por el sidecar.
func (nc *MyNetworkClient) handleMatrixMedia(ctx context.Context, msg *bridgev2.MatrixMessage, content *event.MessageEventContent) (*bridgev2.MatrixMessageResponse, error) {
	if content.URL == "" {
		return nil, bridgev2.ErrUnsupportedMediaType
	}
	data, err := msg.Portal.Bridge.Bot.DownloadMedia(ctx, content.URL, content.File)
	if err != nil {
		return nil, fmt.Errorf("%w: %w", bridgev2.ErrMediaDownloadFailed, err)
	}

	kind := "file"
	switch content.MsgType {
	case event.MsgImage:
		kind = "image"
	case event.MsgVideo:
		kind = "video"
	case event.MsgAudio:
		kind = "audio"
	case event.MsgFile:
		kind = "file"
	}
	if string(content.MsgType) == string(event.CapMsgVoice) {
		kind = "voice"
	}

	fileName := content.GetFileName()
	mime := ""
	width, height, duration := 0, 0, 0
	if content.Info != nil {
		mime = content.Info.MimeType
		width, height = content.Info.Width, content.Info.Height
		duration = content.Info.Duration
	}

	portalID := string(msg.Portal.ID)
	to := portalID
	isGroup := strings.HasPrefix(portalID, groupPortalPrefix)
	if isGroup {
		to = strings.TrimPrefix(portalID, groupPortalPrefix)
	}
	if strings.HasPrefix(portalID, channelPortalPrefix) {
		return nil, bridgev2.ErrUnsupportedMessageType
	}

	req := TodusMediaRequest{
		To:       to,
		Data:     base64.StdEncoding.EncodeToString(data),
		FileName: fileName,
		MimeType: mime,
		Caption:  strings.TrimSpace(content.Body),
		Kind:     kind,
		Width:    width,
		Height:   height,
		Duration: duration,
		IsGroup:  isGroup,
	}
	resp, err := nc.todus.SendMedia(ctx, req)
	if err != nil {
		return nil, fmt.Errorf("send media to todus: %w", err)
	}
	nc.log.Info().Str("todus_msg_id", resp.MsgID).Str("kind", kind).
		Str("to", to).Bool("acked", resp.Acked).Msg("Adjunto enviado a ToDus")
	return &bridgev2.MatrixMessageResponse{
		DB: &database.Message{
			ID: networkid.MessageID(fmt.Sprintf("todus-%s", resp.MsgID)),
		},
	}, nil
}

func (nc *MyNetworkClient) GetUserInfo(ctx context.Context, ghost *bridgev2.Ghost) (*bridgev2.UserInfo, error) {
	phone := string(ghost.ID)
	name := phone
	var avatar *bridgev2.Avatar
	var isBot *bool

	profile, configured := lookupProfile(phone)
	if configured {
		if profile.Name != "" {
			name = profile.Name
		}
		avatar = avatarForURL(profile.AvatarURL)
		if profile.IsBot {
			bot := true
			isBot = &bot
		}
	}

	if !configured || name == phone {
		if contacts, err := nc.todus.Contacts(ctx, []string{phone}); err == nil {
			for _, contact := range contacts {
				if contact.Name != "" {
					name = contact.Name
					break
				}
			}
			if avatar == nil {
				for _, contact := range contacts {
					if contact.AvatarURL != "" {
						avatar = avatarForURL(contact.AvatarURL)
						break
					}
				}
			}
		}
	}

	return &bridgev2.UserInfo{
		Name:   &name,
		Avatar: avatar,
		IsBot:  isBot,
	}, nil
}

// GetChatInfo devuelve la info del portal (DM, grupo o canal de ToDus).
func (nc *MyNetworkClient) GetChatInfo(ctx context.Context, portal *bridgev2.Portal) (*bridgev2.ChatInfo, error) {
	portalID := string(portal.ID)

	if strings.HasPrefix(portalID, groupPortalPrefix) {
		groupID := strings.TrimPrefix(portalID, groupPortalPrefix)
		name := "Grupo ToDus"
		topic := ""
		var avatar *bridgev2.Avatar
		members := []bridgev2.ChatMember{{
			EventSender: bridgev2.EventSender{
				Sender:   networkid.UserID(nc.login.UserMXID),
				IsFromMe: true,
			},
			Membership: event.MembershipJoin,
		}}

		if portal.NameSet {
			name = portal.Name
			topic = portal.Topic
		}

		// Siempre pedimos info aunque la sala ya tenga nombre: es la única
		// fuente del avatar (config.picture) y del tema del grupo.
		if info, err := nc.todus.GroupInfo(ctx, groupID); err == nil && info != nil {
			if !portal.NameSet {
				if info.Name != "" {
					name = info.Name
				}
				topic = info.Subject
			} else if topic == "" {
				topic = info.Subject
			}
			avatar = avatarForURL(info.Picture)
			if avatar == nil {
				avatar = avatarForURL(info.PictureThumbnail)
			}
		} else if err != nil {
			nc.log.Warn().Err(err).Str("group_id", groupID).Msg("GetChatInfo: no se pudo obtener info del grupo")
		}

		if configured, ok := lookupProfile(portalID); ok {
			if configured.Name != "" {
				name = configured.Name
			}
			if avatar == nil {
				avatar = avatarForURL(configured.AvatarURL)
			}
		}

		// Los miembros se cargan SIEMPRE, aunque la sala ya tenga nombre:
		// antes solo se hacía cuando !NameSet y los portales creados por
		// AutoLoadRemotes se quedaban sin perfiles/miembros en Beeper.
		if gm, err := nc.todus.GroupMembers(ctx, groupID); err == nil {
			seen := map[string]bool{}
			for _, member := range gm {
				phone := normalizePhone(member.Phone)
				if phone == "" || !isPhoneNumber(phone) || phone == normalizePhone(nc.phone) || seen[phone] {
					continue
				}
				seen[phone] = true
				members = append(members, bridgev2.ChatMember{
					EventSender: bridgev2.EventSender{
						Sender: networkid.UserID(phone),
					},
					Membership: event.MembershipJoin,
				})
			}
		} else if err != nil {
			nc.log.Warn().Err(err).Str("group_id", groupID).Msg("GetChatInfo: no se pudieron cargar miembros")
		}

		roomType := database.RoomTypeGroupDM
		return &bridgev2.ChatInfo{
			Name:   &name,
			Topic:  &topic,
			Avatar: avatar,
			Type:   &roomType,
			Members: &bridgev2.ChatMemberList{
				Members: members,
			},
		}, nil
	}

	if strings.HasPrefix(portalID, channelPortalPrefix) {
		channelID := strings.TrimPrefix(portalID, channelPortalPrefix)
		name := fmt.Sprintf("Canal ToDus: %s", channelID)
		topic := "Canal de ToDus"
		var avatar *bridgev2.Avatar
		roomType := database.RoomTypeGroupDM
		if info, err := nc.todus.ChannelInfo(ctx, channelID, ""); err == nil && info != nil {
			if info.Name != "" {
				name = info.Name
			}
			if info.Link != "" {
				topic = fmt.Sprintf("Canal de ToDus (%s)", info.Link)
			}
			avatar = avatarForURL(info.Picture)
			if avatar == nil {
				avatar = avatarForURL(info.PictureThumbnail)
			}
		}
		if configured, ok := lookupProfile(portalID); ok {
			if configured.Name != "" {
				name = configured.Name
			}
			if avatar == nil {
				avatar = avatarForURL(configured.AvatarURL)
			}
		}
		return &bridgev2.ChatInfo{
			Name:   &name,
			Topic:  &topic,
			Avatar: avatar,
			Type:   &roomType,
			Members: &bridgev2.ChatMemberList{Members: []bridgev2.ChatMember{{
				EventSender: bridgev2.EventSender{
					Sender:   networkid.UserID(nc.login.UserMXID),
					IsFromMe: true,
				},
				Membership: event.MembershipJoin,
			}}},
		}, nil
	}

	otherPhone := portalID
	name := fmt.Sprintf("ToDus: %s", otherPhone)
	topic := fmt.Sprintf("Chat de ToDus con %s", otherPhone)
	var avatar *bridgev2.Avatar
	var isBot *bool

	profile, configured := lookupProfile(otherPhone)
	if configured {
		if profile.Name != "" {
			name = profile.Name
			topic = profile.Name
		}
		avatar = avatarForURL(profile.AvatarURL)
		if profile.IsBot {
			bot := true
			isBot = &bot
		}
	}

	// El sidecar aprende nombres de las tarjetas <contact> que aparecen en
	// los mensajes; es la única fuente automática de nombres de DM.
	if contacts, err := nc.todus.Contacts(ctx, []string{otherPhone}); err == nil {
		for _, contact := range contacts {
			if contact.Name != "" {
				name = contact.Name
				topic = contact.Name
				break
			}
		}
		if avatar == nil {
			for _, contact := range contacts {
				if contact.AvatarURL != "" {
					avatar = avatarForURL(contact.AvatarURL)
					break
				}
			}
		}
	}

	roomType := database.RoomTypeDM
	otherUserInfo := &bridgev2.UserInfo{
		Name:   &name,
		Avatar: avatar,
		IsBot:  isBot,
	}

	return &bridgev2.ChatInfo{
		Name:   &name,
		Topic:  &topic,
		Avatar: avatar,
		Type:   &roomType,
		Members: &bridgev2.ChatMemberList{
			Members: []bridgev2.ChatMember{
				{
					EventSender: bridgev2.EventSender{
						Sender: networkid.UserID(otherPhone),
					},
					Membership: event.MembershipJoin,
					UserInfo:   otherUserInfo,
				},
				{
					EventSender: bridgev2.EventSender{
						Sender:   networkid.UserID(nc.login.UserMXID),
						IsFromMe: true,
					},
					Membership: event.MembershipJoin,
				},
			},
		},
	}, nil
}

// GetCapabilities define qué features soporta ToDus.
func (nc *MyNetworkClient) GetCapabilities(ctx context.Context, portal *bridgev2.Portal) *event.RoomFeatures {
	return &event.RoomFeatures{
		MaxTextLength: 65536,
		File: event.FileFeatureMap{
			// MIMEs concretos para imagen/video/audio (el comodín "image/*"
			// es válido en el esquema, pero los clientes Beeper los manejan
			// mejor así). "*/*" se reserva para el tipo m.file, igual que
			// en los bridges de WhatsApp/Telegram.
			event.MsgImage: {
				MimeTypes: map[string]event.CapabilitySupportLevel{
					"image/jpeg": event.CapLevelFullySupported,
					"image/png":  event.CapLevelFullySupported,
					"image/gif":  event.CapLevelFullySupported,
					"image/webp": event.CapLevelPartialSupport,
				},
				Caption:          event.CapLevelFullySupported,
				MaxCaptionLength: 2000,
				MaxSize:          50 * 1024 * 1024,
			},
			event.MsgVideo: {
				MimeTypes: map[string]event.CapabilitySupportLevel{
					"video/mp4":       event.CapLevelFullySupported,
					"video/quicktime": event.CapLevelPartialSupport,
				},
				Caption:          event.CapLevelFullySupported,
				MaxCaptionLength: 2000,
				MaxSize:          200 * 1024 * 1024,
			},
			event.MsgAudio: {
				MimeTypes: map[string]event.CapabilitySupportLevel{
					"audio/ogg":  event.CapLevelFullySupported,
					"audio/mpeg": event.CapLevelFullySupported,
					"audio/mp4":  event.CapLevelFullySupported,
					"audio/wav":  event.CapLevelFullySupported,
				},
				Caption:          event.CapLevelFullySupported,
				MaxCaptionLength: 2000,
				MaxSize:          50 * 1024 * 1024,
			},
			event.MsgFile: {
				MimeTypes:        map[string]event.CapabilitySupportLevel{"*/*": event.CapLevelFullySupported},
				Caption:          event.CapLevelFullySupported,
				MaxCaptionLength: 2000,
				MaxSize:          200 * 1024 * 1024,
			},
		},
		Formatting: event.FormattingFeatureMap{
			event.FmtBold:          event.CapLevelFullySupported,
			event.FmtItalic:        event.CapLevelFullySupported,
			event.FmtUnderline:     event.CapLevelFullySupported,
			event.FmtStrikethrough: event.CapLevelFullySupported,
			event.FmtInlineCode:    event.CapLevelFullySupported,
			event.FmtCodeBlock:     event.CapLevelFullySupported,
		},
		Reply: event.CapLevelFullySupported,
	}
}
