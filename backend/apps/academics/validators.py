"""Валидаторы загружаемых файлов.

Расширение проверяет ``FileExtensionValidator``, а здесь — размер и сигнатура:
имя ``*.pdf`` может скрывать HTML или произвольный бинарный файл.
"""

from django.core.exceptions import ValidationError

MAX_METHODIC_BYTES = 10 * 1024 * 1024  # 10 МБ


def validate_pdf_file(file):
    """Размер не больше лимита и заголовок ``%PDF-`` в первых 1024 байтах."""
    size = getattr(file, "size", 0)
    if size > MAX_METHODIC_BYTES:
        limit_mb = MAX_METHODIC_BYTES // (1024 * 1024)
        raise ValidationError(f"Файл больше {limit_mb} МБ.")

    try:
        file.seek(0)
        header = file.read(1024)
    finally:
        file.seek(0)

    if b"%PDF-" not in header:
        raise ValidationError("Файл не является PDF.")
