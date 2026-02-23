import socket
import threading
import json
import struct
import queue
import time
from pathlib import Path

import tkinter as tk
from tkinter import ttk

try:
    import ttkbootstrap as tb
except ImportError:
    tb = None


# =========================
#  ПРОТОКОЛ: length + JSON
# =========================

def send_packet(sock: socket.socket, obj: dict) -> None:
    """
    Отправить пакет в формате:
    [4 bytes length][json bytes]
    """
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    header = struct.pack("!I", len(data))
    sock.sendall(header + data)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """
    Считать ровно n байт из TCP потока.
    """
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed")
        buf += chunk
    return buf


def recv_packet(sock: socket.socket) -> dict:
    """
    Прочитать один пакет:
    - длина (4 байта)
    - payload (length байт)
    - json.loads
    """
    header = recv_exact(sock, 4)
    (length,) = struct.unpack("!I", header)
    payload = recv_exact(sock, length)
    return json.loads(payload.decode("utf-8"))


# =========================
#  ХРАНЕНИЕ ИСТОРИИ В ФАЙЛЫ
# =========================
# Мы храним историю локально на клиенте.
# Формат: JSON Lines (.jsonl) — 1 сообщение = 1 строка JSON
#
# Папка:
#   chat_history/<username>/
# Файл диалога:
#   dm_<contact>.jsonl
#
# Каждая строка:
# {
#   "ts": 1700000000.123,
#   "sender": "Alice",
#   "text": "Привет!"
# }
#
# Плюсы JSONL:
# - легко дописывать append
# - легко читать
# - файл не портится, если внезапно оборвалось (обычно потеряется максимум 1 строка)


def safe_filename(name: str) -> str:
    """
    Очень простая "очистка" имени для файла.
    Убираем опасные символы, оставляем буквы/цифры/._-
    """
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    return "".join(ch if ch in allowed else "_" for ch in name)


