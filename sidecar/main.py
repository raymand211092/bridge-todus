"""
Sidecar HTTP para ToDus. El puente Go (mautrix-todus) llama a estos endpoints
para autenticar y enviar/recibir mensajes vía todus-sdk (versión 1.10.2).
"""
import base64
import json
import logging
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import unescape as xml_unescape

from todus import ToDusClient, ToDusClientWithQueue
from todus import stanza as todus_stanza
from todus.errors import TokenExpiredError
from todus.stanzas.channels import get_my_channels_iq
from todus.stanzas.private import message as private_message_stanza
from todus.types import FileType

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("todus-sidecar")

app = FastAPI(title="todus-sidecar")

# --- Configuración por variables de entorno ---
TODUS_PROXY = os.environ.get("TODUS_PROXY") or None
QUEUE_DB_DIR = os.environ.get("TODUS_QUEUE_DIR", "/opt/todus-service/queues")
os.makedirs(QUEUE_DB_DIR, exist_ok=True)

# --- Estado en memoria ---
_state: Dict[str, Dict[str, Any]] = {}
_inbox: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
_inbox_lock = threading.Lock()
_last_stanza: Dict[str, Dict[str, Any]] = {}

# Acuses de envio (tdack) confirmados por el servidor ToDus:
# phone -> {msg_id: timestamp}. /send_message espera el tdack antes de dar el
# envio por bueno; si no llega, la fila de la cola vuelve a 'pending' para que
# el worker de reintentos del SDK lo reenvie.
_send_acks: Dict[str, Dict[str, float]] = defaultdict(dict)
_send_acks_lock = threading.Lock()
_SEND_ACK_TIMEOUT = 8.0

# Presencia observada: owner_phone -> {contact_phone: {status, show, priority, updated_at}}
_presence: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
# Nombres/fotos aprendidos de tarjetas <contact> dentro de mensajes:
# owner_phone -> {contact_phone: {"name": ..., "avatar_url": ...}}
_contact_profiles: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
# Peticiones IQ pendientes: "phone:msg_id" -> {"event": Event, "stanza": dict|None}
_iq_waiters: Dict[str, Dict[str, Any]] = {}

# El servidor de ToDus devuelve a veces INTERNAL_SERVER_ERROR en info_by_id.
# Cacheamos los metadatos para no repetir la consulta en cada mensaje/sala.
_GROUP_CACHE_TTL = 600.0
_group_info_cache: Dict[str, Dict[str, Any]] = {}
_group_members_cache: Dict[str, Dict[str, Any]] = {}
_channel_cache: Dict[str, Dict[str, Any]] = {}
# Acuses (entrega/lectura/ack) recibidos, pendientes de entregar al puente.
_receipts: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
# El servidor de ToDus reenvia el MISMO acuse decenas de veces (con distinto
# id externo). Guardamos una huella por acuse para no encolar duplicados:
# antes, un solo "leido" generaba decenas de eventos MSS identicos.
_receipt_seen: Dict[str, float] = {}
_receipt_seen_ttl = 900.0
# Serializa los envíos al socket XMPP compartido (evita tramas intercaladas).
_send_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
# El servidor de ToDus se degrada si recibe muchos IQ a la vez por la misma
# sesión: serializamos y dejamos un pequeño espacio entre peticiones.
_iq_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)

# --- Vigilancia de conexión (watchdog) ---
# Última vez que se recibió CUALQUIER stanza de la sesión. Si el servidor
# deja de responder (red caída sin cerrar el socket — caso 2026-11-18) el
# listener queda "vivo" pero sordo; el watchdog hace ping y fuerza reconexión.
_last_inbound: Dict[str, float] = {}
_watchdogs: Dict[str, threading.Event] = {}
_group_presence_threads: Dict[str, threading.Thread] = {}
_group_presence_state: Dict[str, Any] = {}
_WATCHDOG_INTERVAL = 60.0
_WATCHDOG_IDLE_LIMIT = 240.0
_WATCHDOG_PING_TIMEOUT = 15.0
_iq_last_at: Dict[str, float] = {}
_receipt_limit = 500
# Un intento por mensaje: el servidor de ToDus se degrada si lo martillamos.
_group_members_negative_ttl = 30.0
# Canales que el puente esta observando (para backfill de publicaciones).
_watched_channels: Dict[str, Any] = defaultdict(set)
# High-water marks de publicaciones ya entregados por canal: "phone:channel_id" -> {"last_id": str, "done": [pub ids]}
_channel_seen: Dict[str, Dict[str, Any]] = {}

# Directorio donde se persisten los tokens largos, para restaurar la sesión
# automáticamente cuando el sidecar se reinicia (o la máquina arranca).
TOKEN_DIR = os.environ.get("TODUS_TOKEN_DIR", "/opt/todus-service/tokens")
os.makedirs(TOKEN_DIR, exist_ok=True)

# Persistencia del inbox: un mensaje aceptado se escribe a disco ANTES de
# entrar en _inbox. Así, si el sidecar se reinicia (crash, systemd, etc.) antes
# de que el puente lo consuma con /poll, no se pierde. /poll borra los archivos
# bajo el mismo _inbox_lock con el que se escriben, para no hayar carreras.
INBOX_DIR = os.environ.get("TODUS_INBOX_DIR", "/opt/todus-service/inbox")
os.makedirs(INBOX_DIR, exist_ok=True)


# ---------- Modelos ----------
class RequestCodeBody(BaseModel):
    phone: str


class ValidateCodeBody(BaseModel):
    phone: str
    code: str


class LoginBody(BaseModel):
    phone: str
    token: Optional[str] = None


class SendMessageBody(BaseModel):
    phone: str
    to: str
    message: str
    reply_to: Optional[str] = ""


class SendGroupBody(BaseModel):
    phone: str
    group_id: str
    message: str
    reply_to: Optional[str] = ""


class PollBody(BaseModel):
    phone: str


class ContactsBody(BaseModel):
    phone: str
    contacts: Optional[List[str]] = None


class GroupInfoBody(BaseModel):
    phone: str
    group_id: str


class DebugIQBody(BaseModel):
    phone: str
    iq: str
    timeout: Optional[float] = 8.0


class MediaBody(BaseModel):
    phone: str
    url: str


class SendChannelMessageBody(BaseModel):
    phone: str
    channel_id: str
    message: str


class ChannelPublicationsBody(BaseModel):
    phone: str
    channel_id: str
    last_id: str = "0"


class ChannelInfoBody(BaseModel):
    phone: str
    channel_id: str
    link: str = ""


# ---------- Helpers de token ----------
def _token_path(phone: str) -> str:
    return os.path.join(TOKEN_DIR, phone + ".token")


def _save_token(phone: str, token: str) -> None:
    path = _token_path(phone)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    log.info("[%s] token largo persistido en %s", phone, path)


def _drop_token(phone: str) -> None:
    try:
        os.remove(_token_path(phone))
    except FileNotFoundError:
        pass


# ---------- Helpers ----------
def _jid_phone(jid: str) -> str:
    """Saca el teléfono de un JID tipo '535...@im.todus.cu/recurso'."""
    return (jid or "").split("@")[0].split("/")[0].lstrip("+").strip()


def _inbox_dir(phone: str) -> str:
    d = os.path.join(INBOX_DIR, re.sub(r"[^0-9A-Za-z_]", "_", phone))
    os.makedirs(d, exist_ok=True)
    return d


def _persist_inbox_msg(phone: str, msg: Dict[str, Any]) -> None:
    """Escribe un mensaje aceptado a disco (llamado bajo _inbox_lock)."""
    try:
        d = _inbox_dir(phone)
        path = os.path.join(d, f"{time.time_ns()}-{uuid.uuid4().hex}.json")
        tmp = path + ".tmp"
        # Guardar la clave real del teléfono: el nombre del directorio está
        # sanitizado ("+535..." -> "_535...") y /poll usa la clave original.
        payload = dict(msg)
        payload["_persist_phone"] = phone
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        log.exception("[%s] no se pudo persistir mensaje en inbox", phone)


def _clear_persisted_inbox(phone: str) -> None:
    """Borra los mensajes ya entregados a /poll (bajo _inbox_lock)."""
    try:
        d = _inbox_dir(phone)
        for name in os.listdir(d):
            if name.endswith(".json") or name.endswith(".tmp"):
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass
    except Exception:
        log.exception("[%s] no se pudo limpiar inbox persistido", phone)


