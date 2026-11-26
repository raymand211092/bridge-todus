package connector

import (
	"context"
	"fmt"
	"math/rand"
	"time"

	"github.com/rs/zerolog"
	"go.mau.fi/util/configupgrade"
	"go.mau.fi/util/ptr"
	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/database"
	"maunium.net/go/mautrix/bridgev2/networkid"
	"maunium.net/go/mautrix/event"
)

var _ bridgev2.NetworkConnector = (*MyConnector)(nil)

type MyConnector struct {
	log    zerolog.Logger
	bridge *bridgev2.Bridge
}

func NewMyConnector(log zerolog.Logger) *MyConnector {
	return &MyConnector{
		log: log.With().Str("component", "network-connector").Logger(),
	}
}

func (c *MyConnector) Init(br *bridgev2.Bridge) {
	c.bridge = br
	c.log = c.bridge.Log
	c.log.Info().Msg("MyConnector Init called")
	c.registerCommands()
}

func (c *MyConnector) GetName() bridgev2.BridgeName {
	return bridgev2.BridgeName{
		DisplayName: "ToDus",
		NetworkURL:  "https://todus.cu",
		NetworkID:   "todus",
	}
}

func (c *MyConnector) GetNetworkID() string {
	return c.GetName().NetworkID
}

func (c *MyConnector) GetCapabilities() *bridgev2.NetworkGeneralCapabilities {
	return &bridgev2.NetworkGeneralCapabilities{}
}

func (c *MyConnector) GetDBMetaTypes() database.MetaTypes {
	return database.MetaTypes{
		Portal: func() any {
			return &PortalMetadata{}
		},
		Ghost: func() any {
			return &GhostMetadata{}
		},
		UserLogin: func() any {
			return &LoginMetadata{}
		},
	}
}

func (c *MyConnector) GetLoginFlows() []bridgev2.LoginFlow {
	return []bridgev2.LoginFlow{{
		ID:          LoginFlowIDPhoneSMS,
		Name:        "Phone & SMS",
		Description: "Login with your ToDus phone number and the SMS code.",
	}}
}

// CreateLogin implements bridgev2.NetworkConnector.
func (c *MyConnector) CreateLogin(ctx context.Context, user *bridgev2.User, flowID string) (bridgev2.LoginProcess, error) {
	if flowID != LoginFlowIDPhoneSMS { // <--- ¡Aquí estaba el error! Decía UsernamePassword
		return nil, fmt.Errorf("unsupported login flow ID: %s", flowID)
	}
	return &SimpleLogin{
		User: user,
		Main: c,
		Log:  user.Log.With().Str("action", "login").Str("flow", flowID).Logger(),
	}, nil
}

func (c *MyConnector) GetConfig() (string, any, configupgrade.Upgrader) {
	return "sidecar_url: http://127.0.0.1:8765", &TodusConfig{}, nil
}

func (c *MyConnector) GetBridgeInfoVersion() (int, int) {
	// Subir la segunda Componente (capabilities) obliga a bridgev2 a reenviar
	// el evento `com.beeper.room_features` a las salas ya existentes. Sin este
	// bump, Beeper sigue mostrando "este chat no soporta envío de archivos"
	// aunque GetCapabilities ya declare File.
	return 1, 1
}

func (c *MyConnector) Start(ctx context.Context) error {
	c.log.Info().Msg("MyConnector Start called")
	return nil
}

func (c *MyConnector) Stop(ctx context.Context) error {
	c.log.Info().Msg("MyConnector Stop called")
	return nil
}

func (c *MyConnector) LoadUserLogin(ctx context.Context, login *bridgev2.UserLogin) error {
	c.log.Info().
		Str("user_id", string(login.ID)).
		Str("remote_name", login.RemoteName).
		Str("mxid", string(login.User.MXID)).
		Msg("LoadUserLogin called")

	// Recuperar Phone y Token del LoginMetadata guardado en la BD
	var phone, token string
	if meta, ok := login.Metadata.(*LoginMetadata); ok && meta != nil {
		phone = meta.Phone
		token = meta.Token
	}
	if phone == "" {
		c.log.Error().Str("user_id", string(login.ID)).Msg("LoginMetadata sin Phone; el cliente no podrá hablar con ToDus")
	}
	if token == "" {
		c.log.Error().Str("user_id", string(login.ID)).Msg("LoginMetadata sin Token; EnsureLogin fallará si el sidecar se reinicia")
	}

	// Rellenar el perfil remoto para que Beeper muestre el teléfono como
	// identidad de la cuenta (no solo el UUID interno del login).
	if login.RemoteProfile.Name == "" {
		login.RemoteProfile.Name = login.RemoteName
	}
	if login.RemoteProfile.Phone == "" {
		login.RemoteProfile.Phone = phone
	}

	client := &MyNetworkClient{
		log:       c.log.With().Str("user_id", string(login.ID)).Logger(),
		bridge:    c.bridge,
		login:     login,
		connector: c,
		phone:     phone,
		longToken: token,
		todus:     NewTodusHTTPClient(SidecarURL, phone, token),
	}

	login.Client = client

	c.log.Info().
		Str("user_id", string(login.ID)).
		Str("phone", phone).
		Msg("MyNetworkClient creado y asignado al login")

	return nil
}

