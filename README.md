# Локальный стек LibreChat + LiteLLM

LibreChat даёт интерфейс чата. LiteLLM хранит модели и ключи провайдеров и показывает запросы и расходы. Оба работают из официальных образов.

## Запуск

Windows, Git Bash:

```bash
bash deploy.sh
```

В WSL или Linux команда та же. Скрипт получает этот репозиторий, скачивает образы, запускает контейнеры и печатает ссылки.

Нужны Docker в WSL Ubuntu и доступ к репозитории: на Windows через `gh auth login`, в Linux через `GH_TOKEN` или настроенный git.

| Сервис | Адрес |
|---|---|
| LibreChat | http://localhost:3080 |
| LiteLLM | http://localhost:4000/ui |

Порты доступны только с этой машины. Секреты создаются при первом запуске в `~/llm-stack/.env` внутри WSL и в Git не попадают.

## Добавить модель

1. Откройте LiteLLM, раздел **Models + Endpoints**, и нажмите **Add Model**.
2. Выберите провайдера, модель и укажите ключ.
3. Обновите LibreChat и выберите модель в endpoint **LiteLLM**.

## Обновить версию

Образы закреплены по версии и digest в `docker-compose.yml`. Не используйте `latest`, `main-latest` и `main-stable`: 21 октября 2026 года под этими тегами LiteLLM публиковался вредоносный образ.

1. Выберите релиз и проверьте [рекомендации по безопасности LiteLLM](https://github.com/BerriAI/litellm/security/advisories) или [релизы LibreChat](https://github.com/LibreChat-AI/LibreChat/releases).
2. Для LiteLLM проверьте подпись по [инструкции](https://docs.litellm.ai/docs/proxy/docker_image_security).
3. Получите digest и замените версию и digest в `docker-compose.yml`:

   ```bash
   docker buildx imagetools inspect ghcr.io/berriai/litellm:vX.Y.Z
   ```

4. Отправьте изменения в `main` и запустите `deploy.sh`.

Если изменилась версия схемы конфигурации LibreChat, обновите `version` в `librechat.yaml`.

## Управление

```bash
cd ~/llm-stack
F="-f repo/docker-compose.yml --env-file .env"
docker compose $F ps
docker compose $F logs -f litellm
docker compose $F down        # остановить, данные сохранятся
docker compose $F down -v     # удалить вместе с данными
```