def _load_persisted_inbox() -> None:
    """Recupera al arrancar los mensajes que /poll no llegó a entregar."""
    loaded = 0
    try:
        for name in sorted(os.listdir(INBOX_DIR)):
            d = os.path.join(INBOX_DIR, name)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                        msg = json.load(f)
                    real_phone = msg.pop("_persist_phone", None) or name
                    _inbox[real_phone].append(msg)
                    loaded += 1
                    log.info("[%s] mensaje persistido recuperado: id=%s",
                             real_phone, msg.get("id"))
                except Exception:
                    log.exception("no se pudo cargar mensaje persistido %s/%s", name, fn)
    except Exception:
        log.exception("no se pudo restaurar inbox persistido")
    if loaded:
        log.info("inbox persistido: recuperados %d mensajes", loaded)


def _make_callback(phone: str):
    def cb(msg: dict):
        # Guardar SIEMPRE el último stanza para debug
        _last_stanza[phone] = msg
        _last_inbound[phone] = time.time()

        # Respuestas IQ pendientes (grupos, canales, etc.)
        mid = msg.get("id") or ""
        if mid:
            waiter = _iq_waiters.get(f"{phone}:{mid}")
            if waiter is not None:
                waiter["stanza"] = msg
                waiter["event"].set()

        # Acuse de envio del servidor (tdack / ack): confirmacion de que el
        # mensaje llego al servidor de ToDus. No es un mensaje para el inbox;
        # se registra para /send_message y se corta aqui.
        mtype = msg.get("type") or ""
        if mtype in ("tdack", "ack"):
            ack_mid = msg.get("message_id") or msg.get("id") or ""
            if ack_mid:
                now = time.time()
                with _send_acks_lock:
                    acks = _send_acks[phone]
                    acks[str(ack_mid)] = now
                    if len(acks) > 2000:
                        for k in [k for k, ts in acks.items() if now - ts > 3600.0]:
                            acks.pop(k, None)
                log.info("[%s] TDACK servidor id=%s", phone, ack_mid)
            return

        # Mensajes de grupo MUC Light:
        # from = "grupo@muclight.im.todus.cu/remitente@im.todus.cu"
        from_jid = msg.get("from") or ""
        if "@muclight.im.todus.cu" in from_jid:
            group_part, _, sender_part = from_jid.partition("/")
            msg["is_group"] = True
            msg["group_id"] = group_part.split("@")[0]
            msg["sender_phone"] = _jid_phone(sender_part) if sender_part else ""

        # Mensajes de canal (publicaciones). El JID del canal viaja en `from`
        # o `to`, ej. from = "2120932612213051392@ch".
        from_jid = msg.get("from") or ""
        to_jid = msg.get("to") or ""
        if "@ch" in from_jid or "@ch" in to_jid:
            chan_part = from_jid.split("@ch")[0] if "@ch" in from_jid else to_jid.split("@ch")[0]
            msg["is_channel"] = True
            msg["channel_id"] = chan_part
            msg["channel_link"] = msg.get("channel_link") or ""
            msg["pub_by"] = msg.get("pub_by") or msg.get("publisher") or ""
            msg["is_group"] = False
            msg["group_id"] = ""

        # Acuses de entrega/lectura (dd/rd) y ack del servidor (ak).
        # No llevan body, pero son la unica forma de marcar "entregado" en
        # grupos (bridgev2 solo enruta receipts de entrega en DMs).
        receipt_id = msg.get("receipt") or ""
        if receipt_id:
            rec = {
                "id": str(receipt_id),
                "type": msg.get("receipt_type") or "delivered",
                "from": msg.get("sender_phone") or _jid_phone(msg.get("from") or ""),
                "group_id": msg.get("group_id") or "",
                "ts": time.time(),
                "raw": msg.get("raw") or "",
            }
            fingerprint = f"{phone}:{rec['id']}:{rec['type']}:{rec['from']}"
            now = time.time()
            with _inbox_lock:
                last = _receipt_seen.get(fingerprint, 0.0)
                if now - last < _receipt_seen_ttl:
                    duplicate = True
                else:
                    duplicate = False
                    _receipt_seen[fingerprint] = now
                    # Poda barata para que el diccionario no crezca sin limite.
                    if len(_receipt_seen) > 2000:
                        cutoff = now - _receipt_seen_ttl
                        for key in [k for k, ts in _receipt_seen.items() if ts < cutoff]:
                            _receipt_seen.pop(key, None)
                if not duplicate:
                    pending = _receipts[phone]
                    pending.append(rec)
                    if len(pending) > _receipt_limit:
                        del pending[: len(pending) - _receipt_limit]
            if duplicate:
                log.debug("[%s] RECEIPT duplicado ignorado %s id=%r from=%r",
                          phone, rec["type"], rec["id"], rec["from"])
                return
            log.info("[%s] RECEIPT %s id=%r from=%r group=%r raw=%r",
                     phone, rec["type"], rec["id"], rec["from"], rec["group_id"],
                     (rec["raw"] or "")[:400])
            return

        ack_id = msg.get("ack") or ""
        if ack_id:
            log.info("[%s] ACK id=%r from=%r", phone, ack_id, msg.get("from"))
            return

        # Presencia de contactos (stanzas <p ...>)
        if "priority" in msg or "show" in msg or "status" in msg:
            from_phone = _jid_phone(msg.get("from") or "")
            if from_phone:
                _presence[phone][from_phone] = {
                    "status": msg.get("status") or "",
                    "show": msg.get("show") or "",
                    "priority": msg.get("priority") or "",
                    "updated_at": time.time(),
                }
                log.debug("[%s] presencia de %s: %s/%s", phone, from_phone,
                          msg.get("status"), msg.get("show"))
            return

        # Aprender nombre del contacto de las tarjetas <contact> adjuntas.
        # Es la única fuente automática de nombres de DM que ofrece ToDus.
        contact_phone = str(msg.get("contact_phone") or "").strip()
        contact_name = str(msg.get("contact_name") or "").strip()
        if contact_phone and contact_name:
            entry = _contact_profiles[phone].setdefault(contact_phone, {})
            if entry.get("name") != contact_name:
                entry["name"] = contact_name
                log.info("[%s] nombre de contacto aprendido: %s -> %s",
                         phone, contact_phone, contact_name)

        body = msg.get("body") or ""
        msg_type = (msg.get("type") or "").lower()

        # Los adjuntos pueden llegar con <b/> vacío: el cuerpo vive en <file>,
        # <image>, <video>, <voice> o <gif>. No los descartamos.
        media_keys = (
            "url", "video_url", "voice_url", "gif_url", "file_name",
            "image_thumbnail", "video_thumbnail", "gif_thumbnail",
        )
        has_media = any(str(msg.get(k) or "") for k in media_keys)
        if not body and not has_media:
            log.debug("[%s] stanza sin body ni media ignorado: type=%r", phone, msg_type)
            return

        media_info = {k: str(msg.get(k)) for k in (
            "url", "file_name", "file_size", "video_url", "voice_url",
            "gif_url", "sticker_id", "image_thumbnail") if msg.get(k)}
        log.info("[%s] MENSAJE ACEPTADO: type=%r from=%r body=%r id=%r reply_to=%r media=%s",
                 phone, msg_type, msg.get("from"), body, msg.get("id"),
                 msg.get("reply_to"), media_info)
        try:
            from todus.stanzas.replycache import register as _reply_register
            _reply_register(
                str(msg.get("id") or ""),
                body=body,
                from_jid=str(msg.get("from") or ""),
                to_jid=str(msg.get("to") or ""),
                ts=float(msg.get("ts") or 0) or time.time(),
                mtype=msg_type or "chat",
            )
        except Exception:
            log.debug("no se pudo registrar reply info", exc_info=True)
        # Solo las respuestas/reenvíos se registran completos (diagnóstico).
        raw_dbg = str(msg.get("raw") or "")
        if any(k in raw_dbg.lower() for k in ("forwarded", "resend", "quote")):
            log.info("[%s] STANZA RESPUESTA raw=%r", phone, raw_dbg[:4000])
        log.debug("[%s] estructura completa: %s", phone, msg)
        with _inbox_lock:
            _persist_inbox_msg(phone, msg)
            _inbox[phone].append(msg)
    return cb


def _new_queue_client(phone: str, long_token: str) -> ToDusClientWithQueue:
    return ToDusClientWithQueue(
        phone,
        password=long_token,
        enable_queue=True,
        queue_db_path=os.path.join(QUEUE_DB_DIR, f"{phone}.db"),
        proxy=TODUS_PROXY,
    )