class MessengerUI:
    def __init__(self):
        # Очередь сообщений из сети в GUI (GUI поток НЕ должен напрямую трогать сокет recv)
        self.in_q = queue.Queue()

        self.sock: socket.socket | None = None
        self.running = False

        # Ник текущего пользователя
        self.username: str = ""

        # Список контактов и множество онлайн пользователей
        self.contacts: list[str] = []
        self.online: set[str] = set()

        # Текущий активный чат (выбранный контакт)
        self.active_chat: str | None = None

        # История сообщений в оперативной памяти:
        # key=frozenset({me, other}) -> list[(sender, text, ts)]
        self.history: dict[frozenset[str], list[tuple[str, str, float]]] = {}

        # Непрочитанные:
        # {"Bob": 2, "Alice": 0, ...}
        self.unread: dict[str, int] = {}

        # Папка для истории (будет создана после логина)
        self.history_dir: Path | None = None

        # GUI root
        if tb:
            self.root = tb.Window(themename="flatly")
        else:
            self.root = tk.Tk()

        self.root.title("Socket Messenger (DM + Unread + History)")
        self.root.geometry("920x580")
        self.root.minsize(780, 480)

        # Стартовый экран логина
        self._build_login()

        # корректное закрытие окна
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # раз в 50ms проверяем входящую очередь (без блокировок)
        self.root.after(50, self._poll_incoming)

    # =========================
    #           UI: LOGIN
    # =========================
    def _build_login(self) -> None:
        self.login_frame = tk.Frame(self.root)
        self.login_frame.pack(fill="both", expand=True)

        title = tk.Label(self.login_frame, text="Подключение",
                         font=("Segoe UI", 18, "bold"))
        title.pack(pady=(60, 20))

        form = tk.Frame(self.login_frame)
        form.pack()

        tk.Label(form, text="Имя:").grid(
            row=0, column=0, sticky="e", padx=8, pady=8)
        tk.Label(form, text="Хост:").grid(
            row=1, column=0, sticky="e", padx=8, pady=8)
        tk.Label(form, text="Порт:").grid(
            row=2, column=0, sticky="e", padx=8, pady=8)

        self.name_var = tk.StringVar(value="Alice")
        self.host_var = tk.StringVar(value="127.0.0.1")
        self.port_var = tk.StringVar(value="5050")

        tk.Entry(form, textvariable=self.name_var, width=28).grid(
            row=0, column=1, padx=8, pady=8)
        tk.Entry(form, textvariable=self.host_var, width=28).grid(
            row=1, column=1, padx=8, pady=8)
        tk.Entry(form, textvariable=self.port_var, width=28).grid(
            row=2, column=1, padx=8, pady=8)

        self.err_label = tk.Label(self.login_frame, text="", fg="red")
        self.err_label.pack(pady=(10, 0))

        btn = tk.Button(self.login_frame, text="Подключиться",
                        width=20, command=self.connect)
        btn.pack(pady=18)

    # =========================
    #           UI: MAIN
    # =========================
    def _build_main(self) -> None:
        # убираем login
        self.login_frame.destroy()

        self.main = tk.Frame(self.root)
        self.main.pack(fill="both", expand=True)

        # -------- left: contacts --------
        self.left = tk.Frame(self.main, width=280)
        self.left.pack(side="left", fill="y")
        self.left.pack_propagate(False)

        tk.Label(self.left, text="Контакты", font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=12, pady=(12, 6))

        self.contacts_view = ttk.Treeview(self.left, columns=(
            "status",), show="tree headings", height=18)
        self.contacts_view.heading("#0", text="Имя")
        self.contacts_view.heading("status", text="Статус")
        self.contacts_view.column("#0", width=170, anchor="w")
        self.contacts_view.column("status", width=80, anchor="center")
        self.contacts_view.pack(fill="both", expand=True, padx=10, pady=10)

        # событие выбора контакта
        self.contacts_view.bind("<<TreeviewSelect>>", self._on_contact_select)

        # -------- right: chat --------
        self.right = tk.Frame(self.main)
        self.right.pack(side="right", fill="both", expand=True)

        header = tk.Frame(self.right)
        header.pack(fill="x")

        self.title_lbl = tk.Label(
            header, text="Выберите контакт", font=("Segoe UI", 14, "bold"))
        self.title_lbl.pack(side="left", padx=12, pady=10)

        self.status_lbl = tk.Label(
            header, text=f"Вы: {self.username}", fg="#666")
        self.status_lbl.pack(side="right", padx=12)

        # область сообщений (Text + Scrollbar — просто и надёжно)
        self.chat_frame = tk.Frame(self.right)
        self.chat_frame.pack(fill="both", expand=True)

        self.chat_text = tk.Text(
            self.chat_frame, wrap="word", state="disabled")
        self.chat_scroll = tk.Scrollbar(
            self.chat_frame, command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=self.chat_scroll.set)

        self.chat_text.pack(side="left", fill="both",
                            expand=True, padx=(12, 0), pady=12)
        self.chat_scroll.pack(side="right", fill="y", padx=(0, 12), pady=12)

        # нижняя панель ввода
        input_frame = tk.Frame(self.right)
        input_frame.pack(fill="x")

        self.text_var = tk.StringVar()
        self.entry = tk.Entry(input_frame, textvariable=self.text_var)
        self.entry.pack(side="left", fill="x",
                        expand=True, padx=(12, 8), pady=12)
        self.entry.bind("<Return>", lambda e: self.send_dm())

        self.send_btn = tk.Button(
            input_frame, text="Отправить", width=12, command=self.send_dm)
        self.send_btn.pack(side="right", padx=(0, 12), pady=12)

        self._chat_log("Подключено. Выберите контакт слева для диалога.")

    # =========================
    #      CHAT: helpers
    # =========================
    def _chat_log(self, text: str) -> None:
        """
        Удобная функция: добавить строку в чат-окно.
        """
        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", text + "\n")
        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")

    def _dialog_key(self, other: str) -> frozenset[str]:
        """
        Ключ диалога в памяти.
        frozenset({me, other}) — один и тот же ключ для Alice<->Bob
        """
        return frozenset({self.username, other})

    def _render_active_dialog(self) -> None:
        """
        Перерисовать открытый диалог из self.history.
        """
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")

        if not self.active_chat:
            self.chat_text.insert("end", "Выберите контакт слева.\n")
        else:
            key = self._dialog_key(self.active_chat)
            msgs = self.history.get(key, [])
            for sender, text, ts in msgs:
                # можно позже красиво форматировать время
                self.chat_text.insert("end", f"{sender}: {text}\n")

        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")

    # =========================
    #   HISTORY: file helpers
    # =========================
    def _history_file_for(self, other: str) -> Path:
        """
        Путь к файлу истории для конкретного контакта.
        """
        assert self.history_dir is not None
        fname = f"dm_{safe_filename(other)}.jsonl"
        return self.history_dir / fname

    def _append_history_line(self, other: str, sender: str, text: str, ts: float) -> None:
        """
        Добавить одну запись в файл истории (append).
        """
        path = self._history_file_for(other)
        record = {"ts": ts, "sender": sender, "text": text}

        # ensure parent folder exists
        path.parent.mkdir(parents=True, exist_ok=True)

        # append JSON line
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _load_history_from_file(self, other: str) -> list[tuple[str, str, float]]:
        """
        Загрузить всю историю диалога из файла.
        Если файла нет — вернём пустой список.
        """
        path = self._history_file_for(other)
        if not path.exists():
            return []

        msgs: list[tuple[str, str, float]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    sender = str(obj.get("sender", ""))
                    text = str(obj.get("text", ""))
                    ts = float(obj.get("ts", 0.0))
                    if sender and text:
                        msgs.append((sender, text, ts))
        except Exception:
            # если файл испорчен — не падаем, просто покажем то, что удалось
            pass

        return msgs

    def _ensure_history_loaded(self, other: str) -> None:
        """
        Убедиться, что история для контакта уже загружена в память.
        (Чтобы не перечитывать файл каждый раз)
        """
        key = self._dialog_key(other)
        if key in self.history:
            return

        self.history[key] = self._load_history_from_file(other)

    # =========================
    #   CONTACTS: render/select
    # =========================
    def _render_contacts(self) -> None:
        """
        Нарисовать список контактов (с online/offline + badge непрочитанных).
        """
        for item in self.contacts_view.get_children():
            self.contacts_view.delete(item)

        for name in self.contacts:
            if name == self.username:
                continue

            status = "online" if name in self.online else "offline"
            cnt = self.unread.get(name, 0)

            # Показываем "Bob" или "Bob (3)"
            label = name if cnt <= 0 else f"{name} ({cnt})"

            # ВАЖНО: iid делаем = чистому имени, без "(3)".
            # Тогда selection() будет возвращать просто ник.
            self.contacts_view.insert(
                "", "end", iid=name, text=label, values=(status,))

    def _on_contact_select(self, event) -> None:
        """
        Когда пользователь кликает на контакт слева:
        - делаем этот контакт активным диалогом
        - загружаем историю из файла (если ещё не загружена)
        - сбрасываем непрочитанные
        - перерисовываем окно чата
        """
        sel = self.contacts_view.selection()
        if not sel:
            return

        other = sel[0]  # потому что iid=name

        self.active_chat = other
        self.title_lbl.config(text=f"Диалог: {other}")

        # Загружаем историю из файла, если нужно
        self._ensure_history_loaded(other)

        # Сбрасываем непрочитанные
        if self.unread.get(other, 0) > 0:
            self.unread[other] = 0
            self._render_contacts()

        self._render_active_dialog()
        self.entry.focus_set()

    # =========================
    #     NETWORK: connect
    # =========================
    def connect(self) -> None:
        """
        Подключение:
        - читаем имя/host/port
        - соединяемся
        - отправляем hello
        - строим main UI
        - запускаем потоки recv/ping
        """
        self.err_label.config(text="")

        name = self.name_var.get().strip()
        host = self.host_var.get().strip()

        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            self.err_label.config(text="Порт должен быть числом")
            return

        if not name:
            self.err_label.config(text="Введите имя")
            return

        # создаём историю-папку пользователя
        # (делаем это ДО подключения, чтобы всё было готово)
        self.username = name
        self.history_dir = Path("chat_history") / safe_filename(self.username)
        self.history_dir.mkdir(parents=True, exist_ok=True)

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((host, port))
            send_packet(s, {"type": "hello", "name": name})
        except OSError as e:
            self.err_label.config(text=f"Ошибка подключения: {e}")
            return

        self.sock = s
        self.running = True

        self._build_main()
        self.status_lbl.config(text=f"Вы: {self.username}")

        # Поток приёма (recv) — чтобы GUI не зависал
        threading.Thread(target=self._recv_loop, daemon=True).start()

        # Поток ping — keepalive
        threading.Thread(target=self._ping_loop, daemon=True).start()

    # =========================
    #   NETWORK: recv/ping/queue
    # =========================
    def _recv_loop(self) -> None:
        """
        Фоновый поток:
        - блокирующе читает recv_packet
        - складывает пакеты в self.in_q
        """
        try:
            while self.running and self.sock:
                pkt = recv_packet(self.sock)
                self.in_q.put(pkt)
        except Exception:
            self.in_q.put({"type": "system", "text": "Соединение потеряно."})
        finally:
            self.running = False
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass

    def _ping_loop(self) -> None:
        """
        Фоновый поток:
        - раз в 10 секунд отправляет ping
        - это помогает быстрее понять, что соединение умерло
        """
        while self.running and self.sock:
            time.sleep(10)
            try:
                send_packet(self.sock, {"type": "ping"})
            except OSError:
                break

    def _poll_incoming(self) -> None:
        """
        GUI-поток:
        - раз в ~50ms проверяет очередь self.in_q
        - обрабатывает все накопившиеся пакеты
        """
        try:
            while True:
                pkt = self.in_q.get_nowait()
                self._handle_packet(pkt)
        except queue.Empty:
            pass

        # планируем следующий вызов
        self.root.after(50, self._poll_incoming)

    def _handle_packet(self, pkt: dict) -> None:
        """
        Обработка пакета от сервера.
        """
        t = pkt.get("type")

        if t == "contacts":
            # сервер прислал полный список контактов + кто онлайн сейчас
            self.contacts = list(pkt.get("contacts", []))
            self.online = set(pkt.get("online", []))

            # для всех контактов подготовим unread=0 (если ещё нет)
            for c in self.contacts:
                if c != self.username:
                    self.unread.setdefault(c, 0)

            self._render_contacts()

        elif t == "presence":
            # online/offline событие
            user = pkt.get("user")
            online = bool(pkt.get("online"))

            if isinstance(user, str) and user:
                if online:
                    self.online.add(user)
                else:
                    self.online.discard(user)

                self._render_contacts()

        elif t == "dm":
            # входящее личное сообщение
            sender = str(pkt.get("from", "???"))
            to = str(pkt.get("to", ""))
            text = str(pkt.get("text", ""))
            ts = time.time()

            # определяем "другую сторону" диалога
            other = sender if sender != self.username else to
            if not other:
                return

            # убедимся, что история подгружена (если вдруг пришло от контакта, которого ещё не открывали)
            self._ensure_history_loaded(other)

            # добавляем в память
            key = self._dialog_key(other)
            self.history.setdefault(key, []).append((sender, text, ts))

            # добавляем в файл истории
            self._append_history_line(
                other=other, sender=sender, text=text, ts=ts)

            # если чат открыт — сразу показываем, иначе увеличиваем badge
            if self.active_chat == other:
                self._render_active_dialog()
            else:
                self.unread[other] = self.unread.get(other, 0) + 1
                self._render_contacts()

        elif t == "dm_ack":
            # подтверждение "доставлено" (пока не отображаем галочки)
            pass

        elif t == "system":
            self._chat_log("[system] " + str(pkt.get("text", "")))

        elif t == "error":
            self._chat_log("[error] " + str(pkt.get("text", "")))

        elif t == "pong":
            # можно игнорировать
            pass

    # =========================
    #       SEND DM
    # =========================
    def send_dm(self) -> None:
        """
        Отправка сообщения:
        - проверяем, выбран ли контакт
        - добавляем сообщение в память и в файл сразу (optimistic UI)
        - отправляем на сервер
        """
        if not self.running or not self.sock:
            return

        if not self.active_chat:
            self._chat_log("[system] Сначала выберите контакт слева.")
            return

        text = self.text_var.get().strip()
        if not text:
            return

        self.text_var.set("")
        ts = time.time()

        # Чтобы история всегда была в памяти:
        self._ensure_history_loaded(self.active_chat)

        # 1) Добавляем себе в историю (optimistic UI)
        key = self._dialog_key(self.active_chat)
        self.history.setdefault(key, []).append((self.username, text, ts))

        # 2) Записываем в файл историю (как отправленное сообщение)
        self._append_history_line(
            other=self.active_chat, sender=self.username, text=text, ts=ts)

        # 3) Перерисовываем чат
        self._render_active_dialog()

        # 4) Отправляем на сервер
        try:
            send_packet(self.sock, {"type": "dm",
                        "to": self.active_chat, "text": text})
        except OSError:
            self._chat_log("[system] Не удалось отправить: нет соединения.")

    # =========================
    #     CLOSE APP
    # =========================
    def on_close(self) -> None:
        """
        Закрытие приложения:
        - останавливаем потоки
        - закрываем сокет
        - закрываем окно
        """
        self.running = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    MessengerUI().run()
