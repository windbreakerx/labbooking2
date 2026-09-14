"""XLSX-экспорт сетки расписания лаборатории (день 7): лист на аудиторию.

Разметку пар×дней не дублирует — переиспользует build_schedule_grid; записи
вне сетки пар выводятся секцией под сеткой. Стилизация — как в v1
(шапка, границы, закрепление B2).
"""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from apps.scheduling.services.schedule_admin import build_schedule_grid, entries_prefetched
from apps.scheduling.services.slot_grid import UNIVERSITY_PAIR_SLOTS

WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_HEADER_FILL = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_THIN_BORDER = Border(
    left=Side(style="thin", color="BFBFBF"),
    right=Side(style="thin", color="BFBFBF"),
    top=Side(style="thin", color="BFBFBF"),
    bottom=Side(style="thin", color="BFBFBF"),
)


def generate_lab_schedule_excel(laboratory, semester) -> bytes:
    """Сетка расписания лаборатории: по листу на аудиторию активного семестра."""
    wb = Workbook()
    rooms = list(laboratory.rooms.order_by("number"))
    if semester is None or not rooms:
        wb.active.title = "Расписание"
        wb.active.append(["Нет активных записей расписания"])
    else:
        wb.remove(wb.active)
        for room in rooms:
            sheet = wb.create_sheet(title=_sanitize_sheet_title(f"Ауд. {room.number}"))
            entries = list(entries_prefetched(room, semester))
            rows, unplaced = build_schedule_grid(entries)
            _fill_sheet(sheet, rows, unplaced)
            _format_sheet(sheet)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sanitize_sheet_title(title: str) -> str:
    for char in "\\/*?:[]":
        title = title.replace(char, "-")
    return title[:31]


def _chip_text(chip: dict) -> str:
    """Текст ячейки: дисциплины/ЛР слота, дежурный, чётность, места."""
    entry = chip["entry"]
    works = sorted(
        {
            work.title
            for selection in entry.discipline_selections.all()
            for work in selection.lab_works.all()
        }
    )
    duty = entry.duty_person.full_name if entry.duty_person else "—"
    lines = [chip["title"] or "Слот"]
    if chip["time_note"]:
        lines.append(f"  {chip['time_note']}")
    if works:
        lines.append(f"  ЛР: {', '.join(works)}")
    lines.append(f"  Дежурный: {duty} ({entry.get_duty_role_display()})")
    lines.append(f"  Неделя: {chip['parity_label'] or 'Каждая'} | Мест: {entry.capacity}")
    return "\n".join(lines)


def _fill_sheet(ws, rows, unplaced) -> None:
    ws.cell(row=1, column=1, value="День / Пара")
    for col, (pair_number, _start, _end) in enumerate(UNIVERSITY_PAIR_SLOTS, 2):
        ws.cell(row=1, column=col, value=f"{pair_number} пара")
    for row_idx, label in enumerate(WEEKDAY_LABELS, 2):
        ws.cell(row=row_idx, column=1, value=label)
    for col, row in enumerate(rows, 2):
        for weekday, cell in enumerate(row["cells"]):
            if cell:
                ws.cell(row=weekday + 2, column=col, value="\n\n".join(_chip_text(chip) for chip in cell))
    if unplaced:
        foot = len(WEEKDAY_LABELS) + 3
        ws.cell(row=foot, column=1, value="Вне сетки пар")
        ws.cell(row=foot, column=2, value="\n\n".join(_chip_text(chip) for chip in unplaced))


def _format_sheet(ws) -> None:
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell_align = Alignment(vertical="top", wrap_text=True)
    max_row = len(WEEKDAY_LABELS) + 1
    max_col = len(UNIVERSITY_PAIR_SLOTS) + 1
    for row_idx in range(1, max_row + 1):
        for col_idx in range(1, max_col + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.border = _THIN_BORDER
            if row_idx == 1 or col_idx == 1:
                cell.font = _HEADER_FONT
                cell.fill = _HEADER_FILL
                cell.alignment = header_align
            else:
                cell.alignment = cell_align
    ws.freeze_panes = "B2"
    ws.column_dimensions[get_column_letter(1)].width = 12
    for col_idx in range(2, max_col + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 35
