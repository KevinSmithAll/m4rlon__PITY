import socket
import threading
import json
import struct
import os


# Файл со списком контактов (общий для сервера)
CONTACTS_FILE = "contacts.json"


# =========================
#  ПРОТОКОЛ: length + JSON
# =========================
# TCP не знает понятия "сообщение". Он отдаёт поток байт.
# Поэтому мы делаем так:
# 1) отправляем 4 байта длины JSON-пакета
# 2) отправляем сам JSON (UTF-8)
#
# На приёме:
# 1) читаем ровно 4 байта -> длина
# 2) читаем ровно length байт -> полноценный JSON


def send_packet(sock: socket.socket, obj: dict) -> None:
    """
    Отправить один "пакет" по TCP:
    [4 bytes length][payload bytes]
    """
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    # !I = big-endian unsigned int (4 bytes)
    header = struct.pack("!I", len(data))
    sock.sendall(header + data)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """
    Считать ровно n байт.
    TCP recv может вернуть меньше, чем запрошено — это нормально.
    Поэтому читаем в цикле, пока не наберём n.
    """
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            # если recv вернул b'' — соединение закрыто
            raise ConnectionError("Socket closed")
        buf += chunk
    return buf


def recv_packet(sock: socket.socket) -> dict:
    """
    Получить один пакет:
    - читаем длину
    - читаем payload
    - JSON decode
    """
    header = recv_exact(sock, 4)
    (length,) = struct.unpack("!I", header)
    payload = recv_exact(sock, length)
    return json.loads(payload.decode("utf-8"))


# =========================
#   CONTACTS: файл JSON
# =========================

def load_contacts() -> list[str]:
    """
    Загрузить список контактов из contacts.json.
    Возвращаем список уникальных строк (в порядке появления).
    """
    if not os.path.exists(CONTACTS_FILE):
        return []

    try:
        with open(CONTACTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        raw = data.get("contacts", [])
        seen = set()
        out: list[str] = []

        for x in raw:
            if isinstance(x, str):
                name = x.strip()
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)

        return out
    except Exception:
        # если файл битый/не JSON — просто считаем, что контактов нет
        return []


def save_contacts(contacts: list[str]) -> None:
    """
    Сохранить список контактов в contacts.json.
    """
    with open(CONTACTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"contacts": contacts}, f, ensure_ascii=False, indent=2)


