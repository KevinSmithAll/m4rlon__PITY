import tkinter as tk
from tkinter import ttk


class ChatUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Messenger UI")
        self.root.geometry("900x600")
        self.root.configure(bg="#ededed")

        self.setup_ui()

    def setup_ui(self):
        # ====== MAIN CONTAINER ======
        main_frame = tk.Frame(self.root, bg="#ededed")
        main_frame.pack(fill="both", expand=True)

        # ====== LEFT PANEL (Chats List) ======
        self.left_panel = tk.Frame(main_frame, bg="#ffffff", width=250)
        self.left_panel.pack(side="left", fill="y")

        tk.Label(
            self.left_panel,
            text="Чаты",
            bg="#ffffff",
            font=("Arial", 14, "bold")
        ).pack(pady=10)

        self.chat_list = tk.Listbox(
            self.left_panel,
            bg="#ffffff",
            bd=0,
            highlightthickness=0,
            font=("Arial", 12),
            activestyle="none"
        )
        self.chat_list.pack(fill="both", expand=True, padx=10)

        # Тестовые чаты
        for name in ["Алексей", "Мария", "Работа", "Друзья"]:
            self.chat_list.insert("end", name)

        # ====== RIGHT PANEL (Chat Area) ======
        self.right_panel = tk.Frame(main_frame, bg="#e5ddd5")
        self.right_panel.pack(side="right", fill="both", expand=True)

        # ====== HEADER ======
        self.header = tk.Frame(self.right_panel, bg="#075e54", height=60)
        self.header.pack(fill="x")

        self.chat_title = tk.Label(
            self.header,
            text="Алексей",
            bg="#075e54",
            fg="white",
            font=("Arial", 14, "bold")
        )
        self.chat_title.pack(pady=15)

        # ====== MESSAGES AREA ======
        self.messages_frame = tk.Frame(self.right_panel, bg="#e5ddd5")
        self.messages_frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(
            self.messages_frame,
            bg="#e5ddd5",
            highlightthickness=0
        )
        self.scrollbar = ttk.Scrollbar(
            self.messages_frame,
            orient="vertical",
            command=self.canvas.yview
        )

        self.scrollable_frame = tk.Frame(self.canvas, bg="#e5ddd5")

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")
            )
        )

        self.canvas.create_window(
            (0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # Примеры сообщений
        self.add_message("Привет!", incoming=True)
        self.add_message("Привет 🙂 Как дела?", incoming=False)
        self.add_message("Все отлично!", incoming=True)
        self.add_message("Рад слышать!", incoming=False)

        # ====== INPUT AREA ======
        self.input_frame = tk.Frame(self.right_panel, bg="#f0f0f0", height=60)
        self.input_frame.pack(fill="x")

        self.message_entry = tk.Entry(
            self.input_frame,
            font=("Arial", 12),
            bd=0
        )
        self.message_entry.pack(
            side="left",
            fill="both",
            expand=True,
            padx=10,
            pady=10
        )

        self.send_button = tk.Button(
            self.input_frame,
            text="Отправить",
            bg="#25d366",
            fg="white",
            bd=0,
            font=("Arial", 12, "bold"),
            activebackground="#128c7e",
            activeforeground="white"
        )
        self.send_button.pack(padx=10, pady=10, side="right")

    def add_message(self, text, incoming=True):
        msg_frame = tk.Frame(self.scrollable_frame, bg="#e5ddd5")

        if incoming:
            bubble = tk.Label(
                msg_frame,
                text=text,
                bg="white",
                wraplength=300,
                justify="left",
                font=("Arial", 11),
                padx=10,
                pady=5
            )
            bubble.pack(anchor="w", padx=10, pady=5)
        else:
            bubble = tk.Label(
                msg_frame,
                text=text,
                bg="#dcf8c6",
                wraplength=300,
                justify="left",
                font=("Arial", 11),
                padx=10,
                pady=5
            )
            bubble.pack(anchor="e", padx=10, pady=5)

        msg_frame.pack(fill="both", expand=True)


if __name__ == "__main__":
    root = tk.Tk()
    app = ChatUI(root)
    root.mainloop()
