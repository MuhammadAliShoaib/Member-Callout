import urllib.error
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import call, patch
from uuid import uuid4

from django.core.management import call_command
from django.db.models import F
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from callouts.ai import (
    AIConfigurationError,
    AIInvalidResponseError,
    AIProviderError,
    AITimeoutError,
    FakeAnnouncementDraftAI,
    ProviderAnnouncementDraftAI,
    validate_draft_response,
)
from callouts.models import Announcement, AnnouncementRecipient, Local, Member
from callouts.models import AnnouncementStats
from callouts.permissions import IsActiveLeaderInOwnLocal
from callouts.serializers import AnnouncementSerializer
from callouts.services.llm_service import (
    SYSTEM_INSTRUCTION,
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMNetworkError,
    LLMProviderError,
    LLMProviderUnavailableError,
    LLMRateLimitError,
    LLMTimeoutError,
    chat_completion,
    chat_completion_payload,
    is_transient_http_status,
    llm_config,
    parse_chat_completion_text,
    regenerate_announcement_text,
)
from callouts.tasks import (
    MAX_RECIPIENT_BATCH_SIZE,
    TemporaryDeliveryError,
    claim_pending_recipients,
    deliver_recipient_batch,
    mark_temporary_delivery_failure,
    mark_terminal_delivery_failure,
    mark_recipients_sent,
    recipient_claim_stale_before,
    recipients_for_delivery,
    reconcile_announcement_stats,
    retry_countdown,
    update_announcement_stats_for_delivery_batch,
)
from callouts.views import (
    AUDIENCE_QUERY_CHUNK_SIZE,
    AI_REGENERATE_INSTRUCTION_MAX_LENGTH,
    AI_REGENERATE_TEXT_MAX_LENGTH,
    MAX_ANNOUNCEMENT_RECIPIENTS,
    RECIPIENT_BULK_CREATE_BATCH_SIZE,
    announcement_audience_count,
    announcement_audience_filters,
    announcement_audience_values,
    announcement_audience_values_queryset,
    announcement_content_hash,
    announcement_content_is_confirmed,
    announcement_recipient_for_member,
    bulk_create_announcement_recipients,
    create_announcement_recipients_for_send,
    enqueue_pending_recipients_for_delivery,
    expand_and_enqueue_announcement_recipients,
    expand_announcement_audience,
    mark_recipient_acknowledged,
    mark_recipient_read,
    validate_announcement_audience_size,
)


class ActiveLeaderPermissionTests(SimpleTestCase):
    def setUp(self):
        self.local_27 = Local(name='Local 27')
        self.local_99 = Local(name='Local 99')
        self.permission = IsActiveLeaderInOwnLocal()

    def test_local_27_leader_cannot_access_local_99_data_and_vice_versa(self):
        leader_27 = self.member(self.local_27, Member.Role.LEADER)
        leader_99 = self.member(self.local_99, Member.Role.LEADER)

        self.assertTrue(
            self.permission.has_object_permission(
                self.request(leader_27),
                None,
                self.local_data(self.local_27),
            )
        )
        self.assertFalse(
            self.permission.has_object_permission(
                self.request(leader_27),
                None,
                self.local_data(self.local_99),
            )
        )
        self.assertTrue(
            self.permission.has_object_permission(
                self.request(leader_99),
                None,
                self.local_data(self.local_99),
            )
        )
        self.assertFalse(
            self.permission.has_object_permission(
                self.request(leader_99),
                None,
                self.local_data(self.local_27),
            )
        )

    def test_members_cannot_access_local_27_or_local_99_data(self):
        member_27 = self.member(self.local_27, Member.Role.MEMBER)
        member_99 = self.member(self.local_99, Member.Role.MEMBER)

        for user in (member_27, member_99):
            request = self.request(user)
            self.assertFalse(self.permission.has_permission(request, None))
            self.assertFalse(
                self.permission.has_object_permission(
                    request,
                    None,
                    self.local_data(self.local_27),
                )
            )
            self.assertFalse(
                self.permission.has_object_permission(
                    request,
                    None,
                    self.local_data(self.local_99),
                )
            )

    def test_normal_members_cannot_perform_leader_operations(self):
        member_27 = self.member(self.local_27, Member.Role.MEMBER)
        member_99 = self.member(self.local_99, Member.Role.MEMBER)

        self.assertFalse(
            self.permission.has_permission(self.request(member_27), None)
        )
        self.assertFalse(
            self.permission.has_object_permission(
                self.request(member_27),
                None,
                self.local_data(self.local_27),
            )
        )
        self.assertFalse(
            self.permission.has_permission(self.request(member_99), None)
        )
        self.assertFalse(
            self.permission.has_object_permission(
                self.request(member_99),
                None,
                self.local_data(self.local_99),
            )
        )

    def member(self, local, role):
        return Member(
            local=local,
            full_name=f'{local.name} {role.label}',
            email=f'{local.name.lower().replace(" ", "")}.{role}@example.com',
            classification='apprentice',
            status=Member.Status.ACTIVE,
            role=role,
            is_active=True,
        )

    def request(self, user):
        return SimpleNamespace(user=user)

    def local_data(self, local):
        return SimpleNamespace(local_id=local.id)


class AnnouncementDraftTests(SimpleTestCase):
    def test_ai_draft_preview_is_limited_to_120_characters(self):
        draft = FakeAnnouncementDraftAI().draft('Please share this update. ' + ('Details ' * 40))

        self.assertIn('title', draft)
        self.assertIn('body', draft)
        self.assertIn('push_preview', draft)
        self.assertLessEqual(len(draft['push_preview']), 120)

    def test_invalid_ai_response_is_rejected(self):
        with self.assertRaises(AIInvalidResponseError):
            validate_draft_response({
                'title': 'Meeting',
                'body': 'Meeting tonight.',
                'push_preview': 'x' * 121,
            })

    def test_ai_errors_have_safe_status_codes(self):
        self.assertEqual(AIConfigurationError.status_code, 503)
        self.assertEqual(AITimeoutError.status_code, 504)
        self.assertEqual(AIProviderError.status_code, 502)
        self.assertEqual(AIInvalidResponseError.status_code, 502)

    def test_provider_ai_uses_llm_service_output_for_existing_draft_contract(self):
        with patch(
            'callouts.ai.regenerate_announcement_text',
            return_value='Meeting starts at 7 PM. Please arrive early.',
        ):
            draft = ProviderAnnouncementDraftAI().draft('meeting note')

        self.assertEqual(draft['title'], 'Meeting starts at 7 PM')
        self.assertEqual(draft['body'], 'Meeting starts at 7 PM. Please arrive early.')
        self.assertEqual(draft['push_preview'], 'Meeting starts at 7 PM. Please arrive early.')


