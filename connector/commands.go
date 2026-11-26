package connector

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/commands"
	"maunium.net/go/mautrix/bridgev2/networkid"
)

// Prefijos de los IDs de portal que no son DMs.
const (
	groupPortalPrefix   = "group:"
	channelPortalPrefix = "channel:"
)

func (c *MyConnector) registerCommands() {
	proc, ok := c.bridge.Commands.(*commands.Processor)
	if !ok {
		c.log.Error().Msg("El procesador de comandos no es *commands.Processor; no se registran comandos")
		return
	}
	proc.AddHandlers(
		&commands.FullHandler{
			Func:    fnTodusContacts,
			Name:    "contacts",
			Aliases: []string{"contactos"},
			Help: commands.HelpMeta{
				Section:     commands.HelpSectionChats,
				Description: "Lista tus contactos de ToDus y su presencia",
			},
		},
		&commands.FullHandler{
			Func:    fnTodusGroups,
			Name:    "groups",
			Aliases: []string{"grupos"},
			Help: commands.HelpMeta{
				Section:     commands.HelpSectionChats,
				Description: "Lista tus grupos de ToDus",
			},
		},
		&commands.FullHandler{
			Func:    fnTodusChannels,
			Name:    "channels",
			Aliases: []string{"canales"},
			Help: commands.HelpMeta{
				Section:     commands.HelpSectionChats,
				Description: "Lista los canales de ToDus a los que estás suscrito",
			},
		},
		&commands.FullHandler{
			Func:    fnTodusPresence,
			Name:    "presence",
			Aliases: []string{"estados"},
			Help: commands.HelpMeta{
				Section:     commands.HelpSectionChats,
				Description: "Muestra la presencia observada de tus contactos",
			},
		},
		&commands.FullHandler{
			Func:    fnTodusRelay,
			Name:    "relay",
			Help: commands.HelpMeta{
				Section:     commands.HelpSectionChats,
				Description: "Activa el relay de la sala o envía un mensaje directo por ToDus",
				Args:        "[on|off|<teléfono> <mensaje>]",
			},
		},
		&commands.FullHandler{
			Func:    fnTodusResync,
			Name:    "resync",
			Aliases: []string{"offline", "sincronizar"},
			Help: commands.HelpMeta{
				Section:     commands.HelpSectionChats,
				Description: "Vuelve a conectar la sesión de ToDus para traer los mensajes offline",
			},
		},
	)
	c.log.Info().Msg("Comandos de ToDus registrados")
}

// todusClientForEvent busca el login de ToDus asociado a la sala del comando.
func todusClientForEvent(ce *commands.Event) (*MyNetworkClient, *bridgev2.UserLogin, error) {
	if ce.User == nil {
		return nil, nil, fmt.Errorf("no se pudo identificar al usuario")
	}
	receiver := networkid.UserLoginID("")
	if ce.Portal != nil {
		receiver = ce.Portal.Receiver
	}
	var first *MyNetworkClient
	for _, login := range ce.User.GetUserLogins() {
		nc, ok := login.Client.(*MyNetworkClient)
		if !ok {
			continue
		}
		if receiver != "" && login.ID == receiver {
			return nc, login, nil
		}
		if first == nil {
			first = nc
		}
	}
	if first != nil {
		return first, first.login, nil
	}
	return nil, nil, fmt.Errorf("no hay ninguna cuenta de ToDus vinculada. Usa `login` para conectar una")
}

// dmPhones devuelve los teléfonos de los portales DM de este login.
func (nc *MyNetworkClient) dmPhones(ctx context.Context) []string {
	portals, err := nc.bridge.GetAllPortals(ctx)
	if err != nil {
		nc.log.Warn().Err(err).Msg("No se pudieron listar los portales")
		return nil
	}
	seen := map[string]bool{}
	out := make([]string, 0, len(portals))
	for _, p := range portals {
		if p.Receiver != "" && p.Receiver != nc.login.ID {
			continue
		}
		id := string(p.ID)
		if strings.HasPrefix(id, groupPortalPrefix) || strings.HasPrefix(id, channelPortalPrefix) || id == "welcome-room" {
			continue
		}
		phone := normalizePhone(id)
		if !isDigits(phone) || seen[phone] {
			continue
		}
		seen[phone] = true
		out = append(out, phone)
	}
	sort.Strings(out)
	return out
}

