# Colloquium DevSpace — Выбор инструкции развёртывания

Проект развёртывается на **Linux** или **Windows**. Выберите нужную инструкцию:

---

## 🐧 Linux / macOS

**Файл**: [`DEPLOY.md`](DEPLOY.md)

Для систем с bash, sh, или zsh. Репозитории обычно в `~/GitHub/`.

```bash
cd ~/docker/cqds
docker compose up -d
```

---

## 🪟 Windows (PowerShell, CMD)

**Файл**: [`DEPLOY-MSWIN.md`](DEPLOY-MSWIN.md)

Для Windows с Docker Desktop. Использует PowerShell-команды.

```powershell
cd p:\opt\docker\cqds
docker compose up -d
```

---

## 📋 Общая информация

Оба документа содержат:
- Копирование файлов из двух репозиториев (Colloquium-DevSpace + Sandwich-pack)
- Создание конфигураций (mcp_config.toml, llm_pre_prompt.md)
- Сборка Docker-образов
- Инициализация БД и первый вход
- Устранение неполадок

**Основные отличия**:
- **Linux**: `bash`, `cp`, `tail`, пути с `/`
- **Windows**: `PowerShell`, `Copy-Item`, `Get-Content`, пути с `\` или `/`

---

## 🔗 Репозитории

- **Colloquium-DevSpace**: https://github.com/[user]/Colloquium-DevSpace
- **Sandwich-pack**: https://github.com/[user]/Sandwich-pack

**Локальные пути**:
- Linux: `~/GitHub/Colloquium-DevSpace` и `~/GitHub/Sandwich-pack`
- Windows: `p:\GitHub\Colloquium-DevSpace` и `p:\GitHub\Sandwich-pack`

---

## 🚀 Быстрый старт

### Linux
```bash
source <(cat <<'SCRIPT'
cd ~/docker/cqds
git clone https://github.com/[user]/Colloquium-DevSpace.git ~/GitHub/Colloquium-DevSpace
git clone https://github.com/[user]/Sandwich-pack.git ~/GitHub/Sandwich-pack
cp -r ~/GitHub/Colloquium-DevSpace/* ~/docker/cqds/
cp -r ~/GitHub/Sandwich-pack/src/lib ~/docker/cqds/agent/
docker compose up -d
SCRIPT
)
```

### Windows (PowerShell)
```powershell
cd p:\opt\docker\cqds
git clone https://github.com/[user]/Colloquium-DevSpace.git p:\GitHub\Colloquium-DevSpace
git clone https://github.com/[user]/Sandwich-pack.git p:\GitHub\Sandwich-pack
Copy-Item -Path "p:\GitHub\Colloquium-DevSpace\*" -Destination "p:\opt\docker\cqds\" -Recurse -Force
Copy-Item -Path "p:\GitHub\Sandwich-pack\src\lib" -Destination "p:\opt\docker\cqds\agent\" -Recurse -Force
docker compose up -d
```

---

## ❓ Выбор правильного документа

| Ваша ОС | Документ | Команды |
|---------|----------|---------|
| Ubuntu, Debian, CentOS, Fedora | [`DEPLOY.md`](DEPLOY.md) | `bash`, `cp`, `sudo` |
| macOS | [`DEPLOY.md`](DEPLOY.md) | `bash`, `cp`, `brew` |
| Windows 10/11 (PowerShell) | [`DEPLOY-MSWIN.md`](DEPLOY-MSWIN.md) | `PowerShell`, `Copy-Item` |
| Windows 10/11 (WSL2 + bash) | [`DEPLOY.md`](DEPLOY.md) | `bash`, `cp` (в WSL) |
| Windows (Git Bash) | [`DEPLOY.md`](DEPLOY.md) | `bash`, `cp` (в Git Bash) |

---

## 📚 Дополнительные документы

- **`SANDWICH.md`** — API и использование библиотеки Sandwich-pack
- **`README.md`** (Colloquium-DevSpace) — дизайн системы и архитектура
- **`DEPLOY-README.md`** (этот файл) — навигация между инструкциями

---

## 🆘 Если возникли проблемы

1. **Проверить выбранный документ** — выбрана ли нужная инструкция для вашей ОС?
2. **Проверить пути** — адаптированы ли команды под вашу систему?
3. **Проверить Docker** — установлен ли Docker и доступен ли из командной строки?
4. **Проверить репозитории** — клонированы ли оба репозитория?

Подробнее см. раздел "Проблемы и решения" в выбранной инструкции.

---

**Версия**: 1.0  
**Последнее обновление**: 24 марта 2026 г.
