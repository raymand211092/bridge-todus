package connector

import (
	"testing"
	"time"
)

func TestOfflineEventTimestampMs(t *testing.T) {
	got := offlineEventTimestamp(TodusMessage{OfflineTS: "1793625803709"})
	want := time.Date(2026, 11, 2, 13, 23, 23, 709000000, time.UTC)
	if !got.Equal(want) {
		t.Fatalf("ms: got %s want %s", got.UTC(), want)
	}
}

func TestOfflineEventTimestampSeconds(t *testing.T) {
	got := offlineEventTimestamp(TodusMessage{OfflineTS: "1793625803"})
	want := time.Date(2026, 11, 2, 13, 23, 23, 0, time.UTC)
	if !got.Equal(want) {
		t.Fatalf("s: got %s want %s", got.UTC(), want)
	}
}

func TestOfflineEventTimestampEmpty(t *testing.T) {
	before := time.Now().Add(-2 * time.Second)
	got := offlineEventTimestamp(TodusMessage{})
	if got.Before(before) {
		t.Fatalf("empty: got %s, deberia ser ~ahora", got)
	}
}

func TestOfflineEventTimestampInvalid(t *testing.T) {
	before := time.Now().Add(-2 * time.Second)
	got := offlineEventTimestamp(TodusMessage{OfflineTS: "abc"})
	if got.Before(before) {
		t.Fatalf("invalid: got %s, deberia ser ~ahora", got)
	}
}
