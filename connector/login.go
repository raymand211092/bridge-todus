package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/rs/zerolog"
	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/database"
	"maunium.net/go/mautrix/bridgev2/networkid"
	"maunium.net/go/mautrix/bridgev2/status"
)

const (
	LoginFlowIDPhoneSMS = "phone-sms"
	LoginStepIDPhone    = "phone-input"
	LoginStepIDSMS      = "sms-input"
	LoginStepIDComplete = "complete"
)

type SimpleLogin struct {
	User  *bridgev2.User
	Main  *MyConnector
	Log   zerolog.Logger
	Phone string
	Step  string
}

var _ bridgev2.LoginProcessUserInput = (*SimpleLogin)(nil)

func (sl *SimpleLogin) Start(ctx context.Context) (*bridgev2.LoginStep, error) {
	sl.Step = "phone"
	return &bridgev2.LoginStep{
		Type:         bridgev2.LoginStepTypeUserInput,
		StepID:       LoginStepIDPhone,
		Instructions: "Introduce tu número de teléfono de ToDus (ej. 5351234567).",
		UserInputParams: &bridgev2.LoginUserInputParams{
			Fields: []bridgev2.LoginInputDataField{{
				Type: bridgev2.LoginInputFieldTypePhoneNumber,
				ID:   "phone",
				Name: "Número de teléfono",
			}},
		},
	}, nil
}

func (sl *SimpleLogin) SubmitUserInput(ctx context.Context, input map[string]string) (*bridgev2.LoginStep, error) {
	switch sl.Step {
	case "phone":
		return sl.handlePhone(ctx, input)
	case "sms":
		return sl.handleSMS(ctx, input)
	default:
		return nil, fmt.Errorf("estado de login desconocido: %s", sl.Step)
	}
}

func (sl *SimpleLogin) handlePhone(ctx context.Context, input map[string]string) (*bridgev2.LoginStep, error) {
	phone := strings.TrimSpace(input["phone"])
	if phone == "" {
		return nil, fmt.Errorf("el teléfono no puede estar vacío")
	}
	sl.Phone = phone

	if err := sl.callSidecar(ctx, "/request_code", map[string]string{"phone": phone}, nil); err != nil {
		return nil, fmt.Errorf("error solicitando código SMS: %w", err)
	}

	sl.Step = "sms"
	return &bridgev2.LoginStep{
		Type:         bridgev2.LoginStepTypeUserInput,
		StepID:       LoginStepIDSMS,
		Instructions: fmt.Sprintf("Introduce el código SMS de 6 dígitos enviado a %s.", phone),
		UserInputParams: &bridgev2.LoginUserInputParams{
			Fields: []bridgev2.LoginInputDataField{{
				Type: bridgev2.LoginInputFieldType2FACode,
				ID:   "code",
				Name: "Código SMS",
			}},
		},
	}, nil
}

func (sl *SimpleLogin) handleSMS(ctx context.Context, input map[string]string) (*bridgev2.LoginStep, error) {
	code := strings.TrimSpace(input["code"])
	if code == "" {
		return nil, fmt.Errorf("el código no puede estar vacío")
	}

	var resp struct {
		Token string `json:"token"`
		Error string `json:"error"`
	}
	if err := sl.callSidecar(ctx, "/validate_code", map[string]string{
		"phone": sl.Phone, "code": code,
	}, &resp); err != nil {
		return nil, fmt.Errorf("error validando código: %w", err)
	}
	if resp.Token == "" {
		return nil, fmt.Errorf("el sidecar no devolvió token: %s", resp.Error)
	}

	if err := sl.callSidecar(ctx, "/login", map[string]string{
		"phone": sl.Phone, "token": resp.Token,
	}, nil); err != nil {
		return nil, fmt.Errorf("error en login final: %w", err)
	}

	namespace := uuid.MustParse("f7a4f3e3-5d5a-4a9e-8d8a-3b0b9e8a1b2c")
	loginIDStr := uuid.NewSHA1(namespace, []byte(sl.Phone)).String()
	loginID := networkid.UserLoginID(loginIDStr)

	ul, err := sl.User.NewLogin(ctx, &database.UserLogin{
		ID:         loginID,
		RemoteName: sl.Phone,
		Metadata:   &LoginMetadata{Token: resp.Token, Phone: sl.Phone},
		RemoteProfile: status.RemoteProfile{
			Name: sl.Phone,
		},
	}, &bridgev2.NewLoginParams{DeleteOnConflict: true})
	if err != nil {
		return nil, fmt.Errorf("error creando user login: %w", err)
	}

	// Nota: LoadUserLogin() crea el MyNetworkClient y arranca el polling.
	// La sala de bienvenida se crea dentro de LoadUserLogin para no duplicarla.
	go sl.Main.createWelcomeRoomAndSendIntro(ul)

	return &bridgev2.LoginStep{
		Type:         bridgev2.LoginStepTypeComplete,
		StepID:       LoginStepIDComplete,
		Instructions: fmt.Sprintf("Sesión iniciada como %s", sl.Phone),
		CompleteParams: &bridgev2.LoginCompleteParams{
			UserLoginID: ul.ID,
			UserLogin:   ul,
		},
	}, nil
}

func (sl *SimpleLogin) callSidecar(ctx context.Context, path string, reqBody, respBody any) error {
	body, err := json.Marshal(reqBody)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, SidecarURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("no se pudo conectar al sidecar (¿está corriendo en %s?): %w", SidecarURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("sidecar devolvió HTTP %d", resp.StatusCode)
	}
	if respBody != nil {
		return json.NewDecoder(resp.Body).Decode(respBody)
	}
	return nil
}

func (sl *SimpleLogin) Cancel() {
	sl.Log.Debug().Msg("Login cancelado")
}