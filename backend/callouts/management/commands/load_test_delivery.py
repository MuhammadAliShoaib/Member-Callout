import math
import time
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from django.core.management.base import BaseCommand
from django.db import connection
from django.test.utils import CaptureQueriesContext

from callouts.models import Announcement, AnnouncementRecipient, AnnouncementStats, Local, Member
from callouts.tasks import deliver_recipient_batch, recipient_batch_size
from callouts.views import (
    announcement_audience_values,
    announcement_recipient_for_member_values,
    bulk_create_announcement_recipients,
    update_announcement_target_count,
)


DEFAULT_LOAD_TEST_RECIPIENTS = 22400
MEMBER_CREATE_BATCH_SIZE = 1000
RECIPIENT_CREATE_BATCH_SIZE = 500


@contextmanager
def query_counter():
    previous_force_debug_cursor = connection.force_debug_cursor
    connection.force_debug_cursor = True
    try:
        with CaptureQueriesContext(connection) as queries:
            yield queries
    finally:
        connection.force_debug_cursor = previous_force_debug_cursor


def elapsed_since(started_at):
    return time.perf_counter() - started_at


def recipient_id_batches(recipient_ids, batch_size):
    batch = []

    for recipient_id in recipient_ids:
        batch.append(str(recipient_id))

        if len(batch) == batch_size:
            yield batch
            batch = []

    if batch:
        yield batch


