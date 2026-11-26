package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// TodusMessage es la estructura que devuelve POST /poll del sidecar.
type TodusMessage struct {
	From        string `json:"from"`
	To          string `json:"to"`
	ID          string `json:"id"`
	Type        string `json:"type"`
	Body        string `json:"body"`
	IsGroup     bool   `json:"is_group"`
	GroupID     string `json:"group_id"`
	ReplyTo     string `json:"reply_to"`
	OriginalID  string `json:"original_id"`
	SenderPhone string `json:"sender_phone"`

	// OfflineTS es el ts (ms epoch) de <todus_offline> cuando el
	// servidor reentrega la cola offline. 0/vacio = mensaje en vivo.
	OfflineTS string `json:"offline_ts"`

	// Campos para mensajes de canal (publicaciones de canales).
	IsChannel   bool   `json:"is_channel"`
	ChannelID   string `json:"channel_id"`
	ChannelLink string `json:"channel_link"`
	PubID       string `json:"pub_id"`
	PubBy       string `json:"pub_by"`

	// Adjuntos: el parser del SDK rellena estos campos desde <file>, <image>,
	// <video>, <voice> y <gif>. `url` es la URL ToDus a resolver via XMPP.
	URL            string `json:"url"`
	FileName       string `json:"file_name"`
	FileSize       int    `json:"file_size"`
	ImageWidth     int    `json:"image_width"`
	ImageHeight    int    `json:"image_height"`
	ImageThumbnail string `json:"image_thumbnail"`
	VideoURL       string `json:"video_url"`
	VideoName      string `json:"video_name"`
	VideoSize      int    `json:"video_size"`
	VideoDuration  int    `json:"video_duration"`
	VideoWidth     int    `json:"video_width"`
	VideoHeight    int    `json:"video_height"`
	VoiceURL       string `json:"voice_url"`
	VoiceDuration  int    `json:"voice_duration"`
	GifURL         string `json:"gif_url"`
}

// TodusPublication es una publicacion (pub) de un canal recuperada con
// `todus:ch:getpub`.
type TodusPublication struct {
	ID    string `json:"id"`
	TS    string `json:"ts"`
	Publ  string `json:"publ"`
	PubBy string `json:"pub_by"`
}

// TodusHTTPClient encapsula las llamadas al sidecar Python.
type TodusHTTPClient struct {
	baseURL string
	phone   string
	token   string
	http    *http.Client
}

func NewTodusHTTPClient(baseURL, phone, token string) *TodusHTTPClient {
	return &TodusHTTPClient{
		baseURL: baseURL,
		phone:   phone,
		token:   token,
		http:    &http.Client{Timeout: 30 * time.Second},
	}
}

// SendMessage envía un mensaje de texto al destinatario (teléfono sin @).
func (c *TodusHTTPClient) SendMessage(ctx context.Context, to, body, replyTo string) (string, error) {
	payload := map[string]string{
		"phone":   c.phone,
		"to":      to,
		"message": body,
	}
	if replyTo != "" {
		payload["reply_to"] = replyTo
	}
	raw, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/send_message", bytes.NewReader(raw))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("send_message: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("send_message: HTTP %d", resp.StatusCode)
	}
	var out struct {
		Status string `json:"status"`
		MsgID  string `json:"msg_id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", err
	}
	return out.MsgID, nil
}

// SendReadReceipt marca un mensaje del remoto como leido en ToDus (elemento
// dd, semantica real del servidor). El sidecar lo convierte en stanza XMPP.
func (c *TodusHTTPClient) SendReadReceipt(ctx context.Context, to, msgID string) error {
	payload := map[string]string{"phone": c.phone, "to": to, "msg_id": msgID}
	raw, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/send_read_receipt", bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("send_read_receipt: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("send_read_receipt: HTTP %d", resp.StatusCode)
	}
	return nil
}

// TodusReceipt es un acuse de entrega/lectura (dd/rd) o un ack del servidor.
type TodusReceipt struct {
	ID      string  `json:"id"`
	Type    string  `json:"type"`
	From    string  `json:"from"`
	GroupID string  `json:"group_id"`
	TS      float64 `json:"ts"`
}

// TodusPollResult contiene los mensajes y acuses pendientes del sidecar.
type TodusPollResult struct {
	Messages []TodusMessage
	Receipts []TodusReceipt
}

// Poll devuelve los mensajes y acuses pendientes del inbox y los vacía en el sidecar.
func (c *TodusHTTPClient) Poll(ctx context.Context) (*TodusPollResult, error) {
	payload := map[string]string{"phone": c.phone}
	raw, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/poll", bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("poll: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("poll: HTTP %d", resp.StatusCode)
	}
	var out struct {
		Messages []TodusMessage `json:"messages"`
		Receipts []TodusReceipt `json:"receipts"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return &TodusPollResult{Messages: out.Messages, Receipts: out.Receipts}, nil
}