def _ping_session(phone: str, timeout: float = _WATCHDOG_PING_TIMEOUT) -> bool:
    """Manda un XMPP ping y espera cualquier respuesta de la sesión."""
    entry = _state.get(phone) or {}
    client = entry.get("client")
    if client is None:
        return False
    mid = uuid.uuid4().hex
    iq_xml = f"<iq type='get' i='{mid}'><ping xmlns='urn:xmpp:ping'/></iq>"
    waiter: Dict[str, Any] = {"event": threading.Event(), "stanza": None}
    key = f"{phone}:{mid}"
    _iq_waiters[key] = waiter
    try:
        client.send_stanza(iq_xml)
        return bool(waiter["event"].wait(timeout))
    except Exception as e:
        log.debug("[%s] watchdog: ping falló: %s", phone, e)
        return False
    finally:
        _iq_waiters.pop(key, None)


def _force_reconnect(phone: str) -> None:
    """Cierra el socket compartido para que el listener detecte la caída y
    vuelva a conectar (listen_messages tiene backoff propio)."""
    entry = _state.get(phone) or {}
    client = entry.get("client")
    sock = getattr(client, "_shared_sock", None) if client is not None else None
    if sock is None:
        return
    raw = getattr(sock, "_sock", None)
    try:
        if raw is not None:
            raw.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        if raw is not None:
            raw.close()
    except Exception:
        pass


def _shared_sock_ready(phone: str) -> bool:
    """True si el listener tiene un socket XMPP activo y compartido."""
    entry = _state.get(phone) or {}
    client = entry.get("client")
    if client is None:
        return False
    try:
        return bool(client._shared_sock_active.is_set() and client._shared_sock is not None)
    except Exception:
        return False


def _ensure_listener(phone: str, timeout: float = 40.0) -> bool:
    """Espera a que el listener tenga sesion activa antes de enviar.

    Enviar por una sesion de respaldo mientras el listener reconecta compite
    con su handshake; el servidor solo permite una sesion por JID y tumba la
    conexion, lo que provoca un bucle de reconexiones y mensajes perdidos.
    """
    if _shared_sock_ready(phone):
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.25)
        if _shared_sock_ready(phone):
            return True
    return False


def _start_watchdog(phone: str) -> None:
    if phone in _watchdogs:
        return
    ev = threading.Event()
    _watchdogs[phone] = ev

    def run():
        _last_inbound.setdefault(phone, time.time())
        while not ev.is_set():
            ev.wait(_WATCHDOG_INTERVAL)
            if ev.is_set():
                break
            entry = _state.get(phone) or {}
            se = entry.get("stop_event")
            if se is None or se.is_set():
                break
            idle = time.time() - _last_inbound.get(phone, time.time())
            if idle < _WATCHDOG_IDLE_LIMIT:
                continue
            if _ping_session(phone):
                _last_inbound[phone] = time.time()
                log.info("[%s] watchdog: sesión OK tras %.0fs sin tráfico", phone, idle)
                continue
            log.warning("[%s] watchdog: %.0fs sin respuesta; forzando reconexión XMPP", phone, idle)
            _force_reconnect(phone)
            # Dar margen para que el listener reconecte antes de volver a probar.
            _last_inbound[phone] = time.time()

    threading.Thread(target=run, name=f"todus-watchdog-{phone}", daemon=True).start()
    log.info("[%s] watchdog iniciado", phone)


def _list_group_ids(phone: str) -> List[str]:
    """Lista los IDs de los grupos del usuario."""
    resp = _send_iq_retry(
        phone,
        lambda: todus_stanza.group_my_groups_iq(index=0, limit=50),
        attempts=2,
        timeout=8.0,
    ) or {}
    raw = resp.get("raw") or ""
    out: List[str] = []
    seen = set()
    for attrs in _iq_items(raw):
        gid = _first(attrs, "id", "room", "jid", "group") or attrs.get("_text", "")
        if not gid:
            continue
        gid = gid.split("@")[0]
        if gid in seen:
            continue
        seen.add(gid)
        out.append(gid)
    return out


def _start_group_presence(phone: str) -> None:
    """Mantiene presencia MUC en todos los grupos.

    ToDus (MUC Light) solo entrega los mensajes de un grupo a los recursos que
    enviaron presencia a ese grupo. Sin esto el sidecar solo ve mensajes de
    grupo por la cola offline, nunca en vivo.
    """
    if phone in _group_presence_threads and _group_presence_threads[phone].is_alive():
        return

    def run():
        state = _group_presence_state.setdefault(phone, {})
        while True:
            entry = _state.get(phone) or {}
            client = entry.get("client")
            stop_event = entry.get("stop_event")
            if client is None or stop_event is None or stop_event.is_set():
                state["sock_id"] = None
                time.sleep(10)
                continue
            if not _shared_sock_ready(phone):
                time.sleep(5)
                continue
            try:
                sock_id = id(getattr(client, "_shared_sock", None))
                now = time.time()
                groups = state.get("groups") or []
                if (state.get("sock_id") != sock_id
                        or now - state.get("fetched_at", 0) > 900
                        or not groups):
                    groups = _list_group_ids(phone)
                    state["groups"] = groups
                    state["fetched_at"] = now
                    # Socket nuevo = el servidor olvidó las presencias MUC.
                    state["sock_id"] = sock_id
                    state["joined"] = set()
                joined = state.setdefault("joined", set())
                for gid in groups:
                    if gid in joined:
                        continue
                    try:
                        client.groups.join(gid)
                        joined.add(gid)
                        time.sleep(0.05)
                    except Exception:
                        log.warning("[%s] no se pudo unir al grupo %s",
                                    phone, gid, exc_info=True)
                        time.sleep(0.5)
                log.info("[%s] presencia MUC en %d/%d grupos",
                         phone, len(joined), len(groups))
            except Exception:
                log.exception("[%s] fallo manteniendo presencia de grupos", phone)
            time.sleep(60)

    t = threading.Thread(target=run, name=f"todus-groups-{phone}", daemon=True)
    _group_presence_threads[phone] = t
    t.start()


def _start_listener(phone: str, client: ToDusClientWithQueue, long_token: str) -> threading.Event:
    stop_event = threading.Event()

    def run():
        nonlocal client
        while not stop_event.is_set():
            try:
                log.info("[%s] listener arrancado (usando JWT de sesión)", phone)
                client.listen_messages(
                    client.token,
                    _make_callback(phone),
                    stop_event=stop_event,
                    max_retries=0,
                    base_backoff=3.0,
                    max_backoff=20.0,
                )
            except TokenExpiredError:
                if stop_event.is_set():
                    break
                log.warning("[%s] Token JWT expirado, reautenticando con token largo...", phone)
                try:
                    new_client = _new_queue_client(phone, long_token)
                    new_client.login()
                    _ = new_client.groups  # activa el enriquecido de mensajes de grupo
                    _state[phone]["client"] = new_client
                    client = new_client
                    log.info("[%s] Reautenticación exitosa, JWT renovado", phone)
                except Exception as e:
                    log.exception("[%s] Falló la reautenticación: %s", phone, e)
                    stop_event.wait(30)
            except Exception as e:
                if stop_event.is_set():
                    break
                log.exception("[%s] listener terminó con error inesperado, reintentando en 15s: %s", phone, e)
                stop_event.wait(15)
        log.info("[%s] listener detenido", phone)

    t = threading.Thread(target=run, name=f"todus-listen-{phone}", daemon=True)
    t.start()
    _start_group_presence(phone)
    return stop_event


def _send_iq_and_wait(phone: str, iq_xml: str, timeout: float = 12.0) -> Optional[dict]:
    """Envía un IQ y espera su respuesta (que llega por el listener).

    Los IQ de una misma sesión se serializan: el servidor de ToDus devuelve
    errores o deja de contestar cuando recibe muchas peticiones concurrentes
    por el socket compartido del listener.
    """
    m = (re.search(r"\bid='([^']+)'", iq_xml)
         or re.search(r'\bid="([^"]+)"', iq_xml)
         or re.search(r"\bi='([^']+)'", iq_xml)
         or re.search(r'\bi="([^"]+)"', iq_xml))
    if not m:
        raise RuntimeError("IQ sin id")
    mid = m.group(1)
    entry = _state.get(phone) or {}
    client = entry.get("client")
    if client is None:
        raise RuntimeError("usuario no logueado")
    if not _ensure_listener(phone, timeout=30.0):
        raise RuntimeError("listener no conectado")

    with _iq_locks[phone]:
        # Espaciado mínimo entre IQ para no saturar el servidor.
        last = _iq_last_at.get(phone, 0.0)
        wait = 0.15 - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        _iq_last_at[phone] = time.time()

        waiter: Dict[str, Any] = {"event": threading.Event(), "stanza": None}
        key = f"{phone}:{mid}"
        _iq_waiters[key] = waiter
        try:
            client.send_stanza(iq_xml)
            waiter["event"].wait(timeout)
            return waiter["stanza"]
        finally:
            _iq_waiters.pop(key, None)