class LLMServiceTests(SimpleTestCase):
    def test_provider_config_requires_llm_settings_when_used(self):
        with self.settings(LLM_API_KEY='', LLM_MODEL='gpt-4o-mini', LLM_API_ENDPOINT='https://llm.example/chat'):
            with self.assertRaises(LLMConfigurationError):
                llm_config()

    def test_llm_config_uses_provider_agnostic_settings(self):
        with self.settings(
            LLM_API_KEY='secret-key',
            LLM_MODEL='custom-model',
            LLM_API_ENDPOINT='https://llm.example/chat',
            LLM_MAX_TOKENS=321,
            LLM_TEMPERATURE=0.2,
            LLM_MAX_RETRIES=0,
            LLM_RETRY_BASE_DELAY_MS=250,
            LLM_TIMEOUT_MS=5000,
        ):
            config = llm_config()

        self.assertEqual(config['api_key'], 'secret-key')
        self.assertEqual(config['model'], 'custom-model')
        self.assertEqual(config['api_endpoint'], 'https://llm.example/chat')
        self.assertEqual(config['max_retries'], 1)

    def test_chat_payload_includes_text_and_optional_instruction_without_api_key(self):
        config = {'model': 'custom-model', 'max_tokens': 321, 'temperature': 0.2}

        payload = chat_completion_payload(
            'Picnic is Saturday at noon.',
            'Make it more professional.',
            config,
        )

        self.assertEqual(payload['model'], 'custom-model')
        self.assertEqual(payload['max_tokens'], 321)
        self.assertEqual(payload['temperature'], 0.2)
        self.assertEqual(payload['messages'][0]['content'], SYSTEM_INSTRUCTION)
        self.assertIn('Picnic is Saturday at noon.', payload['messages'][-1]['content'])
        self.assertIn('Make it more professional.', payload['messages'][-1]['content'])
        self.assertNotIn('api_key', payload)
        self.assertNotIn('secret-key', str(payload))

    def test_regenerate_announcement_text_returns_generated_text_only(self):
        response = {
            'choices': [
                {
                    'message': {
                        'content': 'Please join us Saturday at noon.',
                    },
                },
            ],
        }

        with self.settings(
            LLM_API_KEY='secret-key',
            LLM_MODEL='custom-model',
            LLM_API_ENDPOINT='https://llm.example/chat',
        ):
            with patch('callouts.services.llm_service.chat_completion', return_value=response):
                result = regenerate_announcement_text('Join Saturday.', 'Make it warmer.')

        self.assertEqual(result, 'Please join us Saturday at noon.')

    def test_parse_chat_completion_rejects_invalid_text(self):
        with self.assertRaises(LLMInvalidResponseError):
            parse_chat_completion_text({'choices': [{'message': {'content': '   '}}]})

    def test_transient_status_classification(self):
        self.assertTrue(is_transient_http_status(429))
        self.assertTrue(is_transient_http_status(500))
        self.assertTrue(is_transient_http_status(503))
        self.assertFalse(is_transient_http_status(400))
        self.assertFalse(is_transient_http_status(401))
        self.assertFalse(is_transient_http_status(403))

    def test_http_validation_and_auth_errors_are_not_retried(self):
        config = {
            'api_key': 'secret-key',
            'model': 'custom-model',
            'api_endpoint': 'https://llm.example/chat',
            'max_tokens': 321,
            'temperature': 0.2,
            'max_retries': 3,
            'retry_base_delay_ms': 250,
            'timeout_ms': 5000,
        }
        error = urllib.error.HTTPError('https://llm.example/chat', 401, 'Unauthorized', {}, None)

        with patch('urllib.request.urlopen', side_effect=error) as urlopen:
            with self.assertRaises(LLMAuthenticationError):
                chat_completion({'messages': []}, config)

        urlopen.assert_called_once()

    def test_transient_http_errors_are_retried_then_raise_provider_error(self):
        config = {
            'api_key': 'secret-key',
            'model': 'custom-model',
            'api_endpoint': 'https://llm.example/chat',
            'max_tokens': 321,
            'temperature': 0.2,
            'max_retries': 3,
            'retry_base_delay_ms': 250,
            'timeout_ms': 5000,
        }
        error = urllib.error.HTTPError('https://llm.example/chat', 503, 'Unavailable', {}, None)

        with patch('urllib.request.urlopen', side_effect=error) as urlopen:
            with patch('callouts.services.llm_service.time.sleep') as sleep:
                with self.assertRaises(LLMProviderUnavailableError):
                    chat_completion({'messages': []}, config)

        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_connection_errors_are_retried_then_raise_network_error(self):
        config = {
            'api_key': 'secret-key',
            'model': 'custom-model',
            'api_endpoint': 'https://llm.example/chat',
            'max_tokens': 321,
            'temperature': 0.2,
            'max_retries': 2,
            'retry_base_delay_ms': 250,
            'timeout_ms': 5000,
        }

        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError('temporary')) as urlopen:
            with patch('callouts.services.llm_service.time.sleep') as sleep:
                with self.assertRaises(LLMNetworkError):
                    chat_completion({'messages': []}, config)

        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()


