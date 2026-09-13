import re
from abc import ABC, abstractmethod

from django.conf import settings

from callouts.services.llm_service import (
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMServiceError,
    LLMTimeoutError,
    regenerate_announcement_text,
)


def truncate_text(value, max_length):
    if len(value) <= max_length:
        return value

    return value[: max_length - 3].rstrip() + '...'


class AnnouncementDraftAI(ABC):
    @abstractmethod
    def draft(self, note):
        raise NotImplementedError


class AIError(Exception):
    detail = 'AI generation failed.'
    status_code = 502


class AITimeoutError(AIError):
    detail = 'AI generation timed out.'
    status_code = 504


class AIProviderError(AIError):
    detail = 'AI provider failed.'


class AIConfigurationError(AIError):
    detail = 'AI provider is not configured.'
    status_code = 503


class AIInvalidResponseError(AIError):
    detail = 'AI provider returned an invalid response.'


def validate_draft_response(draft):
    required_fields = ('title', 'body', 'push_preview')

    if not isinstance(draft, dict):
        raise AIInvalidResponseError()

    for field in required_fields:
        if not isinstance(draft.get(field), str) or not draft[field].strip():
            raise AIInvalidResponseError()

    if len(draft['push_preview']) > 120:
        raise AIInvalidResponseError()

    return {
        'title': draft['title'].strip(),
        'body': draft['body'].strip(),
        'push_preview': draft['push_preview'].strip(),
    }


class FakeAnnouncementDraftAI(AnnouncementDraftAI):
    def draft(self, note):
        normalized = re.sub(r'\s+', ' ', note).strip()
        first_sentence = re.split(r'[.!?]', normalized, maxsplit=1)[0].strip()

        return validate_draft_response({
            'title': truncate_text(first_sentence or 'Announcement', 80),
            'body': normalized,
            'push_preview': truncate_text(normalized, 120),
        })


class ProviderAnnouncementDraftAI(AnnouncementDraftAI):
    def draft(self, note):
        try:
            body = regenerate_announcement_text(note)
        except LLMServiceError as exc:
            raise ai_error_from_llm_error(exc) from exc

        return validate_draft_response({
            'title': truncate_text(title_from_text(body), 80),
            'body': body,
            'push_preview': truncate_text(body, 120),
        })


def title_from_text(text):
    normalized = re.sub(r'\s+', ' ', text).strip()
    return re.split(r'[.!?]', normalized, maxsplit=1)[0].strip() or 'Announcement'


def ai_error_from_llm_error(error):
    if isinstance(error, LLMConfigurationError):
        return AIConfigurationError()
    if isinstance(error, LLMTimeoutError):
        return AITimeoutError()
    if isinstance(error, LLMInvalidResponseError):
        return AIInvalidResponseError()
    if isinstance(error, LLMProviderError):
        return AIProviderError()
    return AIProviderError()


def get_announcement_draft_ai():
    if settings.AI_PROVIDER == 'provider':
        return ProviderAnnouncementDraftAI()

    return FakeAnnouncementDraftAI()
