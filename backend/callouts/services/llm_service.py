import json
import time
import urllib.error
import urllib.request

from django.conf import settings


SYSTEM_INSTRUCTION = (
    'You assist union/local leadership in rewriting announcement text. '
    'Preserve the original meaning and factual information. '
    'Do not invent dates, locations, names, commitments, or event details. '
    'Return only the rewritten announcement text.'
)


class LLMServiceError(Exception):
    detail = 'LLM generation failed.'


class LLMConfigurationError(LLMServiceError):
    detail = 'LLM provider is not configured.'


class LLMTimeoutError(LLMServiceError):
    detail = 'LLM generation timed out.'


class LLMProviderError(LLMServiceError):
    detail = 'LLM provider failed.'


class LLMInvalidResponseError(LLMServiceError):
    detail = 'LLM provider returned an invalid response.'


def regenerate_announcement_text(text, instruction=None):
    config = llm_config()
    payload = chat_completion_payload(text, instruction, config)
    response = chat_completion(payload, config)
    return parse_chat_completion_text(response)


def llm_config():
    required_settings = {
        'LLM_API_KEY': settings.LLM_API_KEY,
        'LLM_MODEL': settings.LLM_MODEL,
        'LLM_API_ENDPOINT': settings.LLM_API_ENDPOINT,
    }
    missing = [
        name
        for name, value in required_settings.items()
        if not str(value or '').strip()
    ]

    if missing:
        raise LLMConfigurationError()

    return {
        'api_key': settings.LLM_API_KEY,
        'model': settings.LLM_MODEL,
        'api_endpoint': settings.LLM_API_ENDPOINT,
        'max_tokens': settings.LLM_MAX_TOKENS,
        'temperature': settings.LLM_TEMPERATURE,
        'max_retries': max(1, settings.LLM_MAX_RETRIES),
        'retry_base_delay_ms': settings.LLM_RETRY_BASE_DELAY_MS,
        'timeout_ms': settings.LLM_TIMEOUT_MS,
    }


def chat_completion_payload(text, instruction, config):
    user_content = f'Announcement text:\n{text}'
    if instruction:
        user_content = f'{user_content}\n\nRewrite instruction:\n{instruction}'

    return {
        'model': config['model'],
        'max_tokens': config['max_tokens'],
        'temperature': config['temperature'],
        'messages': [
            {
                'role': 'system',
                'content': SYSTEM_INSTRUCTION,
            },
            {
                'role': 'user',
                'content': user_content,
            },
        ],
    }


def chat_completion(payload, config):
    request = urllib.request.Request(
        config['api_endpoint'],
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f"Bearer {config['api_key']}",
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    last_error = None

    for attempt in range(config['max_retries']):
        try:
            with urllib.request.urlopen(
                request,
                timeout=config['timeout_ms'] / 1000,
            ) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            if not is_transient_http_status(exc.code):
                raise LLMProviderError() from exc
            last_error = exc
        except TimeoutError as exc:
            last_error = exc
        except urllib.error.URLError as exc:
            last_error = exc
        except json.JSONDecodeError as exc:
            raise LLMInvalidResponseError() from exc

        if attempt < config['max_retries'] - 1:
            time.sleep(retry_delay_seconds(config, attempt))

    if isinstance(last_error, urllib.error.HTTPError):
        raise LLMProviderError() from last_error

    raise LLMTimeoutError() from last_error


def is_transient_http_status(status_code):
    return status_code == 429 or 500 <= status_code <= 599


def retry_delay_seconds(config, attempt):
    return (config['retry_base_delay_ms'] / 1000) * (2 ** attempt)


def parse_chat_completion_text(response):
    try:
        content = response['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMInvalidResponseError() from exc

    if not isinstance(content, str) or not content.strip():
        raise LLMInvalidResponseError()

    return content.strip()