class AnnouncementAIRegenerateEndpointTests(TestCase):
    def setUp(self):
        self.local = Local.objects.create(name='Local 27')
        self.other_local = Local.objects.create(name='Local 99')
        self.leader = self.member('leader@example.com', Member.Role.LEADER)
        self.member_user = self.member('member@example.com', Member.Role.MEMBER)
        self.url = reverse('announcement-ai-regenerate')

    def test_requires_authenticated_user(self):
        response = self.client.post(
            self.url,
            {'text': 'Meeting tomorrow at 6 PM.'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 401)

    def test_requires_active_leader_permissions(self):
        self.authenticate(self.member_user)

        response = self.client.post(
            self.url,
            {'text': 'Meeting tomorrow at 6 PM.'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 403)

    def test_non_leader_cannot_spoof_another_local_privilege(self):
        self.authenticate(self.member_user)
        other_local_leader = self.member(
            'other-leader@example.com',
            Member.Role.LEADER,
            local=self.other_local,
        )

        with patch('callouts.views.regenerate_announcement_text') as regenerate:
            response = self.client.post(
                self.url,
                {
                    'text': 'Meeting tomorrow at 6 PM.',
                    'local_id': str(self.other_local.id),
                    'created_by': str(other_local_leader.id),
                },
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 403)
        regenerate.assert_not_called()

    def test_returns_generated_text_without_saving_or_sending(self):
        self.authenticate(self.leader)

        with patch('callouts.views.deliver_recipient_batch.apply_async') as apply_async:
            with patch(
                'callouts.views.regenerate_announcement_text',
                return_value="Please attend tomorrow's meeting at 6 PM.",
            ) as regenerate:
                response = self.client.post(
                    self.url,
                    {
                        'text': 'Meeting tomorrow at 6 PM.',
                        'instruction': 'Make this more professional',
                    },
                    content_type='application/json',
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {'generated_text': "Please attend tomorrow's meeting at 6 PM.", 'client_request_id': None},
        )
        regenerate.assert_called_once_with(
            'Meeting tomorrow at 6 PM.',
            'Make this more professional',
        )
        self.assertEqual(Announcement.objects.count(), 0)
        self.assertEqual(AnnouncementRecipient.objects.count(), 0)
        apply_async.assert_not_called()

    def test_regenerate_does_not_update_existing_announcement(self):
        self.authenticate(self.leader)
        announcement = self.announcement()

        with patch(
            'callouts.views.regenerate_announcement_text',
            return_value='New generated body.',
        ):
            response = self.client.post(
                self.url,
                {
                    'announcement_id': str(announcement.id),
                    'text': announcement.body,
                },
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200)
        announcement.refresh_from_db()
        self.assertEqual(announcement.title, 'Original title')
        self.assertEqual(announcement.body, 'Original body.')
        self.assertEqual(announcement.push_preview, 'Original preview.')

    def test_late_response_cannot_modify_announcement_text(self):
        self.authenticate(self.leader)
        announcement = self.announcement()

        with patch(
            'callouts.views.regenerate_announcement_text',
            side_effect=['Stale generated body.', 'Fresh generated body.'],
        ):
            stale_response = self.client.post(
                self.url,
                {
                    'announcement_id': str(announcement.id),
                    'text': announcement.body,
                    'client_request_id': 'older-request',
                },
                content_type='application/json',
            )
            fresh_response = self.client.post(
                self.url,
                {
                    'announcement_id': str(announcement.id),
                    'text': announcement.body,
                    'client_request_id': 'newer-request',
                },
                content_type='application/json',
            )

        self.assertEqual(stale_response.status_code, 200)
        self.assertEqual(fresh_response.status_code, 200)
        self.assertEqual(stale_response.json()['client_request_id'], 'older-request')
        self.assertEqual(fresh_response.json()['client_request_id'], 'newer-request')
        announcement.refresh_from_db()
        self.assertEqual(announcement.body, 'Original body.')

    def test_echoes_client_request_id(self):
        self.authenticate(self.leader)

        with patch(
            'callouts.views.regenerate_announcement_text',
            return_value='Meeting text.',
        ):
            response = self.client.post(
                self.url,
                {
                    'text': 'Meeting tomorrow at 6 PM.',
                    'client_request_id': 'abc-123-uuid',
                },
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['client_request_id'], 'abc-123-uuid')

    def test_instruction_is_optional(self):
        self.authenticate(self.leader)

        with patch(
            'callouts.views.regenerate_announcement_text',
            return_value='Meeting tomorrow at 6 PM.',
        ) as regenerate:
            response = self.client.post(
                self.url,
                {'text': '  Meeting tomorrow at 6 PM.  '},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 200)
        regenerate.assert_called_once_with('Meeting tomorrow at 6 PM.', None)

    def test_validates_required_text(self):
        self.authenticate(self.leader)

        with patch('callouts.views.regenerate_announcement_text') as regenerate:
            response = self.client.post(
                self.url,
                {'text': '   '},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'text': 'Text is required.'})
        regenerate.assert_not_called()

    def test_validates_text_and_instruction_length(self):
        self.authenticate(self.leader)

        with patch('callouts.views.regenerate_announcement_text') as regenerate:
            response = self.client.post(
                self.url,
                {
                    'text': 'x' * (AI_REGENERATE_TEXT_MAX_LENGTH + 1),
                    'instruction': 'x' * (AI_REGENERATE_INSTRUCTION_MAX_LENGTH + 1),
                },
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn('text', response.json())
        self.assertIn('instruction', response.json())
        regenerate.assert_not_called()

    def test_returns_safe_service_error(self):
        self.authenticate(self.leader)

        with patch(
            'callouts.views.regenerate_announcement_text',
            side_effect=LLMProviderError(),
        ):
            response = self.client.post(
                self.url,
                {'text': 'Meeting tomorrow at 6 PM.'},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {'detail': LLMProviderError.detail})

    def test_llm_timeout_is_handled(self):
        self.authenticate(self.leader)

        with patch(
            'callouts.views.regenerate_announcement_text',
            side_effect=LLMTimeoutError(),
        ):
            response = self.client.post(
                self.url,
                {'text': 'Meeting tomorrow at 6 PM.'},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json(), {'detail': LLMTimeoutError.detail})

    def test_llm_rate_limit_is_handled(self):
        self.authenticate(self.leader)

        with patch(
            'callouts.views.regenerate_announcement_text',
            side_effect=LLMRateLimitError(),
        ):
            response = self.client.post(
                self.url,
                {'text': 'Meeting tomorrow at 6 PM.'},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'detail': LLMRateLimitError.detail})

    def test_llm_5xx_failure_is_handled(self):
        self.authenticate(self.leader)

        with patch(
            'callouts.views.regenerate_announcement_text',
            side_effect=LLMProviderUnavailableError(),
        ):
            response = self.client.post(
                self.url,
                {'text': 'Meeting tomorrow at 6 PM.'},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'detail': LLMProviderUnavailableError.detail})

    def test_invalid_provider_response_is_handled(self):
        self.authenticate(self.leader)

        with patch(
            'callouts.views.regenerate_announcement_text',
            side_effect=LLMInvalidResponseError(),
        ):
            response = self.client.post(
                self.url,
                {'text': 'Meeting tomorrow at 6 PM.'},
                content_type='application/json',
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {'detail': LLMInvalidResponseError.detail})

    def test_api_key_is_never_returned(self):
        self.authenticate(self.leader)

        with self.settings(LLM_API_KEY='sk-never-return-this'):
            with patch(
                'callouts.views.regenerate_announcement_text',
                side_effect=LLMProviderError(),
            ):
                response = self.client.post(
                    self.url,
                    {'text': 'Meeting tomorrow at 6 PM.'},
                    content_type='application/json',
                )

        self.assertNotIn('sk-never-return-this', response.content.decode('utf-8'))

    def announcement(self):
        return Announcement.objects.create(
            local=self.local,
            created_by=self.leader,
            title='Original title',
            body='Original body.',
            push_preview='Original preview.',
            status=Announcement.Status.DRAFT,
        )

    def member(self, email, role, local=None):
        return Member.objects.create_user(
            email=email,
            password='password',
            local=local or self.local,
            full_name=email,
            classification='journeyman',
            status=Member.Status.ACTIVE,
            role=role,
            is_active=True,
        )

    def authenticate(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.defaults['HTTP_AUTHORIZATION'] = f'Token {token.key}'


class AnnouncementEditLifecycleEndpointTests(TestCase):
    def setUp(self):
        self.local = Local.objects.create(name='Local 27')
        self.leader = self.member('leader@example.com')
        self.authenticate(self.leader)

    def test_serializer_exposes_content_editable_from_backend_lifecycle(self):
        draft = self.announcement(Announcement.Status.DRAFT)
        confirmed = self.announcement(Announcement.Status.CONFIRMED)
        queued = self.announcement(Announcement.Status.QUEUED)
        sent = self.announcement(Announcement.Status.SENT)

        self.assertTrue(AnnouncementSerializer(draft).data['content_editable'])
        self.assertTrue(AnnouncementSerializer(confirmed).data['content_editable'])
        self.assertFalse(AnnouncementSerializer(queued).data['content_editable'])
        self.assertFalse(AnnouncementSerializer(sent).data['content_editable'])

    def test_confirmed_announcement_content_can_still_be_edited_and_returns_to_draft(self):
        announcement = self.announcement(Announcement.Status.CONFIRMED)

        response = self.client.patch(
            reverse('announcement-detail', args=[announcement.id]),
            {'body': 'Updated meeting details.'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], Announcement.Status.DRAFT)
        self.assertTrue(response.json()['content_editable'])

    def test_queued_announcement_content_cannot_be_edited(self):
        announcement = self.announcement(Announcement.Status.QUEUED)

        response = self.client.patch(
            reverse('announcement-detail', args=[announcement.id]),
            {'body': 'Updated meeting details.'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'Announcement content can no longer be edited.'})
        announcement.refresh_from_db()
        self.assertEqual(announcement.status, Announcement.Status.QUEUED)
        self.assertEqual(announcement.body, 'Meeting tonight.')

    def test_sent_announcement_content_cannot_be_edited(self):
        announcement = self.announcement(Announcement.Status.SENT)

        response = self.client.patch(
            reverse('announcement-detail', args=[announcement.id]),
            {'title': 'Updated title'},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'Announcement content can no longer be edited.'})
        announcement.refresh_from_db()
        self.assertEqual(announcement.status, Announcement.Status.SENT)
        self.assertEqual(announcement.title, 'Meeting')

    def member(self, email):
        return Member.objects.create_user(
            email=email,
            password='password',
            local=self.local,
            full_name=email,
            classification='journeyman',
            status=Member.Status.ACTIVE,
            role=Member.Role.LEADER,
            is_active=True,
        )

    def announcement(self, status):
        return Announcement.objects.create(
            local=self.local,
            created_by=self.leader,
            title='Meeting',
            body='Meeting tonight.',
            push_preview='Meeting tonight.',
            status=status,
            confirmed_content_hash='confirmed',
        )

    def authenticate(self, user):
        token, _ = Token.objects.get_or_create(user=user)
        self.client.defaults['HTTP_AUTHORIZATION'] = f'Token {token.key}'


class AnnouncementConfirmationTests(SimpleTestCase):
    def test_confirmed_content_hash_matches_current_content(self):
        announcement = self.announcement()
        announcement.confirmed_content_hash = announcement_content_hash(announcement)

        self.assertTrue(announcement_content_is_confirmed(announcement))

    def test_changed_content_no_longer_matches_confirmed_hash(self):
        announcement = self.announcement()
        announcement.confirmed_content_hash = announcement_content_hash(announcement)
        announcement.body = 'Changed meeting details.'

        self.assertFalse(announcement_content_is_confirmed(announcement))

    def test_content_field_changes_are_detected_after_confirmation(self):
        previous = self.announcement(title='Old title')
        current = self.announcement(title='New title')

        self.assertTrue(current.content_fields_changed(previous))

    def test_non_content_field_changes_do_not_clear_confirmation(self):
        previous = self.announcement()
        current = self.announcement(needs_ack=True)

        self.assertFalse(current.content_fields_changed(previous))

    def test_reset_confirmation_clears_confirmation_and_returns_to_draft(self):
        announcement = self.announcement(
            status=Announcement.Status.CONFIRMED,
            confirmed_content_hash='abc123',
            confirmed_at=timezone.now(),
        )

        announcement.reset_confirmation()

        self.assertEqual(announcement.confirmed_content_hash, '')
        self.assertIsNone(announcement.confirmed_at)
        self.assertEqual(announcement.status, Announcement.Status.DRAFT)

    def announcement(self, **overrides):
        defaults = {
            'title': 'Meeting',
            'body': 'Meeting tonight.',
            'push_preview': 'Meeting tonight.',
            'status': Announcement.Status.CONFIRMED,
        }
        defaults.update(overrides)
        return Announcement(**defaults)


