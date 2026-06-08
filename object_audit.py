"""
Общий формат листа «выгрузка» по объектам для parser_template_a / parser_template_b.
Колонка на каждую ячейку отчёта (да/пусто) + диагностика и текст критериев.
"""
from __future__ import annotations

from datetime import date

# Порядок колонок «да» по ячейкам отчёта (Кв = I*, Кл = P*)
OBJECT_AUDIT_CELL_COLUMNS: tuple[str, ...] = (
    "I12", "P12", "I13", "P13",
    "I15", "P15", "I16", "P16",
    "I18", "P18", "I19", "P19", "I20", "P20",
    "H24", "H25", "H28", "H29",
    "C32", "C34", "L32", "L34", "V32", "V34",
    "Q32", "Q34",
    "I38", "P38", "I40", "P40", "I41", "P41", "I42", "P42", "I43", "P43",
    "I51", "P51", "I52", "P52", "I53", "P53", "I54", "P54",
    "I56", "P56",
    "I58", "I59", "P58", "P59",
    "F32", "F34",
    "K51", "R51",
)

# Ячейки только на уровне отчёта (на объекте всегда пусто)
REPORT_LEVEL_CELLS = frozenset({
    "I16", "P16", "I18", "P18", "I20", "P20",
    "H24", "H25", "H28", "H29",
    "I58", "I59", "P58", "P59", "Q32", "Q34",
})

OBJECT_AUDIT_BASE_HEADER: tuple[str, ...] = (
    "Дата выгрузки",
    "Тип ОН",
    "ID помещения",
    "Номер",
    "Статус помещения",
    "ДДУ/ДКП",
    "Последняя приёмка",
    "История приёмок (хронология)",
)

OBJECT_AUDIT_DIAG_HEADER: tuple[str, ...] = (
    "status_id",
    "insp: была «Не принята»",
    "insp: последняя (id/статус)",
    "red_pool (посл.=5)",
    "claim выполнено (посл. приёмка)",
)

OBJECT_AUDIT_TAIL_HEADER: tuple[str, ...] = (
    "Метрики (ячейки)",
    "Критерии попадания",
)


def object_audit_header() -> list[str]:
    return list(
        OBJECT_AUDIT_BASE_HEADER
        + OBJECT_AUDIT_CELL_COLUMNS
        + OBJECT_AUDIT_DIAG_HEADER
        + OBJECT_AUDIT_TAIL_HEADER
    )


def _yn(flag: bool) -> str:
    return "да" if flag else ""


def _cell_for_type(cell_id: str, on_type: str) -> bool:
    """Колонка относится к этому типу ОН (Кв → I*, Кл → P*)."""
    if cell_id in REPORT_LEVEL_CELLS:
        return False
    if on_type == "Кв":
        return cell_id.startswith("I") or cell_id in ("C32", "L32", "V32", "F32", "K51")
    if on_type == "Кл":
        return cell_id.startswith("P") or cell_id in ("C34", "L34", "V34", "F34", "R51")
    return False


def audit_cell_values(on_type: str, flags: dict[str, bool]) -> list[str]:
    out: list[str] = []
    for cell_id in OBJECT_AUDIT_CELL_COLUMNS:
        if not _cell_for_type(cell_id, on_type):
            out.append("")
            continue
        out.append(_yn(flags.get(cell_id, False)))
    return out


def join_criteria(reasons: dict[str, str]) -> str:
    parts = [f"{k}: {v}" for k, v in reasons.items() if v]
    return " | ".join(parts)


def build_flags_for_room(
    on_type: str,
    *,
    in_house: bool = True,
    is_free: bool = False,
    is_recommended: bool = False,
    is_zapisano: bool = False,
    is_c32: bool = False,
    is_ddu: bool = False,
    is_dkp: bool = False,
    is_green: bool = False,
    is_blue: bool = False,
    is_blue_ups: bool = False,
    is_blue_done: bool = False,
    is_blue_inwork: bool = False,
    is_red: bool = False,
    is_red_done: bool = False,
    is_red_ups: bool = False,
    is_red_inwork: bool = False,
    is_oapp: bool = False,
    is_k51_today: bool = False,
    is_f32_today: bool = False,
) -> dict[str, bool]:
    """Флаги по ячейкам отчёта для одного объекта (только своя половина I*/P*)."""
    flags: dict[str, bool] = {c: False for c in OBJECT_AUDIT_CELL_COLUMNS}
    if on_type == "Кв":
        flags["I12"] = in_house
        flags["I13"] = is_free
        flags["I15"] = is_recommended
        flags["I19"] = is_zapisano
        flags["C32"] = is_c32
        flags["L32"] = is_ddu
        flags["V32"] = is_dkp
        flags["I38"] = is_green
        flags["I40"] = is_blue
        flags["I41"] = is_blue_inwork
        flags["I42"] = is_blue_ups
        flags["I43"] = is_blue_done
        flags["I51"] = is_red
        flags["I52"] = is_red_inwork
        flags["I53"] = is_red_ups
        flags["I54"] = is_red_done
        flags["I56"] = is_oapp
        flags["K51"] = is_k51_today
        flags["F32"] = is_f32_today
    elif on_type == "Кл":
        flags["P12"] = in_house
        flags["P13"] = is_free
        flags["P15"] = is_recommended
        flags["P19"] = is_zapisano
        flags["C34"] = is_c32
        flags["L34"] = is_ddu
        flags["V34"] = is_dkp
        flags["P38"] = is_green
        flags["P40"] = is_blue
        flags["P41"] = is_blue_inwork
        flags["P42"] = is_blue_ups
        flags["P43"] = is_blue_done
        flags["P51"] = is_red
        flags["P52"] = is_red_inwork
        flags["P53"] = is_red_ups
        flags["P54"] = is_red_done
        flags["P56"] = is_oapp
        flags["R51"] = is_k51_today
        flags["F34"] = is_f32_today
    return flags


def build_object_audit_row(
    *,
    date_str: str,
    on_type: str,
    rid_i: int,
    number: str,
    room_status_label: str,
    contract_label: str,
    latest_insp_label: str,
    insp_history: str,
    flags: dict[str, bool],
    diag: dict[str, str],
    criteria: dict[str, str],
) -> list:
    metrics = [c for c in OBJECT_AUDIT_CELL_COLUMNS if flags.get(c)]
    row = [
        date_str,
        on_type,
        rid_i,
        number,
        room_status_label,
        contract_label,
        latest_insp_label,
        insp_history,
    ]
    row.extend(audit_cell_values(on_type, flags))
    row.extend([
        diag.get("status_id", ""),
        diag.get("insp_had_5", ""),
        diag.get("insp_latest", ""),
        diag.get("red_pool", ""),
        diag.get("claim_done_latest", ""),
        ", ".join(metrics),
        join_criteria(criteria),
    ])
    return row
