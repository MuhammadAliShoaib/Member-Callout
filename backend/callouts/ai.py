import re
from abc import ABC, abstractmethod

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def truncate_text(value, max_length):
    if len(value) <= max_length:
        return value

    return value[: max_length - 3].rstrip() + '...'


class AnnouncementDraftAI(ABC):
    @abstractmethod
    def draft(self, note):
        raise NotImplementedError


class FakeAnnouncementDraftAI(AnnouncementDraftAI):
    def draft(self, note):
        normalized = re.sub(r'\s+', ' ', note).strip()
        first_sentence = re.split(r'[.!?]', normalized, maxsplit=1)[0].strip()

        return {
            'title': truncate_text(first_sentence or 'Announcement', 80),
            'body': normalized,
            'push_preview': truncate_text(normalized, 120),
        }


class ProviderAnnouncementDraftAI(AnnouncementDraftAI):
    def __init__(self, api_key=None):
        self.api_key = api_key or settings.AI_API_KEY
        if not self.api_key:
            raise ImproperlyConfigured('AI_API_KEY is required when AI_PROVIDER=provider.')

    def draft(self, note):
        raise NotImplementedError('Connect the real AI provider here.')


def get_announcement_draft_ai():
    if settings.AI_PROVIDER == 'provider':
        return ProviderAnnouncementDraftAI()

    return FakeAnnouncementDraftAI()