class AnnouncementAudienceTests(SimpleTestCase):
    def test_audience_filters_include_same_local_and_active_members(self):
        local = Local(name='Local 27')
        announcement = Announcement(local=local)

        self.assertEqual(
            announcement_audience_filters(announcement),
            {
                'local_id': local.id,
                'status': Member.Status.ACTIVE,
                'is_active': True,
            },
        )

    def test_audience_filters_include_target_classification_when_specified(self):
        local = Local(name='Local 27')
        announcement = Announcement(
            local=local,
            target_classification='journeyman',
        )

        self.assertEqual(
            announcement_audience_filters(announcement),
            {
                'local_id': local.id,
                'status': Member.Status.ACTIVE,
                'is_active': True,
                'classification': 'journeyman',
            },
        )

    def test_audience_recipients_use_announcement_local_and_member_snapshot(self):
        local = Local(name='Local 27')
        announcement = Announcement(local=local)
        member = Member(local=local, classification='journeyman')

        recipient = announcement_recipient_for_member(announcement, member)

        self.assertEqual(recipient.local, local)
        self.assertEqual(recipient.announcement, announcement)
        self.assertEqual(recipient.member_id, member.id)
        self.assertEqual(recipient.classification_snapshot, 'journeyman')

    def test_audience_values_queryset_selects_only_required_fields_and_caps_results(self):
        local = Local(name='Local 27')
        announcement = Announcement(local=local)

        queryset = announcement_audience_values_queryset(announcement)

        self.assertEqual(queryset.query.values_select, ('id', 'classification'))
        self.assertEqual(queryset.query.high_mark, MAX_ANNOUNCEMENT_RECIPIENTS)
        self.assertEqual(queryset.query.order_by, ('id',))

    @patch('callouts.views.announcement_audience_queryset')
    def test_audience_count_uses_database_count(self, announcement_audience_queryset):
        local = Local(name='Local 27')
        announcement = Announcement(local=local)

        count = announcement_audience_count(announcement)

        self.assertEqual(count, announcement_audience_queryset.return_value.count.return_value)
        announcement_audience_queryset.return_value.count.assert_called_once_with()

    @patch('callouts.views.announcement_audience_values_queryset')
    def test_audience_values_iterates_database_rows_in_chunks(self, announcement_audience_values_queryset):
        local = Local(name='Local 27')
        announcement = Announcement(local=local)

        values = announcement_audience_values(announcement)

        self.assertEqual(values, announcement_audience_values_queryset.return_value.iterator.return_value)
        announcement_audience_values_queryset.return_value.iterator.assert_called_once_with(
            chunk_size=AUDIENCE_QUERY_CHUNK_SIZE,
        )

    @patch('callouts.views.announcement_audience_count')
    def test_oversized_audience_returns_clear_validation_error(self, announcement_audience_count):
        local = Local(name='Local 27')
        announcement = Announcement(local=local)
        audience_count = MAX_ANNOUNCEMENT_RECIPIENTS + 1
        announcement_audience_count.return_value = audience_count

        response = validate_announcement_audience_size(announcement)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data,
            {
                'detail': (
                    f'Announcement audience has {audience_count} eligible members; '
                    f'maximum is {MAX_ANNOUNCEMENT_RECIPIENTS}.'
                ),
            },
        )

    @patch('callouts.views.update_announcement_target_count')
    @patch('callouts.views.expand_announcement_audience')
    @patch('callouts.views.announcement_audience_count')
    def test_oversized_audience_is_rejected_before_creating_recipients(
        self,
        announcement_audience_count,
        expand_announcement_audience,
        update_announcement_target_count,
    ):
        local = Local(name='Local 27')
        announcement = Announcement(local=local)
        announcement_audience_count.return_value = MAX_ANNOUNCEMENT_RECIPIENTS + 1

        response = create_announcement_recipients_for_send(announcement)

        self.assertEqual(response.status_code, 400)
        expand_announcement_audience.assert_not_called()
        update_announcement_target_count.assert_not_called()

    @patch('callouts.views.bulk_create_announcement_recipients')
    @patch('callouts.views.announcement_audience_values')
    def test_expand_audience_streams_recipients_in_bulk_create_batches(
        self,
        announcement_audience_values,
        bulk_create_announcement_recipients,
    ):
        local = Local(name='Local 27')
        announcement = Announcement(local=local)
        audience_size = RECIPIENT_BULK_CREATE_BATCH_SIZE + 1
        announcement_audience_values.return_value = (
            (f'member-{number}', 'journeyman')
            for number in range(audience_size)
        )

        created_count = expand_announcement_audience(announcement)

        self.assertEqual(created_count, audience_size)
        self.assertEqual(bulk_create_announcement_recipients.call_count, 2)
        first_batch = bulk_create_announcement_recipients.call_args_list[0].args[0]
        second_batch = bulk_create_announcement_recipients.call_args_list[1].args[0]
        self.assertEqual(len(first_batch), RECIPIENT_BULK_CREATE_BATCH_SIZE)
        self.assertEqual(len(second_batch), 1)

    @patch('callouts.views.AnnouncementRecipient.objects')
    def test_bulk_create_recipients_ignores_unique_conflicts(self, recipient_manager):
        bulk_create_announcement_recipients(['recipient'])

        recipient_manager.bulk_create.assert_called_once_with(
            ['recipient'],
            batch_size=RECIPIENT_BULK_CREATE_BATCH_SIZE,
            ignore_conflicts=True,
        )

    @patch('callouts.views.deliver_recipient_batch.apply_async')
    @patch('callouts.views.pending_recipient_ids_for_delivery')
    def test_enqueue_pending_recipients_uses_configurable_batches(
        self,
        pending_recipient_ids_for_delivery,
        apply_async,
    ):
        pending_recipient_ids_for_delivery.return_value = (
            f'recipient-{number}' for number in range(5)
        )

        with self.settings(RECIPIENT_DELIVERY_BATCH_SIZE=2):
            enqueue_pending_recipients_for_delivery('announcement-id')

        apply_async.assert_has_calls([
            call(args=[['recipient-0', 'recipient-1']]),
            call(args=[['recipient-2', 'recipient-3']]),
            call(args=[['recipient-4']]),
        ])
        self.assertEqual(apply_async.call_count, 3)

    @patch('callouts.views.deliver_recipient_batch.apply_async')
    @patch('callouts.views.transaction.on_commit')
    @patch('callouts.views.update_announcement_target_count')
    @patch('callouts.views.expand_announcement_audience')
    def test_expansion_registers_delivery_enqueue_after_commit(
        self,
        expand_announcement_audience,
        update_announcement_target_count,
        on_commit,
        apply_async,
    ):
        local = Local(name='Local 27')
        announcement = Announcement(local=local)

        expand_and_enqueue_announcement_recipients(announcement)

        expand_announcement_audience.assert_called_once_with(announcement)
        update_announcement_target_count.assert_called_once_with(announcement)
        on_commit.assert_called_once()
        apply_async.assert_not_called()


class AnnouncementAudienceExpansionDatabaseTests(TestCase):
    def test_repeated_audience_expansion_does_not_duplicate_recipients(self):
        local = Local.objects.create(name='Local 27')
        leader = self.member(local, 'leader@example.com', role=Member.Role.LEADER)
        member = self.member(local, 'member@example.com')
        announcement = self.announcement(local, leader)

        expand_announcement_audience(announcement)
        expand_announcement_audience(announcement)

        recipients = AnnouncementRecipient.objects.filter(announcement=announcement)
        self.assertEqual(recipients.count(), 2)
        self.assertEqual(
            set(recipients.values_list('member_id', flat=True)),
            {leader.id, member.id},
        )

    def test_audience_expansion_only_inserts_members_from_announcement_local(self):
        local_27 = Local.objects.create(name='Local 27')
        local_99 = Local.objects.create(name='Local 99')
        leader = self.member(local_27, 'leader@example.com', role=Member.Role.LEADER)
        same_local_member = self.member(local_27, 'same-local@example.com')
        other_local_member = self.member(local_99, 'other-local@example.com')
        announcement = self.announcement(local_27, leader)

        expand_announcement_audience(announcement)

        recipients = AnnouncementRecipient.objects.filter(announcement=announcement)
        self.assertEqual(recipients.count(), 2)
        self.assertIn(
            same_local_member.id,
            recipients.values_list('member_id', flat=True),
        )
        self.assertNotIn(
            other_local_member.id,
            recipients.values_list('member_id', flat=True),
        )
        self.assertFalse(recipients.exclude(local=local_27).exists())

    def test_member_can_only_be_inserted_once_per_announcement(self):
        constraint_names = {
            constraint.name
            for constraint in AnnouncementRecipient._meta.constraints
        }

        self.assertIn('unique_announcement_member', constraint_names)

    def member(self, local, email, role=Member.Role.MEMBER):
        return Member.objects.create_user(
            email=email,
            password='password',
            local=local,
            full_name=email,
            classification='journeyman',
            status=Member.Status.ACTIVE,
            role=role,
            is_active=True,
        )

    def announcement(self, local, created_by):
        return Announcement.objects.create(
            local=local,
            created_by=created_by,
            title='Meeting',
            body='Meeting tonight.',
            push_preview='Meeting tonight.',
            status=Announcement.Status.CONFIRMED,
            confirmed_content_hash='confirmed',
        )