def _send_iq_retry(phone: str, build_iq, attempts: int = 3, timeout: float = 6.0) -> Optional[dict]:
    """Envía un IQ y reintenta si el servidor no contesta (fallo intermitente)."""
    last: Optional[dict] = None
    for attempt in range(1, attempts + 1):
        try:
            resp = _send_iq_and_wait(phone, build_iq(), timeout=timeout)
        except Exception as e:
            log.warning("IQ intento %d/%d falló: %s", attempt, attempts, e)
            resp = None
        if resp and resp.get("raw"):
            return resp
        last = resp
        if attempt < attempts:
            time.sleep(0.6)
    return last


def _iq_items(raw: str) -> List[Dict[str, str]]:
    """Extrae elementos con atributos de la respuesta IQ (tolera XML raro)."""
    if not raw:
        return []
    try:
        root = ET.fromstring(raw.strip())
    except Exception:
        return []
    out: List[Dict[str, str]] = []
    for el in root.iter():
        if el.tag.lower() in ("iq", "query"):
            continue
        attrs = {k: str(v) for k, v in el.attrib.items()}
        text = (el.text or "").strip()
        if text:
            attrs["_text"] = text
        if attrs:
            out.append(attrs)
    return out


def _first(attrs: Dict[str, str], *keys: str) -> str:
    for k in keys:
        v = attrs.get(k)
        if v:
            return v
    return ""


def _iq_query_attrs(raw: str) -> Dict[str, str]:
    try:
        root = ET.fromstring(raw.strip())
    except Exception:
        return {}
    for child in root:
        if child.tag.split("}")[-1] == "query":
            return {k: str(v) for k, v in child.attrib.items()}
    return {}


def _decode_info(raw: str) -> Dict[str, Any]:
    """La info de grupo/canal viene como atributo info=<base64 json>."""
    attrs = _iq_query_attrs(raw)
    payload = attrs.get("info")
    if payload:
        try:
            return json.loads(base64.b64decode(payload).decode("utf-8"))
        except Exception:
            log.warning("no se pudo decodificar info base64")
    return attrs


def _fetch_group_info(phone: str, group_id: str, timeout: float = 6.0) -> tuple[Dict[str, Any], str]:
    cache_key = f"{phone}:{group_id}"
    cached = _group_info_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < _GROUP_CACHE_TTL:
        return cached["info"], cached["raw"]

    group_jid = group_id if "@" in group_id else group_id + "@muclight.im.todus.cu"
    last_raw = ""
    for target in (group_jid, "muclight.im.todus.cu"):
        for attempt in range(2):
            mid = secrets.token_hex(16)
            # El servidor MUC de ToDus SOLO enruta estos IQ con los atributos
            # estandar (type/to); con o=/t= responde "No module is handling
            # this query" o no contesta.
            iq = (
                f"<iq type='get' id='{mid}' i='{mid}' to='{target}'>"
                f"<query xmlns='td:g:info_by_id' room='{group_jid}'/></iq>"
            )
            try:
                resp = _send_iq_and_wait(phone, iq, timeout=timeout) or {}
            except Exception as e:
                log.warning("group_info intento falló: %s", e)
                break
            raw = resp.get("raw") or ""
            if not raw:
                continue
            last_raw = raw
            # A veces el servidor devuelve INTERNAL_SERVER_ERROR; reintentar.
            if "t='error'" in raw or " error='" in raw:
                time.sleep(0.7)
                continue
            info = _decode_info(raw)
            _group_info_cache[cache_key] = {"ts": time.time(), "info": info, "raw": raw}
            return info, raw
    return {}, last_raw


# ---------- Endpoints ----------
@app.get("/health")
async def health():
    # async: no compite por el threadpool de uvicorn con los IQ bloqueantes.
    return {
        "status": "ok",
        "sessions": {
            phone: {
                "logged_in": "client" in s,
                "listening": bool(s.get("stop_event") and not s["stop_event"].is_set()
                                 and _shared_sock_ready(phone)),
                "connected": _shared_sock_ready(phone),
                "inbox_pending": len(_inbox.get(phone, [])),
                "receipts_pending": len(_receipts.get(phone, [])),
            }
            for phone, s in _state.items()
        },
    }


@app.get("/debug_history")
def debug_history(phone: str, to: str, timeout: float = 10.0):
    """Consulta el historial (todus:last:2) de un JID y devuelve la stanza cruda."""
    iq = (f"<iq type='get' id='hist{uuid.uuid4().hex}' to='{to}' "
          f"><query xmlns='todus:last:2'/></iq>")
    resp = _send_iq_and_wait(phone, iq, timeout=timeout)
    return {"ok": bool(resp), "raw": (resp or {}).get("raw", "")}


@app.post("/request_code")
def request_code(body: RequestCodeBody):
    phone = body.phone.strip()
    if not phone:
        raise HTTPException(400, "phone vacío")
    try:
        client = ToDusClient()
        client.request_code(phone)
    except Exception as e:
        log.exception("request_code falló para %s", phone)
        raise HTTPException(500, f"request_code falló: {e}")
    _state.setdefault(phone, {})["pending_client"] = client
    log.info("Código SMS solicitado para %s", phone)
    return {"status": "ok"}


@app.post("/validate_code")
def validate_code(body: ValidateCodeBody):
    phone = body.phone.strip()
    code = body.code.strip()
    if not phone or not code:
        raise HTTPException(400, "phone o code vacíos")

    client: ToDusClient = _state.get(phone, {}).get("pending_client") or ToDusClient()
    try:
        token = client.validate_code(phone, code)
    except Exception as e:
        log.exception("validate_code falló para %s", phone)
        raise HTTPException(500, f"validate_code falló: {e}")

    if not token:
        raise HTTPException(500, "validate_code no devolvió token")

    _state.setdefault(phone, {})["token"] = token
    _save_token(phone, token)
    log.info("Token largo obtenido para %s (len=%d)", phone, len(token))
    return {"status": "ok", "token": token}


@app.post("/login")
def login(body: LoginBody):
    phone = body.phone.strip()
    if not phone:
        raise HTTPException(400, "phone vacío")

    long_token = body.token or _state.get(phone, {}).get("token")
    if not long_token:
        raise HTTPException(400, "no hay token para este teléfono; haz validate_code primero")

    # Si ya había una sesión para este teléfono (p.ej. el puente re-loguea
    # tras un reinicio), paramos su listener antes de crear otro.
    previous = _state.get(phone)
    if previous and previous.get("stop_event"):
        previous["stop_event"].set()

    try:
        client = _new_queue_client(phone, long_token)
        client.login()
        _ = client.groups  # activa el enriquecido de mensajes de grupo
    except Exception as e:
        log.exception("login falló para %s", phone)
        raise HTTPException(500, f"login falló: {e}")

    stop_event = _start_listener(phone, client, long_token)
    _start_watchdog(phone)

    _state[phone] = {
        "client": client,
        "token": long_token,
        "stop_event": stop_event,
    }
    _state[phone].pop("pending_client", None)
    _save_token(phone, long_token)
    log.info("Login completado para %s", phone)
    return {"status": "ok"}


