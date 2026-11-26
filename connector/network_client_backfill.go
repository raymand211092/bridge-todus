package connector

// Backfill NO está soportado para ToDus:
//   - la API de ToDus no expone historial de mensajes antiguos (ni DMs ni
//     grupos), y reenviar publicaciones de canales se maneja en
//     network_client.go / todus_client.go.
//   - el stub anterior inyectaba 3 mensajes falsos ("Backfilled history
//     (placeholder)") de un ghost inexistente y los marcaba como LEÍDOS
//     (MarkRead: true). Eso hacía que cada sala nueva (sobre todo los DMs)
//     apareciera con el mensaje ya leído en cuanto llegaba.
//
// Al no implementar bridgev2.BackfillingNetworkAPI, el puente sabe que no
// hay backfill y no marca nada como leído al crear un portal.