class DeliverAnnouncementsCommandTests(TestCase):
    def test_command_requeues_unclaimed_and_stale_pending_recipients_only(self):
        local = Local.objects.create(name='Local 27')
        leader = self.member(local, 'leader@example.com', role=Member.Role.LEADER)
        announcement = self.announcement(local, leader)
        unclaimed = self.recipient(announcement, self.member(local, 'unclaimed@example.com'))
        actively_claimed = self.recipient(
            announcement,
            self.member(local, 'active-claim@example.com'),
            claimed_at=timezone.now(),
            claimed_by='worker-1',
        )
        stale_claimed = self.recipient(
            announcement,
            self.member(local, 'stale-claim@example.com'),
            claimed_at=timezone.now() - timedelta(seconds=301),
            claimed_by='worker-2',
        )
        sent = self.recipient(
            announcement,
            self.member(local, 'sent@example.com'),
            delivery_status=AnnouncementRecipient.DeliveryStatus.SENT,
        )
        out = StringIO()

        with self.settings(RECIPIENT_CLAIM_TIMEOUT_SECONDS=300, RECIPIENT_DELIVERY_BATCH_SIZE=2):
            with patch(
                'callouts.management.commands.deliver_announcements.deliver_recipient_batch.apply_async'
            ) as apply_async:
                call_command('deliver_announcements', stdout=out)

        apply_async.assert_called_once_with(args=[[str(unclaimed.id), str(stale_claimed.id)]])
        self.assertIn('Enqueued 2 pending announcement recipients for delivery.', out.getvalue())

        for recipient in (unclaimed, actively_claimed, stale_claimed, sent):
            recipient.refresh_from_db()

        self.assertEqual(unclaimed.delivery_status, AnnouncementRecipient.DeliveryStatus.PENDING)
        self.assertEqual(actively_claimed.delivery_status, AnnouncementRecipient.DeliveryStatus.PENDING)
        self.assertEqual(stale_claimed.delivery_status, AnnouncementRecipient.DeliveryStatus.PENDING)
        self.assertEqual(sent.delivery_status, AnnouncementRecipient.DeliveryStatus.SENT)
        self.assertIsNone(unclaimed.sent_at)
        self.assertIsNone(stale_claimed.sent_at)

    def test_command_enqueues_recipient_ids_in_configurable_batches(self):
        local = Local.objects.create(name='Local 27')
        leader = self.member(local, 'leader@example.com', role=Member.Role.LEADER)
        announcement = self.announcement(local, leader)
        recipients = [
            self.recipient(announcement, self.member(local, f'member-{number}@example.com'))
            for number in range(5)
        ]

        with self.settings(RECIPIENT_DELIVERY_BATCH_SIZE=2):
            with patch(
                'callouts.management.commands.deliver_announcements.deliver_recipient_batch.apply_async'
            ) as apply_async:
                call_command('deliver_announcements', stdout=StringIO())

        apply_async.assert_has_calls([
            call(args=[[str(recipients[0].id), str(recipients[1].id)]]),
            call(args=[[str(recipients[2].id), str(recipients[3].id)]]),
            call(args=[[str(recipients[4].id)]]),
        ])
        self.assertEqual(apply_async.call_count, 3)

    def member(self, local, email, role=Member.Role.MEMBER):
        return Member.objects.create_user(
            email=email,
            password='password',
            local=local,
            full_name=email,
            classification='journeyman',
            status=Member.Status.ACTIVE,
            role=role,
            is_active=True,
        )

    def announcement(self, local, created_by):
        return Announcement.objects.create(
            local=local,
            created_by=created_by,
            title='Meeting',
            body='Meeting tonight.',
            push_preview='Meeting tonight.',
            status=Announcement.Status.QUEUED,
            confirmed_content_hash='confirmed',
        )

    def recipient(self, announcement, member, **overrides):
        defaults = {
            'local': announcement.local,
            'announcement': announcement,
            'member': member,
            'classification_snapshot': member.classification,
        }
        defaults.update(overrides)
        return AnnouncementRecipient.objects.create(**defaults)


class LoadTestDeliveryCommandTests(TestCase):
    def test_load_test_delivery_reports_metrics_and_cleans_up(self):
        out = StringIO()

        call_command('load_test_delivery', '--recipients', '3', stdout=out)

        output = out.getvalue()
        self.assertIn('Bottlenecks first:', output)
        self.assertIn('- target_recipients: 3', output)
        self.assertIn('- recipients_created_or_matched: 3', output)
        self.assertIn('- celery_task_count: 1', output)
        self.assertIn('- delivered_count: 3', output)
        self.assertFalse(Local.objects.filter(name__startswith='Load Test Local').exists())


class DeliveryStatsDatabaseTests(TestCase):
    def test_duplicate_sent_transition_does_not_double_count_stats(self):
        local = Local.objects.create(name='Local 27')
        leader = self.member(local, 'leader@example.com', role=Member.Role.LEADER)
        announcement = self.announcement(local, leader)
        stats = AnnouncementStats.objects.create(
            announcement=announcement,
            local=local,
            target_count=1,
        )
        recipient = self.recipient(announcement, self.member(local, 'member@example.com'))

        update_announcement_stats_for_delivery_batch({
            announcement_id: {'sent': sent_count, 'failed': 0}
            for announcement_id, sent_count in mark_recipients_sent([recipient.id]).items()
        })
        update_announcement_stats_for_delivery_batch({
            announcement_id: {'sent': sent_count, 'failed': 0}
            for announcement_id, sent_count in mark_recipients_sent([recipient.id]).items()
        })

        stats.refresh_from_db()
        self.assertEqual(stats.sent_count, 1)
        self.assertEqual(stats.failed_count, 0)

    def test_reconciliation_recomputes_stats_and_marks_announcement_sent_when_complete(self):
        local = Local.objects.create(name='Local 27')
        leader = self.member(local, 'leader@example.com', role=Member.Role.LEADER)
        announcement = self.announcement(local, leader)
        stats = AnnouncementStats.objects.create(
            announcement=announcement,
            local=local,
            target_count=999,
            sent_count=999,
            failed_count=999,
            read_count=999,
            acknowledged_count=999,
        )
        self.recipient(
            announcement,
            self.member(local, 'sent@example.com'),
            delivery_status=AnnouncementRecipient.DeliveryStatus.SENT,
            read_at=timezone.now(),
            acknowledged_at=timezone.now(),
        )
        self.recipient(
            announcement,
            self.member(local, 'failed@example.com'),
            delivery_status=AnnouncementRecipient.DeliveryStatus.FAILED,
        )

        completed = reconcile_announcement_stats(announcement.id)

        self.assertTrue(completed)
        stats.refresh_from_db()
        announcement.refresh_from_db()
        self.assertEqual(stats.target_count, 2)
        self.assertEqual(stats.sent_count, 1)
        self.assertEqual(stats.failed_count, 1)
        self.assertEqual(stats.read_count, 1)
        self.assertEqual(stats.acknowledged_count, 1)
        self.assertEqual(announcement.status, Announcement.Status.SENT)
        self.assertIsNotNone(announcement.sent_at)

    def test_reconciliation_does_not_mark_sent_while_pending_recipients_remain(self):
        local = Local.objects.create(name='Local 27')
        leader = self.member(local, 'leader@example.com', role=Member.Role.LEADER)
        announcement = self.announcement(local, leader)
        stats = AnnouncementStats.objects.create(announcement=announcement, local=local)
        self.recipient(
            announcement,
            self.member(local, 'sent@example.com'),
            delivery_status=AnnouncementRecipient.DeliveryStatus.SENT,
        )
        self.recipient(announcement, self.member(local, 'pending@example.com'))

        completed = reconcile_announcement_stats(announcement.id)

        self.assertFalse(completed)
        stats.refresh_from_db()
        announcement.refresh_from_db()
        self.assertEqual(stats.target_count, 2)
        self.assertEqual(stats.sent_count, 1)
        self.assertEqual(stats.failed_count, 0)
        self.assertEqual(announcement.status, Announcement.Status.QUEUED)
        self.assertIsNone(announcement.sent_at)

    def test_reconciliation_marks_announcement_sent_once(self):
        local = Local.objects.create(name='Local 27')
        leader = self.member(local, 'leader@example.com', role=Member.Role.LEADER)
        announcement = self.announcement(local, leader)
        AnnouncementStats.objects.create(announcement=announcement, local=local)
        self.recipient(
            announcement,
            self.member(local, 'sent@example.com'),
            delivery_status=AnnouncementRecipient.DeliveryStatus.SENT,
        )

        first_completed = reconcile_announcement_stats(announcement.id)
        second_completed = reconcile_announcement_stats(announcement.id)

        self.assertTrue(first_completed)
        self.assertFalse(second_completed)

    def member(self, local, email, role=Member.Role.MEMBER):
        return Member.objects.create_user(
            email=email,
            password='password',
            local=local,
            full_name=email,
            classification='journeyman',
            status=Member.Status.ACTIVE,
            role=role,
            is_active=True,
        )

    def announcement(self, local, created_by):
        return Announcement.objects.create(
            local=local,
            created_by=created_by,
            title='Meeting',
            body='Meeting tonight.',
            push_preview='Meeting tonight.',
            status=Announcement.Status.QUEUED,
            confirmed_content_hash='confirmed',
        )

    def recipient(self, announcement, member, **overrides):
        defaults = {
            'local': announcement.local,
            'announcement': announcement,
            'member': member,
            'classification_snapshot': member.classification,
        }
        defaults.update(overrides)
        return AnnouncementRecipient.objects.create(**defaults)