// Health comprueba que el sidecar responde.
func (c *TodusHTTPClient) Health(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/health", nil)
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("health: HTTP %d", resp.StatusCode)
	}
	return nil
}

// EnsureLogin comprueba /health y, si el teléfono no está logueado en el sidecar,
// llama a /login con el token largo guardado. Sirve para recuperar la sesión
// tras reiniciar el sidecar (que pierde el estado en memoria).
// EnsureLogin reintenta durante un rato: al arrancar el sistema, el puente
// puede llegar antes de que el sidecar abra el puerto.
func (c *TodusHTTPClient) EnsureLogin(ctx context.Context) error {
	var err error
	for attempt := 1; attempt <= 10; attempt++ {
		err = c.ensureLoginOnce(ctx)
		if err == nil {
			return nil
		}
		if attempt == 10 {
			break
		}
		select {
		case <-ctx.Done():
			return err
		case <-time.After(2 * time.Second):
		}
	}
	return err
}

func (c *TodusHTTPClient) ensureLoginOnce(ctx context.Context) error {
	// 1) Consultar /health
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/health", nil)
	if err != nil {
		return err
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("health: %w", err)
	}
	var health struct {
		Sessions map[string]struct {
			LoggedIn  bool `json:"logged_in"`
			Listening bool `json:"listening"`
		} `json:"sessions"`
	}
	decodeErr := json.NewDecoder(resp.Body).Decode(&health)
	resp.Body.Close()
	if decodeErr != nil {
		return fmt.Errorf("health decode: %w", decodeErr)
	}

	if s, ok := health.Sessions[c.phone]; ok && s.LoggedIn {
		return nil // ya está logueado
	}

	// 2) No está logueado: llamar a /login con el token largo
	payload := map[string]string{"phone": c.phone, "token": c.token}
	raw, _ := json.Marshal(payload)
	req, err = http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/login", bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err = c.http.Do(req)
	if err != nil {
		return fmt.Errorf("login: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("login: HTTP %d", resp.StatusCode)
	}
	return nil
}

// TodusContact representa la presencia observada de un contacto.
// Name/AvatarURL se aprenden de las tarjetas <contact> vistas en mensajes.
type TodusContact struct {
	Phone     string  `json:"phone"`
	Name      string  `json:"name"`
	AvatarURL string  `json:"avatar_url"`
	Status    string  `json:"status"`
	Show      string  `json:"show"`
	Priority  string  `json:"priority"`
	UpdatedAt float64 `json:"updated_at"`
}

// Contacts pide al sidecar la lista de contactos con su última presencia.
func (c *TodusHTTPClient) Contacts(ctx context.Context, contacts []string) ([]TodusContact, error) {
	payload := map[string]any{"phone": c.phone, "contacts": contacts}
	var out struct {
		Contacts []TodusContact `json:"contacts"`
	}
	if err := c.postJSON(ctx, "/contacts", payload, &out); err != nil {
		return nil, err
	}
	return out.Contacts, nil
}

// TodusGroup es un grupo MUC Light de ToDus.
type TodusGroup struct {
	ID               string `json:"id"`
	Name             string `json:"name"`
	Subject          string `json:"subject"`
	Members          string `json:"members"`
	Role             string `json:"role"`
	Picture          string `json:"picture"`
	PictureThumbnail string `json:"picture_thumbnail_url"`
}

// Groups lista los grupos a los que pertenece la cuenta.
func (c *TodusHTTPClient) Groups(ctx context.Context) ([]TodusGroup, error) {
	var out struct {
		Groups []TodusGroup `json:"groups"`
	}
	if err := c.postJSON(ctx, "/groups", map[string]string{"phone": c.phone}, &out); err != nil {
		return nil, err
	}
	return out.Groups, nil
}

// TodusGroupMember es un miembro de un grupo con su rol.
type TodusGroupMember struct {
	Phone string `json:"phone"`
	Role  string `json:"role"`
}

// GroupInfo devuelve nombre, asunto y número de miembros de un grupo.
func (c *TodusHTTPClient) GroupInfo(ctx context.Context, groupID string) (*TodusGroup, error) {
	out := &TodusGroup{}
	err := c.postJSON(ctx, "/group_info", map[string]string{
		"phone": c.phone, "group_id": groupID,
	}, out)
	if err != nil {
		return nil, err
	}
	return out, nil
}

// GroupMembers devuelve la lista de miembros del grupo.
func (c *TodusHTTPClient) GroupMembers(ctx context.Context, groupID string) ([]TodusGroupMember, error) {
	var out struct {
		Members []TodusGroupMember `json:"members"`
	}
	err := c.postJSON(ctx, "/group_members", map[string]string{
		"phone": c.phone, "group_id": groupID,
	}, &out)
	if err != nil {
		return nil, err
	}
	return out.Members, nil
}

// SendGroupMessage envía un mensaje de texto a un grupo MUC Light.
func (c *TodusHTTPClient) SendGroupMessage(ctx context.Context, groupID, body, replyTo string) (string, error) {
	var out struct {
		MsgID string `json:"msg_id"`
	}
	payload := map[string]string{
		"phone": c.phone, "group_id": groupID, "message": body,
	}
	if replyTo != "" {
		payload["reply_to"] = replyTo
	}
	err := c.postJSON(ctx, "/send_group_message", payload, &out)
	if err != nil {
		return "", err
	}
	return out.MsgID, nil
}

// SendChannelMessage publica un mensaje de texto en un canal de ToDus.
// El protocolo (v1.10.2) usa: IQ set a `<channel_id>@ch` con
// `<query xmlns='todus:ch:publish' publ='<message><body>...</body></message>'/>`.
func (c *TodusHTTPClient) SendChannelMessage(ctx context.Context, channelID, body string) (string, error) {
	var out struct {
		MsgID string `json:"msg_id"`
	}
	err := c.postJSON(ctx, "/send_channel_message", map[string]string{
		"phone": c.phone, "channel_id": channelID, "message": body,
	}, &out)
	if err != nil {
		return "", err
	}
	return out.MsgID, nil
}

// ChannelPublications devuelve las publicaciones (getpub) de un canal con
// `last_id` mayor/igual (high-water mark). Se usa para backfill de canales.
func (c *TodusHTTPClient) ChannelPublications(ctx context.Context, channelID, lastID string) ([]TodusPublication, error) {
	var out struct {
		Publications []TodusPublication `json:"publications"`
	}
	err := c.postJSON(ctx, "/channel_publications", map[string]string{
		"phone": c.phone, "channel_id": channelID, "last_id": lastID,
	}, &out)
	if err != nil {
		return nil, err
	}
	return out.Publications, nil
}

// ChannelInfo devuelve nombre/info de un canal (por id o link).
func (c *TodusHTTPClient) ChannelInfo(ctx context.Context, channelID, link string) (*TodusChannel, error) {
	var out struct {
		Channel TodusChannel `json:"channel"`
	}
	err := c.postJSON(ctx, "/channel_info", map[string]string{
		"phone": c.phone, "channel_id": channelID, "link": link,
	}, &out)
	if err != nil {
		return nil, err
	}
	if out.Channel.ID == "" {
		out.Channel.ID = channelID
	}
	return &out.Channel, nil
}

// TodusChannel es un canal suscrito.
type TodusChannel struct {
	ID               string `json:"id"`
	Name             string `json:"name"`
	Link             string `json:"link"`
	Public           string `json:"public"`
	Picture          string `json:"picture"`
	PictureThumbnail string `json:"picture_thumbnail_url"`
}

// Channels lista los canales suscritos por la cuenta.
func (c *TodusHTTPClient) Channels(ctx context.Context) ([]TodusChannel, error) {
	var out struct {
		Channels []TodusChannel `json:"channels"`
	}
	if err := c.postJSON(ctx, "/channels", map[string]string{"phone": c.phone}, &out); err != nil {
		return nil, err
	}
	return out.Channels, nil
}

// postJSON es el helper común para los endpoints del sidecar.
func (c *TodusHTTPClient) postJSON(ctx context.Context, path string, payload any, out any) error {
	raw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return fmt.Errorf("%s: HTTP %d: %s", path, resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// DownloadMedia pide al sidecar que descargue un adjunto de ToDus y devuelve
// los bytes. El sidecar resuelve la URL firmada y aplica las cabeceras de la
// app oficial; aqui solo hacemos HTTP local.
func (c *TodusHTTPClient) DownloadMedia(ctx context.Context, url string) ([]byte, error) {
	payload := map[string]string{"phone": c.phone, "url": url}
	raw, _ := json.Marshal(payload)
	var lastErr error
	// El sidecar puede estar reiniciándose justo cuando llega un adjunto (por
	// ejemplo tras un crash del listener). Un único EOF provoca el aviso de
	// "An error occurred while processing an incoming message" en Beeper.
	// Reintentamos con backoff corto antes de rendirnos.
	for attempt := 1; attempt <= 3; attempt++ {
		client := &http.Client{Timeout: 180 * time.Second}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/media", bytes.NewReader(raw))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("media: %w", err)
		} else {
			body, readErr := io.ReadAll(resp.Body)
			resp.Body.Close()
			switch {
			case readErr != nil:
				lastErr = fmt.Errorf("media read: %w", readErr)
			case resp.StatusCode >= 400:
				lastErr = fmt.Errorf("media: HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
			case len(body) == 0:
				lastErr = fmt.Errorf("media: respuesta vacia")
			default:
				return body, nil
			}
		}
		if attempt < 3 {
			select {
			case <-ctx.Done():
				return nil, lastErr
			case <-time.After(time.Duration(attempt) * time.Second):
			}
		}
	}
	return nil, lastErr
}

// TodusMediaRequest es el payload para POST /send_media del sidecar.
type TodusMediaRequest struct {
	Phone    string `json:"phone"`
	To       string `json:"to"`
	Data     string `json:"data"` // base64 de los bytes del archivo
	FileName string `json:"file_name"`
	MimeType string `json:"mime_type"`
	Caption  string `json:"caption"`
	Kind     string `json:"kind"`
	Width    int    `json:"width"`
	Height   int    `json:"height"`
	Duration int    `json:"duration"`
	IsGroup  bool   `json:"is_group"`
}

// TodusMediaResponse es la respuesta de POST /send_media.
type TodusMediaResponse struct {
	Status string `json:"status"`
	MsgID  string `json:"msg_id"`
	Acked  bool   `json:"acked"`
	URL    string `json:"url"`
}

// SendMedia sube el archivo a ToDus y envia el stanza de adjunto via sidecar.
func (c *TodusHTTPClient) SendMedia(ctx context.Context, req TodusMediaRequest) (*TodusMediaResponse, error) {
	req.Phone = c.phone
	raw, _ := json.Marshal(req)
	client := &http.Client{Timeout: 400 * time.Second}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/send_media", bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("send_media: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("send_media: HTTP %d: %s", resp.StatusCode, string(body))
	}
	var out TodusMediaResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("send_media decode: %w", err)
	}
	return &out, nil
}
