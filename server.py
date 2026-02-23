import socket
import threading
import json
import struct
import os


CONTACTS_FILE = "contacts.json"


# ---------- protocol helpers ----------
def send_packet(sock: socket.socket, obj: dict):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    header = struct.pack("!I", len(data))
    sock.sendall(header + data)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed")
        buf += chunk
    return buf


def recv_packet(sock: socket.socket) -> dict:
    header = recv_exact(sock, 4)
    (length,) = struct.unpack("!I", header)
    payload = recv_exact(sock, length)
    return json.loads(payload.decode("utf-8"))


# ---------- contacts storage ----------
def load_contacts() -> list[str]:
    if not os.path.exists(CONTACTS_FILE):
        return []
    try:
        with open(CONTACTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        contacts = data.get("contacts", [])
        seen = set()
        out = []
        for x in contacts:
            if isinstance(x, str):
                name = x.strip()
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
        return out
    except Exception:
        return []


def save_contacts(contacts: list[str]) -> None:
    with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"contacts": contacts}, f, ensure_ascii=False, indent=2)


class ChatServer:
    """
    Сервер поддерживает:
    - contacts.json (общий список контактов)
    - presence online/offline
    - личные сообщения (DM): {"type":"dm","to":"Bob","text":"..."}
    """

    def __init__(self, host="0.0.0.0", port=5050):
        self.host = host
        self.port = port

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # ВАЖНО: RLock чтобы не словить дедлок при вложенных вызовах
        self.lock = threading.RLock()

        # sock -> username
        self.clients: dict[socket.socket, str] = {}

        # username -> sock
        self.user_to_sock: dict[str, socket.socket] = {}

        # contacts list from file
        self.contacts: list[str] = load_contacts()

    def _online_set(self) -> set[str]:
        return set(self.user_to_sock.keys())

    def _safe_send(self, sock: socket.socket, msg: dict) -> bool:
        try:
            send_packet(sock, msg)
            return True
        except OSError:
            return False

    def _broadcast(self, msg: dict, exclude: socket.socket | None = None):
        """
        Рассылка всем клиентам.
        ВАЖНО: не держим lock во время сетевых sendall (чтобы не блокировать сервер).
        """
        with self.lock:
            targets = list(self.clients.keys())

        dead: list[socket.socket] = []
        for c in targets:
            if exclude is not None and c is exclude:
                continue
            if not self._safe_send(c, msg):
                dead.append(c)

        if dead:
            with self.lock:
                for c in dead:
                    self._drop_client(c, announce=False)

    def _drop_client(self, sock: socket.socket, announce: bool = True):
        """
        Удаление клиента (вызывается под lock или аккуратно).
        """
        name = self.clients.get(sock)

        try:
            sock.close()
        except OSError:
            pass

        if sock in self.clients:
            del self.clients[sock]

        if name and self.user_to_sock.get(name) is sock:
            del self.user_to_sock[name]

        if announce and name:
            self._broadcast(
                {"type": "presence", "user": name, "online": False})

    def _ensure_contact_exists(self, name: str) -> bool:
        """
        Добавляем контакт в contacts.json, если он новый.
        Возвращает True, если список изменился.
        """
        changed = False
        with self.lock:
            if name not in self.contacts:
                self.contacts.append(name)
                save_contacts(self.contacts)
                changed = True
        return changed

    def _send_contacts_snapshot_to(self, sock: socket.socket):
        """
        Отправляет подключившемуся актуальные contacts+online.
        """
        with self.lock:
            contacts_snapshot = list(self.contacts)
            online_snapshot = sorted(self._online_set())

        self._safe_send(sock, {
                        "type": "contacts", "contacts": contacts_snapshot, "online": online_snapshot})

    def _broadcast_contacts_snapshot(self):
        """
        Рассылает всем актуальные contacts+online.
        """
        with self.lock:
            contacts_snapshot = list(self.contacts)
            online_snapshot = sorted(self._online_set())

        self._broadcast(
            {"type": "contacts", "contacts": contacts_snapshot, "online": online_snapshot})

    def _send_to_user(self, username: str, msg: dict) -> bool:
        with self.lock:
            sock = self.user_to_sock.get(username)
        if not sock:
            return False
        return self._safe_send(sock, msg)

    def handle_client(self, sock: socket.socket, addr):
        name = None
        try:
            hello = recv_packet(sock)
            if hello.get("type") != "hello" or not hello.get("name"):
                self._safe_send(sock, {"type": "error", "text": "Bad hello"})
                sock.close()
                return

            name = str(hello["name"]).strip()[:32]
            if not name:
                self._safe_send(sock, {"type": "error", "text": "Empty name"})
                sock.close()
                return

            print(f"[connect] {name} from {addr}")

            # зарегистрировать клиента
            with self.lock:
                # если ник уже онлайн — выкинуть старое соединение (простая политика)
                old = self.user_to_sock.get(name)
                if old and old is not sock:
                    self._drop_client(old, announce=False)

                self.clients[sock] = name
                self.user_to_sock[name] = sock

            # гарантируем, что ник есть в contacts.json
            changed = self._ensure_contact_exists(name)

            # подключившемуся — contacts + online
            self._send_contacts_snapshot_to(sock)

            # всем — presence online
            self._broadcast({"type": "presence", "user": name, "online": True})

            # если контакты обновились — всем разослать обновлённый список
            if changed:
                self._broadcast_contacts_snapshot()

            # основной цикл
            while True:
                pkt = recv_packet(sock)
                t = pkt.get("type")

                if t == "ping":
                    self._safe_send(sock, {"type": "pong"})

                elif t == "dm":
                    to_user = str(pkt.get("to", "")).strip()[:32]
                    text = str(pkt.get("text", "")).strip()[:2000]

                    if not to_user or not text:
                        self._safe_send(
                            sock, {"type": "error", "text": "Bad dm"})
                        continue

                    # доставить получателю
                    delivered = self._send_to_user(
                        to_user,
                        {"type": "dm", "from": name, "to": to_user, "text": text},
                    )

                    # ack / system
                    if delivered:
                        self._safe_send(
                            sock, {"type": "dm_ack", "to": to_user})
                    else:
                        self._safe_send(sock, {
                                        "type": "system", "text": f"{to_user} сейчас offline. Сообщение не доставлено."})

                else:
                    self._safe_send(
                        sock, {"type": "error", "text": "Unknown packet type"})

        except (ConnectionError, OSError, json.JSONDecodeError):
            pass
        finally:
            with self.lock:
                if sock in self.clients:
                    self._drop_client(sock, announce=True)
            if name:
                print(f"[disconnect] {name}")

    def serve_forever(self):
        self.server.bind((self.host, self.port))
        self.server.listen(50)
        print(f"Server listening on {self.host}:{self.port}")

        try:
            while True:
                try:
                    sock, addr = self.server.accept()
                except KeyboardInterrupt:
                    print("\nServer stopped by Ctrl+C")
                    break
                threading.Thread(target=self.handle_client,
                                 args=(sock, addr), daemon=True).start()
        finally:
            try:
                self.server.close()
            except Exception:
                pass


if __name__ == "__main__":
    ChatServer(port=5050).serve_forever()
