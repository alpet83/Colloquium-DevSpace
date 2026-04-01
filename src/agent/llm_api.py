# /app/agent/lib/llm_api.py, updated 2025-08-11 12:02 EEST
# Formatted with proper line breaks and indentation for project compliance.

import aiohttp

import json
import re
import codecs
import threading
import time
import urllib.error
import urllib.request
# from typing import dict, Optional  PROHIBITED OBSOLETE CODE, NEVER USE!
from managers.db import Database
from managers.runtime_config import get_int
from openai import AsyncOpenAI, RateLimitError, APIStatusError, APIConnectionError
import globals
import datetime

Optional = Dict = None
log = globals.get_logger("llm_api")

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_openrouter_pricing_lock = threading.Lock()
_openrouter_pricing_cache: dict | None = None  # {"t": float, "by_id": dict[str, tuple[float, float]]}


def _openrouter_pricing_cache_ttl_sec() -> int:
    return get_int("OPENROUTER_PRICING_CACHE_TTL", 3600, 60, 999_999_999)


def _openrouter_fetch_models_pricing() -> dict[str, tuple[float, float]]:
    req = urllib.request.Request(
        OPENROUTER_MODELS_URL,
        headers={"User-Agent": "cqds-colloquium/openrouter-pricing"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out: dict[str, tuple[float, float]] = {}
    for m in data.get("data") or []:
        mid = m.get("id")
        if not mid:
            continue
        pr = m.get("pricing") or {}
        try:
            p = float(pr.get("prompt") or 0)
            c = float(pr.get("completion") or 0)
        except (TypeError, ValueError):
            p, c = 0.0, 0.0
        out[str(mid)] = (p, c)
    return out


def _openrouter_pricing_map_cached() -> dict[str, tuple[float, float]]:
    """Кэш каталога OpenRouter (USD за 1 токен → позже ×1e6 для users)."""
    global _openrouter_pricing_cache
    now = time.time()
    ttl = _openrouter_pricing_cache_ttl_sec()
    with _openrouter_pricing_lock:
        c = _openrouter_pricing_cache
        if c is not None and (now - c["t"]) <= ttl:
            return c["by_id"]
    try:
        by_id = _openrouter_fetch_models_pricing()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
        log.warn("OpenRouter: не удалось загрузить %s: %s", OPENROUTER_MODELS_URL, e)
        with _openrouter_pricing_lock:
            if _openrouter_pricing_cache is not None:
                return _openrouter_pricing_cache["by_id"]
        return {}
    with _openrouter_pricing_lock:
        _openrouter_pricing_cache = {"t": time.time(), "by_id": by_id}
        return by_id


def _openrouter_pricing_valid(prompt_per_token: float, completion_per_token: float, model_id: str) -> bool:
    """Нулевые/отрицательные цены — ошибка, кроме моделей с подстрокой «free» в id (регистр не важен)."""
    if prompt_per_token < 0 or completion_per_token < 0:
        return False
    if "free" in model_id.lower():
        return True
    return prompt_per_token > 0 and completion_per_token > 0


def _maybe_sync_openrouter_user_pricing(db, user_id: int | None, openrouter_model_id: str) -> None:
    """Обновляет users.tokens_input_cost / tokens_output_cost (USD за 1M токенов) из каталога OpenRouter."""
    if user_id is None or not db.is_postgres():
        return
    by_id = _openrouter_pricing_map_cached()
    if not by_id:
        return
    pair = by_id.get(openrouter_model_id)
    if pair is None:
        log.warn(
            "OpenRouter: модель «%s» не найдена в API, tokens_*_cost не обновлены (user_id=%s)",
            openrouter_model_id,
            user_id,
        )
        return
    p_tok, c_tok = pair
    if not _openrouter_pricing_valid(p_tok, c_tok, openrouter_model_id):
        log.error(
            "OpenRouter: отклонены цены для «%s»: prompt=%s completion=%s "
            "(ожидаются положительные оба, если в id нет «free»)",
            openrouter_model_id,
            p_tok,
            c_tok,
        )
        return
    in_m = p_tok * 1_000_000.0
    out_m = c_tok * 1_000_000.0
    try:
        db.execute(
            "UPDATE users SET tokens_input_cost = :a, tokens_output_cost = :b WHERE user_id = :uid",
            {"a": in_m, "b": out_m, "uid": int(user_id)},
        )
        log.info(
            "OpenRouter: user_id=%s model=%s — tokens_input_cost=%.8g tokens_output_cost=%.8g (USD за 1M токенов)",
            user_id,
            openrouter_model_id,
            in_m,
            out_m,
        )
    except Exception as e:
        # db.execute уже логирует через log.excpt; здесь только краткое пояснение без второго traceback
        log.warn("OpenRouter: не обновлены цены в users для user_id=%s: %s", user_id, e)


class LLMConnection:
    def __init__(self, config: dict):
        self.api_key = config.get("api_key")
        self.name = "Local"
        self.model = config.get("model", "default")
        self.flood = [r'\.\s+\?']
        self.last_result = {}
        self.base_url = 'http://localhost'  # dummy local model
        self.search_sources = ["web"]
        self.db = Database.get_database()
        self.pre_prompt = ''
        self.payload = {}
        # TODO: нужно считывать параметры role, max_tokens, max_completion_tokens, reasoning_effort из таблицы users, поле JSON model_params
        self.model_params = {"max_tokens": 27000, "max_completion_tokens": 13500, "temperature": 0.3}
        self.timeout = aiohttp.ClientTimeout(total=600, connect=45)  # default for OpenAI

    def _process_result(self, result: dict, http_status=200):
        self.last_result = result
        sr = []
        if http_status != 200:
            log.error(f"Ошибка {self.name} API: status=%d, response=%s", http_status, json.dumps(result, indent=2))
        else:
            log.debug(f"Ответ {self.name} API: %s", json.dumps(result, indent=2))
            sr = self.get_search_results()

        return {
            "text": self.get_text(),
            "usage": result.get("usage", {}),
            "search_results": sr
        }

    def api_error_result(self, _class: str, e: Exception, code: int = 500):
        if hasattr(e, 'status_code'):
            code = e.status_code
            log.error(f"Ошибка {self.name} API (%s) %s: status=%d", _class, str(e), code)
        else:
            log.excpt(f"Сбой {self.name} API (%s):", _class, e=e)
        result = None
        if hasattr(e, 'response'):
            try:
                result = json.loads(e.response.text)
            except BaseException as E:
                log.excpt("Response load fails ", e=E)
        if isinstance(result, dict) and hasattr(result, 'choices'):
            return self._process_result(result, http_status=code)
        else:
            return {"text": f"<llm_error>{_class} API Error: {e}\nhttp_status: {code}</llm_error>",
                    "usage": {},
                    "search_results": []}

    async def call(self) -> dict:
        """Calls the LLM API using OpenAI SDK, overriding in subclasses for specific APIs."""
        _void = {"choices": []}
        try:
            messages = self.payload.get("messages", [])
            if not messages:
                log.error("Нет сообщений для запроса к API")
                return {"text": "<llm_error>No messages in payload\nError code: InvalidRequest\nProvider: %s</llm_error>" % self.name, "usage": {}}
            client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout.total)
            comp = await client.chat.completions.create(**self.payload)
            return self._process_result(comp.to_dict())
        except TypeError as e:
            # Some providers/models reject reasoning args; retry once without them.
            err = str(e)
            if ('reasoning' in err or 'reasoning_effort' in err) and isinstance(self.payload, dict):
                payload = dict(self.payload)
                payload.pop('reasoning', None)
                payload.pop('reasoning_effort', None)
                try:
                    client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout.total)
                    comp = await client.chat.completions.create(**payload)
                    self.payload = payload
                    return self._process_result(comp.to_dict())
                except Exception as inner_e:
                    return self.api_error_result('Unknown', inner_e, 500)
            return self.api_error_result('Unknown', e, 500)
        except RateLimitError as e:
            return self.api_error_result('RateLimit', e, 429)
        except APIStatusError as e:
            return self.api_error_result('Status', e)
        except APIConnectionError as e:
            return self.api_error_result('Connection', e, 503)
        except Exception as e:
            return self.api_error_result('Unknown', e, 500)

    async def call_debug(self) -> dict:
        try:
            messages = self.payload.get("messages", [])
            if messages:
                log.debug(f"{self.name} API вызов: model=%s, макро-сообщений %d", self.model, len(messages))
            else:
                log.error(" Нет сообщений для запроса к API")
                return {}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json; charset=utf-8",
                            "Accept": "application/json; charset=UTF-8",
                            "Accept-Charset": "UTF-8"
                        },
                        json=self.payload,
                        timeout=self.timeout
                ) as response:
                    result = await response.json()
                    return self._process_result(result, response.status)
        except Exception as e:
            log.excpt(f"Ошибка {self.name} API: ", e=e)
            return {}

    def make_payload(self, prompt: str, extra=None) -> dict:
        messages = []
        if self.pre_prompt:
            messages.append({"role": "system", "content": self.pre_prompt})
        messages.append({"role": "user", "content": prompt})
        self.payload = {
            "model": self.model,
            "messages": messages
        }
        self.payload.update(self.model_params)
        if extra is not None:
            self.payload.update(extra)
        return self.payload

    def clean_response(self, text: str) -> str:
        for token in self.flood:
            text = re.sub(token, '', text)  # response artefact removing
        return text

    def get_text(self) -> str:
        """Extracts text from API response, formatting errors in <llm_error> tags if present.

        Returns:
            str: Extracted text or formatted error message.
        """
        response = self.last_result
        choices = response.get("choices", [{}])
        if not choices:
            dump = json.dumps(response, indent=2)
            log.error("No choices in response: %s", dump)
            return f"<llm_error>No choices in response:{dump}</llm_error>"

        choice = choices[0]
        if "error" in choice:
            error = choice["error"]
            message = error.get("message", "Unknown error")
            code = error.get("code", "Unknown")
            provider = response.get("provider", "Unknown")
            log.error("API error: %s, code=%s, provider=%s", message, code, provider)
            return f"<llm_error>\n{message}\nError code: {code}\nProvider: {provider}\n</llm_error>"

        msg = choice.get("message", {})
        text = msg.get("content", "") + msg.get("text", "")

        text = text.strip()
        if not text:
            reason = choice.get("native_finish_reason", choice.get("finish_reason", "unknown reason"))
            text = f"<void_response>Due {reason}</void_response>\n<response>\n{json.dumps(choice, indent=2)}\n</response>"
        return self.clean_response(text)

    def add_search_tool(self, params: dict) -> dict:
        if 'off' == params.get('mode', 'off'):
            return self.payload
        tools = self.payload.get('tools', [])
        tools.append({
            "type": "web_search",
            "web_search": {
                "query": {"type": "string"},
                "max_results": params.get("max_search_results", 5),
                "search_depth": params.get("search_depth", "basic")
            }
        })
        self.payload['tools'] = tools
        return self.payload

    def get_search_params(self, user_id: int) -> dict:
        """Настраивает параметры поиска web_search."""
        log.debug("Настройка search_params для user_id=%d", user_id)
        settings = self.db.fetch_one(
            'SELECT search_mode, search_sources, max_search_results, from_date, to_date FROM user_settings '
            'WHERE user_id = :user_id',
            {'user_id': user_id}
        )
        today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        def_sources = self.search_sources
        if settings:
            try:
                sources = json.loads(settings[1]) if settings[1] else def_sources
                sources = [{"type": src} for src in sources if src in def_sources]
            except json.JSONDecodeError:
                sources = []
            return {
                "mode": settings[0] or "off",
                "sources": sources,
                "max_search_results": settings[2] or 20,
                "from_date": settings[3].replace("today", today),
                "to_date": settings[4].replace("today", today)
            }
        return {
            "mode": "off",
            "sources": [],
            "max_search_results": 20,
            "from_date": today,
            "to_date": today
        }

    def get_search_results(self) -> list:
        tool_calls = self.last_result.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
        for call in tool_calls:
            if call.get("function", {}).get("name") == "web_search":
                return call.get("function", {}).get("arguments", "{}").get("results", [])
        return []