// createWelcomeRoomAndSendIntro performs the room creation and ghost interaction logic.
func (c *MyConnector) createWelcomeRoomAndSendIntro(login *bridgev2.UserLogin) {
	ctx := context.Background()
	user := login.User
	log := c.log.With().Str("user_mxid", string(user.MXID)).Str("login_id", string(login.ID)).Logger()
	ctx = log.WithContext(ctx)

	rand.Seed(time.Now().UnixNano())

	log.Info().Msg("Starting welcome room creation process")

	portalID := networkid.PortalID("welcome-room")
	portalKey := networkid.PortalKey{
		ID:       portalID,
		Receiver: login.ID,
	}

	portal, err := c.bridge.GetPortalByKey(ctx, portalKey)
	if err != nil {
		log.Err(err).Str("portal_key", string(portalKey.ID)).Msg("Failed to get portal")
		return
	}

	log.Info().Str("portal_id", string(portal.ID)).Msg("Successfully retrieved portal")

	ghostNetworkUserID := networkid.UserID(fmt.Sprintf("%s_ghosty_ghost", c.GetNetworkID()))
	ghostDisplayName := "Ghosty Ghost"

	ghost, err := c.bridge.GetGhostByID(ctx, ghostNetworkUserID)
	if err != nil {
		log.Err(err).Str("ghost_network_user_id", string(ghostNetworkUserID)).Msg("Failed to get ghost")
		return
	}

	ghostInfo := &bridgev2.UserInfo{
		Name: &ghostDisplayName,
	}
	ghost.UpdateInfo(ctx, ghostInfo)

	log = log.With().Str("ghost_mxid", string(ghost.ID)).Logger()
	log.Info().Msg("Successfully retrieved/provisioned ghost")

	err = portal.CreateMatrixRoom(ctx, user.GetDefaultLogin(), &bridgev2.ChatInfo{
		Name:  ptr.Ptr(fmt.Sprintf("Welcome %s!", login.RemoteName)),
		Topic: ptr.Ptr("Your special welcome room."),
		Members: &bridgev2.ChatMemberList{Members: []bridgev2.ChatMember{
			{
				EventSender: bridgev2.EventSender{
					Sender:      networkid.UserID(user.MXID),
					SenderLogin: networkid.UserLoginID(user.GetDefaultLogin().ID),
				},
				Membership: event.MembershipJoin,
				Nickname:   ptr.Ptr(login.RemoteName),
				PowerLevel: ptr.Ptr(100),
				UserInfo: &bridgev2.UserInfo{
					Name: ptr.Ptr(login.RemoteName),
				},
			},
		}},
	})
	if err != nil {
		log.Err(err).Msg("Failed to create matrix room")
		return
	}
	log.Info().Msg("Successfully created matrix room")

	err = ghost.Intent.EnsureJoined(ctx, portal.MXID)
	if err != nil {
		log.Err(err).Msg("Failed to ensure ghost was joined before sending message")
		return
	}

	greetings := []string{"Hello there!", "Welcome!", "Greetings!", "Hi!", "Hey!"}
	randomGreeting := greetings[rand.Intn(len(greetings))]
	messageContent := &event.MessageEventContent{
		MsgType: event.MsgText,
		Body:    fmt.Sprintf("%s I'm %s, your friendly welcome bot for the %s bridge.", randomGreeting, ghostDisplayName, c.GetName().DisplayName),
	}

	content := &event.Content{Parsed: messageContent}

	_, err = ghost.Intent.SendMessage(ctx, portal.MXID, event.EventMessage, content, nil)
	if err != nil {
		log.Err(err).Msg("Failed to send welcome message from ghost")
		return
	}
	log.Info().Msg("Successfully sent welcome message from ghost")
}
