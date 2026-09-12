from rest_framework import serializers

from callouts.models import Announcement


class AnnouncementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Announcement
        fields = [
            'id',
            'local',
            'created_by',
            'title',
            'body',
            'push_preview',
            'target_classification',
            'needs_ack',
            'status',
            'confirmed_content_hash',
            'created_at',
            'confirmed_at',
        ]
        read_only_fields = [
            'id',
            'local',
            'created_by',
            'status',
            'confirmed_content_hash',
            'created_at',
            'confirmed_at',
        ]