class DeliveryTaskDatabaseTests(TestCase):
    def test_duplicate_tasks_do_not_process_sent_recipients_or_double_count_stats(self):
        local, announcement, stats = self.delivery_setup(target_count=2)
        first = self.recipient(announcement, self.member(local, 'first@example.com'))
        second = self.recipient(announcement, self.member(local, 'second@example.com'))
        recipient_ids = [str(first.id), str(second.id)]

        with patch('callouts.tasks.fake_push_delivery') as fake_push_delivery:
            first_result = deliver_recipient_batch.run(recipient_ids)
            second_result = deliver_recipient_batch.run(recipient_ids)

        self.assertEqual(fake_push_delivery.call_count, 2)
        self.assertEqual(first_result['processed'], 2)
        self.assertEqual(second_result['processed'], 0)
        self.assertEqual(second_result['completed_announcements'], 0)

        first.refresh_from_db()
        second.refresh_from_db()
        stats.refresh_from_db()
        announcement.refresh_from_db()
        self.assertEqual(first.delivery_status, AnnouncementRecipient.DeliveryStatus.SENT)
        self.assertEqual(second.delivery_status, AnnouncementRecipient.DeliveryStatus.SENT)
        self.assertEqual(first.attempt_count, 0)
        self.assertEqual(second.attempt_count, 0)
        self.assertEqual(stats.sent_count, 2)
        self.assertEqual(stats.failed_count, 0)
        self.assertEqual(announcement.status, Announcement.Status.SENT)

    def test_temporary_failure_leaves_pending_and_increments_attempt_count(self):
        local, announcement, stats = self.delivery_setup(target_count=1)
        recipient = self.recipient(announcement, self.member(local, 'member@example.com'))

        with patch('callouts.tasks.fake_push_delivery', side_effect=TemporaryDeliveryError('try again')):
            with patch.object(deliver_recipient_batch, 'apply_async') as apply_async:
                result = deliver_recipient_batch.run([str(recipient.id)])

        recipient.refresh_from_db()
        stats.refresh_from_db()
        announcement.refresh_from_db()
        self.assertEqual(result['temporary_failures'], 1)
        self.assertEqual(result['retryable'], 1)
        self.assertEqual(recipient.delivery_status, AnnouncementRecipient.DeliveryStatus.PENDING)
        self.assertEqual(recipient.attempt_count, 1)
        self.assertIsNone(recipient.claimed_at)
        self.assertEqual(stats.sent_count, 0)
        self.assertEqual(stats.failed_count, 0)
        self.assertEqual(announcement.status, Announcement.Status.QUEUED)
        apply_async.assert_called_once_with(args=[[str(recipient.id)]], countdown=1)

    def test_successful_retry_marks_sent_after_temporary_failure(self):
        local, announcement, stats = self.delivery_setup(target_count=1)
        recipient = self.recipient(announcement, self.member(local, 'member@example.com'))

        with patch('callouts.tasks.fake_push_delivery', side_effect=TemporaryDeliveryError('try again')):
            with patch.object(deliver_recipient_batch, 'apply_async'):
                deliver_recipient_batch.run([str(recipient.id)])

        with patch('callouts.tasks.fake_push_delivery'):
            result = deliver_recipient_batch.run([str(recipient.id)])

        recipient.refresh_from_db()
        stats.refresh_from_db()
        announcement.refresh_from_db()
        self.assertEqual(result['processed'], 1)
        self.assertEqual(recipient.delivery_status, AnnouncementRecipient.DeliveryStatus.SENT)
        self.assertEqual(recipient.attempt_count, 1)
        self.assertEqual(stats.sent_count, 1)
        self.assertEqual(stats.failed_count, 0)
        self.assertEqual(announcement.status, Announcement.Status.SENT)
        self.assertIsNotNone(announcement.sent_at)

    def test_multiple_temporary_failures_keep_retrying_until_limit(self):
        local, announcement, stats = self.delivery_setup(target_count=1)
        recipient = self.recipient(announcement, self.member(local, 'member@example.com'))

        with self.settings(MAX_DELIVERY_ATTEMPTS=4):
            with patch('callouts.tasks.fake_push_delivery', side_effect=TemporaryDeliveryError('try again')):
                with patch.object(deliver_recipient_batch, 'apply_async') as apply_async:
                    first_result = deliver_recipient_batch.run([str(recipient.id)])
                    second_result = deliver_recipient_batch.run([str(recipient.id)])

        recipient.refresh_from_db()
        stats.refresh_from_db()
        self.assertEqual(first_result['retryable'], 1)
        self.assertEqual(second_result['retryable'], 1)
        self.assertEqual(apply_async.call_count, 2)
        self.assertEqual(recipient.delivery_status, AnnouncementRecipient.DeliveryStatus.PENDING)
        self.assertEqual(recipient.attempt_count, 2)
        self.assertEqual(stats.failed_count, 0)

    def test_retries_exhausted_marks_failed_and_counts_failure(self):
        local, announcement, stats = self.delivery_setup(target_count=1)
        recipient = self.recipient(
            announcement,
            self.member(local, 'member@example.com'),
            attempt_count=2,
        )

        with self.settings(MAX_DELIVERY_ATTEMPTS=3):
            with patch('callouts.tasks.fake_push_delivery', side_effect=TemporaryDeliveryError('try again')):
                with patch.object(deliver_recipient_batch, 'apply_async') as apply_async:
                    result = deliver_recipient_batch.run([str(recipient.id)])

        recipient.refresh_from_db()
        stats.refresh_from_db()
        announcement.refresh_from_db()
        self.assertEqual(result['temporary_failures'], 1)
        self.assertEqual(result['retryable'], 0)
        self.assertEqual(recipient.delivery_status, AnnouncementRecipient.DeliveryStatus.FAILED)
        self.assertEqual(recipient.attempt_count, 3)
        self.assertEqual(stats.sent_count, 0)
        self.assertEqual(stats.failed_count, 1)
        self.assertEqual(announcement.status, Announcement.Status.SENT)
        apply_async.assert_not_called()

    def test_already_sent_recipient_is_not_processed_again(self):
        local, announcement, stats = self.delivery_setup(target_count=1, sent_count=1)
        recipient = self.recipient(
            announcement,
            self.member(local, 'member@example.com'),
            delivery_status=AnnouncementRecipient.DeliveryStatus.SENT,
            sent_at=timezone.now(),
        )

        with patch('callouts.tasks.fake_push_delivery') as fake_push_delivery:
            result = deliver_recipient_batch.run([str(recipient.id)])

        recipient.refresh_from_db()
        stats.refresh_from_db()
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['temporary_failures'], 0)
        fake_push_delivery.assert_not_called()
        self.assertEqual(recipient.delivery_status, AnnouncementRecipient.DeliveryStatus.SENT)
        self.assertEqual(recipient.attempt_count, 0)
        self.assertEqual(stats.sent_count, 1)
        self.assertEqual(stats.failed_count, 0)

    def test_stale_claim_recovery_processes_recipient(self):
        local, announcement, stats = self.delivery_setup(target_count=1)
        recipient = self.recipient(
            announcement,
            self.member(local, 'member@example.com'),
            claimed_at=timezone.now() - timedelta(seconds=301),
            claimed_by='old-worker',
        )

        with self.settings(RECIPIENT_CLAIM_TIMEOUT_SECONDS=300):
            with patch('callouts.tasks.fake_push_delivery'):
                result = deliver_recipient_batch.run([str(recipient.id)])

        recipient.refresh_from_db()
        stats.refresh_from_db()
        self.assertEqual(result['processed'], 1)
        self.assertEqual(recipient.delivery_status, AnnouncementRecipient.DeliveryStatus.SENT)
        self.assertEqual(recipient.attempt_count, 0)
        self.assertIsNone(recipient.claimed_at)
        self.assertIsNone(recipient.claimed_by)
        self.assertEqual(stats.sent_count, 1)

    def test_multiple_workers_claim_each_recipient_once(self):
        local, announcement, stats = self.delivery_setup(target_count=2)
        first = self.recipient(announcement, self.member(local, 'first@example.com'))
        second = self.recipient(announcement, self.member(local, 'second@example.com'))
        recipient_ids = [str(first.id), str(second.id)]

        first_claim = claim_pending_recipients(recipient_ids, 'worker-1')
        second_claim = claim_pending_recipients(recipient_ids, 'worker-2')

        self.assertEqual(set(first_claim), {first.id, second.id})
        self.assertEqual(second_claim, [])

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.claimed_by, 'worker-1')
        self.assertEqual(second.claimed_by, 'worker-1')
        self.assertEqual(first.delivery_status, AnnouncementRecipient.DeliveryStatus.PENDING)
        self.assertEqual(second.delivery_status, AnnouncementRecipient.DeliveryStatus.PENDING)

    def test_overlapping_worker_batches_do_not_duplicate_processing_or_stats(self):
        local, announcement, stats = self.delivery_setup(target_count=4)
        recipients = [
            self.recipient(announcement, self.member(local, f'member-{number}@example.com'))
            for number in range(4)
        ]
        first_batch = [str(recipients[0].id), str(recipients[1].id), str(recipients[2].id)]
        second_batch = [str(recipients[1].id), str(recipients[2].id), str(recipients[3].id)]

        with patch('callouts.tasks.fake_push_delivery') as fake_push_delivery:
            first_result = deliver_recipient_batch.run(first_batch)
            second_result = deliver_recipient_batch.run(second_batch)

        self.assertEqual(fake_push_delivery.call_count, 4)
        self.assertEqual(first_result['processed'], 3)
        self.assertEqual(second_result['processed'], 1)

        stats.refresh_from_db()
        announcement.refresh_from_db()
        self.assertEqual(stats.sent_count, 4)
        self.assertEqual(stats.failed_count, 0)
        self.assertEqual(announcement.status, Announcement.Status.SENT)

        for recipient in recipients:
            recipient.refresh_from_db()
            self.assertEqual(recipient.delivery_status, AnnouncementRecipient.DeliveryStatus.SENT)
            self.assertEqual(recipient.attempt_count, 0)

    def test_overlapping_retry_tasks_remain_safe(self):
        local, announcement, stats = self.delivery_setup(target_count=1)
        recipient = self.recipient(announcement, self.member(local, 'member@example.com'))
        recipient_id = str(recipient.id)

        with patch('callouts.tasks.fake_push_delivery', side_effect=TemporaryDeliveryError('try again')):
            with patch.object(deliver_recipient_batch, 'apply_async') as apply_async:
                first_result = deliver_recipient_batch.run([recipient_id])
                second_result = deliver_recipient_batch.run([recipient_id])

        recipient.refresh_from_db()
        stats.refresh_from_db()
        announcement.refresh_from_db()
        self.assertEqual(first_result['temporary_failures'], 1)
        self.assertEqual(second_result['temporary_failures'], 1)
        self.assertEqual(recipient.delivery_status, AnnouncementRecipient.DeliveryStatus.PENDING)
        self.assertEqual(recipient.attempt_count, 2)
        self.assertEqual(stats.sent_count, 0)
        self.assertEqual(stats.failed_count, 0)
        self.assertEqual(announcement.status, Announcement.Status.QUEUED)
        self.assertEqual(apply_async.call_count, 2)

        with patch('callouts.tasks.fake_push_delivery'):
            retry_result = deliver_recipient_batch.run([recipient_id])

        recipient.refresh_from_db()
        stats.refresh_from_db()
        announcement.refresh_from_db()
        self.assertEqual(retry_result['processed'], 1)
        self.assertEqual(recipient.delivery_status, AnnouncementRecipient.DeliveryStatus.SENT)
        self.assertEqual(recipient.attempt_count, 2)
        self.assertEqual(stats.sent_count, 1)
        self.assertEqual(stats.failed_count, 0)
        self.assertEqual(announcement.status, Announcement.Status.SENT)

    def delivery_setup(self, target_count=0, sent_count=0, failed_count=0):
        local = Local.objects.create(name='Local 27')
        leader = self.member(local, 'leader@example.com', role=Member.Role.LEADER)
        announcement = self.announcement(local, leader)
        stats = AnnouncementStats.objects.create(
            announcement=announcement,
            local=local,
            target_count=target_count,
            sent_count=sent_count,
            failed_count=failed_count,
        )
        return local, announcement, stats

    def member(self, local, email, role=Member.Role.MEMBER):
        return Member.objects.create_user(
            email=email,
            password='password',
            local=local,
            full_name=email,
            classification='journeyman',
            status=Member.Status.ACTIVE,
            role=role,
            is_active=True,
        )

    def announcement(self, local, created_by):
        return Announcement.objects.create(
            local=local,
            created_by=created_by,
            title='Meeting',
            body='Meeting tonight.',
            push_preview='Meeting tonight.',
            status=Announcement.Status.QUEUED,
            confirmed_content_hash='confirmed',
        )

    def recipient(self, announcement, member, **overrides):
        defaults = {
            'local': announcement.local,
            'announcement': announcement,
            'member': member,
            'classification_snapshot': member.classification,
        }
        defaults.update(overrides)
        return AnnouncementRecipient.objects.create(**defaults)