// linkedPortalIDs devuelve los IDs remotos ya enlazados con un prefijo.
func (nc *MyNetworkClient) linkedPortalIDs(ctx context.Context, prefix string) map[string]bool {
	out := map[string]bool{}
	portals, err := nc.bridge.GetAllPortals(ctx)
	if err != nil {
		return out
	}
	for _, p := range portals {
		if p.Receiver != "" && p.Receiver != nc.login.ID {
			continue
		}
		id := string(p.ID)
		if strings.HasPrefix(id, prefix) {
			out[strings.TrimPrefix(id, prefix)] = true
		}
	}
	return out
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

func presenceLabel(c TodusContact) string {
	switch strings.ToLower(strings.TrimSpace(c.Show)) {
	case "chat":
		return "disponible"
	case "away":
		return "ausente"
	case "dnd":
		return "no molestar"
	case "xa":
		return "inactivo"
	}
	if c.Status != "" && !strings.EqualFold(c.Status, "offline") {
		return c.Status
	}
	if c.Priority != "" && c.Priority != "0" {
		return "en línea"
	}
	return "desconectado"
}

func truncate(s string, n int) string {
	s = strings.TrimSpace(strings.ReplaceAll(s, "\n", " "))
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

func fnTodusContacts(ce *commands.Event) {
	nc, _, err := todusClientForEvent(ce)
	if err != nil {
		ce.Reply("%s", err)
		return
	}
	contacts, err := nc.todus.Contacts(ce.Ctx, nc.dmPhones(ce.Ctx))
	if err != nil {
		ce.Reply("No se pudo obtener la lista de contactos: %v", err)
		return
	}
	if len(contacts) == 0 {
		ce.Reply("No hay contactos de ToDus todavía. Abre un chat o espera a recibir un mensaje.")
		return
	}
	sort.Slice(contacts, func(i, j int) bool { return contacts[i].Phone < contacts[j].Phone })

	var b strings.Builder
	fmt.Fprintf(&b, "**Contactos de ToDus (%d)**\n", len(contacts))
	for _, ct := range contacts {
		fmt.Fprintf(&b, "- `%s` — %s", ct.Phone, presenceLabel(ct))
		if ct.Status != "" && !strings.EqualFold(ct.Status, presenceLabel(ct)) {
			fmt.Fprintf(&b, " · %s", ct.Status)
		}
		if ct.Priority != "" && ct.Priority != "0" {
			fmt.Fprintf(&b, " · prioridad %s", ct.Priority)
		}
		b.WriteString("\n")
	}
	ce.Reply(strings.TrimRight(b.String(), "\n"))
}

func fnTodusPresence(ce *commands.Event) {
	nc, _, err := todusClientForEvent(ce)
	if err != nil {
		ce.Reply("%s", err)
		return
	}
	contacts, err := nc.todus.Contacts(ce.Ctx, nc.dmPhones(ce.Ctx))
	if err != nil {
		ce.Reply("No se pudo consultar la presencia: %v", err)
		return
	}
	withData := make([]TodusContact, 0, len(contacts))
	for _, ct := range contacts {
		if ct.Show != "" || ct.Status != "" || (ct.Priority != "" && ct.Priority != "0") {
			withData = append(withData, ct)
		}
	}
	if len(withData) == 0 {
		ce.Reply("Todavía no se ha observado presencia de ningún contacto (%d conocidos).", len(contacts))
		return
	}
	sort.Slice(withData, func(i, j int) bool { return withData[i].Phone < withData[j].Phone })

	var b strings.Builder
	fmt.Fprintf(&b, "**Presencia de contactos (%d/%d)**\n", len(withData), len(contacts))
	for _, ct := range withData {
		fmt.Fprintf(&b, "- `%s` — %s", ct.Phone, presenceLabel(ct))
		if ct.Status != "" && !strings.EqualFold(ct.Status, presenceLabel(ct)) {
			fmt.Fprintf(&b, " · %s", ct.Status)
		}
		if ct.Priority != "" && ct.Priority != "0" {
			fmt.Fprintf(&b, " · prioridad %s", ct.Priority)
		}
		b.WriteString("\n")
	}
	ce.Reply(strings.TrimRight(b.String(), "\n"))
}

func fnTodusResync(ce *commands.Event) {
	nc, _, err := todusClientForEvent(ce)
	if err != nil {
		ce.Reply("%s", err)
		return
	}
	ce.Reply("Reconectando la sesión de ToDus para traer los mensajes offline…")
	ctx, cancel := context.WithTimeout(ce.Ctx, 30*time.Second)
	defer cancel()
	if err := nc.todus.Resync(ctx); err != nil {
		ce.Reply("Falló la resincronización: %v", err)
		return
	}
	ce.Reply("Sesión de ToDus reiniciada. Los mensajes offline llegarán en unos segundos.")
}

func fnTodusGroups(ce *commands.Event) {
	nc, _, err := todusClientForEvent(ce)
	if err != nil {
		ce.Reply("%s", err)
		return
	}
	groups, err := nc.todus.Groups(ce.Ctx)
	if err != nil {
		ce.Reply("No se pudieron obtener los grupos: %v", err)
		return
	}
	if len(groups) == 0 {
		ce.Reply("No perteneces a ningún grupo de ToDus.")
		return
	}
	sort.Slice(groups, func(i, j int) bool {
		return strings.ToLower(groups[i].Name) < strings.ToLower(groups[j].Name)
	})
	linked := nc.linkedPortalIDs(ce.Ctx, groupPortalPrefix)

	var b strings.Builder
	fmt.Fprintf(&b, "**Grupos de ToDus (%d)**\n", len(groups))
	for _, g := range groups {
		name := g.Name
		if name == "" {
			name = "(sin nombre)"
		}
		fmt.Fprintf(&b, "- **%s** — `%s`", name, g.ID)
		if g.Members != "" {
			fmt.Fprintf(&b, " · %s miembros", g.Members)
		}
		if g.Role != "" {
			fmt.Fprintf(&b, " · %s", g.Role)
		}
		if linked[g.ID] {
			b.WriteString(" · ya enlazado")
		}
		b.WriteString("\n")
		if g.Subject != "" {
			fmt.Fprintf(&b, "  _%s_\n", truncate(g.Subject, 140))
		}
	}
	ce.Reply(strings.TrimRight(b.String(), "\n"))
}

func fnTodusChannels(ce *commands.Event) {
	nc, _, err := todusClientForEvent(ce)
	if err != nil {
		ce.Reply("%s", err)
		return
	}
	channels, err := nc.todus.Channels(ce.Ctx)
	if err != nil {
		ce.Reply("No se pudieron obtener los canales: %v", err)
		return
	}
	if len(channels) == 0 {
		ce.Reply("No estás suscrito a ningún canal de ToDus.")
		return
	}
	linked := nc.linkedPortalIDs(ce.Ctx, channelPortalPrefix)

	var b strings.Builder
	fmt.Fprintf(&b, "**Canales de ToDus (%d)**\n", len(channels))
	for _, ch := range channels {
		name := ch.Name
		if name == "" {
			name = "(nombre no disponible)"
		}
		fmt.Fprintf(&b, "- **%s** — `%s`", name, ch.ID)
		if ch.Link != "" {
			fmt.Fprintf(&b, " · %s", ch.Link)
		}
		if linked[ch.ID] {
			b.WriteString(" · ya enlazado")
		}
		b.WriteString("\n")
	}
	b.WriteString("\n_ToDus solo expone los IDs de los canales suscritos; los nombres requieren el enlace del canal._")
	b.WriteString("\n_Los canales son de solo lectura por ahora._")
	ce.Reply(strings.TrimRight(b.String(), "\n"))
}

func fnTodusRelay(ce *commands.Event) {
	nc, login, err := todusClientForEvent(ce)
	if err != nil {
		ce.Reply("%s", err)
		return
	}

	if len(ce.Args) == 0 {
		if ce.Portal == nil {
			ce.Reply("Uso: `$cmdprefix relay <teléfono> <mensaje>` — envía un mensaje directo por ToDus.")
			return
		}
		state := "desactivado"
		if ce.Portal.RelayLoginID != "" {
			state = fmt.Sprintf("activado (`%s`)", ce.Portal.RelayLoginID)
		}
		ce.Reply("Relay en esta sala: **%s**.\n\n- `$cmdprefix relay on` / `$cmdprefix relay off` — activar o desactivar relay.\n- `$cmdprefix relay <teléfono> <mensaje>` — enviar un mensaje directo por ToDus.", state)
		return
	}

	switch strings.ToLower(ce.Args[0]) {
	case "on", "off":
		if ce.Portal == nil {
			ce.Reply("El relay solo se puede activar dentro de una sala del puente.")
			return
		}
		var relay *bridgev2.UserLogin
		if strings.ToLower(ce.Args[0]) == "on" {
			relay = login
		}
		if err := ce.Portal.SetRelay(ce.Ctx, relay); err != nil {
			ce.Reply("No se pudo cambiar el relay: %v", err)
			return
		}
		if relay == nil {
			ce.Reply("Relay desactivado en esta sala.")
		} else {
			ce.Reply("Relay activado con `%s`.", relay.ID)
		}
		return
	}

	phone := normalizePhone(ce.Args[0])
	if !isDigits(phone) || len(phone) < 7 {
		ce.Reply("Teléfono inválido: `%s`. Usa el número con o sin `+`.", ce.Args[0])
		return
	}
	body := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(ce.RawArgs), ce.Args[0]))
	if body == "" {
		ce.Reply("Uso: `$cmdprefix relay <teléfono> <mensaje>`")
		return
	}
	msgID, err := nc.todus.SendMessage(ce.Ctx, phone, body, "")
	if err != nil {
		ce.Reply("No se pudo enviar el mensaje: %v", err)
		return
	}
	ce.Reply("Mensaje enviado a `%s` (id `%s`).", phone, msgID)
}