class Command(BaseCommand):
    help = 'Run a delivery load test for a large announcement audience.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--recipients',
            type=int,
            default=DEFAULT_LOAD_TEST_RECIPIENTS,
            help='Number of active members to target. Defaults to 22,400.',
        )
        parser.add_argument(
            '--keep-data',
            action='store_true',
            help='Keep generated load-test data after the run.',
        )

    def handle(self, *args, **options):
        recipient_count = options['recipients']
        keep_data = options['keep_data']
        run_id = uuid.uuid4().hex[:12]
        metrics = {}
        local = None

        try:
            local, announcement = self.create_load_test_data(run_id, recipient_count, metrics)
            self.measure_expansion(announcement, metrics)
            pending_ids = list(
                AnnouncementRecipient.objects.filter(
                    announcement=announcement,
                    delivery_status=AnnouncementRecipient.DeliveryStatus.PENDING,
                )
                .order_by('id')
                .values_list('id', flat=True)
            )
            self.measure_task_count(pending_ids, metrics)
            self.measure_delivery(pending_ids, metrics)
            self.report(metrics)
        finally:
            if local and not keep_data:
                self.cleanup(local)

    def create_load_test_data(self, run_id, recipient_count, metrics):
        started_at = time.perf_counter()
        local = Local.objects.create(name=f'Load Test Local {run_id}')
        leader = Member.objects.create_user(
            email=f'load-test-leader-{run_id}@example.com',
            password='password',
            local=local,
            full_name='Load Test Leader',
            classification='leader',
            status=Member.Status.ACTIVE,
            role=Member.Role.LEADER,
            is_active=True,
        )
        announcement = Announcement.objects.create(
            local=local,
            created_by=leader,
            title='Load Test Announcement',
            body='Load test body.',
            push_preview='Load test.',
            status=Announcement.Status.QUEUED,
            confirmed_content_hash='load-test',
        )
        AnnouncementStats.objects.create(
            announcement=announcement,
            local=local,
        )

        member_count = max(recipient_count - 1, 0)
        members = (
            Member(
                local=local,
                full_name=f'Load Test Member {number}',
                email=f'load-test-{run_id}-{number}@example.com',
                classification='journeyman',
                status=Member.Status.ACTIVE,
                role=Member.Role.MEMBER,
                is_active=True,
                password='',
            )
            for number in range(member_count)
        )
        Member.objects.bulk_create(members, batch_size=MEMBER_CREATE_BATCH_SIZE)

        metrics['member_seed_seconds'] = elapsed_since(started_at)
        metrics['target_recipients'] = recipient_count
        return local, announcement

    def measure_expansion(self, announcement, metrics):
        started_at = time.perf_counter()
        created_count = 0
        recipient_creation_seconds = 0
        batch = []

        with query_counter() as queries:
            for member_id, classification in announcement_audience_values(announcement):
                batch.append(announcement_recipient_for_member_values(announcement, member_id, classification))
                created_count += 1

                if len(batch) == RECIPIENT_CREATE_BATCH_SIZE:
                    creation_started_at = time.perf_counter()
                    bulk_create_announcement_recipients(batch)
                    recipient_creation_seconds += elapsed_since(creation_started_at)
                    batch = []

            if batch:
                creation_started_at = time.perf_counter()
                bulk_create_announcement_recipients(batch)
                recipient_creation_seconds += elapsed_since(creation_started_at)

            update_announcement_target_count(announcement)

        metrics['audience_expansion_seconds'] = elapsed_since(started_at)
        metrics['recipient_creation_seconds'] = recipient_creation_seconds
        metrics['audience_expansion_queries'] = len(queries)
        metrics['recipients_created_or_matched'] = created_count

    def measure_task_count(self, pending_ids, metrics):
        started_at = time.perf_counter()
        batch_size = recipient_batch_size()
        task_count = math.ceil(len(pending_ids) / batch_size) if pending_ids else 0

        metrics['celery_batch_size'] = batch_size
        metrics['celery_task_count'] = task_count
        metrics['celery_task_count_seconds'] = elapsed_since(started_at)

    def measure_delivery(self, pending_ids, metrics):
        started_at = time.perf_counter()
        delivered_count = 0

        with query_counter() as queries:
            with patch('callouts.tasks.fake_push_delivery'):
                for batch in recipient_id_batches(pending_ids, recipient_batch_size()):
                    result = deliver_recipient_batch.run(batch)
                    delivered_count += result['processed']

        delivery_seconds = elapsed_since(started_at)
        metrics['delivery_seconds'] = delivery_seconds
        metrics['delivery_queries'] = len(queries)
        metrics['delivered_count'] = delivered_count
        metrics['delivery_throughput_per_second'] = (
            delivered_count / delivery_seconds if delivery_seconds else 0
        )

    def report(self, metrics):
        duration_metrics = {
            'member_seed_seconds': metrics['member_seed_seconds'],
            'audience_expansion_seconds': metrics['audience_expansion_seconds'],
            'recipient_creation_seconds': metrics['recipient_creation_seconds'],
            'delivery_seconds': metrics['delivery_seconds'],
        }
        bottlenecks = sorted(duration_metrics.items(), key=lambda item: item[1], reverse=True)

        self.stdout.write(self.style.WARNING('Bottlenecks first:'))
        for name, value in bottlenecks:
            self.stdout.write(f'- {name}: {value:.3f}s')

        self.stdout.write('')
        self.stdout.write('Delivery load test metrics:')
        self.stdout.write(f"- target_recipients: {metrics['target_recipients']}")
        self.stdout.write(f"- recipients_created_or_matched: {metrics['recipients_created_or_matched']}")
        self.stdout.write(f"- celery_batch_size: {metrics['celery_batch_size']}")
        self.stdout.write(f"- celery_task_count: {metrics['celery_task_count']}")
        self.stdout.write(f"- delivered_count: {metrics['delivered_count']}")
        self.stdout.write(f"- delivery_throughput_per_second: {metrics['delivery_throughput_per_second']:.2f}")
        self.stdout.write(f"- audience_expansion_queries: {metrics['audience_expansion_queries']}")
        self.stdout.write(f"- delivery_queries: {metrics['delivery_queries']}")

    def cleanup(self, local):
        AnnouncementRecipient.objects.filter(local=local).delete()
        AnnouncementStats.objects.filter(local=local).delete()
        Announcement.objects.filter(local=local).delete()
        Member.objects.filter(local=local).delete()
        local.delete()
