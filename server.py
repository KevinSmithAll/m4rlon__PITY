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
    Сервер теперь поддерживает:
    - общий список контактов из файла contacts.json
    - online/offline presence
    - личные сообщения (DM): клиент -> {"type":"dm","to":"Bob","text":"..."}
    """

    def __init__(self, host="0.0.0.0", port=5050):
        self.host = host
        self.port = port
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self.lock = threading.Lock()

        # sock -> username
        self.clients: dict[socket.socket, str] = {}

        # username -> sock (удобно для DM)
        self.user_to_sock: dict[str, socket.socket] = {}

        self.contacts: list[str] = load_contacts()

    def _online_set(self) -> set[str]:
        return set(self.user_to_sock.keys())

    def broadcast(self, msg: dict, exclude: socket.socket | None = None):
        dead = []
        with self.lock:
            for c in list(self.clients.keys()):
                if exclude is not None and c is exclude:
                    continue
                try:
                    send_packet(c, msg)
                except OSError:
                    dead.append(c)
            for c in dead:
                self._drop_client(c, announce=False)

    def _drop_client(self, sock: socket.socket, announce: bool = True):
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
            self.broadcast({"type": "presence", "user": name, "online": False})

    def _ensure_contact_exists(self, name: str):
        if name not in self.contacts:
            self.contacts.append(name)
            save_contacts(self.contacts)

        # всем сообщаем актуальный список контактов + онлайн
        self.broadcast({
            "type": "contacts",
            "contacts": self.contacts,
            "online": sorted(self._online_set())
        })

    def _send_to_user(self, username: str, msg: dict) -> bool:
        """
        Отправить сообщение пользователю по имени.
        Возвращает True если пользователь онлайн и отправка успешна.
        """
        sock = self.user_to_sock.get(username)
        if not sock:
            return False
        try:
            send_packet(sock, msg)
            return True
        except OSError:
            return False

    def handle_client(self, sock: socket.socket, addr):
        try:
            hello = recv_packet(sock)
            if hello.get("type") != "hello" or not hello.get("name"):
                send_packet(sock, {"type": "error", "text": "Bad hello"})
                sock.close()
                return

            name = str(hello["name"]).strip()[:32]
            if not name:
                send_packet(sock, {"type": "error", "text": "Empty name"})
                sock.close()
                return

            with self.lock:
                # Если такой ник уже онлайн — выкидываем старое соединение (простая политика)
                old = self.user_to_sock.get(name)
                if old and old is not sock:
                    self._drop_client(old, announce=False)

                self.clients[sock] = name
                self.user_to_sock[name] = sock

                self._ensure_contact_exists(name)

                # подключившемуся — контакты и онлайн
                send_packet(sock, {
                    "type": "contacts",
                    "contacts": self.contacts,
                    "online": sorted(self._online_set()),
                })

            # всем — presence online
            self.broadcast({"type": "presence", "user": name, "online": True})

            while True:
                pkt = recv_packet(sock)
                t = pkt.get("type")

                if t == "ping":
                    send_packet(sock, {"type": "pong"})

                elif t == "dm":
                    to_user = str(pkt.get("to", "")).strip()[:32]
                    text = str(pkt.get("text", "")).strip()[:2000]

                    if not to_user or not text:
                        send_packet(sock, {"type": "error", "text": "Bad dm"})
                        continue

                    # сообщение для получателя
                    delivered = False
                    with self.lock:
                        delivered = self._send_to_user(to_user, {
                            "type": "dm",
                            "from": name,
                            "to": to_user,
                            "text": text,
                        })

                    # отправителю — ack/ошибка (заглушка)
                    if delivered:
                        send_packet(sock, {"type": "dm_ack", "to": to_user})
                    else:
                        send_packet(sock, {
                                    "type": "system", "text": f"{to_user} сейчас offline. Сообщение не доставлено."})

                else:
                    send_packet(
                        sock, {"type": "error", "text": "Unknown packet type"})

        except (ConnectionError, OSError, json.JSONDecodeError):
            pass
        finally:
            with self.lock:
                if sock in self.clients:
                    self._drop_client(sock, announce=True)

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
