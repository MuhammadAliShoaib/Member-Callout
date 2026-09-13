from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from callouts.ai import (
    AIInvalidResponseError,
    AIProviderError,
    AITimeoutError,
    FakeAnnouncementDraftAI,
    validate_draft_response,
)
from callouts.models import Announcement, Local, Member
from callouts.permissions import IsActiveLeaderInOwnLocal
from callouts.tasks import (
    MAX_RECIPIENT_BATCH_SIZE,
    deliver_recipient_batch,
    mark_recipients_sent,
    recipient_claim_stale_before,
)
from callouts.views import (
    announcement_audience_filters,
    announcement_content_hash,
    announcement_content_is_confirmed,
    announcement_recipient_for_member,
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
        self.assertEqual(AITimeoutError.status_code, 504)
        self.assertEqual(AIProviderError.status_code, 502)
        self.assertEqual(AIInvalidResponseError.status_code, 502)


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
        mark_recipients_sent(['recipient-id'])

        recipient_manager.filter.assert_called_once_with(
            id__in=['recipient-id'],
            delivery_status='pending',
        )
        recipient_manager.filter.return_value.update.assert_called_once()
        update_kwargs = recipient_manager.filter.return_value.update.call_args.kwargs
        self.assertEqual(update_kwargs['delivery_status'], 'sent')
        self.assertIsNotNone(update_kwargs['sent_at'])
        self.assertIsNone(update_kwargs['claimed_at'])
        self.assertIsNone(update_kwargs['claimed_by'])