class DeliveryTaskTests(SimpleTestCase):
    def test_recipient_batch_has_250_id_limit(self):
        recipient_ids = [str(number) for number in range(MAX_RECIPIENT_BATCH_SIZE + 1)]

        with self.assertRaises(ValueError):
            deliver_recipient_batch.run(recipient_ids)

    def test_delivery_task_accepts_ids_only_argument(self):
        self.assertEqual(deliver_recipient_batch.name, 'callouts.tasks.deliver_recipient_batch')

    def test_recipient_claim_timeout_controls_stale_cutoff(self):
        now = timezone.now()

        with self.settings(RECIPIENT_CLAIM_TIMEOUT_SECONDS=60):
            self.assertEqual(recipient_claim_stale_before(now), now - timedelta(seconds=60))

    @patch('callouts.tasks.AnnouncementRecipient.objects')
    def test_successful_delivery_marks_sent_and_clears_claim(self, recipient_manager):
        recipient_manager.filter.return_value.values_list.return_value = [
            ('announcement-id', 'recipient-id'),
        ]
        recipient_manager.filter.return_value.update.return_value = 1

        mark_recipients_sent(['recipient-id'])

        recipient_manager.filter.assert_any_call(
            id__in=['recipient-id'],
            delivery_status='pending',
        )
        self.assertEqual(recipient_manager.filter.call_count, 2)
        recipient_manager.filter.return_value.update.assert_called_once()
        update_kwargs = recipient_manager.filter.return_value.update.call_args.kwargs
        self.assertEqual(update_kwargs['delivery_status'], 'sent')
        self.assertIsNotNone(update_kwargs['sent_at'])
        self.assertIsNone(update_kwargs['claimed_at'])
        self.assertIsNone(update_kwargs['claimed_by'])

    @patch('callouts.tasks.AnnouncementRecipient.objects')
    def test_temporary_failure_keeps_pending_and_clears_claim(self, recipient_manager):
        recipient = SimpleNamespace(id='recipient-id', attempt_count=0)

        with self.settings(MAX_DELIVERY_ATTEMPTS=3):
            updated, retryable = mark_temporary_delivery_failure(recipient, RuntimeError('temporary outage'))

        recipient_manager.filter.assert_called_once_with(
            id='recipient-id',
            delivery_status='pending',
        )
        update_kwargs = recipient_manager.filter.return_value.update.call_args.kwargs
        self.assertEqual(update_kwargs['delivery_status'], 'pending')
        self.assertEqual(update_kwargs['last_error'], 'temporary outage')
        self.assertIsNone(update_kwargs['claimed_at'])
        self.assertIsNone(update_kwargs['claimed_by'])
        self.assertEqual(updated, recipient_manager.filter.return_value.update.return_value)
        self.assertTrue(retryable)

    @patch('callouts.tasks.AnnouncementRecipient.objects')
    def test_temporary_failure_at_max_attempts_marks_failed(self, recipient_manager):
        recipient = SimpleNamespace(id='recipient-id', attempt_count=2)

        with self.settings(MAX_DELIVERY_ATTEMPTS=3):
            updated, retryable = mark_temporary_delivery_failure(recipient, RuntimeError('temporary outage'))

        update_kwargs = recipient_manager.filter.return_value.update.call_args.kwargs
        self.assertEqual(update_kwargs['delivery_status'], 'failed')
        self.assertEqual(updated, recipient_manager.filter.return_value.update.return_value)
        self.assertFalse(retryable)

    @patch('callouts.tasks.AnnouncementRecipient.objects')
    def test_terminal_failure_marks_failed_and_clears_claim(self, recipient_manager):
        recipient = SimpleNamespace(id='recipient-id')

        mark_terminal_delivery_failure(recipient, RuntimeError('bad token'))

        recipient_manager.filter.assert_called_once_with(
            id='recipient-id',
            delivery_status='pending',
        )
        update_kwargs = recipient_manager.filter.return_value.update.call_args.kwargs
        self.assertEqual(update_kwargs['delivery_status'], 'failed')
        self.assertEqual(update_kwargs['last_error'], 'bad token')
        self.assertIsNone(update_kwargs['claimed_at'])
        self.assertIsNone(update_kwargs['claimed_by'])

    def test_retry_countdown_uses_exponential_backoff(self):
        self.assertEqual(retry_countdown(1), 1)
        self.assertEqual(retry_countdown(2), 2)
        self.assertEqual(retry_countdown(3), 4)

    def test_recipients_for_delivery_fetches_only_required_delivery_fields(self):
        queryset = recipients_for_delivery([uuid4()])

        self.assertEqual(queryset.query.select_related, {'announcement': {}, 'member': {}})
        self.assertEqual(
            queryset.query.deferred_loading,
            (
                frozenset({
                    'id',
                    'attempt_count',
                    'announcement_id',
                    'member_id',
                    'announcement__title',
                    'announcement__push_preview',
                    'member__email',
                }),
                False,
            ),
        )

    @patch('callouts.tasks.AnnouncementStats.objects')
    def test_delivery_stats_update_uses_single_atomic_update_per_announcement(self, stats_manager):
        update_announcement_stats_for_delivery_batch({
            'announcement-id': {
                'sent': 2,
                'failed': 1,
            },
        })

        stats_manager.filter.assert_called_once_with(announcement_id='announcement-id')
        update_kwargs = stats_manager.filter.return_value.update.call_args.kwargs
        self.assertEqual(update_kwargs['sent_count'], F('sent_count') + 2)
        self.assertEqual(update_kwargs['failed_count'], F('failed_count') + 1)
        self.assertIsNotNone(update_kwargs['updated_at'])

    @patch('callouts.tasks.update_announcement_stats_for_delivery_batch')
    @patch('callouts.tasks.reconcile_completed_announcements', return_value=0)
    @patch('callouts.tasks.mark_recipients_sent', return_value={'announcement-id': 1})
    @patch('callouts.tasks.mark_temporary_delivery_failure', return_value=(1, True))
    @patch('callouts.tasks.fake_push_delivery')
    @patch('callouts.tasks.claim_pending_recipients', return_value=['success-id', 'retry-id'])
    @patch('callouts.tasks.recipients_for_delivery')
    def test_temporary_failures_requeue_only_retryable_ids(
        self,
        recipients_for_delivery,
        claim_pending_recipients,
        fake_push_delivery,
        mark_temporary_delivery_failure,
        mark_recipients_sent,
        reconcile_completed_announcements,
        update_announcement_stats_for_delivery_batch,
    ):
        success_recipient = SimpleNamespace(id='success-id', attempt_count=0, announcement_id='announcement-id')
        retry_recipient = SimpleNamespace(id='retry-id', attempt_count=0, announcement_id='announcement-id')
        recipients_for_delivery.return_value = [
            success_recipient,
            retry_recipient,
        ]
        fake_push_delivery.side_effect = [None, TemporaryDeliveryError('try again')]

        with patch.object(deliver_recipient_batch, 'apply_async') as apply_async:
            result = deliver_recipient_batch.run(['success-id', 'retry-id'])

        mark_recipients_sent.assert_called_once_with(['success-id'])
        update_announcement_stats_for_delivery_batch.assert_called_once_with({
            'announcement-id': {'sent': 1, 'failed': 0},
        })
        apply_async.assert_called_once_with(args=[['retry-id']], countdown=1)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['temporary_failures'], 1)
        self.assertEqual(result['retryable'], 1)


