# Диаграммы

Три вида схемы — как в [dataroom-cms](https://github.com/sikuykus-lab/dataroom-cms):
**данные**, **взаимодействие пользователя**, **процессы администратора**.

Рендер: скопировать блок в [mermaid.live](https://mermaid.live).

## Схема данных

```mermaid
flowchart TB
  subgraph src ["Источники"]
    CRM["CRM API\nrooms · inspections"]
    IMP["Лист Импорт"]
    INS["Лист Инструкция"]
  end

  subgraph parse ["Парсеры"]
    PA["parser_template_a.py"]
    PB["parser_template_b.py"]
    AUD["object_audit.py"]
  end

  subgraph out ["Результат"]
    CELLS["Ячейки шаблона"]
    AUDSH["Лист Audit"]
  end

  CRM --> PA
  CRM --> PB
  IMP --> PA
  IMP --> PB
  INS --> PA
  INS --> PB
  PA --> CELLS
  PB --> CELLS
  PB --> AUD
  AUD --> AUDSH
```

## Процесс пользователя

```mermaid
flowchart LR
  A["Открыл шаблон\nутром"] --> B["Ячейки заполнены\nпосле ночного cron"]
  B --> C{"Audit OK?"}
  C -->|да| D["Отчёт в BI /\nсовещание"]
  C -->|нет| E["Строка audit →\nразбор с планом"]
```

## Процессы администратора

```mermaid
flowchart TD
  R1["16:40 / 16:45 UTC cron"] --> R2["*.cron.log"]
  R2 --> R3["ServerConsole\nf11 / f17"]
  R3 --> R4["При сбое —\nручной прогон SSH"]
```
