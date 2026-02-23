import socket
import threading
import json
import struct
import os


CONTACTS_FILE = "contacts.json"


# ---------- protocol helpers ----------
def send_packet(sock: socket.socket, obj: dict):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    header = struct.pack("!I", len(data))  # 4 bytes, big-endian
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
        # нормализуем: только строки, без дублей, сохраняем порядок
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
    data = {"contacts": contacts}
    with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class ChatServer:
    """
    Это уже не "общий чат".
    Сервер сейчас делает только:
    - хранит список контактов в contacts.json
    - отслеживает, кто онлайн
    - рассылает всем presence-события (онлайн/оффлайн)
    - отправляет список контактов и текущий онлайн при подключении
    """

    def __init__(self, host="0.0.0.0", port=5050):
        self.host = host
        self.port = port

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        self.lock = threading.Lock()

        # sock -> username
        self.clients: dict[socket.socket, str] = {}

        # контакты общие для всех (из файла)
        self.contacts: list[str] = load_contacts()

    # ----- helpers -----
    def _online_set(self) -> set[str]:
        # множество имён, которые сейчас онлайн
        return set(self.clients.values())

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

        # сообщаем всем, что пользователь оффлайн
        if announce and name:
            self.broadcast({"type": "presence", "user": name, "online": False})

    def _ensure_contact_exists(self, name: str):
        """
        Удобная "затычка":
        если пользователь вошёл с новым ником, добавим его в общий файл,
        чтобы он появился в контактах у всех.
        """
        if name not in self.contacts:
            self.contacts.append(name)
            save_contacts(self.contacts)
            # сообщим всем, что список контактов обновился
            self.broadcast({"type": "contacts", "contacts": self.contacts,
                           "online": sorted(self._online_set())})

    # ----- client handler -----
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

            # 1) регистрируем клиента
            with self.lock:
                self.clients[sock] = name

            # 2) гарантируем, что имя есть в contacts.json (для всех)
            with self.lock:
                self._ensure_contact_exists(name)

            # 3) отправляем подключившемуся:
            #    - список контактов
            #    - кто онлайн прямо сейчас
            with self.lock:
                send_packet(sock, {
                    "type": "contacts",
                    "contacts": self.contacts,
                    "online": sorted(self._online_set()),
                })

            # 4) всем рассылаем, что пользователь онлайн
            self.broadcast({"type": "presence", "user": name,
                           "online": True}, exclude=None)

            # 5) основной цикл: пока только пинг/понг
            while True:
                pkt = recv_packet(sock)
                t = pkt.get("type")

                if t == "ping":
                    send_packet(sock, {"type": "pong"})
                elif t == "msg":
                    # сообщений пока нет (не общий чат)
                    # можно вернуть заглушку
                    send_packet(
                        sock, {"type": "system", "text": "Личные сообщения ещё не реализованы."})
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
                sock, addr = self.server.accept()
                threading.Thread(target=self.handle_client,
                                 args=(sock, addr), daemon=True).start()
        finally:
            self.server.close()


if __name__ == "__main__":
    ChatServer(port=5050).serve_forever()