# =========================
#         SERVER
# =========================
class ChatServer:
    """
    Сервер:
    - принимает подключения
    - хранит user online/offline (presence)
    - хранит общий список контактов в contacts.json
    - пересылает личные сообщения (DM)
    """

    def __init__(self, host="0.0.0.0", port=5050):
        self.host = host
        self.port = port

        # TCP серверный сокет
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # чтобы можно было быстро перезапускать сервер без ожидания освобождения порта
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # ВАЖНО: RLock (re-entrant lock)
        # Обычный Lock может дать дедлок, если функция под lock вызывает другую функцию,
        # которая тоже пытается взять lock.
        self.lock = threading.RLock()

        # sock -> username (кто под каким соединением сидит)
        self.clients: dict[socket.socket, str] = {}

        # username -> sock (чтобы быстро отправлять DM по имени)
        self.user_to_sock: dict[str, socket.socket] = {}

        # список контактов из файла
        self.contacts: list[str] = load_contacts()

    # ---------- helpers ----------
    def _online_set(self) -> set[str]:
        """
        Текущие онлайн пользователи (их имена).
        """
        return set(self.user_to_sock.keys())

    def _safe_send(self, sock: socket.socket, msg: dict) -> bool:
        """
        Безопасная отправка: если сокет умер, вернём False.
        """
        try:
            send_packet(sock, msg)
            return True
        except OSError:
            return False

    def _broadcast(self, msg: dict, exclude: socket.socket | None = None) -> None:
        """
        Разослать сообщение всем подключенным клиентам.

        ВАЖНО: мы не держим lock во время sendall,
        чтобы не блокировать сервер на медленных клиентах.
        """
        with self.lock:
            targets = list(self.clients.keys())

        dead: list[socket.socket] = []

        for c in targets:
            if exclude is not None and c is exclude:
                continue
            if not self._safe_send(c, msg):
                dead.append(c)

        # если кто-то отвалился — удаляем
        if dead:
            with self.lock:
                for c in dead:
                    self._drop_client(c, announce=False)

    def _drop_client(self, sock: socket.socket, announce: bool = True) -> None:
        """
        Удалить клиента из таблиц и (опционально) оповестить всех, что он оффлайн.
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

        # Сообщаем всем: пользователь оффлайн
        if announce and name:
            self._broadcast(
                {"type": "presence", "user": name, "online": False})

    def _ensure_contact_exists(self, name: str) -> bool:
        """
        Если такого пользователя нет в contacts.json — добавим.
        Возвращает True, если список изменился.
        """
        with self.lock:
            if name not in self.contacts:
                self.contacts.append(name)
                save_contacts(self.contacts)
                return True
        return False

    def _send_contacts_snapshot_to(self, sock: socket.socket) -> None:
        """
        Отправить подключившемуся клиенту:
        - список контактов
        - кто онлайн сейчас
        """
        with self.lock:
            contacts_snapshot = list(self.contacts)
            online_snapshot = sorted(self._online_set())

        self._safe_send(sock, {
                        "type": "contacts", "contacts": contacts_snapshot, "online": online_snapshot})

    def _broadcast_contacts_snapshot(self) -> None:
        """
        Разослать всем клиентам обновлённый список контактов + онлайн.
        """
        with self.lock:
            contacts_snapshot = list(self.contacts)
            online_snapshot = sorted(self._online_set())

        self._broadcast(
            {"type": "contacts", "contacts": contacts_snapshot, "online": online_snapshot})

    def _send_to_user(self, username: str, msg: dict) -> bool:
        """
        Отправить сообщение конкретному пользователю по его имени.
        """
        with self.lock:
            sock = self.user_to_sock.get(username)

        if not sock:
            return False

        return self._safe_send(sock, msg)

    # ---------- main client handler ----------
    def handle_client(self, sock: socket.socket, addr) -> None:
        """
        Поток обработки одного клиента:
        - ждём hello
        - регистрируем пользователя
        - отправляем контакты/онлайн
        - принимаем пакеты и обрабатываем DM/ping
        """
        name = None

        try:
            # Клиент должен первым делом прислать hello:
            # {"type":"hello","name":"Alice"}
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

            # Регистрируем пользователя как онлайн
            with self.lock:
                # Если такой ник уже онлайн — "вышибаем" старое соединение (простая политика)
                old = self.user_to_sock.get(name)
                if old and old is not sock:
                    self._drop_client(old, announce=False)

                self.clients[sock] = name
                self.user_to_sock[name] = sock

            # Добавляем пользователя в contacts.json (если новый)
            changed = self._ensure_contact_exists(name)

            # Отправляем подключившемуся список контактов и кто онлайн
            self._send_contacts_snapshot_to(sock)

            # Сообщаем всем, что пользователь онлайн
            self._broadcast({"type": "presence", "user": name, "online": True})

            # Если список контактов изменился — обновим всем
            if changed:
                self._broadcast_contacts_snapshot()

            # Основной цикл: читаем пакеты
            while True:
                pkt = recv_packet(sock)
                t = pkt.get("type")

                if t == "ping":
                    # простой keepalive
                    self._safe_send(sock, {"type": "pong"})

                elif t == "dm":
                    # личное сообщение:
                    # {"type":"dm","to":"Bob","text":"Привет"}
                    to_user = str(pkt.get("to", "")).strip()[:32]
                    text = str(pkt.get("text", "")).strip()[:2000]

                    if not to_user or not text:
                        self._safe_send(
                            sock, {"type": "error", "text": "Bad dm"})
                        continue

                    # Доставляем получателю
                    delivered = self._send_to_user(
                        to_user,
                        {"type": "dm", "from": name, "to": to_user, "text": text},
                    )

                    # Отправителю — подтверждение или заглушка "offline"
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
            # любые сетевые проблемы/разрывы — просто выходим
            pass
        finally:
            # гарантированно снимаем пользователя с онлайна
            with self.lock:
                if sock in self.clients:
                    self._drop_client(sock, announce=True)

            if name:
                print(f"[disconnect] {name}")

    def serve_forever(self) -> None:
        """
        Запуск сервера:
        - bind
        - listen
        - accept в цикле
        - каждый клиент в отдельном потоке
        """
        self.server.bind((self.host, self.port))
        self.server.listen(50)
        print(f"Server listening on {self.host}:{self.port}")

        try:
            while True:
                try:
                    client_sock, addr = self.server.accept()
                except KeyboardInterrupt:
                    print("\nServer stopped by Ctrl+C")
                    break

                # Поток на клиента (daemon=True => не мешает закрытию программы)
                threading.Thread(target=self.handle_client, args=(
                    client_sock, addr), daemon=True).start()
        finally:
            try:
                self.server.close()
            except Exception:
                pass


if __name__ == "__main__":
    ChatServer(port=5050).serve_forever()
