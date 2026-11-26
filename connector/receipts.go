package connector

import (
	"context"
	"strings"
	"sync"
	"time"

	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/database"
	"maunium.net/go/mautrix/bridgev2/matrix"
	"maunium.net/go/mautrix/bridgev2/networkid"
	"maunium.net/go/mautrix/event"
	"maunium.net/go/mautrix/id"
)

// ghostMXIDFormatter permite obtener el MXID de un ghost desde el conector
// Matrix. FormatGhostMXID no forma parte de la interfaz bridgev2.MatrixConnector,
// así que se usa una aserción de tipo.
type ghostMXIDFormatter interface {
	FormatGhostMXID(networkid.UserID) id.UserID
}

func (nc *MyNetworkClient) ghostMXID(user networkid.UserID) id.UserID {
	if f, ok := nc.bridge.Matrix.(ghostMXIDFormatter); ok {
		return f.FormatGhostMXID(user)
	}
	return ""
}

// isPhoneNumber comprueba que una cadena sea un teléfono (solo dígitos).
// Evita confundir el id hex de un grupo con un usuario al procesar acuses.
func isPhoneNumber(s string) bool {
	if len(s) < 5 {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// receiptSeen evita reenviar el mismo MSS cuando el servidor repite acuses
// (ToDus reenvía el mismo `rd` decenas de veces con distinto id externo).
// deliveredSeen acumula, por mensaje, los ghosts a los que ya se marcó entregado.
var (
	receiptSeenMu  sync.Mutex
	receiptSeen    = map[string]time.Time{}
	receiptSeenTTL = 15 * time.Minute

	deliveredMu   sync.Mutex
	deliveredSeen = map[string]map[id.UserID]struct{}{}
)

func shouldProcessReceipt(key string) bool {
	now := time.Now()
	receiptSeenMu.Lock()
	defer receiptSeenMu.Unlock()
	if last, ok := receiptSeen[key]; ok && now.Sub(last) < receiptSeenTTL {
		return false
	}
	receiptSeen[key] = now
	if len(receiptSeen) > 5000 {
		cutoff := now.Add(-receiptSeenTTL)
		for k, ts := range receiptSeen {
			if ts.Before(cutoff) {
				delete(receiptSeen, k)
			}
		}
	}
	return true
}

// accumulatedDelivered añade el ghost recién confirmado al conjunto del mensaje
// y devuelve la lista completa. Cada MSS lleva así todos los miembros que ya
// recibieron/leyeron el mensaje, no solo el último.
func accumulatedDelivered(key string, add []id.UserID) []id.UserID {
	deliveredMu.Lock()
	defer deliveredMu.Unlock()
	set, ok := deliveredSeen[key]
	if !ok {
		set = map[id.UserID]struct{}{}
		deliveredSeen[key] = set
		if len(deliveredSeen) > 5000 {
			for k := range deliveredSeen {
				if k != key {
					delete(deliveredSeen, k)
					break
				}
			}
		}
	}
	for _, u := range add {
		set[u] = struct{}{}
	}
	out := make([]id.UserID, 0, len(set))
	for u := range set {
		out = append(out, u)
	}
	return out
}

// processReceipts marca como entregados/leídos los mensajes que ToDus confirma.
//
// bridgev2 solo enruta los receipts de entrega en portales DM
// (handleRemoteDeliveryReceipt exige RoomTypeDM), así que aquí se llama
// directamente a SendMessageStatus, que funciona igual en DMs, grupos y canales.
func (nc *MyNetworkClient) processReceipts(recs []TodusReceipt) {
	if nc.bridge == nil || nc.login == nil || len(recs) == 0 {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	for _, r := range recs {
		if !shouldProcessReceipt(r.ID + "|" + r.Type + "|" + r.From) {
			continue
		}
		nc.processReceipt(ctx, r)
	}
}

func (nc *MyNetworkClient) processReceipt(ctx context.Context, r TodusReceipt) {
	msgID := strings.TrimSpace(r.ID)
	if msgID == "" {
		return
	}

	// El puente guarda los IDs de mensajes como "todus-<id de ToDus>".
	target := networkid.MessageID("todus-" + msgID)
	parts, err := nc.bridge.DB.Message.GetAllPartsByID(ctx, nc.login.ID, target)
	if err != nil {
		nc.log.Warn().Err(err).Str("todus_msg_id", msgID).Msg("receipt: no se pudo buscar el mensaje")
		return
	}
	if len(parts) == 0 {
		nc.log.Debug().Str("todus_msg_id", msgID).Msg("receipt: mensaje aún no está en la BD")
		return
	}

	var deliveredTo []id.UserID
	from := normalizePhone(r.From)
	if isPhoneNumber(from) {
		if ghost := nc.ghostMXID(networkid.UserID(from)); ghost != "" {
			deliveredTo = append(deliveredTo, ghost)
		}
	}

	for _, part := range parts {
		portal, err := nc.bridge.GetPortalByKey(ctx, part.Room)
		if err != nil || portal == nil || portal.MXID == "" {
			nc.log.Warn().Err(err).Str("portal", string(part.Room.ID)).
				Msg("receipt: portal no encontrado")
			continue
		}
		// bridgev2 no acumula DeliveredTo en los MSS: cada evento lleva solo lo
		// que se le pasa. En grupos con muchos miembros eso haría que Beeper
		// "perdiera" los acuses anteriores, así que acumulamos por mensaje.
		all := accumulatedDelivered(string(part.Room.ID)+"|"+string(part.MXID), deliveredTo)
		// SendMessageStatus lanza un goroutine: el contexto debe sobrevivir a
		// esta función. Por eso se usa el contexto del puente, no uno con
		// cancelación diferida.
		nc.bridge.Matrix.SendMessageStatus(nc.bridge.BackgroundCtx, &bridgev2.MessageStatus{
			Status:      event.MessageStatusSuccess,
			DeliveredTo: all,
		}, &bridgev2.MessageStatusEventInfo{
			RoomID:                      portal.MXID,
			SourceEventID:               part.MXID,
			Sender:                      part.SenderMXID,
			IsSourceEventDoublePuppeted: part.IsDoublePuppeted,
		})
		nc.log.Info().
			Str("todus_msg_id", msgID).
			Str("event_id", string(part.MXID)).
			Str("portal", string(part.Room.ID)).
			Str("receipt_type", r.Type).
			Str("from", from).
			Int("delivered_to", len(all)).
			Msg("receipt procesado")

		// Semántica real de ToDus (verificada empíricamente): el servidor
		// tiene invertidos los elementos respecto a lo asumido —
		//   <rd> = ENTREGADO (la app oficial lo manda al recibir)
		//   <dd> = LEÍDO (la app oficial lo manda al abrir el chat)
		// Por eso el sidecar clasifica rd->"read" y dd->"delivered":
		// el tick azul en Beeper se manda con los "delivered" (dd).
		if r.Type == "delivered" && len(all) > 0 {
			if mc, ok := nc.bridge.Matrix.(*matrix.Connector); ok {
				intent := mc.AS.Intent(all[0])
				if intent != nil && intent.Client != nil {
					if err := intent.SendReceipt(ctx, portal.MXID, part.MXID,
						event.ReceiptTypeRead, struct{}{}); err != nil {
						nc.log.Warn().Err(err).
							Str("todus_msg_id", msgID).
							Str("ghost", string(all[0])).
							Msg("receipt: no se pudo mandar el read receipt del ghost")
					} else {
						nc.log.Info().
							Str("todus_msg_id", msgID).
							Str("ghost", string(all[0])).
							Msg("read receipt del ghost enviado (dd)")
					}
				}
			}
		}
	}
}

// HandleMatrixReadReceipt reenvía a ToDus las lecturas reales del usuario de
// Beeper en salas DM. Semántica ToDus: marcar como leído = <dd> (elemento
// invertido). Se ignoran las lecturas implícitas y las de mensajes propios
// (hungryserv marca leído la sala al enviar), para no generar azules falsos.
func (nc *MyNetworkClient) HandleMatrixReadReceipt(ctx context.Context, msg *bridgev2.MatrixReadReceipt) error {
	if msg == nil || msg.Portal == nil || msg.ExactMessage == nil || msg.Implicit {
		return nil
	}
	if msg.Portal.RoomType != database.RoomTypeDM {
		return nil // solo reenviamos lecturas de DMs
	}
	parts, err := nc.bridge.DB.Message.GetAllPartsByID(ctx, nc.login.ID, msg.ExactMessage.ID)
	if err != nil {
		nc.log.Warn().Err(err).Str("message_id", string(msg.ExactMessage.ID)).
			Msg("read receipt: no se pudo buscar el mensaje")
		return nil
	}
	var part *database.Message
	for _, p := range parts {
		if p.MXID == msg.EventID {
			part = p
			break
		}
	}
	if part == nil && len(parts) > 0 {
		part = parts[0]
	}
	if part == nil {
		return nil
	}
	// Lectura del propio mensaje (hungryserv auto-marca la sala como leída
	// hasta tu propio mensaje al enviar): ignorar.
	if part.SenderMXID == nc.login.UserMXID {
		return nil
	}
	todusID := strings.TrimPrefix(string(msg.ExactMessage.ID), "todus-")
	if todusID == "" {
		return nil
	}
	if !shouldProcessReceipt("fw|" + todusID) {
		return nil // ya reenviado recientemente
	}
	remote := string(msg.Portal.ID)
	if err := nc.todus.SendReadReceipt(ctx, remote, todusID); err != nil {
		nc.log.Warn().Err(err).
			Str("todus_msg_id", todusID).
			Str("portal", remote).
			Msg("no se pudo reenviar la lectura a ToDus")
	} else {
		nc.log.Info().
			Str("todus_msg_id", todusID).
			Str("portal", remote).
			Msg("lectura reenviada a ToDus (dd)")
	}
	return nil
}
