package connector

import (
	"strconv"
	"time"
)

// offlineEventTimestamp devuelve la hora real del evento remoto. Si el mensaje
// viene de la cola offline de ToDus (<todus_offline ts='...'>), usa esa hora
// original para que en Matrix aparezca con su fecha correcta; si no, usa la
// hora actual.
func offlineEventTimestamp(m TodusMessage) time.Time {
	if m.OfflineTS == "" {
		return time.Now()
	}
	n, err := strconv.ParseInt(m.OfflineTS, 10, 64)
	if err != nil || n <= 0 {
		return time.Now()
	}
	if n > 1e12 { // milisegundos
		return time.UnixMilli(n)
	}
	return time.Unix(n, 0) // segundos
}
