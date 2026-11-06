# Модели

Все модели добавляются в LiteLLM. LibreChat видит их сразу после обновления страницы. dsh получает список автоматически, пока его настройки моделей не правили вручную.

Добавить модель можно двумя способами: помощником `add-model.sh` или в интерфейсе LiteLLM. Результат одинаковый.

## Помощник add-model.sh

```text
bash add-model.sh <провайдер> <модель> [--name ИМЯ] [--api-base АДРЕС] [--no-login]
bash add-model.sh list
bash add-model.sh remove ИМЯ
```

`--name` задаёт имя модели в чатах. По умолчанию имя совпадает с моделью.

| Провайдер | Пример | Что нужно |
|---|---|---|
| `ollama` | `bash add-model.sh ollama qwen3:8b` | Запущенная Ollama и скачанная модель |
| `deepseek` | `bash add-model.sh deepseek deepseek-v4-flash` | Ключ [DeepSeek](https://platform.deepseek.com/) |
| `openai`, `anthropic`, `gemini`, `openrouter` | `bash add-model.sh openrouter qwen/qwen3-coder --name coder` | Ключ провайдера |
| `compatible` | `bash add-model.sh compatible my-model --api-base https://gw.example/v1` | Любой OpenAI-совместимый API |
| `chatgpt` | `bash add-model.sh chatgpt gpt-5.5` | Подписка ChatGPT |
| `mock` | `bash add-model.sh mock` | Ничего: модель отвечает заготовленным текстом |

Ключ помощник спрашивает без вывода на экран. В скриптах ключ можно передать через переменную `MODEL_API_KEY`. Ключ не сохраняется в файлах стека: он уходит только в LiteLLM.

## Локальная модель Ollama

Ollama работает на машине, а не в Docker. LiteLLM обращается к ней из контейнера.

- **Ollama в Windows, стек в WSL:** помощник по умолчанию использует адрес `http://windows-host:11434`. `deploy.sh` при каждом запуске записывает IP Windows для этого имени. Ollama должна принимать подключения не только с `127.0.0.1`:
  1. Задайте переменную окружения Windows `OLLAMA_HOST=0.0.0.0:11434` и перезапустите Ollama.
  2. Разрешите в брандмауэре Windows входящие подключения к порту 11434 из сети WSL.
- **Ollama в WSL или Linux:** укажите `--api-base http://host.docker.internal:11434`, а Ollama должна слушать `0.0.0.0`.

Модель сначала скачивается в Ollama, потом регистрируется:

```bash
ollama pull qwen3:8b
bash add-model.sh ollama qwen3:8b
```

## API по ключу

Оплата идёт по токенам у провайдера, расходы видны в LiteLLM в разделе **Usage**.

```bash
bash add-model.sh deepseek deepseek-v4-pro
bash add-model.sh anthropic claude-sonnet-4-5
bash add-model.sh compatible qwen-max --api-base https://dashscope.example/v1
```

Модели DeepSeek V4 рассуждают по умолчанию. Для dsh помощник сам добавляет переключатель рассуждения (`thinkingFormat: deepseek`), и в dsh появляется выбор уровня Effort.

## Подписка ChatGPT

LiteLLM умеет работать через подписку ChatGPT вместо API-ключа. После регистрации модели помощник запускает вход: откройте показанную ссылку и введите код. Токен хранится в томе `litellm-chatgpt` и обновляется сам.

```bash
bash add-model.sh chatgpt gpt-5.5
```

Если вход нужно повторить, удалите модель и добавьте её снова или выполните вход вручную:

```bash
docker compose -f ~/llm-stack/repo/docker-compose.yml --env-file ~/llm-stack/.env exec litellm \
  python3 -c 'from litellm.llms.chatgpt.authenticator import Authenticator; Authenticator().get_access_token()'
```

Подписка личная: не открывайте этот стек другим людям.

## Через интерфейс LiteLLM

1. Откройте http://localhost:4000/ui и войдите как `admin`.
2. Откройте **Models + Endpoints** и нажмите **Add Model**.
3. Выберите провайдера, модель и укажите ключ. Для Ollama выберите провайдера Ollama и укажите API Base из раздела выше.

Модель, добавленная так, попадёт в dsh при следующем запуске `deploy.sh` или `add-model.sh`.

## Модели в dsh

`deploy.sh` и `add-model.sh` записывают в dsh провайдер `litellm` со всеми моделями из LiteLLM. Файл настроек: `~/llm-stack/dsh-home/profiles/web/cordis.patch.yml`.

Если вы поменяли настройки моделей в самом dsh, скрипты перестают перезаписывать этот файл, чтобы не затереть ваши правки. Тогда обновляйте список в dsh: **Settings → Models → litellm → Fetch available models**. Чтобы снова отдать файл под управление скриптов, удалите его и запустите `deploy.sh`.
