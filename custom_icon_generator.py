"""
create_custom_icon.py - Создание иконки из вашего логотипа

Использование:
1. Положите ваше лого в файл "logo.png" (рекомендуемый размер: 512x512 или больше)
2. Запустите: python create_custom_icon.py
3. Получите icon.ico для использования в PyInstaller
"""

from PIL import Image, ImageDraw, ImageOps
import os
import sys


def create_icon_from_logo(logo_path="logo.png", output_path="icon.ico"):
    """
    Создаёт .ico файл из вашего логотипа
    
    Args:
        logo_path: путь к вашему лого (PNG, JPG, и т.д.)
        output_path: куда сохранить .ico файл
    """
    
    if not os.path.exists(logo_path):
        print(f"❌ Файл {logo_path} не найден!")
        print("\n📝 Инструкция:")
        print("1. Поместите ваше лого как 'logo.png' в эту папку")
        print("2. Рекомендуемый размер: 512x512 пикселей или больше")
        print("3. Формат: PNG (с прозрачностью) или JPG")
        print("\n💡 Если у вас нет лого, создам дефолтное...")
        create_default_icon(output_path)
        return
    
    try:
        # Открываем исходное изображение
        logo = Image.open(logo_path)
        print(f"✅ Загружено лого: {logo.size[0]}x{logo.size[1]} пикселей")
        
        # Конвертируем в RGBA если нужно
        if logo.mode != 'RGBA':
            logo = logo.convert('RGBA')
        
        # Размеры для Windows иконки
        sizes = [16, 32, 48, 64, 128, 256]
        images = []
        
        for size in sizes:
            # Создаём квадратное изображение с прозрачностью
            img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
            
            # Вписываем лого с сохранением пропорций
            logo_resized = logo.copy()
            logo_resized.thumbnail((size, size), Image.Resampling.LANCZOS)
            
            # Центрируем
            offset = ((size - logo_resized.size[0]) // 2,
                     (size - logo_resized.size[1]) // 2)
            img.paste(logo_resized, offset, logo_resized)
            
            images.append(img)
            print(f"  ✓ Создан размер {size}x{size}")
        
        # Сохраняем как .ico
        images[0].save(
            output_path,
            format='ICO',
            sizes=[(s, s) for s in sizes],
            append_images=images[1:]
        )
        
        print(f"\n✅ Иконка создана: {output_path}")
        print(f"📦 Размеры: {', '.join(str(s) for s in sizes)}")
        print("\n🚀 Теперь можно использовать в PyInstaller:")
        print(f"   pyinstaller --icon={output_path} ...")
        
    except Exception as e:
        print(f"❌ Ошибка при создании иконки: {e}")
        print("\n💡 Создам дефолтную иконку...")
        create_default_icon(output_path)


def create_default_icon(output_path="icon.ico"):
    """Создаёт дефолтную иконку в стиле Telegram"""
    sizes = [16, 32, 48, 64, 128, 256]
    images = []
    
    for size in sizes:
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Синий круг
        margin = size // 8
        draw.ellipse(
            [margin, margin, size - margin, size - margin],
            fill='#27B0FF',
            outline=None
        )
        
        # Белый треугольник (стрелка)
        triangle_margin = size // 4
        points = [
            (triangle_margin, triangle_margin),
            (size - triangle_margin, size // 2),
            (triangle_margin + size // 8, size // 2 + size // 8),
            (triangle_margin, size - triangle_margin),
            (triangle_margin - size // 16, size // 2 + size // 16),
        ]
        draw.polygon(points, fill='#FFFFFF')
        
        images.append(img)
    
    images[0].save(
        output_path,
        format='ICO',
        sizes=[(s, s) for s in sizes],
        append_images=images[1:]
    )
    print(f"✅ Создана дефолтная иконка: {output_path}")


def create_logo_backup():
    """Создаёт резервное лого для встраивания в приложение"""
    try:
        if os.path.exists("logo.png"):
            logo = Image.open("logo.png")
        else:
            # Создаём дефолтное лого
            logo = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
            draw = ImageDraw.Draw(logo)
            
            margin = 32
            draw.ellipse(
                [margin, margin, 256 - margin, 256 - margin],
                fill='#27B0FF',
                outline=None
            )
            
            triangle_margin = 64
            points = [
                (triangle_margin, triangle_margin),
                (256 - triangle_margin, 128),
                (triangle_margin + 32, 128 + 32),
                (triangle_margin, 256 - triangle_margin),
                (triangle_margin - 16, 128 + 16),
            ]
            draw.polygon(points, fill='#FFFFFF')
        
        # Сохраняем в разных размерах для GUI
        logo.save("logo_256.png")
        
        logo_64 = logo.copy()
        logo_64.thumbnail((64, 64), Image.Resampling.LANCZOS)
        logo_64.save("logo_64.png")
        
        print("✅ Созданы файлы лого для GUI:")
        print("   - logo_256.png (для заставки)")
        print("   - logo_64.png (для иконки в интерфейсе)")
        
    except Exception as e:
        print(f"⚠️  Не удалось создать резервное лого: {e}")


if __name__ == "__main__":
    print("=" * 60)
    print("  СОЗДАНИЕ ИКОНКИ ДЛЯ TELEGRAM EXPORT STUDIO")
    print("=" * 60)
    print()
    
    # Проверяем наличие кастомного лого
    if os.path.exists("logo.png"):
        print("✅ Найдено кастомное лого: logo.png")
        create_icon_from_logo()
    else:
        print("ℹ️  Кастомное лого не найдено")
        print("\n📝 Чтобы использовать своё лого:")
        print("1. Сохраните ваше изображение как 'logo.png'")
        print("2. Запустите этот скрипт снова")
        print("\nСоздаю дефолтную иконку...\n")
        create_default_icon()
    
    print()
    create_logo_backup()
    
    print("\n" + "=" * 60)
    print("✅ ГОТОВО!")
    print("=" * 60)
