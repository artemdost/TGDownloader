"""
logo_helper.py - Утилиты для работы с логотипом в GUI
"""

import os
import sys
from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageTk


def get_resource_path(relative_path: str) -> str:
    """
    Получить путь к ресурсу (работает в .exe и в разработке)
    
    PyInstaller распаковывает файлы во временную папку _MEIPASS
    """
    try:
        # PyInstaller создаёт временную папку и сохраняет путь в _MEIPASS
        base_path = sys._MEIPASS
    except AttributeError:
        # В режиме разработки используем корневую директорию проекта
        base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    return os.path.join(base_path, "assets", relative_path)


def load_logo_image(size: int = 64) -> Optional[Image.Image]:
    """
    Загружает лого из файла или создаёт дефолтное
    
    Args:
        size: желаемый размер (будет подогнано с сохранением пропорций)
    
    Returns:
        PIL.Image или None если не удалось загрузить
    """
    # Пробуем найти кастомное лого
    logo_paths = [
        get_resource_path(f"logo_{size}.png"),
        get_resource_path("logo.png")
    ]
    
    for path in logo_paths:
        if os.path.exists(path):
            try:
                logo = Image.open(path)
                # Подгоняем размер с сохранением пропорций
                logo.thumbnail((size, size), Image.Resampling.LANCZOS)
                return logo
            except Exception as e:
                print(f"Не удалось загрузить {path}: {e}")
                continue
    
    # Если не нашли, создаём дефолтное
    return create_default_logo(size)


def create_default_logo(size: int = 64) -> Image.Image:
    """
    Создаёт дефолтное лого в стиле Telegram
    
    Args:
        size: размер иконки
    
    Returns:
        PIL.Image с дефолтным лого
    """
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Синий круг (фон)
    margin = size // 8
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill='#27B0FF',
        outline=None
    )
    
    # Белый треугольник (стрелка Telegram)
    triangle_margin = size // 4
    points = [
        (triangle_margin, triangle_margin),
        (size - triangle_margin, size // 2),
        (triangle_margin + size // 8, size // 2 + size // 8),
        (triangle_margin, size - triangle_margin),
        (triangle_margin - size // 16, size // 2 + size // 16),
    ]
    draw.polygon(points, fill='#FFFFFF')
    
    return img


def get_logo_for_canvas(canvas_size: Tuple[int, int], 
                        logo_size: Optional[int] = None) -> Tuple[Image.Image, int]:
    """
    Подготавливает лого для отрисовки на Canvas
    
    Args:
        canvas_size: размер области Canvas (width, height)
        logo_size: желаемый размер лого (если None, вычисляется автоматически)
    
    Returns:
        (PIL.Image, размер) - готовое изображение и его размер
    """
    if logo_size is None:
        # Вычисляем оптимальный размер (80% от минимальной стороны canvas)
        logo_size = int(min(canvas_size) * 0.8)
    
    logo = load_logo_image(logo_size)
    
    if logo is None:
        logo = create_default_logo(logo_size)
    
    return logo, logo_size


def create_tray_icon(size: int = 64) -> Optional[Image.Image]:
    """
    Создаёт иконку для трея (системного лотка)
    
    Args:
        size: размер иконки
    
    Returns:
        PIL.Image для использования в pystray
    """
    return load_logo_image(size)


# Вспомогательная функция для интеграции с tkinter
def create_canvas_logo(canvas, x: int, y: int, size: int = 56) -> Optional[int]:
    """
    Создаёт и отрисовывает лого на tkinter Canvas
    
    Args:
        canvas: tkinter.Canvas объект
        x, y: координаты (левый верхний угол)
        size: размер лого
    
    Returns:
        ID созданного изображения на canvas или None
    """
    try:
        logo_img = load_logo_image(size)
        if logo_img is None:
            return None
        
        # Конвертируем для tkinter
        photo = ImageTk.PhotoImage(logo_img)
        
        # Сохраняем ссылку, чтобы изображение не было удалено сборщиком мусора
        if not hasattr(canvas, '_logo_images'):
            canvas._logo_images = []
        canvas._logo_images.append(photo)
        
        # Создаём изображение на canvas
        img_id = canvas.create_image(x, y, image=photo, anchor='nw')
        
        return img_id
        
    except Exception as e:
        print(f"Ошибка при создании лого на canvas: {e}")
        return None
