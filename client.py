import socket
import threading
import json
import struct
import queue
import time

import tkinter as tk
from tkinter import ttk

try:
    import ttkbootstrap as tb
except ImportError:
    tb = None


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


class MessengerUI:
    def __init__(self):
        self.in_q = queue.Queue()
        self.sock = None
        self.running = False

        # ВАЖНО: переменная имени пользователя (ник)
        self.username: str = ""

        # Контакты и онлайн-статусы (затычка)
        self.contacts: list[str] = []
        self.online: set[str] = set()

        if tb:
            self.root = tb.Window(themename="flatly")
        else:
            self.root = tk.Tk()

        self.root.title("Socket Messenger (Contacts Stub)")
        self.root.geometry("900x560")
        self.root.minsize(780, 480)

        self._build_login()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # каждые 50ms читаем очередь событий от сети
        self.root.after(50, self._poll_incoming)

    # ---------- login UI ----------
    def _build_login(self):
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

        self.name_var = tk.StringVar(value="User")
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

    # ---------- chat UI (now: contacts + system log) ----------
    def _build_main(self):
        self.login_frame.destroy()

        self.main = tk.Frame(self.root)
        self.main.pack(fill="both", expand=True)

        # left panel: contacts
        self.left = tk.Frame(self.main, width=280)
        self.left.pack(side="left", fill="y")
        self.left.pack_propagate(False)

        tk.Label(self.left, text="Контакты", font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=12, pady=(12, 6))

        # Treeview удобнее для статуса online/offline
        self.contacts_view = ttk.Treeview(self.left, columns=(
            "status",), show="tree headings", height=18)
        self.contacts_view.heading("#0", text="Имя")
        self.contacts_view.heading("status", text="Статус")
        self.contacts_view.column("#0", width=160, anchor="w")
        self.contacts_view.column("status", width=80, anchor="center")

        self.contacts_view.pack(fill="both", expand=True, padx=10, pady=10)

        # right panel: system messages (log)
        self.right = tk.Frame(self.main)
        self.right.pack(side="right", fill="both", expand=True)

        header = tk.Frame(self.right)
        header.pack(fill="x")

        self.title_lbl = tk.Label(
            header, text="Системные сообщения", font=("Segoe UI", 14, "bold"))
        self.title_lbl.pack(side="left", padx=12, pady=10)

        self.status_lbl = tk.Label(
            header, text=f"Вы: {self.username}", fg="#666")
        self.status_lbl.pack(side="right", padx=12)

        # log area (scrollable)
        self.log_frame = tk.Frame(self.right)
        self.log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(self.log_frame, wrap="word", state="disabled")
        self.log_scroll = tk.Scrollbar(
            self.log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=self.log_scroll.set)

        self.log_text.pack(side="left", fill="both",
                           expand=True, padx=(12, 0), pady=12)
        self.log_scroll.pack(side="right", fill="y", padx=(0, 12), pady=12)

        # input area (disabled stub)
        input_frame = tk.Frame(self.right)
        input_frame.pack(fill="x")

        self.text_var = tk.StringVar()
        self.entry = tk.Entry(input_frame, textvariable=self.text_var)
        self.entry.pack(side="left", fill="x",
                        expand=True, padx=(12, 8), pady=12)
        self.entry.bind("<Return>", lambda e: self.send_message_stub())

        self.send_btn = tk.Button(
            input_frame, text="Отправить", width=12, command=self.send_message_stub)
        self.send_btn.pack(side="right", padx=(0, 12), pady=12)

        self._log(
            "Подключено. Загружены контакты (затычка). Личные сообщения пока не реализованы.")

    # ---------- logging ----------
    def _log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------- contacts rendering ----------
    def _render_contacts(self):
        # очистить текущее дерево
        for item in self.contacts_view.get_children():
            self.contacts_view.delete(item)

        # отрисовать контакты
        for name in self.contacts:
            status = "online" if name in self.online else "offline"
            self.contacts_view.insert("", "end", text=name, values=(status,))

    # ---------- connect / network ----------
    def connect(self):
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

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((host, port))
            send_packet(s, {"type": "hello", "name": name})
        except OSError as e:
            self.err_label.config(text=f"Ошибка подключения: {e}")
            return

        self.sock = s
        self.running = True
        self.username = name  # <-- ник клиента: считается онлайн на сервере

        self._build_main()

        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._ping_loop, daemon=True).start()

    def _recv_loop(self):
        try:
            while self.running:
                pkt = recv_packet(self.sock)
                self.in_q.put(pkt)
        except Exception:
            self.in_q.put({"type": "system", "text": "Соединение потеряно."})
        finally:
            self.running = False
            try:
                self.sock.close()
            except Exception:
                pass

    def _ping_loop(self):
        while self.running:
            time.sleep(10)
            try:
                send_packet(self.sock, {"type": "ping"})
            except OSError:
                break

    def _poll_incoming(self):
        try:
            while True:
                pkt = self.in_q.get_nowait()
                self._handle_packet(pkt)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_incoming)

    def _handle_packet(self, pkt: dict):
        t = pkt.get("type")

        if t == "contacts":
            # сервер прислал полный список контактов и кто онлайн сейчас
            self.contacts = list(pkt.get("contacts", []))
            self.online = set(pkt.get("online", []))
            self._render_contacts()
            self._log(
                f"Получен список контактов: {len(self.contacts)}; онлайн: {len(self.online)}")

        elif t == "presence":
            # сервер сообщил: user стал online/offline
            user = pkt.get("user")
            online = bool(pkt.get("online"))
            if isinstance(user, str) and user:
                if online:
                    self.online.add(user)
                    self._log(f"{user} сейчас online")
                else:
                    self.online.discard(user)
                    self._log(f"{user} сейчас offline")
                self._render_contacts()

        elif t == "system":
            self._log(str(pkt.get("text", "")))

        elif t == "error":
            self._log("Ошибка: " + str(pkt.get("text", "")))

        elif t == "pong":
            # можно не логировать
            pass

    # ---------- messaging stub ----------
    def send_message_stub(self):
        text = self.text_var.get().strip()
        if text:
            self.text_var.set("")
        self._log(
            "Затычка: личные сообщения ещё не реализованы. Сейчас есть только контакты и онлайн-статус.")

    def on_close(self):
        self.running = False
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    MessengerUI().run()
