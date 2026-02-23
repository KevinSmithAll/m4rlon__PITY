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

        # Ник текущего пользователя
        self.username: str = ""

        # Контакты + онлайн
        self.contacts: list[str] = []
        self.online: set[str] = set()

        # Текущий выбранный собеседник
        self.active_chat: str | None = None

        # История сообщений по диалогам: key=frozenset({me, other}) -> list[tuple(sender,text)]
        self.history: dict[frozenset[str], list[tuple[str, str]]] = {}

        if tb:
            self.root = tb.Window(themename="flatly")
        else:
            self.root = tk.Tk()

        self.root.title("Socket Messenger (DM)")
        self.root.geometry("920x580")
        self.root.minsize(780, 480)

        self._build_login()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
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

    # ---------- main UI ----------
    def _build_main(self):
        self.login_frame.destroy()

        self.main = tk.Frame(self.root)
        self.main.pack(fill="both", expand=True)

        # left: contacts
        self.left = tk.Frame(self.main, width=280)
        self.left.pack(side="left", fill="y")
        self.left.pack_propagate(False)

        tk.Label(self.left, text="Контакты", font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=12, pady=(12, 6))

        self.contacts_view = ttk.Treeview(self.left, columns=(
            "status",), show="tree headings", height=18)
        self.contacts_view.heading("#0", text="Имя")
        self.contacts_view.heading("status", text="Статус")
        self.contacts_view.column("#0", width=160, anchor="w")
        self.contacts_view.column("status", width=80, anchor="center")
        self.contacts_view.pack(fill="both", expand=True, padx=10, pady=10)

        self.contacts_view.bind("<<TreeviewSelect>>", self._on_contact_select)

        # right: chat
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

        # messages (scrollable) using Text for simplicity
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

        # input
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

    # ---------- chat helpers ----------
    def _chat_log(self, text: str):
        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", text + "\n")
        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")

    def _dialog_key(self, other: str) -> frozenset[str]:
        return frozenset({self.username, other})

    def _render_active_dialog(self):
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")

        if not self.active_chat:
            self.chat_text.insert("end", "Выберите контакт слева.\n")
        else:
            key = self._dialog_key(self.active_chat)
            msgs = self.history.get(key, [])
            for sender, text in msgs:
                self.chat_text.insert("end", f"{sender}: {text}\n")

        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")

    # ---------- contacts rendering ----------
    def _render_contacts(self):
        for item in self.contacts_view.get_children():
            self.contacts_view.delete(item)

        for name in self.contacts:
            if name == self.username:
                continue  # себя не показываем в контактах
            status = "online" if name in self.online else "offline"
            self.contacts_view.insert(
                "", "end", iid=name, text=name, values=(status,))

    def _on_contact_select(self, event):
        sel = self.contacts_view.selection()
        if not sel:
            return
        other = sel[0]
        self.active_chat = other
        self.title_lbl.config(text=f"Диалог: {other}")
        self._render_active_dialog()
        self.entry.focus_set()

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
        self.username = name

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
            self.contacts = list(pkt.get("contacts", []))
            self.online = set(pkt.get("online", []))
            self._render_contacts()

        elif t == "presence":
            user = pkt.get("user")
            online = bool(pkt.get("online"))
            if isinstance(user, str) and user:
                if online:
                    self.online.add(user)
                else:
                    self.online.discard(user)
                self._render_contacts()

        elif t == "dm":
            sender = str(pkt.get("from", "???"))
            to = str(pkt.get("to", ""))
            text = str(pkt.get("text", ""))

            # определить "другую сторону" диалога
            other = sender if sender != self.username else to
            if not other:
                return

            key = self._dialog_key(other)
            self.history.setdefault(key, []).append((sender, text))

            # если этот диалог сейчас открыт — перерисовать
            if self.active_chat == other:
                self._render_active_dialog()
            else:
                # можно потом добавить индикатор непрочитанных
                pass

        elif t == "dm_ack":
            # можно позже рисовать галочку "доставлено"
            pass

        elif t == "system":
            self._chat_log("[system] " + str(pkt.get("text", "")))

        elif t == "error":
            self._chat_log("[error] " + str(pkt.get("text", "")))

    # ---------- send DM ----------
    def send_dm(self):
        if not self.running or not self.sock:
            return

        if not self.active_chat:
            self._chat_log("[system] Сначала выберите контакт слева.")
            return

        text = self.text_var.get().strip()
        if not text:
            return

        self.text_var.set("")

        # сразу добавим к себе в историю (оптимистично)
        key = self._dialog_key(self.active_chat)
        self.history.setdefault(key, []).append((self.username, text))
        self._render_active_dialog()

        try:
            send_packet(self.sock, {"type": "dm",
                        "to": self.active_chat, "text": text})
        except OSError:
            self._chat_log("[system] Не удалось отправить: нет соединения.")

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