class AnnouncementReadAcknowledgeTests(TestCase):
    def setUp(self):
        self.local = Local.objects.create(name='Local 27')
        self.leader = self.make_member(self.local, 'leader@example.com', role=Member.Role.LEADER)
        self.member = self.make_member(self.local, 'member@example.com')
        self.announcement = self.make_announcement(self.local, self.leader)
        self.stats = AnnouncementStats.objects.create(
            announcement=self.announcement,
            local=self.local,
            target_count=1,
            sent_count=1,
        )
        self.recipient = AnnouncementRecipient.objects.create(
            local=self.local,
            announcement=self.announcement,
            member=self.member,
            classification_snapshot=self.member.classification,
            delivery_status=AnnouncementRecipient.DeliveryStatus.SENT,
            sent_at=timezone.now(),
        )

    def test_first_read_sets_read_at_and_increments_read_count(self):
        result = mark_recipient_read(self.recipient.id, self.announcement.id)

        self.assertTrue(result)
        self.recipient.refresh_from_db()
        self.stats.refresh_from_db()
        self.assertIsNotNone(self.recipient.read_at)
        self.assertEqual(self.stats.read_count, 1)

    def test_repeated_read_does_not_increment_read_count(self):
        mark_recipient_read(self.recipient.id, self.announcement.id)
        result = mark_recipient_read(self.recipient.id, self.announcement.id)

        self.assertFalse(result)
        self.stats.refresh_from_db()
        self.assertEqual(self.stats.read_count, 1)

    def test_first_acknowledge_when_unread_sets_read_at_and_increments_both_counts(self):
        read_incremented, ack_incremented = mark_recipient_acknowledged(
            self.recipient.id, self.announcement.id
        )

        self.assertTrue(read_incremented)
        self.assertTrue(ack_incremented)
        self.recipient.refresh_from_db()
        self.stats.refresh_from_db()
        self.assertIsNotNone(self.recipient.read_at)
        self.assertIsNotNone(self.recipient.acknowledged_at)
        self.assertEqual(self.stats.read_count, 1)
        self.assertEqual(self.stats.acknowledged_count, 1)

    def test_first_acknowledge_when_already_read_only_increments_acknowledged_count(self):
        mark_recipient_read(self.recipient.id, self.announcement.id)

        read_incremented, ack_incremented = mark_recipient_acknowledged(
            self.recipient.id, self.announcement.id
        )

        self.assertFalse(read_incremented)
        self.assertTrue(ack_incremented)
        self.stats.refresh_from_db()
        self.assertEqual(self.stats.read_count, 1)
        self.assertEqual(self.stats.acknowledged_count, 1)

    def test_read_then_acknowledge_does_not_double_increment_read_count(self):
        mark_recipient_read(self.recipient.id, self.announcement.id)
        mark_recipient_acknowledged(self.recipient.id, self.announcement.id)

        self.stats.refresh_from_db()
        self.assertEqual(self.stats.read_count, 1)
        self.assertEqual(self.stats.acknowledged_count, 1)

    def test_repeated_acknowledge_does_not_increment_counts(self):
        mark_recipient_acknowledged(self.recipient.id, self.announcement.id)
        read_incremented, ack_incremented = mark_recipient_acknowledged(
            self.recipient.id, self.announcement.id
        )

        self.assertFalse(read_incremented)
        self.assertFalse(ack_incremented)
        self.stats.refresh_from_db()
        self.assertEqual(self.stats.read_count, 1)
        self.assertEqual(self.stats.acknowledged_count, 1)

    def test_already_acknowledged_recipient_is_idempotent(self):
        self.recipient.read_at = timezone.now()
        self.recipient.acknowledged_at = timezone.now()
        self.recipient.save(update_fields=['read_at', 'acknowledged_at'])

        read_incremented, ack_incremented = mark_recipient_acknowledged(
            self.recipient.id, self.announcement.id
        )

        self.assertFalse(read_incremented)
        self.assertFalse(ack_incremented)
        self.stats.refresh_from_db()
        self.assertEqual(self.stats.read_count, 0)
        self.assertEqual(self.stats.acknowledged_count, 0)

    def make_member(self, local, email, role=Member.Role.MEMBER):
        return Member.objects.create_user(
            email=email,
            password='password',
            local=local,
            full_name=email,
            classification='journeyman',
            status=Member.Status.ACTIVE,
            role=role,
            is_active=True,
        )

    def make_announcement(self, local, created_by):
        return Announcement.objects.create(
            local=local,
            created_by=created_by,
            title='Meeting',
            body='Meeting tonight.',
            push_preview='Meeting tonight.',
            status=Announcement.Status.SENT,
            confirmed_content_hash='confirmed',
            sent_at=timezone.now(),
        )
