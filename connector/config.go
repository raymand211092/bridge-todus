package connector

// TodusConfig contiene las opciones de configuración específicas de la red ToDus.
type TodusConfig struct {
	SidecarURL string `yaml:"sidecar_url"`
}

func NewTodusConfig() *TodusConfig {
	return &TodusConfig{
		SidecarURL: "http://127.0.0.1:8765",
	}
}