def _wait_send_ack(phone: str, msg_id: str, timeout: float = _SEND_ACK_TIMEOUT) -> bool:
    """Espera el tdack del servidor para msg_id. True si llego a tiempo."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _send_acks_lock:
            if msg_id in _send_acks[phone]:
                return True
        time.sleep(0.2)
    return False


def _requeue_unacked(phone: str, msg_id: str):
    """Devuelve la fila de la cola a 'pending' para reintento automatico.

    El SDK marca 'sent' al escribir el stanza al socket, sin confirmacion del
    servidor; si la red estaba caida el mensaje se pierde. Volver a 'pending'
    hace que el worker de reintentos lo reenvie.
    """
    try:
        db_path = os.path.join(QUEUE_DB_DIR, f"{phone}.db")
        con = sqlite3.connect(db_path, timeout=10)
        try:
            con.execute(
                "update messages set status='pending', sent_at=NULL, retry_count=0, "
                "last_error='sin tdack del servidor' where msg_id=?",
                (msg_id,),
            )
            con.commit()
        finally:
            con.close()
        log.warning("[%s] mensaje %s sin tdack del servidor; reencolado para reintento",
                    phone, msg_id)
    except Exception:
        log.exception("[%s] no se pudo reencolar %s", phone, msg_id)


@app.post("/send_message")
def send_message(body: SendMessageBody):
    entry = _state.get(body.phone)
    if not entry or "client" not in entry:
        raise HTTPException(401, "usuario no logueado; haz login primero")
    if not _ensure_listener(body.phone):
        raise HTTPException(503, "listener no conectado; reintenta")
    try:
        with _send_locks[body.phone]:
            msg_id = entry["client"].send_message_queued(
                body.to, body.message, body.reply_to or "")
        try:
            from todus.stanzas.replycache import register as _reply_register
            own = body.phone.replace("+", "").strip() + "@im.todus.cu"
            _reply_register(str(msg_id), body=body.message, from_jid=own,
                            to_jid=body.to, ts=time.time(), mtype="chat")
        except Exception:
            log.debug("no se pudo registrar reply saliente", exc_info=True)
    except Exception as e:
        log.exception("send_message falló %s -> %s", body.phone, body.to)
        raise HTTPException(500, f"send_message falló: {e}")
    acked = _wait_send_ack(body.phone, str(msg_id))
    if not acked:
        _requeue_unacked(body.phone, str(msg_id))
    return {"status": "ok", "msg_id": str(msg_id), "acked": acked}


@app.post("/send_group_message")
def send_group_message(body: SendGroupBody):
    entry = _state.get(body.phone)
    if not entry or "client" not in entry:
        raise HTTPException(401, "usuario no logueado; haz login primero")
    if not _ensure_listener(body.phone):
        raise HTTPException(503, "listener no conectado; reintenta")
    try:
        with _send_locks[body.phone]:
            groups = entry["client"].groups
            if not groups.is_joined(body.group_id):
                groups.join(body.group_id)
            msg_id = groups.send_message(
                body.group_id, body.message, reply_to_id=body.reply_to or "")
    except Exception as e:
        log.exception("send_group_message falló %s -> %s", body.phone, body.group_id)
        raise HTTPException(500, f"send_group_message falló: {e}")
    acked = _wait_send_ack(body.phone, str(msg_id))
    return {"status": "ok", "msg_id": str(msg_id), "acked": acked}


class SendReadReceiptBody(BaseModel):
    phone: str
    to: str
    msg_id: str


@app.post("/send_read_receipt")
def send_read_receipt(body: SendReadReceiptBody):
    """Marca un mensaje del remoto como leído en ToDus (elemento <dd>).

    Semántica real del servidor (verificada empíricamente): <dd> = leído,
    <rd> = entregado. El puente Go llama aquí cuando el usuario realmente
    lee el mensaje en Matrix. Formato oficial: o = autor del mensaje (el
    remoto), f = quien acusa (la cuenta de esta sesión).
    """
    entry = _state.get(body.phone)
    if not entry or "client" not in entry:
        raise HTTPException(401, "usuario no logueado; haz login primero")
    client = entry["client"]
    own = f"{client.phone_number}@im.todus.cu"
    to_jid = body.to if "@" in body.to else f"{body.to}@im.todus.cu"
    stanza = (
        "<m o='%s' f='%s' t='c' i='%s' xmlns='jc'>"
        "<dd xmlns='x8' i='%s'/></m>"
    ) % (to_jid, own, uuid.uuid4(), body.msg_id)
    try:
        with client._xmpp_session(client.token) as sock:
            sock.sendall(stanza.encode())
    except Exception as e:
        log.exception("send_read_receipt falló %s / %s", body.phone, body.msg_id)
        raise HTTPException(500, f"send_read_receipt falló: {e}")
    log.info("[%s] READ-RECEIPT dd enviado msg_id=%s to=%s", body.phone, body.msg_id, body.to)
    return {"status": "ok"}


class InjectMessageBody(BaseModel):
    phone: str
    message: Dict[str, Any]


@app.post("/inject_message")
def inject_message(body: InjectMessageBody):
    """Solo para pruebas locales: inyecta una stanza en el inbox del puente.

    Si la stanza tiene `receipt`/`receipt_type`, se inyecta en la cola de
    acuses (`/poll` la entrega como receipt) para poder probar el marcado de
    "entregado" sin depender de un contacto real.
    """
    msg = dict(body.message)
    msg.setdefault("id", secrets.token_hex(16))
    if msg.get("receipt"):
        rec = {
            "id": str(msg.get("receipt")),
            "type": msg.get("receipt_type") or "delivered",
            "from": _jid_phone(msg.get("from") or ""),
            "group_id": msg.get("group_id") or "",
            "ts": time.time(),
            "raw": "injected",
        }
        with _inbox_lock:
            _receipts[body.phone].append(rec)
            pending = len(_receipts[body.phone])
        return {"status": "ok", "queued_receipts": pending}
    with _inbox_lock:
        _inbox[body.phone].append(msg)
        pending = len(_inbox[body.phone])
    return {"status": "ok", "queued": pending}


_last_channel_drain: Dict[str, float] = {}


def _fetch_channel_pubs(phone: str, channel_id: str, last_id: str = "0") -> List[Dict[str, Any]]:
    """Consulta `todus:ch:getpub` y devuelve las publicaciones nuevas."""
    resp = _send_iq_retry(
        phone,
        lambda: (
            f"<iq o='{channel_id}@ch' t='get' i='{secrets.token_hex(16)}'>"
            f"<query xmlns='todus:ch:getpub' last_id='{last_id}' limit='50'/></iq>"
        ),
        attempts=1,
        timeout=4.0,
    ) or {}
    raw = resp.get("raw") or ""
    pubs: List[Dict[str, Any]] = []
    for m in re.finditer(r"<ch_pub\b([^>]*?)/>", raw):
        attrs = {k: v for k, v in re.findall(r"(\w+)='([^']*)'", m.group(1))}
        pubs.append({
            "id": attrs.get("i") or "",
            "ts": attrs.get("ts") or "",
            "publ": attrs.get("publ") or "",
            "pub_by": attrs.get("pub_by") or "",
        })
    return pubs


def _body_from_publ(publ: str) -> str:
    m = re.search(r"<body[^>]*>(.*?)</body>", publ, re.DOTALL)
    if not m:
        return ""
    return xml_unescape(m.group(1))


def _drain_channel_pubs(phone: str) -> List[Dict[str, Any]]:
    """Consulta publicaciones nuevas de los canales observados y encola mensajes."""
    out: List[Dict[str, Any]] = []
    watched = _watched_channels.get(phone, set())
    if not watched:
        return out
    for cid in sorted(watched):
        state_key = f"{phone}:{cid}"
        state = _channel_seen.get(state_key) or {}
        done = set(state.get("done") or [])
        now = time.time()
        if now - _last_channel_drain.get(f"{phone}:{cid}", 0) < 600.0:
            # No saturar el servidor con getpub: cada canal se revisa cada 10 min.
            continue
        _last_channel_drain[f"{phone}:{cid}"] = now
        pubs = _fetch_channel_pubs(phone, cid, str(state.get("last_id") or "0"))
        max_seen: Optional[str] = None
        for p in pubs:
            pub_id = p.get("id") or ""
            if pub_id and (max_seen is None or int(pub_id) > int(max_seen)):
                max_seen = pub_id
            if not pub_id or pub_id in done:
                continue
            pub_by = str(p.get("pub_by") or "")
            body = _body_from_publ(p.get("publ") or "")
            if not pub_by or not body or pub_by == phone:
                # Publicacion propia o sin cuerpo util: marcar como procesada.
                done.add(pub_id)
                continue
            done.add(pub_id)
            out.append({
                "from": f"{cid}@ch",
                "to": f"{phone}@im.todus.cu",
                "id": pub_id,
                "type": "message",
                "original_id": pub_id,
                "body": body,
                "is_group": False,
                "group_id": "",
                "sender_phone": pub_by,
                "is_channel": True,
                "channel_id": cid,
                "channel_link": "",
                "pub_id": pub_id,
                "pub_by": pub_by,
            })
        new_last = str(max_seen or state.get("last_id") or "0")
        _channel_seen[f"{phone}:{cid}"] = {"last_id": new_last, "done": sorted(done)}
    return out


# Bucle background: drena publicaciones de canales cada N segundos y encola
# los mensajes nuevos en el inbox del puente. Corre fuera del endpoint /poll para
# no bloquear ni generar actividad XMPP dentro de las peticiones HTTP.
_drain_stop = threading.Event()
_drain_thread: Optional[threading.Thread] = None


def _channel_drain_loop() -> None:
    while not _drain_stop.is_set():
        _drain_stop.wait(30)
        if _drain_stop.is_set():
            break
        phones = sorted(set(list(_watched_channels.keys()) + list(_state.keys())))
        for ph in phones:
            try:
                msgs = _drain_channel_pubs(ph)
            except Exception:
                log.exception("drain de canales falló para %s", ph)
                continue
            if not msgs:
                continue
            with _inbox_lock:
                _inbox[ph].extend(msgs)


def start_channel_drain() -> None:
    global _drain_thread
    if _drain_thread is not None and _drain_thread.is_alive():
        return
    _drain_stop.clear()
    _drain_thread = threading.Thread(target=_channel_drain_loop, name="todus-channel-drain", daemon=True)
    _drain_thread.start()


@app.post("/poll")
async def poll(body: PollBody):
    # async: /poll solo vacia colas en memoria, nunca debe esperar al socket XMPP.
    phone = body.phone.strip()
    with _inbox_lock:
        msgs = _inbox.pop(phone, [])
        recs = _receipts.pop(phone, [])
        # El puente ya tiene los mensajes: retirar las copias persistidas.
        _clear_persisted_inbox(phone)
    return {"messages": msgs, "receipts": recs}


@app.post("/contacts")
def contacts(body: ContactsBody):
    """Lista contactos conocidos con su última presencia observada.

    El puente pasa los teléfonos de sus portales DM; el sidecar añade los que
    haya visto en stanzas de presencia.
    """
    phone = body.phone.strip()
    known = _presence.get(phone, {})
    phones = list(dict.fromkeys([*(body.contacts or []), *known.keys()]))
    out: List[Dict[str, Any]] = []
    profiles = _contact_profiles.get(phone, {})
    for c in phones:
        c_phone = _jid_phone(c)
        if not c_phone:
            continue
        p = known.get(c_phone, {})
        profile = profiles.get(c_phone, {})
        out.append({
            "phone": c_phone,
            "name": str(profile.get("name") or ""),
            "avatar_url": str(profile.get("avatar_url") or ""),
            "status": str(p.get("status") or ""),
            "show": str(p.get("show") or ""),
            "priority": str(p.get("priority") or ""),
            "updated_at": p.get("updated_at", 0),
        })
    return {"contacts": out}


@app.post("/groups")
def groups(body: PollBody):
    phone = body.phone.strip()
    try:
        resp = _send_iq_retry(
            phone,
            lambda: todus_stanza.group_my_groups_iq(index=0, limit=50),
            attempts=2,
            timeout=8.0,
        ) or {}
    except Exception as e:
        log.exception("groups falló para %s", phone)
        raise HTTPException(500, f"groups falló: {e}")

    raw = resp.get("raw") or ""
    if resp.get("error"):
        return {"groups": [], "raw": raw, "error": resp.get("error")}

    found: List[Dict[str, str]] = []
    seen = set()
    for attrs in _iq_items(raw):
        gid = _first(attrs, "id", "room", "jid", "group") or attrs.get("_text", "")
        if not gid:
            continue
        gid = gid.split("@")[0]
        if gid in seen:
            continue
        seen.add(gid)
        found.append({
            "id": gid,
            "name": _first(attrs, "name", "subject", "title"),
        })

    # El listado no trae nombres: el puente los pide por grupo con /group_info
    # (cacheado en el sidecar). Resolverlos aqui lanzaba 18 IQ por peticion y
    # saturaba el servidor.
    return {"groups": found, "raw": raw}


@app.post("/group_info")
def group_info(body: GroupInfoBody):
    phone = body.phone.strip()
    info, raw = _fetch_group_info(phone, body.group_id)
    cfg = info.get("config") or {}
    return {
        "id": body.group_id,
        "name": cfg.get("roomname") or "",
        "subject": cfg.get("subject") or "",
        "members": str(info.get("aff_count") or ""),
        "role": info.get("aff") or "",
        "admin": info.get("admin") or "",
        "version": info.get("version") or "",
        "picture": cfg.get("picture") or "",
        "picture_thumbnail_url": cfg.get("picture_thumbnail_url") or "",
        "raw": raw,
    }


@app.post("/group_members")
def group_members(body: GroupInfoBody):
    """Lista los miembros del grupo con su rol (IQ x11).

    Un solo intento: el servidor de ToDus tarda o ignora x11 cuando esta
    cargado. El puente reintenta mas tarde; una cache negativa corta de 30s
    evita martillarlo en cada resync de sala.
    """
    phone = body.phone.strip()
    cache_key = f"{phone}:{body.group_id}"
    cached = _group_members_cache.get(cache_key)
    if cached:
        age = time.time() - cached["ts"]
        ttl = _GROUP_CACHE_TTL if cached.get("members") else _group_members_negative_ttl
        if age < ttl:
            return {
                "members": cached["members"],
                "cached": True,
                "raw": cached.get("raw", ""),
            }

    group_jid = body.group_id if "@" in body.group_id else body.group_id + "@muclight.im.todus.cu"

    def build_members_iq() -> str:
        # Formato estandar type/to: con o=/t= el servidor MUC ignora x11.
        mid = secrets.token_hex(16)
        return (
            f"<iq type='get' id='{mid}' i='{mid}' to='{group_jid}'>"
            f"<query xmlns='x11'/></iq>"
        )

    try:
        resp = _send_iq_retry(
            phone, build_members_iq, attempts=3, timeout=10.0
        ) or {}
    except Exception as e:
        log.exception("group_members falló para %s / %s", phone, body.group_id)
        raise HTTPException(500, f"group_members falló: {e}")

    raw = resp.get("raw") or ""
    members: List[Dict[str, str]] = []
    # El SDK/servidor usa <user affiliation='rol'>telefono@im.todus.cu</user>;
    # se aceptan comillas simples o dobles por si cambia el formato.
    for match in re.finditer(
        r"<user\b[^>]*affiliation=(['\"])([^'\"]+)\1[^>]*>([^<@]+)", raw
    ):
        members.append({"phone": match.group(3).strip(), "role": match.group(2)})
    if not members:
        # Algunos servidores devuelven el telefono como atributo.
        for match in re.finditer(r"<user\b([^>]*)/>", raw):
            attrs = dict(re.findall(r"(\w+)=(['\"])([^'\"]*)\2", match.group(1)))
            attrs = {k: v[1] for k, v in attrs.items()}
            ph = attrs.get("phone") or attrs.get("jid") or attrs.get("o") or ""
            if ph:
                members.append({
                    "phone": ph.split("@")[0].split("/")[0],
                    "role": attrs.get("affiliation") or attrs.get("role") or "",
                })
    _group_members_cache[cache_key] = {
        "ts": time.time(),
        "members": members,
        "raw": raw,
    }
    if not members:
        log.warning("group_members sin miembros para %s raw=%r", body.group_id, raw[:200])
    return {"members": members, "raw": raw}


def _resolve_media_url(phone: str, url: str, attempts: int = 3) -> str:
    """Resuelve una URL de descarga ToDus sin leer del socket compartido.

    El ``get_real_download_url`` del SDK hace ``_recv_all(sock)`` sobre el
    socket XMPP que también lee el listener. Dos hilos leyendo el mismo socket
    SSL pueden robarse respuestas y corromper el heap (SIGABRT
    ``free(): corrupted unsorted chunks``). Aquí solo enviamos el IQ; la
    respuesta la entrega el callback del listener vía ``_iq_waiters``.
    """
    def build():
        return todus_stanza.download_query(secrets.token_hex(5), url)

    resp = _send_iq_retry(phone, build, attempts=attempts, timeout=8.0)
    raw = (resp or {}).get("raw") or ""
    real = (_iq_query_attrs(raw).get("du") or "").replace("&amp;", "&")
    if not real:
        raise RuntimeError(f"sin URL de descarga en respuesta ToDus: {raw[:160]!r}")
    return real


def _reserve_upload_url(phone: str, size: int, file_type: FileType,
                        file_name: str = "") -> tuple[str, str]:
    """Reserva URLs de subida/bajada usando el listener como único lector."""
    def build():
        return todus_stanza.upload_query(
            secrets.token_hex(5), size, int(file_type), file_name=file_name)

    resp = _send_iq_retry(phone, build, attempts=3, timeout=10.0)
    raw = (resp or {}).get("raw") or ""
    attrs = _iq_query_attrs(raw)
    up = (attrs.get("put") or "").replace("&amp;", "&")
    down = (attrs.get("get") or "").replace("&amp;", "&")
    if not up or not down:
        raise RuntimeError(f"respuesta de reserva sin put/get: {raw[:200]!r}")
    return up, down


def _download_media_bytes(phone: str, url: str) -> tuple[bytes, str]:
    """Descarga un adjunto de ToDus y devuelve (bytes, url_real)."""
    entry = _state.get(phone.strip())
    if not entry or "client" not in entry:
        raise HTTPException(401, "usuario no logueado")
    client = entry["client"]
    real = url
    try:
        real = _resolve_media_url(phone, url)
    except Exception as e:
        # Algunas URLs ya son firmadas/directas; el resolve XMPP falla en esos casos.
        if not str(url).startswith("http"):
            raise
        log.warning("[%s] no se pudo resolver URL ToDus (%s); usando URL directa", phone, e)
        real = ""
    # get_real_download_url devuelve '' (sin excepcion) para URLs que no son
    # de ToDus (por ejemplo URLs publicas en mensajes inyectados en pruebas).
    if not real:
        real = url
    from todus.client.file import _download_headers, _needs_auth
    headers = _download_headers(client.token if _needs_auth(real) else "")
    resp = client.session.get(real, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.content, real


@app.post("/media")
def media(body: MediaBody):
    """Sirve los bytes de un adjunto de ToDus al puente Go (mismo host)."""
    try:
        data, _real = _download_media_bytes(body.phone, body.url)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("media fallo para %s", body.phone)
        raise HTTPException(500, f"media fallo: {e}")
    return Response(content=data, media_type="application/octet-stream")


class SendMediaBody(BaseModel):
    phone: str
    to: str
    data: str                 # base64 de los bytes del archivo
    file_name: str = "archivo"
    mime_type: str = ""
    caption: str = ""
    kind: str = "file"        # image|video|audio|voice|file
    width: int = 0
    height: int = 0
    duration: int = 0
    is_group: bool = False


@app.post("/send_media")
def send_media(body: SendMediaBody):
    """Sube un archivo a ToDus y envia el stanza de adjunto."""
    entry = _state.get(body.phone)
    if not entry or "client" not in entry:
        raise HTTPException(401, "usuario no logueado; haz login primero")
    client = entry["client"]
    if not _ensure_listener(body.phone, timeout=60.0):
        raise HTTPException(503, "listener no conectado; reintenta")
    try:
        raw = base64.b64decode(body.data)
        if not raw:
            raise HTTPException(400, "data vacia")
        kind = (body.kind or "file").lower()
        ft_map = {
            "image": FileType.PICTURE,
            "video": FileType.VIDEO,
            "voice": FileType.VOICE,
            "audio": FileType.AUDIO,
            "file": FileType.FILE,
        }
        file_type = ft_map.get(kind, FileType.FILE)
        mid = secrets.token_hex(16)
        to_jid = body.to if "@" in body.to else body.to + (
            "@muclight.im.todus.cu" if body.is_group else "@im.todus.cu")

        # El listener XMPP se reconecta cada cierto tiempo; durante esa ventana
        # los IQ fallan. Reintentamos la reserva y el envío del stanza.
        last_err = None
        down_url = ""
        for attempt in range(3):
            try:
                with _send_locks[body.phone]:
                    if body.is_group and not client.groups.is_joined(body.to):
                        client.groups.join(body.to)
                    # No usar client.upload_file(): internamente llama a
                    # reserve_upload_url, que hace _recv_all() sobre el socket
                    # compartido con el listener (causa del SIGABRT). Reserva
                    # por IQ con waiter + PUT HTTP directo a S3.
                    log.info("[%s] media[%d]: reservando URL (%d bytes)", body.phone, attempt + 1, len(raw))
                    up_url, down_url = _reserve_upload_url(
                        body.phone, len(raw), file_type, body.file_name)
                    log.info("[%s] media[%d]: URL reservada", body.phone, attempt + 1)
                    up_resp = client.session.put(
                        up_url,
                        data=raw,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(len(raw)),
                        },
                        timeout=180,
                    )
                    up_resp.raise_for_status()
                    log.info("[%s] media[%d]: subido a S3 (%d)", body.phone, attempt + 1, up_resp.status_code)
                    if kind == "image":
                        stanza = todus_stanza.image_message(
                            to_jid, down_url, body.file_name, len(raw),
                            body.width, body.height, "", body.caption, msg_id=mid)
                    elif kind == "video":
                        stanza = todus_stanza.video_message(
                            to_jid, down_url, secrets.token_hex(16), body.file_name,
                            len(raw), body.duration, body.width, body.height, "",
                            info_text=body.caption, msg_id=mid)
                    elif kind == "voice":
                        stanza = todus_stanza.voice_message(
                            to_jid, down_url, body.file_name, len(raw),
                            body.duration, "", caption=body.caption, msg_id=mid)
                    else:
                        stanza = todus_stanza.file_message(
                            to_jid, down_url, int(file_type), body.caption,
                            msg_id=mid, file_name=body.file_name, file_size=len(raw))
                    log.info("[%s] media[%d]: enviando stanza (shared=%s len=%d)",
                             body.phone, attempt + 1, _shared_sock_ready(body.phone), len(stanza))
                    with client._xmpp_session(client.token) as sock:
                        sock.sendall(stanza.encode())
                    log.info("[%s] media[%d]: stanza escrita", body.phone, attempt + 1)
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    log.warning("[%s] send_media reintento %d/3: %s",
                                body.phone, attempt + 2, e)
                    time.sleep(4 + attempt * 4)
        if last_err is not None:
            raise last_err

        acked = _wait_send_ack(body.phone, mid)
        log.info("[%s] MEDIA enviado kind=%s to=%s msg_id=%s acked=%s bytes=%d",
                 body.phone, kind, to_jid, mid, acked, len(raw))
        return {"status": "ok", "msg_id": mid, "acked": acked, "url": down_url}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("send_media fallo %s -> %s", body.phone, body.to)
        raise HTTPException(500, f"send_media fallo: {e}")


class DebugSendRawBody(BaseModel):
    phone: str
    to: str
    message: str


@app.post("/debug_send_raw")
def debug_send_raw(body: DebugSendRawBody):
    """Envía un <m> a un JID arbitrario (chat de bots/tarjetas, depuración).

    El SDK valida que el destino sea un número; los chats oficiales de ToDus
    usan JIDs con nombre (p.ej. unehabana@im.todus.cu), así que aquí se manda
    la stanza cruda por la sesión compartida del listener.
    """
    entry = _state.get(body.phone)
    if not entry or "client" not in entry:
        raise HTTPException(401, "usuario no logueado; haz login primero")
    client = entry["client"]
    mid = uuid.uuid4().hex
    try:
        stanza_xml = private_message_stanza(body.to, body.message, msg_id=mid)
        with _send_locks[body.phone]:
            client.send_stanza(stanza_xml)
    except Exception as e:
        log.exception("debug_send_raw falló %s -> %s", body.phone, body.to)
        raise HTTPException(500, f"debug_send_raw falló: {e}")
    acked = _wait_send_ack(body.phone, mid)
    log.info("[%s] DEBUG-SEND-RAW to=%s acked=%s", body.phone, body.to, acked)
    return {"status": "ok", "msg_id": mid, "acked": acked}


@app.post("/debug_iq")
def debug_iq(body: DebugIQBody):
    """Envía un IQ crudo y devuelve la respuesta (solo para depuración local)."""
    try:
        resp = _send_iq_and_wait(body.phone, body.iq, timeout=body.timeout or 8.0)
    except Exception as e:
        log.exception("debug_iq falló")
        raise HTTPException(500, f"debug_iq falló: {e}")
    return {"response": resp}


@app.post("/channels")
def channels(body: PollBody):
    phone = body.phone.strip()
    try:
        resp = _send_iq_retry(phone, get_my_channels_iq) or {}
    except Exception as e:
        log.exception("channels falló para %s", phone)
        raise HTTPException(500, f"channels falló: {e}")

    raw = resp.get("raw") or ""
    if resp.get("error"):
        return {"channels": [], "raw": raw, "error": resp.get("error")}

    # El listado oficial viene como atributo channels='id1,id2,...'
    qattrs = _iq_query_attrs(raw)
    listed = qattrs.get("channels") or qattrs.get("items") or ""
    ids = [c.strip() for c in listed.split(",") if c.strip()]
    if ids:
        _watched_channels[phone].update(ids)
        return {
            "channels": [{"id": c, "name": "", "link": ""} for c in ids],
            "raw": raw,
        }

    found: List[Dict[str, str]] = []
    seen = set()
    for attrs in _iq_items(raw):
        cid = _first(attrs, "jid", "id", "link", "channel")
        if not cid:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        found.append({
            "id": cid,
            "name": _first(attrs, "name", "subject", "title"),
            "link": _first(attrs, "link"),
        })
    return {"channels": found, "raw": raw}


@app.post("/send_channel_message")
def send_channel_message(body: SendChannelMessageBody):
    """Publica un mensaje de texto en un canal de ToDus (IQ todus:ch:publish)."""
    phone = body.phone.strip()
    channel_id = body.channel_id.strip()
    if not channel_id:
        raise HTTPException(400, "channel_id vacío")
    entry = _state.get(phone)
    if not entry or "client" not in entry:
        raise HTTPException(401, "usuario no logueado; haz login primero")
    channel_jid = f"{channel_id}@ch"
    publ = f"<message><body>{xml_escape(body.message)}</body></message>"
    iq = (
        f"<iq o='{channel_jid}' t='set' i='{secrets.token_hex(16)}'>"
        f"<query xmlns='todus:ch:publish' publ='{publ}'/></iq>"
    )
    try:
        resp = _send_iq_retry(phone, lambda: iq) or {}
    except Exception as e:
        log.exception("send_channel_message falló %s / %s", phone, channel_id)
        raise HTTPException(500, f"send_channel_message falló: {e}")
    raw = resp.get("raw") or ""
    pub_id = ""
    m = re.search(r"pub_id='([^']+)'", raw)
    if m:
        pub_id = m.group(1)
    return {"status": "ok", "msg_id": str(pub_id), "raw": raw}


@app.post("/channel_publications")
def channel_publications(body: ChannelPublicationsBody):
    """Devuelve las publicaciones recientes de un canal (>= last_id).

    `last_id` actúa como high-water mark: con "0" devuelve las publicaciones
    desde el principio (backfill inicial).
    """
    phone = body.phone.strip()
    channel_id = body.channel_id.strip()
    last_id = (body.last_id or "0").strip() or "0"
    resp = _send_iq_retry(
        phone,
        lambda: (
            f"<iq o='{channel_id}@ch' t='get' i='{secrets.token_hex(16)}'>"
            f"<query xmlns='todus:ch:getpub' last_id='{last_id}' limit='50'/></iq>"
        ),
    ) or {}
    raw = resp.get("raw") or ""
    pubs: List[Dict[str, Any]] = []
    for m in re.finditer(r"<ch_pub\b([^>]*?)/>", raw):
        attrs = {k: v for k, v in re.findall(r"(\w+)='([^']*)'", m.group(1))}
        pubs.append({
            "id": attrs.get("i") or "",
            "ts": attrs.get("ts") or "",
            "publ": attrs.get("publ") or "",
            "pub_by": attrs.get("pub_by") or "",
        })
    return {"publications": pubs, "raw": raw}


@app.post("/channel_info")
def channel_info(body: ChannelInfoBody):
    """Devuelve info del canal por id (usando link si se conoce)."""
    phone = body.phone.strip()
    channel_id = body.channel_id.strip()
    cache_key = f"{phone}:{channel_id}"
    cached = _channel_cache.get(cache_key)
    if cached and time.time() - cached.get("ts", 0) < _GROUP_CACHE_TTL:
        return {"channel": cached, "cached": True}

    info: Dict[str, Any] = {}
    if body.link:
        iq = (
            f"<iq o='ch' t='get' i='{secrets.token_hex(16)}'>"
            f"<query xmlns='todus:ch:info:link:v2' link='{body.link}'/></iq>"
        )
        resp = _send_iq_retry(phone, lambda: iq) or {}
        raw = resp.get("raw") or ""
        if not resp.get("error"):
            info = _iq_items(raw)
            info["link"] = body.link
            info["id"] = channel_id

    if not info:
        return {"channel": {"id": channel_id, "name": "", "link": "",
                            "picture": "", "picture_thumbnail_url": ""}, "raw": ""}

    info = {k: str(v) for k, v in info.items() if k != "_text"}
    _channel_cache[cache_key] = {
        "ts": time.time(),
        "id": channel_id,
        "name": str(info.get("name") or ""),
        "link": str(info.get("link") or ""),
        "desc": str(info.get("desc") or ""),
        "picture": str(info.get("picture") or ""),
        "picture_thumbnail_url": str(info.get("picture_thumbnail_url") or ""),
    }
    return {"channel": _channel_cache[cache_key], "raw": raw}


@app.get("/last_stanza/{phone}")
def last_stanza(phone: str):
    """Devuelve el último stanza recibido sin filtrar (para inspeccionar estructura)."""
    return {"stanza": _last_stanza.get(phone)}


@app.post("/logout")
def logout(body: PollBody):
    entry = _state.pop(body.phone, None)
    if entry:
        ev = entry.get("stop_event")
        if ev:
            ev.set()
    _drop_token(body.phone.strip())
    return {"status": "ok"}


def _restore_phone(phone: str) -> bool:
    """Restaura una sesión guardada. Devuelve True si quedó viva."""
    cur = _state.get(phone)
    if cur and cur.get("client") and cur.get("stop_event") and not cur["stop_event"].is_set():
        return True
    try:
        token_path = os.path.join(TOKEN_DIR, phone + ".token")
        with open(token_path, encoding="utf-8") as f:
            token = f.read().strip()
        if not token:
            return False
        client = _new_queue_client(phone, token)
        client.login()
        _ = client.groups  # activa el enriquecido de mensajes de grupo
        stop_event = _start_listener(phone, client, token)
        _state[phone] = {"client": client, "token": token, "stop_event": stop_event}
        _start_watchdog(phone)
        log.info("[%s] sesión restaurada desde disco", phone)
        return True
    except Exception as e:
        log.warning("[%s] no se pudo restaurar la sesión: %s", phone, e)
        return False


def _restore_saved_sessions() -> None:
    for name in sorted(os.listdir(TOKEN_DIR)):
        if not name.endswith(".token"):
            continue
        _restore_phone(name[: -len(".token")])


def _session_guard() -> None:
    """Reintenta indefinidamente restaurar sesiones caídas (red/SSL/JWT).

    La red de ToDus falla de forma intermitente; sin esto el sidecar arranca
    sin sesión y se queda así hasta que alguien lo reinicia a mano.
    """
    backoff = 15.0
    while True:
        time.sleep(backoff)
        try:
            phones = [n[: -len(".token")] for n in sorted(os.listdir(TOKEN_DIR)) if n.endswith(".token")]
            pending = []
            for phone in phones:
                cur = _state.get(phone)
                alive = bool(cur and cur.get("client") and cur.get("stop_event") and not cur["stop_event"].is_set())
                if not alive:
                    pending.append(phone)
            if not pending:
                backoff = 60.0
                continue
            ok = True
            for phone in pending:
                log.info("[%s] guard: sesión caída, reintentando login (token guardado)", phone)
                if not _restore_phone(phone):
                    ok = False
            backoff = 60.0 if ok else min(backoff * 2, 300.0)
        except Exception as e:
            log.exception("guard de sesiones falló: %s", e)
            backoff = min(backoff * 2, 300.0)


@app.on_event("startup")
def _startup_restore() -> None:
    # Sincrono: uvicorn no abre el puerto hasta que terminen los handlers de
    # startup, asi el puente nunca llama a /login a la vez que la restauracion.
    try:
        _load_persisted_inbox()
        start_channel_drain()
        _restore_saved_sessions()
        threading.Thread(target=_session_guard, name="todus-session-guard", daemon=True).start()
    except Exception as e:
        log.exception("Fallo restaurando sesiones guardadas: %s", e)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("TODUS_PORT", "8765")))
