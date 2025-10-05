"""
gui_app_logo_patch.py - Изменения для gui_app.py для поддержки кастомного лого

ИНСТРУКЦИЯ ПО ПРИМЕНЕНИЮ:
1. Добавьте import в начало gui_app.py:
   from logo_helper import load_logo_image, create_canvas_logo, create_tray_icon

2. Замените метод _build_header() на версию ниже
3. Замените метод _create_tray_image() на версию ниже

Или просто скопируйте нужные части кода ниже.
"""

# ═══════════════════════════════════════════════════
# ДОБАВЬТЕ В НАЧАЛО gui_app.py (после других импортов)
# ═══════════════════════════════════════════════════

from logo_helper import load_logo_image, create_canvas_logo, create_tray_icon
from PIL import ImageTk


# ═══════════════════════════════════════════════════
# ЗАМЕНИТЕ МЕТОД _build_header() В КЛАССЕ App
# ═══════════════════════════════════════════════════

def _build_header(self, parent: ttk.Frame) -> None:
    """Шапка приложения с кастомным лого"""
    
    # Попытка загрузить кастомное лого
    logo_size = 56
    logo_img = load_logo_image(logo_size)
    
    if logo_img:
        # ВАРИАНТ А: Используем PIL Image на Canvas (рекомендуется)
        try:
            logo_canvas = tk.Canvas(
                parent, 
                width=logo_size, 
                height=logo_size,
                highlightthickness=0, 
                bg=self.colors["glass"], 
                bd=0
            )
            logo_canvas.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))
            
            # Конвертируем PIL Image для tkinter
            photo = ImageTk.PhotoImage(logo_img)
            logo_canvas.create_image(0, 0, image=photo, anchor='nw')
            
            # ВАЖНО: сохраняем ссылку, чтобы изображение не удалилось
            logo_canvas._logo_photo = photo
            
        except Exception as e:
            print(f"Ошибка при загрузке лого: {e}")
            # Fallback на дефолтное лого
            self._draw_default_logo_canvas(parent, logo_size)
    else:
        # Если не удалось загрузить, рисуем дефолтное
        self._draw_default_logo_canvas(parent, logo_size)
    
    # Заголовок
    title = ttk.Label(parent, text="Telegram Export Studio", style="Header.TLabel")
    title.grid(row=0, column=1, sticky="w")
    
    subtitle = ttk.Label(
        parent, 
        text="Connect your private channels and archive everything with a single click.", 
        style="Caption.TLabel"
    )
    subtitle.grid(row=1, column=1, sticky="w", pady=(4, 0))
    
    # Кнопки действий
    action_row = ttk.Frame(parent, style="Glass.TFrame")
    action_row.grid(row=0, column=2, rowspan=2, sticky="e")
    
    ttk.Button(
        action_row, 
        text="Hide to tray", 
        style="Secondary.TButton", 
        command=self._minimize_to_tray
    ).grid(row=0, column=0, padx=(0, 8))
    
    ttk.Button(
        action_row, 
        text="Exit", 
        style="Accent.TButton", 
        command=self._on_exit
    ).grid(row=0, column=1)


def _draw_default_logo_canvas(self, parent: ttk.Frame, size: int = 56) -> None:
    """Рисует дефолтное лого на Canvas (fallback)"""
    logo_canvas = tk.Canvas(
        parent, 
        width=size, 
        height=size,
        highlightthickness=0, 
        bg=self.colors["glass"], 
        bd=0
    )
    logo_canvas.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))
    
    # Синий круг
    margin = size // 8
    logo_canvas.create_oval(
        margin, margin, size - margin, size - margin,
        fill=self.colors["accent"],
        outline=""
    )
    
    # Белый треугольник
    triangle_margin = size // 4
    points = [
        triangle_margin, triangle_margin,
        size - triangle_margin, size // 2,
        triangle_margin + size // 8, size // 2 + size // 8,
        triangle_margin, size - triangle_margin,
        triangle_margin - size // 16, size // 2 + size // 16,
    ]
    logo_canvas.create_polygon(
        points,
        fill=self.colors["accent_contrast"],
        outline=""
    )


# ═══════════════════════════════════════════════════
# ЗАМЕНИТЕ МЕТОД _create_tray_image() В КЛАССЕ App
# ═══════════════════════════════════════════════════

def _create_tray_image(self):
    """Создаёт иконку для трея из вашего лого"""
    if Image is None or ImageDraw is None:
        return None
    
    # Пытаемся загрузить кастомное лого
    tray_icon_img = create_tray_icon(size=64)
    
    if tray_icon_img:
        return tray_icon_img
    
    # Fallback: создаём дефолтное
    size = 64
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    
    # Синий круг
    draw.ellipse(
        (8, 8, 56, 56), 
        fill=self.colors.get('accent', '#229ED9')
    )
    
    # Белый треугольник
    draw.polygon(
        (26, 22, 44, 30, 30, 34, 26, 50, 22, 36), 
        fill=self.colors.get('accent_contrast', '#FFFFFF')
    )
    
    return image


# ═══════════════════════════════════════════════════
# ДОПОЛНИТЕЛЬНО: Установка иконки окна (для Windows)
# ═══════════════════════════════════════════════════

def set_window_icon(window, icon_path: str = None):
    """
    Устанавливает иконку окна (отображается в заголовке и панели задач)
    
    Добавьте в __init__ класса App после super().__init__():
    
    try:
        set_window_icon(self, "icon.ico")
    except Exception as e:
        print(f"Не удалось установить иконку окна: {e}")
    """
    try:
        if icon_path is None:
            # Пытаемся найти icon.ico
            from logo_helper import get_resource_path
            icon_path = get_resource_path("icon.ico")
        
        if os.path.exists(icon_path):
            window.iconbitmap(icon_path)
        else:
            print(f"Файл иконки не найден: {icon_path}")
    
    except Exception as e:
        print(f"Ошибка при установке иконки окна: {e}")


# ═══════════════════════════════════════════════════
# ПОЛНЫЙ ПРИМЕР ИНТЕГРАЦИИ
# ═══════════════════════════════════════════════════

"""
В методе __init__ класса App добавьте:

def __init__(self) -> None:
    super().__init__()
    self.title("Telegram Export Studio")
    self.geometry("1180x720")
    self.minsize(1100, 700)
    
    # НОВОЕ: Установка иконки окна
    try:
        from logo_helper import get_resource_path
        icon_path = get_resource_path("icon.ico")
        if os.path.exists(icon_path):
            self.iconbitmap(icon_path)
    except Exception as e:
        print(f"Не удалось установить иконку: {e}")
    
    # ... остальной код __init__ ...
"""


# ═══════════════════════════════════════════════════
# ТЕСТИРОВАНИЕ
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    print("Этот файл содержит патчи для gui_app.py")
    print("\nПроверка доступности лого:")
    
    import os
    from logo_helper import load_logo_image, get_resource_path
    
    # Проверяем наличие файлов
    files_to_check = ["logo.png", "logo_64.png", "logo_256.png", "icon.ico"]
    
    for filename in files_to_check:
        path = get_resource_path(filename)
        exists = os.path.exists(path)
        status = "✅" if exists else "❌"
        print(f"{status} {filename}: {path}")
    
    # Пробуем загрузить лого
    print("\nТестирование загрузки лого:")
    logo = load_logo_image(64)
    if logo:
        print("✅ Лого успешно загружено")
        print(f"   Размер: {logo.size}")
        print(f"   Режим: {logo.mode}")
    else:
        print("❌ Не удалось загрузить лого")
