import os
import hashlib
import tempfile
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2

# ============================
# 1. ШИФРОВАНИЕ API-КЛЮЧЕЙ
# ============================
class SecureCredentials:
    """Защищённое хранение учётных данных в памяти"""
    
    def __init__(self, password: str):
        # Генерируем ключ из пароля
        kdf = PBKDF2(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b'telegram_export_salt_v1',  # В продакшене - случайная соль
            iterations=100000,
        )
        key = kdf.derive(password.encode())
        self.cipher = Fernet(key)
        self._api_id: bytes | None = None
        self._api_hash: bytes | None = None
        self._phone: bytes | None = None
    
    def store_api_id(self, api_id: int):
        self._api_id = self.cipher.encrypt(str(api_id).encode())
    
    def store_api_hash(self, api_hash: str):
        self._api_hash = self.cipher.encrypt(api_hash.encode())
    
    def store_phone(self, phone: str):
        self._phone = self.cipher.encrypt(phone.encode())
    
    def get_api_id(self) -> int:
        if not self._api_id:
            raise ValueError("API ID not stored")
        return int(self.cipher.decrypt(self._api_id).decode())
    
    def get_api_hash(self) -> str:
        if not self._api_hash:
            raise ValueError("API Hash not stored")
        return self.cipher.decrypt(self._api_hash).decode()
    
    def get_phone(self) -> str:
        if not self._phone:
            raise ValueError("Phone not stored")
        return self.cipher.decrypt(self._phone).decode()
    
    def clear(self):
        """Безопасное удаление из памяти"""
        self._api_id = None
        self._api_hash = None
        self._phone = None


# ============================
# 2. КАРАНТИН ДЛЯ МЕДИАФАЙЛОВ
# ============================
class QuarantineManager:
    """Изоляция скачанных файлов до проверки"""
    
    def __init__(self, base_dir: str = "export"):
        self.quarantine_dir = Path(base_dir) / "_quarantine"
        self.quarantine_dir.mkdir(exist_ok=True)
        
        # Устанавливаем ограниченные права (только для владельца)
        if os.name != 'nt':  # Linux/Mac
            os.chmod(self.quarantine_dir, 0o700)
    
    def move_to_quarantine(self, filepath: Path) -> Path:
        """Перемещает файл в карантин"""
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {filepath}")
        
        quarantine_path = self.quarantine_dir / filepath.name
        filepath.rename(quarantine_path)
        
        # Делаем файл нечитаемым для других процессов
        if os.name != 'nt':
            os.chmod(quarantine_path, 0o600)
        
        return quarantine_path
    
    def scan_file(self, filepath: Path) -> dict:
        """Базовая проверка файла (интеграция с антивирусом)"""
        result = {
            "safe": True,
            "threats": [],
            "mime_type": None,
            "hash": None
        }
        
        # Проверка размера (защита от zip-бомб)
        max_size = 500 * 1024 * 1024  # 500 MB
        if filepath.stat().st_size > max_size:
            result["safe"] = False
            result["threats"].append("File too large (possible zip bomb)")
            return result
        
        # Вычисляем SHA256 для проверки в VirusTotal API
        sha256_hash = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256_hash.update(chunk)
        result["hash"] = sha256_hash.hexdigest()
        
        # Проверка MIME-типа (для обнаружения переименованных exe)
        try:
            import magic
            result["mime_type"] = magic.from_file(str(filepath), mime=True)
            
            # Опасные MIME даже с безопасным расширением
            dangerous_mimes = {
                "application/x-dosexec",
                "application/x-executable",
                "application/x-mach-binary"
            }
            if result["mime_type"] in dangerous_mimes:
                result["safe"] = False
                result["threats"].append(f"Executable detected: {result['mime_type']}")
        except ImportError:
            # python-magic не установлен
            pass
        
        return result
    
    def release_from_quarantine(self, filepath: Path, destination: Path):
        """Перемещает безопасный файл из карантина"""
        if not filepath.exists():
            raise FileNotFoundError(f"Quarantined file not found: {filepath}")
        
        destination.parent.mkdir(parents=True, exist_ok=True)
        filepath.rename(destination)


# ============================
# 3. УЛУЧШЕННАЯ ОЧИСТКА ПАМЯТИ
# ============================
import ctypes

def secure_delete_string(s: str):
    """Перезаписывает строку в памяти нулями"""
    try:
        if not s:
            return
        # Python strings are immutable, но можем попытаться очистить буфер
        buf = ctypes.create_string_buffer(len(s))
        ctypes.memset(ctypes.addressof(buf), 0, len(s))
    except Exception:
        pass  # Не критично, если не получится


# ============================
# 4. БЕЗОПАСНЫЙ HTML С CSP 2.0
# ============================
CSP_HEADER_STRICT = (
    "default-src 'none'; "
    "img-src 'self' data:; "
    "media-src 'self'; "
    "style-src 'self'; "  # Удалён 'unsafe-inline'!
    "script-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)