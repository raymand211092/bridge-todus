package main

import (
	"maunium.net/go/mautrix/bridgev2/matrix/mxmain"

	"github.com/raymand211092/bridge-todus/connector"
)

var (
	Tag       = "unknown"
	Commit    = "unknown"
	BuildTime = "unknown"
)

func main() {
	connector := &connector.MyConnector{}

	m := mxmain.BridgeMain{
		Name:        "todus",
		Description: "A Matrix bridge to ToDus.",
		Version:     "0.1.0",
		URL:         "https://todus.cu",
		Connector:   connector,
	}

	m.InitVersion(Tag, Commit, BuildTime)
	m.Run()
}
