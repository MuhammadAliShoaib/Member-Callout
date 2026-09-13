from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import call, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from callouts.ai import (
    AIInvalidResponseError,
    AIProviderError,
    AITimeoutError,
    FakeAnnouncementDraftAI,
    validate_draft_response,
)
from callouts.models import Announcement, AnnouncementRecipient, Local, Member
from callouts.permissions import IsActiveLeaderInOwnLocal
from callouts.tasks import (
    MAX_RECIPIENT_BATCH_SIZE,
    TemporaryDeliveryError,
    deliver_recipient_batch,
    mark_temporary_delivery_failure,
    mark_terminal_delivery_failure,
    mark_recipients_sent,
    recipient_claim_stale_before,
    retry_countdown,
)
from callouts.views import (
    AUDIENCE_QUERY_CHUNK_SIZE,
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

    @patch('callouts.tasks.mark_recipients_sent', return_value=1)
    @patch('callouts.tasks.mark_temporary_delivery_failure', return_value=(1, True))
    @patch('callouts.tasks.fake_push_delivery')
    @patch('callouts.tasks.claim_pending_recipients', return_value=['success-id', 'retry-id'])
    @patch('callouts.tasks.AnnouncementRecipient.objects')
    def test_temporary_failures_requeue_only_retryable_ids(
        self,
        recipient_manager,
        claim_pending_recipients,
        fake_push_delivery,
        mark_temporary_delivery_failure,
        mark_recipients_sent,
    ):
        success_recipient = SimpleNamespace(id='success-id', attempt_count=0)
        retry_recipient = SimpleNamespace(id='retry-id', attempt_count=0)
        recipient_manager.select_related.return_value.filter.return_value = [
            success_recipient,
            retry_recipient,
        ]
        fake_push_delivery.side_effect = [None, TemporaryDeliveryError('try again')]

        with patch.object(deliver_recipient_batch, 'apply_async') as apply_async:
            result = deliver_recipient_batch.run(['success-id', 'retry-id'])

        mark_recipients_sent.assert_called_once_with(['success-id'])
        apply_async.assert_called_once_with(args=[['retry-id']], countdown=1)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['temporary_failures'], 1)
        self.assertEqual(result['retryable'], 1)
