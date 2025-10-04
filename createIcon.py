# create_icon.py - Создание простой иконки для приложения
from PIL import Image, ImageDraw

def create_icon():
    """Создаёт простую иконку в стиле Telegram"""
    sizes = [16, 32, 48, 64, 128, 256]
    images = []
    
    for size in sizes:
        # Создаём квадратное изображение
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Синий круг (фон)
        margin = size // 8
        draw.ellipse(
            [margin, margin, size - margin, size - margin],
            fill='#27B0FF',
            outline=None
        )
        
        # Белый треугольник (стрелка) - упрощённый логотип Telegram
        triangle_margin = size // 4
        points = [
            (triangle_margin, triangle_margin),  # Верх
            (size - triangle_margin, size // 2),  # Право
            (triangle_margin + size // 8, size // 2 + size // 8),  # Низ-лево
            (triangle_margin, size - triangle_margin),  # Низ
            (triangle_margin - size // 16, size // 2 + size // 16),  # Центр-лево
        ]
        draw.polygon(points, fill='#FFFFFF')
        
        images.append(img)
    
    # Сохраняем как .ico с несколькими размерами
    images[0].save(
        'icon.ico',
        format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=images[1:]
    )
    print("✅ Иконка создана: icon.ico")

if __name__ == '__main__':
    create_icon()