class XAIConnection(LLMConnection):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "xAI"
        self.base_url = "https://api.x.ai/v1"
        self.search_sources = ["web", "x", "news"]
        eff = config.get("reasoning_eff")
        model_l = (self.model or "").lower()
        # Restrict only known-bad family (grok-code-fast*). Keep fast-reasoning models enabled.
        supports_reasoning = ("non-reasoning" not in model_l) and (not re.match(r"^grok-code-fast", model_l))
        if "fast-reasoning" in model_l:
            supports_reasoning = True
        if eff and eff != "none" and supports_reasoning:
            self.model_params["reasoning"] = {"effort": eff}


class OpenAIConnection(LLMConnection):
    def __init__(self, config: dict):
        super().__init__(config)
        self.name = "OpenAI"
        self.base_url = "https://api.openai.com/v1"
        eff = config.get("reasoning_eff", "low")  # default low (fast) for OpenAI o-series
        if eff and eff != "none":
            self.model_params["reasoning_effort"] = eff


class OpenRouterConnection(LLMConnection):
    def __init__(self, config: dict):
        raw_llm_class = (config.get("model") or "").strip()
        sync_pricing = "openrouter:" in raw_llm_class.lower()
        super().__init__(config)
        model_value = (self.model or "").strip()
        self.model = model_value[len("openrouter:"):] if model_value.lower().startswith("openrouter:") else model_value
        self.name = "OpenRouter"
        self.base_url = "https://openrouter.ai/api/v1"
        eff = config.get("reasoning_eff")
        # Keep conservative: add reasoning only for OpenAI models on OpenRouter.
        if eff and eff != "none" and self.model.startswith("openai/"):
            self.model_params["reasoning"] = {"effort": eff}
        if sync_pricing:
            _maybe_sync_openrouter_user_pricing(self.db, config.get("user_id"), self.model)

